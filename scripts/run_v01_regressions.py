#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run v0.1 end-to-end golden, recovery, and hostile-input regressions."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import resource
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import TracebackType
from typing import NoReturn, cast
from unittest.mock import patch

import visualworld.cli as cli
from visualworld.coordinator import (
    CommitBoundary,
    CoordinatorError,
    IngestionConfig,
    IngestionCoordinator,
    IngestionDisposition,
    IngestionResult,
    ManualEvidenceInput,
    sample_index_bytes,
)
from visualworld.ingestion import (
    EvidenceRef,
    FrameRef,
    Rational,
    RunManifest,
    Sampling,
    Source,
    dumps_record,
)
from visualworld.ports import FakeFrameSampler, FakeVideoSource, PortError
from visualworld.storage import ArtifactState, LocalEvidenceStore
from visualworld.world_store import DeletionState, LocalWorldStore

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "fixtures" / "v01-regression" / "manifest.json"
PROBE_GOLDEN_PATH = ROOT / "tests" / "goldens" / "cli-probe.json"
_EXPECTED_CROP = bytes((3, 4, 5, 9, 10, 11))
_STORE_LOGICAL_LIMIT_BYTES = 32 * 1024 * 1024
_HOSTILE_CASES = [
    "artifact-content-injection",
    "corrupt-artifact",
    "malicious-record-json",
    "no-egress-or-process-launch",
    "oversized-frame",
    "path-traversal",
    "redaction-and-terminal-control",
    "symlink-store-and-output",
]


class _SimulatedCrash(BaseException):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        raise ValueError(f"invalid {name}")
    return cast(dict[str, object], value)


def load_manifest() -> dict[str, object]:
    try:
        loaded = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("unable to read v0.1 regression manifest") from error
    manifest = _mapping(loaded, "v0.1 regression manifest")
    _validate_manifest(manifest)
    return manifest


def _validate_manifest(manifest: dict[str, object]) -> None:
    if set(manifest) != {
        "fixture",
        "golden",
        "hostile_cases",
        "privacy",
        "rights",
        "schema",
        "schema_version",
    }:
        raise ValueError("invalid v0.1 regression manifest")
    if (
        manifest["schema"] != "visualworld.v01-regression-fixture-manifest"
        or manifest["schema_version"] != 1
        or manifest["hostile_cases"] != _HOSTILE_CASES
    ):
        raise ValueError("invalid v0.1 regression manifest")
    if _mapping(manifest["fixture"], "fixture") != {
        "candidate_frame_count": 2,
        "default_box_xyxy": [1, 0, 2, 2],
        "fixture_name": "deterministic-rgb24-v1",
        "height": 2,
        "pixel_generation": "packed RGB24 byte values 0 through 11",
        "selected_frame_count": 1,
        "width": 2,
    }:
        raise ValueError("invalid v0.1 regression manifest")
    if _mapping(manifest["rights"], "rights") != {
        "allowed_uses": ["benchmarking", "modification", "redistribution", "testing"],
        "derivatives_allowed": True,
        "external_sources": [],
        "license_expression": "Apache-2.0",
        "redistribution_allowed": True,
    }:
        raise ValueError("invalid v0.1 regression manifest")
    if _mapping(manifest["privacy"], "privacy") != {
        "classification": "public-synthetic-no-personal-data",
        "consent_status": "not-applicable-no-personal-data",
        "contains_faces": False,
        "contains_imported_assets": False,
        "contains_personal_data": False,
        "contains_plates": False,
        "contains_real_people": False,
        "temporary_outputs_retained": False,
    }:
        raise ValueError("invalid v0.1 regression manifest")
    golden = _mapping(manifest["golden"], "golden")
    if set(golden) != {
        "artifact_bytes",
        "artifact_media_type",
        "artifact_sha256",
        "deletion_artifact_count",
        "deletion_record_count",
        "deletion_shared_retention_count",
        "evidence_id",
        "frame_id",
        "probe_golden_sha256",
        "run_id",
        "run_manifest_sha256",
        "sample_index_sha256",
        "source_id",
        "world_artifact_count",
        "world_intent_count",
        "world_record_count",
    }:
        raise ValueError("invalid v0.1 regression manifest")


def _memory_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def _peak_rss_bytes() -> int:
    try:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if platform.system() == "Darwin" else value * 1024
    except (OSError, ValueError):
        return 0


def _disk_bytes(root: Path) -> int:
    def handle_walk_error(error: OSError) -> None:
        if not isinstance(error, FileNotFoundError):
            raise error

    total = 0
    for directory, _, names in os.walk(root, onerror=handle_walk_error):
        for name in names:
            try:
                metadata = (Path(directory) / name).stat(follow_symlinks=False)
            except OSError as error:
                if isinstance(error, FileNotFoundError):
                    continue
                raise
            if metadata.st_mode & 0o170000 == 0o100000:
                total += metadata.st_size
    return total


def _disk_within_limit(value: int) -> bool:
    return 0 < value <= _STORE_LOGICAL_LIMIT_BYTES


class _DiskPeakSampler:
    def __init__(self, root: Path, *, interval_seconds: float = 0.001) -> None:
        self._root = root
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._errors: list[BaseException] = []
        self._sampled_bytes = 0
        self._thread = threading.Thread(
            target=self._sample,
            name="visualworld-v01-regression-disk-sampler",
            daemon=True,
        )

    def _sample(self) -> None:
        try:
            while not self._stop.wait(self._interval_seconds):
                self._sampled_bytes = max(self._sampled_bytes, _disk_bytes(self._root))
        except BaseException as error:
            self._errors.append(error)

    def __enter__(self) -> _DiskPeakSampler:
        self._thread.start()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception, traceback
        self._stop.set()
        self._thread.join()
        if exception_type is not None:
            return
        if self._errors:
            raise RuntimeError("disk sampler failed") from self._errors[0]
        self._sampled_bytes = max(self._sampled_bytes, _disk_bytes(self._root))

    @property
    def sampled_bytes(self) -> int:
        if self._thread.is_alive():
            raise RuntimeError("disk sampler is still running")
        return self._sampled_bytes


@contextmanager
def _deny_ambient_capabilities(attempts: list[str]) -> Iterator[None]:
    def deny_network(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        attempts.append("network")
        raise RuntimeError("network capability denied")

    def deny_process(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        attempts.append("process")
        raise RuntimeError("process capability denied")

    with (
        patch.object(socket, "socket", deny_network),
        patch.object(socket, "create_connection", deny_network),
        patch.object(urllib.request, "urlopen", deny_network),
        patch.object(subprocess, "Popen", deny_process),
        patch.object(os, "system", deny_process),
    ):
        yield


def _invoke_cli(arguments: list[str]) -> tuple[int, dict[str, object], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli.main(arguments)
    out = stdout.getvalue()
    err = stderr.getvalue()
    selected = out if exit_code == 0 else err
    other = err if exit_code == 0 else out
    if other or selected.count("\n") != 1:
        raise ValueError("CLI stream contract failed")
    document = _mapping(json.loads(selected), "CLI document")
    if selected != cli.canonical_json(document) + "\n":
        raise ValueError("CLI document was not canonical")
    return exit_code, document, out + err


def _result(document: dict[str, object]) -> dict[str, object]:
    return _mapping(document.get("result"), "CLI result")


def _error_code(document: dict[str, object]) -> str:
    error = _mapping(document.get("error"), "CLI error")
    code = error.get("code")
    if type(code) is not str:
        raise ValueError("invalid CLI error code")
    return code


def _port_error_code(operation: Callable[[], object]) -> str:
    try:
        operation()
    except PortError as error:
        return error.code.value
    raise ValueError("operation unexpectedly succeeded")


def generated_fixture() -> tuple[Source, tuple[FrameRef, ...], bytes]:
    """Recreate the public CLI fixture from its versioned probe document."""

    probe = _result(cli.probe_result())
    source = Source.from_mapping(probe["source"])
    raw_frames = probe["frames"]
    if not isinstance(raw_frames, list):
        raise ValueError("invalid fixture frames")
    frames = tuple(FrameRef.from_mapping(value) for value in raw_frames)
    pixels = bytes(range(12))
    return source, frames, pixels


def _domain_ingest(
    coordinator: IngestionCoordinator,
    *,
    max_frame_bytes: int = 12,
    fault_hook: Callable[[CommitBoundary, str], None] | None = None,
) -> IngestionResult:
    source, frames, pixels = generated_fixture()
    return coordinator.ingest(
        FakeVideoSource(source, frames),
        FakeFrameSampler((frames[1].frame_id,)),
        IngestionConfig(Sampling(Rational("5", "1")), max_frame_bytes=max_frame_bytes),
        (ManualEvidenceInput(frames[1].frame_id, pixels, (1, 0, 2, 2)),),
        fault_hook=fault_hook,
    )


def _success_reopen_retry_delete(
    private_root: Path,
    golden: dict[str, object],
) -> tuple[dict[str, bool], list[str]]:
    root = private_root / "success-store"
    rendered: list[str] = []
    exit_code, ingest_document, text = _invoke_cli(["ingest", "--store", os.fspath(root)])
    rendered.append(text)
    ingested = _result(ingest_document)
    checks = {
        "ingest_golden": exit_code == 0
        and ingested
        == {
            "disposition": "committed",
            "evidence_ids": [golden["evidence_id"]],
            "run_id": golden["run_id"],
            "sample_count": 1,
            "source_id": golden["source_id"],
        }
    }

    world = LocalWorldStore(root)
    evidence_store = LocalEvidenceStore(root)
    manifest = world.get(cast(str, golden["run_id"]))
    if not isinstance(manifest, RunManifest):
        raise ValueError("golden run was not a manifest")
    frames = world.list_run_frames(manifest.run_id, limit=64)
    evidence = world.list_run_evidence(manifest.run_id, limit=64)
    if len(frames) != 1 or len(evidence) != 1:
        raise ValueError("golden run cardinality mismatch")
    item = evidence[0]
    stats = world.verify()
    checks.update(
        {
            "reopened_world_golden": (
                frames[0].frame_id == golden["frame_id"]
                and item.evidence_id == golden["evidence_id"]
                and item.artifact.sha256 == golden["artifact_sha256"]
                and item.artifact.bytes == golden["artifact_bytes"]
                and item.artifact.media_type == golden["artifact_media_type"]
                and hashlib.sha256(dumps_record(manifest)).hexdigest()
                == golden["run_manifest_sha256"]
                and hashlib.sha256(sample_index_bytes(frames)).hexdigest()
                == golden["sample_index_sha256"]
                and stats.record_count == golden["world_record_count"]
                and stats.artifact_count == golden["world_artifact_count"]
                and stats.intent_count == golden["world_intent_count"]
            ),
            "reopened_evidence_checksum": evidence_store.get(item.artifact.sha256)
            == _EXPECTED_CROP,
        }
    )

    exit_code, inspected, text = _invoke_cli(
        ["inspect-run", "--store", os.fspath(root), "--run-id", manifest.run_id]
    )
    rendered.append(text)
    exit_code_list, listed, text = _invoke_cli(
        ["list-samples", "--store", os.fspath(root), "--run-id", manifest.run_id]
    )
    rendered.append(text)
    output = private_root / "success-crop.rgb24"
    exit_code_show, shown, text = _invoke_cli(
        [
            "show-evidence",
            "--store",
            os.fspath(root),
            "--evidence-id",
            item.evidence_id,
            "--output",
            os.fspath(output),
        ]
    )
    rendered.append(text)
    shown_result = _result(shown)
    checks["inspect_list_export_golden"] = (
        exit_code == exit_code_list == exit_code_show == 0
        and _result(inspected)["manifest"] == manifest.to_mapping()
        and _result(listed)["samples"]
        == [{"evidence": [item.to_mapping()], "frame": frames[0].to_mapping()}]
        and _mapping(shown_result["export"], "export")
        == {
            "bytes": 6,
            "height": 2,
            "media_type": golden["artifact_media_type"],
            "sha256": golden["artifact_sha256"],
            "width": 1,
        }
        and output.read_bytes() == _EXPECTED_CROP
    )
    output.unlink()

    exit_code, retry_document, text = _invoke_cli(["ingest", "--store", os.fspath(root)])
    rendered.append(text)
    retried = _result(retry_document)
    checks["idempotent_retry_golden"] = (
        exit_code == 0
        and retried["disposition"] == "already_committed"
        and retried["run_id"] == golden["run_id"]
        and retried["evidence_ids"] == [golden["evidence_id"]]
    )

    deletion_id = "del_" + hashlib.sha256(b"v01-regression-delete").hexdigest()

    def crash(boundary: CommitBoundary, identifier: str) -> None:
        del identifier
        if boundary is CommitBoundary.DELETION_ARTIFACTS_REMOVED:
            raise _SimulatedCrash

    interrupted = False
    coordinator = IngestionCoordinator(LocalEvidenceStore(root), LocalWorldStore(root))
    try:
        coordinator.delete_source(
            cast(str, golden["source_id"]),
            deletion_id=deletion_id,
            fault_hook=crash,
        )
    except _SimulatedCrash:
        interrupted = True
    pending = LocalWorldStore(root).deletion_status(deletion_id)
    recovered = IngestionCoordinator(LocalEvidenceStore(root), LocalWorldStore(root)).recover()
    final_world = LocalWorldStore(root)
    receipt = final_world.deletion_status(deletion_id)
    deleted_stats = final_world.verify()
    final_evidence = LocalEvidenceStore(root)
    exit_code, deleted_document, text = _invoke_cli(
        ["inspect-run", "--store", os.fspath(root), "--run-id", manifest.run_id]
    )
    rendered.append(text)
    checks.update(
        {
            "interrupted_delete_recovered": (
                interrupted
                and pending.state is DeletionState.PENDING
                and recovered.deletions_completed == 1
                and recovered.integrity_issues == 0
                and receipt.state is DeletionState.COMPLETE
            ),
            "deletion_golden": (
                receipt.record_count == golden["deletion_record_count"]
                and receipt.artifact_count == golden["deletion_artifact_count"]
                and receipt.shared_retention_count == golden["deletion_shared_retention_count"]
                and deleted_stats.record_count == 0
                and deleted_stats.artifact_count == 0
                and deleted_stats.intent_count == 0
                and final_evidence.inventory().entries == ()
            ),
            "deleted_records_and_evidence_absent": (
                exit_code == 1
                and _error_code(deleted_document) == "not_found"
                and _port_error_code(lambda: final_evidence.get(item.artifact.sha256))
                == "not_found"
                and not output.exists()
            ),
        }
    )
    return checks, rendered


def _interrupted_ingest_recovery(
    private_root: Path,
    golden: dict[str, object],
) -> dict[str, bool]:
    root = private_root / "recovery-store"
    crashed = False

    def crash(boundary: CommitBoundary, identifier: str) -> None:
        del identifier
        if boundary is CommitBoundary.ARTIFACTS_PROMOTED:
            raise _SimulatedCrash

    coordinator = IngestionCoordinator(LocalEvidenceStore(root), LocalWorldStore(root))
    try:
        _domain_ingest(coordinator, fault_hook=crash)
    except _SimulatedCrash:
        crashed = True
    interrupted_world = LocalWorldStore(root)
    interrupted_evidence = LocalEvidenceStore(root)
    preparing_record = interrupted_world.get(cast(str, golden["run_id"]))
    hidden_run_frames_code = _port_error_code(
        lambda: interrupted_world.list_run_frames(
            cast(str, golden["run_id"]),
            limit=64,
        )
    )
    hidden_evidence_code = _port_error_code(
        lambda: interrupted_world.get(cast(str, golden["evidence_id"]))
    )
    promoted_bytes = interrupted_evidence.get(cast(str, golden["artifact_sha256"]))
    reopened = IngestionCoordinator(LocalEvidenceStore(root), LocalWorldStore(root))
    report = reopened.recover()
    recovered_marker = LocalWorldStore(root).get(cast(str, golden["run_id"]))
    cleaned_artifact_code = _port_error_code(
        lambda: LocalEvidenceStore(root).get(cast(str, golden["artifact_sha256"]))
    )
    cleaned_inventory = LocalEvidenceStore(root).inventory()
    result = _domain_ingest(reopened)
    artifact = result.evidence[0].artifact
    recovered_bytes = LocalEvidenceStore(root).get(artifact.sha256)
    stats = LocalWorldStore(root).verify()
    receipt = reopened.delete_source(
        cast(str, golden["source_id"]),
        deletion_id="del_" + hashlib.sha256(b"v01-regression-recovery-cleanup").hexdigest(),
    )
    return {
        "interrupted_ingest_was_hidden_and_cleaned": (
            crashed
            and isinstance(preparing_record, RunManifest)
            and preparing_record.state == "preparing"
            and hidden_run_frames_code == "not_found"
            and hidden_evidence_code == "not_found"
            and promoted_bytes == _EXPECTED_CROP
            and report.runs_cleaned == 1
            and report.deletions_completed == 0
            and report.integrity_issues == 0
            and isinstance(recovered_marker, RunManifest)
            and recovered_marker.state == "failed"
            and cleaned_artifact_code == "not_found"
            and cleaned_inventory.entries == ()
        ),
        "recovered_ingest_matches_golden": (
            result.disposition is IngestionDisposition.COMMITTED
            and result.manifest.run_id == golden["run_id"]
            and result.evidence[0].evidence_id == golden["evidence_id"]
            and recovered_bytes == _EXPECTED_CROP
            and stats.record_count == golden["world_record_count"]
            and receipt.state is DeletionState.COMPLETE
        ),
    }


def _hostile_regressions(
    private_root: Path,
    golden: dict[str, object],
) -> tuple[dict[str, bool], list[str], tuple[str, ...]]:
    rendered: list[str] = []
    hostile_values: list[str] = []
    pixels = generated_fixture()[2]

    oversize_root = private_root / "oversize-store"
    oversize_world = LocalWorldStore(oversize_root)
    oversize = IngestionCoordinator(LocalEvidenceStore(oversize_root), oversize_world)
    oversize_code = ""
    try:
        _domain_ingest(oversize, max_frame_bytes=11)
    except CoordinatorError as error:
        oversize_code = error.code.value
    empty_stats = oversize_world.verify()
    recovered_oversize = _domain_ingest(oversize)

    path_root = private_root / "path-store"
    exit_code, path_ingest, text = _invoke_cli(["ingest", "--store", os.fspath(path_root)])
    rendered.append(text)
    path_result = _result(path_ingest)
    path_evidence_ids = path_result.get("evidence_ids")
    if (
        not isinstance(path_evidence_ids, list)
        or len(path_evidence_ids) != 1
        or type(path_evidence_ids[0]) is not str
    ):
        raise ValueError("invalid path-case evidence identifiers")
    evidence_id = path_evidence_ids[0]

    relative_code, relative_document, text = _invoke_cli(["ingest", "--store", "../escape-store"])
    rendered.append(text)
    target_store = private_root / "symlink-target"
    target_store.mkdir(mode=0o700)
    linked_store = private_root / "linked-store"
    linked_store.symlink_to(target_store, target_is_directory=True)
    symlink_store_code, symlink_store_document, text = _invoke_cli(
        ["ingest", "--store", os.fspath(linked_store)]
    )
    rendered.append(text)

    keep = private_root / "keep.txt"
    keep.write_bytes(b"keep")
    keep.chmod(0o600)
    linked_output = private_root / "linked-output.rgb24"
    linked_output.symlink_to(keep)
    symlink_output_code, symlink_output_document, text = _invoke_cli(
        [
            "show-evidence",
            "--store",
            os.fspath(path_root),
            "--evidence-id",
            evidence_id,
            "--output",
            os.fspath(linked_output),
        ]
    )
    rendered.append(text)
    traversal_code, traversal_document, text = _invoke_cli(
        [
            "show-evidence",
            "--store",
            os.fspath(path_root),
            "--evidence-id",
            evidence_id,
            "--output",
            "../escape.rgb24",
        ]
    )
    rendered.append(text)

    hostile_identifier = "run_" + "f" * 63 + "' OR 1=1;--\x1b[31m"
    hostile_values.append(hostile_identifier)
    injection_code, injection_document, text = _invoke_cli(
        [
            "inspect-run",
            "--store",
            os.fspath(path_root),
            "--run-id",
            hostile_identifier,
        ]
    )
    rendered.append(text)
    hostile_path = private_root / "private-\x1b[31m-location"
    hostile_values.append(os.fspath(hostile_path))
    redaction_code, redaction_document, text = _invoke_cli(
        [
            "inspect-run",
            "--store",
            os.fspath(hostile_path),
            "--run-id",
            hostile_identifier,
        ]
    )
    rendered.append(text)

    corrupt_root = private_root / "corrupt-store"
    exit_code_corrupt, corrupt_ingest, text = _invoke_cli(
        ["ingest", "--store", os.fspath(corrupt_root)]
    )
    rendered.append(text)
    corrupt_result = _result(corrupt_ingest)
    corrupt_evidence_ids = corrupt_result.get("evidence_ids")
    if (
        not isinstance(corrupt_evidence_ids, list)
        or len(corrupt_evidence_ids) != 1
        or type(corrupt_evidence_ids[0]) is not str
    ):
        raise ValueError("invalid corruption-case evidence identifiers")
    corrupt_evidence_id = corrupt_evidence_ids[0]
    corrupt_world = LocalWorldStore(corrupt_root)
    record = corrupt_world.get(corrupt_evidence_id)
    if not isinstance(record, EvidenceRef):
        raise ValueError("golden evidence record missing")
    sentinel = private_root / "must-not-exist"
    hostile_metadata_text = f"$(touch {sentinel}) https://example.invalid/\x1b[31m"
    hostile_values.extend((hostile_metadata_text, os.fspath(sentinel)))
    malicious_record = json.dumps(
        {
            "instruction": hostile_metadata_text,
            "schema": "visualworld.evidence_ref",
            "schema_version": 1,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    with sqlite3.connect(corrupt_root / "world.sqlite3", autocommit=True) as connection:
        connection.execute(
            "UPDATE evidence SET record_json = ?, record_sha256 = ? WHERE evidence_id = ?",
            (
                malicious_record,
                hashlib.sha256(malicious_record).hexdigest(),
                corrupt_evidence_id,
            ),
        )
    metadata_output = private_root / "metadata-output.rgb24"
    metadata_code, metadata_document, text = _invoke_cli(
        [
            "show-evidence",
            "--store",
            os.fspath(corrupt_root),
            "--evidence-id",
            corrupt_evidence_id,
            "--output",
            os.fspath(metadata_output),
        ]
    )
    rendered.append(text)

    canonical_record = dumps_record(record)
    with sqlite3.connect(corrupt_root / "world.sqlite3", autocommit=True) as connection:
        connection.execute(
            "UPDATE evidence SET record_json = ?, record_sha256 = ? WHERE evidence_id = ?",
            (
                canonical_record,
                hashlib.sha256(canonical_record).hexdigest(),
                corrupt_evidence_id,
            ),
        )
    artifact_path = (
        corrupt_root
        / "artifacts"
        / "v1"
        / "sha256"
        / record.artifact.sha256[:2]
        / record.artifact.sha256[2:4]
        / record.artifact.sha256
    )
    hostile_artifact = hostile_metadata_text.encode("utf-8")
    artifact_path.chmod(0o600)
    artifact_path.write_bytes(hostile_artifact)
    artifact_path.chmod(0o400)
    artifact_output = private_root / "artifact-output.rgb24"
    artifact_code, artifact_document, text = _invoke_cli(
        [
            "show-evidence",
            "--store",
            os.fspath(corrupt_root),
            "--evidence-id",
            corrupt_evidence_id,
            "--output",
            os.fspath(artifact_output),
        ]
    )
    rendered.append(text)
    artifact_state = LocalEvidenceStore(corrupt_root).inspect((record.artifact,))[0].state

    combined = "".join(rendered)
    return (
        {
            "oversized_frame_rejected_before_effects": (
                oversize_code == "invalid_request"
                and empty_stats.record_count == 0
                and empty_stats.artifact_count == 0
            ),
            "oversize_failure_remains_retryable": (
                recovered_oversize.disposition is IngestionDisposition.COMMITTED
                and recovered_oversize.manifest.run_id == golden["run_id"]
            ),
            "relative_paths_rejected": (
                relative_code == 1
                and _error_code(relative_document) == "invalid_request"
                and traversal_code == 1
                and _error_code(traversal_document) == "invalid_request"
            ),
            "symlink_store_rejected_without_target_mutation": (
                symlink_store_code == 1
                and _error_code(symlink_store_document)
                in {"invalid_request", "corrupt", "unsupported"}
                and tuple(target_store.iterdir()) == ()
            ),
            "symlink_output_rejected_without_overwrite": (
                symlink_output_code == 1
                and _error_code(symlink_output_document) == "invalid_request"
                and keep.read_bytes() == b"keep"
            ),
            "sql_and_terminal_injection_rejected": (
                exit_code == 0
                and injection_code == 1
                and _error_code(injection_document) == "invalid_request"
                and redaction_code == 1
                and _error_code(redaction_document) == "not_found"
            ),
            "malicious_metadata_is_inert_and_redacted": (
                exit_code_corrupt == 0
                and metadata_code == 1
                and _error_code(metadata_document) == "corrupt"
                and not metadata_output.exists()
                and not sentinel.exists()
            ),
            "corrupt_artifact_is_inert_and_redacted": (
                artifact_code == 1
                and _error_code(artifact_document) == "corrupt"
                and artifact_state is ArtifactState.CORRUPT
                and not artifact_output.exists()
                and not sentinel.exists()
            ),
            "hostile_values_and_paths_redacted": (
                os.fspath(private_root) not in combined
                and "\x1b" not in combined
                and all(value not in combined for value in hostile_values)
                and repr(pixels) not in combined
            ),
        },
        rendered,
        tuple(hostile_values),
    )


def _run(work_root: Path) -> dict[str, object]:
    manifest = load_manifest()
    golden = _mapping(manifest["golden"], "golden")
    cpu_count = os.cpu_count() or 0
    memory_bytes = _memory_bytes()
    profile_ok = (
        platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and cpu_count >= 4
        and memory_bytes >= 16_000_000_000
    )
    attempts: list[str] = []
    measurements: dict[str, int] = {}
    rendered: list[str] = []
    started_total = time.perf_counter_ns()
    temporary_path: Path | None = None

    with tempfile.TemporaryDirectory(prefix="visualworld-v01-regression-", dir=work_root) as temp:
        temporary_path = Path(temp)
        with (
            _DiskPeakSampler(temporary_path) as disk_sampler,
            _deny_ambient_capabilities(attempts),
        ):
            started = time.perf_counter_ns()
            success_checks, success_output = _success_reopen_retry_delete(temporary_path, golden)
            measurements["success_reopen_retry_delete_wall_ns"] = max(
                1, time.perf_counter_ns() - started
            )
            rendered.extend(success_output)

            started = time.perf_counter_ns()
            recovery_checks = _interrupted_ingest_recovery(temporary_path, golden)
            measurements["interrupted_recovery_wall_ns"] = max(1, time.perf_counter_ns() - started)

            started = time.perf_counter_ns()
            hostile_checks, hostile_output, hostile_values = _hostile_regressions(
                temporary_path, golden
            )
            measurements["hostile_regressions_wall_ns"] = max(1, time.perf_counter_ns() - started)
            rendered.extend(hostile_output)
        sampled_peak_store_logical_bytes = disk_sampler.sampled_bytes

    if temporary_path is None:
        raise AssertionError("temporary path was not created")
    total_wall_ns = max(1, time.perf_counter_ns() - started_total)
    peak_rss = _peak_rss_bytes()
    combined = "".join(rendered)
    checks = {
        "fixture_manifest_valid": True,
        "fixture_rights_and_privacy_proven": True,
        "probe_golden_bound": _sha256(PROBE_GOLDEN_PATH) == golden["probe_golden_sha256"],
        **success_checks,
        **recovery_checks,
        **hostile_checks,
        "no_network_or_process_capability_used": attempts == [],
        "temporary_outputs_removed": not temporary_path.exists(),
        "combined_output_redacted": (
            os.fspath(temporary_path) not in combined
            and "\x1b" not in combined
            and all(value not in combined for value in hostile_values)
        ),
    }
    resource_checks = {
        "timing_measured": all(value > 0 for value in measurements.values()),
        "suite_wall_bounded": total_wall_ns <= 10_000_000_000,
        "rss_bounded": 0 < peak_rss <= 512 * 1024 * 1024,
        "disk_bounded": _disk_within_limit(sampled_peak_store_logical_bytes),
    }
    passed = profile_ok and all(checks.values()) and all(resource_checks.values())
    return {
        "schema": "visualworld.v01-regression-receipt",
        "schema_version": 1,
        "status": "pass" if passed else "fail",
        "implementation": {
            "cli_module_sha256": _sha256(ROOT / "src" / "visualworld" / "cli.py"),
            "coordinator_module_sha256": _sha256(ROOT / "src" / "visualworld" / "coordinator.py"),
            "executable_implementation": sys.implementation.name,
            "fixture_manifest_sha256": _sha256(MANIFEST_PATH),
            "harness_sha256": _sha256(Path(__file__)),
            "python_version": platform.python_version(),
            "storage_module_sha256": _sha256(ROOT / "src" / "visualworld" / "storage.py"),
            "world_store_module_sha256": _sha256(ROOT / "src" / "visualworld" / "world_store.py"),
        },
        "profile": {
            "cpu_count": cpu_count,
            "gpu_required": False,
            "machine": platform.machine(),
            "meets_requirements": profile_ok,
            "memory_bytes": memory_bytes,
            "name": "CPU-LITE",
            "os": platform.platform(),
            "required_memory_bytes": 16_000_000_000,
            "required_vcpu": 4,
        },
        "workload": {
            "candidate_frame_count": 2,
            "crop_rgb24_bytes": len(_EXPECTED_CROP),
            "hostile_case_count": len(_HOSTILE_CASES),
            "perception_dataset_used": False,
            "sample_count": 1,
            "scenario_count": len(measurements),
            "synthetic_fixture_generated_at_test_time": True,
            "temporary_outputs_retained": False,
        },
        "checks": checks,
        "resources": {
            **resource_checks,
            "peak_rss_limit_bytes": 512 * 1024 * 1024,
            "process_peak_rss_bytes": peak_rss,
            "sampled_peak_store_logical_bytes": sampled_peak_store_logical_bytes,
            "store_logical_limit_bytes": _STORE_LOGICAL_LIMIT_BYTES,
            "suite_wall_limit_ns": 10_000_000_000,
            "total_wall_ns": total_wall_ns,
        },
        "measurements": measurements,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = _run(arguments.work_root.resolve(strict=True))
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": result["status"]}, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
