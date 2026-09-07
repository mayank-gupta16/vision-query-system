#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run the pinned v0.1 generated CPU-LITE application benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import resource
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from visualworld.geometry import Rgb24Crop, extract_rgb24_crop, source_geometry
from visualworld.ingestion import (
    EvidenceRef,
    Fingerprint,
    FrameRef,
    MediaTime,
    Producer,
    Rational,
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
    dumps_record,
)
from visualworld.sampling import PtsFrameSampler, SamplingLimits
from visualworld.storage import ArtifactState, LocalEvidenceStore
from visualworld.world_store import LocalWorldStore, WorldStoreStats

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "fixtures" / "v01-benchmark" / "manifest.json"
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_ID = re.compile(r"src_[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"run_[0-9a-f]{64}\Z")
_PEAK_RSS_LIMIT_BYTES = 2 * 1024 * 1024 * 1024


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


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


def _validate_digest(value: object, name: str) -> None:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ValueError(f"invalid {name}")


def load_manifest() -> dict[str, object]:
    try:
        loaded = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("unable to read v0.1 benchmark manifest") from error
    manifest = _mapping(loaded, "v0.1 benchmark manifest")
    _validate_manifest(manifest)
    return manifest


def _validate_manifest(manifest: dict[str, object]) -> None:
    if set(manifest) != {
        "comparator",
        "configuration",
        "golden",
        "privacy",
        "rights",
        "schema",
        "schema_version",
        "workload",
    }:
        raise ValueError("invalid v0.1 benchmark manifest")
    if (
        manifest["schema"] != "visualworld.v01-benchmark-manifest"
        or manifest["schema_version"] != 1
    ):
        raise ValueError("invalid v0.1 benchmark manifest")
    if _mapping(manifest["workload"], "workload") != {
        "candidate_frame_count": 1800,
        "full_frame_rgb24_bytes": 6_220_800,
        "materialized_encoded_media": False,
        "perception_dataset_used": False,
        "repetition_count": 1,
        "sample_count": 300,
        "source_dimensions": [1920, 1080],
        "source_seconds": 60,
        "virtual_source_kind": "deterministic-generated-rgb24",
    }:
        raise ValueError("invalid v0.1 benchmark manifest")
    if _mapping(manifest["configuration"], "configuration") != {
        "candidate_fps": {"denominator": "1", "numerator": "30"},
        "crop_box_xyxy": [320, 180, 1600, 900],
        "frame_duration_ticks": "1000",
        "metadata_batch_size": 64,
        "pixel_generation": "packed RGB24 byte values 0 through 255 repeated",
        "sample_page_candidates": 64,
        "target_fps": {"denominator": "1", "numerator": "5"},
        "time_base": {"denominator": "30000", "numerator": "1"},
        "warmup_crop_count": 1,
        "warmup_sampling_candidates": 64,
    }:
        raise ValueError("invalid v0.1 benchmark manifest")
    if _mapping(manifest["comparator"], "comparator") != {
        "max_regression_basis_points": 2000,
        "metrics": {
            "combined_store_logical_bytes": "lower_is_better",
            "frames_per_second_milli": "higher_is_better",
            "process_cpu_ns": "lower_is_better",
            "process_peak_rss_bytes": "lower_is_better",
            "real_time_factor_milli": "higher_is_better",
            "total_wall_ns": "lower_is_better",
        },
    }:
        raise ValueError("invalid v0.1 benchmark manifest")
    if _mapping(manifest["rights"], "rights") != {
        "allowed_uses": ["benchmarking", "modification", "redistribution", "testing"],
        "derivatives_allowed": True,
        "external_sources": [],
        "license_expression": "Apache-2.0",
        "redistribution_allowed": True,
    }:
        raise ValueError("invalid v0.1 benchmark manifest")
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
        raise ValueError("invalid v0.1 benchmark manifest")
    golden = _mapping(manifest["golden"], "golden")
    if set(golden) != {
        "candidate_index_sha256",
        "crop_aggregate_sha256",
        "crop_rgb24_bytes",
        "crop_sha256",
        "evidence_ids_sha256",
        "full_frame_sha256",
        "run_id",
        "run_manifest_sha256",
        "sample_index_sha256",
        "selected_frame_ids_sha256",
        "source_id",
        "unique_artifact_count",
        "world_artifact_count",
        "world_intent_count",
        "world_record_count",
    }:
        raise ValueError("invalid v0.1 benchmark manifest")
    for name in (
        "candidate_index_sha256",
        "crop_aggregate_sha256",
        "crop_sha256",
        "evidence_ids_sha256",
        "full_frame_sha256",
        "run_manifest_sha256",
        "sample_index_sha256",
        "selected_frame_ids_sha256",
    ):
        _validate_digest(golden[name], f"golden.{name}")
    if (
        type(golden["source_id"]) is not str
        or not _SOURCE_ID.fullmatch(golden["source_id"])
        or type(golden["run_id"]) is not str
        or not _RUN_ID.fullmatch(golden["run_id"])
        or golden["crop_rgb24_bytes"] != 2_764_800
        or golden["unique_artifact_count"] != 1
        or golden["world_record_count"] != 602
        or golden["world_artifact_count"] != 1
        or golden["world_intent_count"] != 0
    ):
        raise ValueError("invalid v0.1 benchmark manifest")


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
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    try:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if platform.system() == "Darwin" else value * 1024
    except (OSError, ValueError):
        return 0


def _measure[T](operation: Callable[[], T]) -> tuple[T, dict[str, int]]:
    started_wall = time.perf_counter_ns()
    started_cpu = time.process_time_ns()
    result = operation()
    return result, {
        "cpu_ns": max(1, time.process_time_ns() - started_cpu),
        "wall_ns": max(1, time.perf_counter_ns() - started_wall),
    }


def _batches[T](values: tuple[T, ...], size: int) -> tuple[tuple[T, ...], ...]:
    return tuple(values[index : index + size] for index in range(0, len(values), size))


def _aggregate_strings(values: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(value.encode("ascii") + b"\n")
    return digest.hexdigest()


def _generated_input(
    workload: dict[str, object],
    configuration: dict[str, object],
) -> tuple[Source, tuple[FrameRef, ...], bytes]:
    dimensions = cast(list[int], workload["source_dimensions"])
    width, height = dimensions
    time_base = TimeBase.from_mapping(configuration["time_base"])
    descriptor = _canonical(
        {
            "configuration": configuration,
            "schema": "visualworld.v01-benchmark-virtual-source",
            "schema_version": 1,
            "workload": workload,
        }
    )
    source = Source.create(
        Fingerprint(hashlib.sha256(descriptor).hexdigest(), str(len(descriptor))),
        (SourceStream(0, width, height, 0, time_base),),
    )
    duration = MediaTime(cast(str, configuration["frame_duration_ticks"]), time_base)
    candidate_count = cast(int, workload["candidate_frame_count"])
    candidates = tuple(
        FrameRef.create(
            source.source_id,
            0,
            str(index),
            MediaTime(str(index * int(duration.value)), time_base),
            duration,
            index % 30 == 0,
        )
        for index in range(candidate_count)
    )
    frame_bytes = cast(int, workload["full_frame_rgb24_bytes"])
    pattern = bytes(range(256))
    frame = (pattern * ((frame_bytes + 255) // 256))[:frame_bytes]
    return source, candidates, frame


def _sample_all(
    source: Source,
    candidates: tuple[FrameRef, ...],
    sampling: Sampling,
    page_size: int,
    source_seconds: int,
) -> tuple[tuple[FrameRef, ...], int]:
    sampler = PtsFrameSampler(
        limits=SamplingLimits(
            max_page_candidates=page_size,
            max_total_candidates=len(candidates),
            max_duration_seconds=source_seconds,
        )
    )
    selected: list[FrameRef] = []
    cursor = None
    next_index = 0
    page_count = 0
    while next_index < len(candidates):
        if cursor is None:
            page = candidates[:page_size]
            next_index = len(page)
        else:
            new = candidates[next_index : next_index + page_size - 1]
            page = (candidates[next_index - 1], *new)
            next_index += len(new)
        end_of_stream = next_index == len(candidates)
        result = sampler.sample_page(
            source,
            page,
            sampling,
            cursor=cursor,
            end_of_stream=end_of_stream,
        )
        selected.extend(result.frames)
        cursor = result.cursor
        page_count += 1
    return tuple(selected), page_count


def _disk_usage(root: Path) -> tuple[int, int, int]:
    files = 0
    logical = 0
    allocated = 0

    def raise_error(error: OSError) -> None:
        raise error

    for directory, _, names in os.walk(root, onerror=raise_error):
        for name in names:
            metadata = (Path(directory) / name).stat(follow_symlinks=False)
            if metadata.st_mode & 0o170000 != 0o100000:
                continue
            files += 1
            logical += metadata.st_size
            allocated += metadata.st_blocks * 512
    return files, logical, allocated


def _database_usage(root: Path) -> tuple[int, int, int]:
    files = 0
    logical = 0
    allocated = 0
    for name in ("world.sqlite3", "world.sqlite3-wal", "world.sqlite3-shm"):
        path = root / name
        if not path.exists():
            continue
        metadata = path.stat(follow_symlinks=False)
        files += 1
        logical += metadata.st_size
        allocated += metadata.st_blocks * 512
    return files, logical, allocated


def _run(work_root: Path, revision: str) -> dict[str, object]:
    if not _REVISION.fullmatch(revision):
        raise ValueError("invalid source revision")
    manifest = load_manifest()
    workload = _mapping(manifest["workload"], "workload")
    configuration = _mapping(manifest["configuration"], "configuration")
    golden = _mapping(manifest["golden"], "golden")
    cpu_count = os.cpu_count() or 0
    memory_bytes = _memory_bytes()
    profile_ok = (
        platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and cpu_count >= 4
        and memory_bytes >= 16_000_000_000
    )

    (source, candidates, frame), input_measurement = _measure(
        lambda: _generated_input(workload, configuration)
    )
    sampling = Sampling(Rational.from_mapping(configuration["target_fps"], "target_fps"))
    page_size = cast(int, configuration["sample_page_candidates"])
    source_seconds = cast(int, workload["source_seconds"])
    dimensions = cast(list[int], workload["source_dimensions"])
    width, height = dimensions
    crop_box_values = cast(list[int], configuration["crop_box_xyxy"])
    crop_box = cast(tuple[int, int, int, int], tuple(crop_box_values))
    geometry = source_geometry(width, height, crop_box)

    warmup_candidates = cast(int, configuration["warmup_sampling_candidates"])
    PtsFrameSampler().sample(source, candidates[:warmup_candidates], sampling)
    extract_rgb24_crop(frame, width, height, geometry)

    stage_measurements: dict[str, dict[str, int]] = {"input_generation": input_measurement}
    started_total_wall = time.perf_counter_ns()
    started_total_cpu = time.process_time_ns()

    (selected, sample_page_count), stage_measurements["sampling"] = _measure(
        lambda: _sample_all(source, candidates, sampling, page_size, source_seconds)
    )

    def crop_all() -> tuple[Rgb24Crop, str]:
        aggregate = hashlib.sha256()
        retained: Rgb24Crop | None = None
        for _ in selected:
            crop = extract_rgb24_crop(frame, width, height, geometry)
            aggregate.update(bytes.fromhex(crop.sha256))
            retained = crop
        if retained is None:
            raise ValueError("benchmark selected no frames")
        return retained, aggregate.hexdigest()

    crop_result, stage_measurements["crop_and_hash"] = _measure(crop_all)
    crop = crop_result[0]
    artifact = crop.artifact()
    crop_pixels = crop.pixels
    crop_aggregate_sha256 = crop_result[1]

    def build_records() -> tuple[
        tuple[EvidenceRef, ...],
        RunManifest,
        RunManifest,
        str,
    ]:
        evidence = tuple(
            EvidenceRef.create(frame_ref.frame_id, artifact, geometry) for frame_ref in selected
        )
        producer_configuration = hashlib.sha256(
            _canonical({"configuration": configuration, "workload": workload})
        ).hexdigest()
        producer = Producer("visualworld.v01-benchmark", "1", producer_configuration)
        preparing = RunManifest.create(source.source_id, (producer,), sampling, "preparing")
        sample_index = b"".join(dumps_record(frame_ref) + b"\n" for frame_ref in selected)
        sample_index_sha256 = hashlib.sha256(sample_index).hexdigest()
        committed = RunManifest.create(
            source.source_id,
            (producer,),
            sampling,
            "committed",
            RunOutputs(str(len(selected)), sample_index_sha256),
        )
        return evidence, preparing, committed, sample_index_sha256

    (
        (
            evidence,
            preparing,
            committed,
            sample_index_sha256,
        ),
        stage_measurements["record_construction"],
    ) = _measure(build_records)

    batch_size = cast(int, configuration["metadata_batch_size"])
    with tempfile.TemporaryDirectory(prefix="visualworld-v01-benchmark-", dir=work_root) as temp:
        root = Path(temp) / "store"

        def initialize_stores() -> tuple[LocalEvidenceStore, LocalWorldStore]:
            return (
                LocalEvidenceStore(root),
                LocalWorldStore(root, max_audit_records=2 + 2 * len(selected)),
            )

        (evidence_store, world_store), stage_measurements["store_initialization"] = _measure(
            initialize_stores
        )
        with evidence_store.writer_session() as session:
            _, stage_measurements["prepare_transaction"] = _measure(
                lambda: world_store.commit((source, preparing), evidence_session=session)
            )
            stage, stage_measurements["artifact_stage"] = _measure(
                lambda: session.stage(preparing.run_id, artifact, crop_pixels)
            )
            _, stage_measurements["intent_transaction"] = _measure(
                lambda: world_store.record_artifact_intents(
                    preparing.run_id,
                    (stage,),
                    evidence_session=session,
                )
            )
            _, stage_measurements["artifact_promotion"] = _measure(
                lambda: session.commit_stage(stage)
            )

            def commit_metadata() -> None:
                for frame_batch in _batches(selected, batch_size):
                    world_store.commit_for_run(
                        preparing.run_id,
                        frame_batch,
                        evidence_session=session,
                    )
                for evidence_batch in _batches(evidence, batch_size):
                    world_store.commit_for_run(
                        preparing.run_id,
                        evidence_batch,
                        evidence_session=session,
                    )

            _, stage_measurements["metadata_transactions"] = _measure(commit_metadata)
            _, stage_measurements["finalize_transaction"] = _measure(
                lambda: world_store.finalize_run(committed, evidence_session=session)
            )

        def reopen_and_verify() -> tuple[
            LocalWorldStore,
            LocalEvidenceStore,
            WorldStoreStats,
            bytes,
        ]:
            reopened_world = LocalWorldStore(
                root,
                max_audit_records=2 + 2 * len(selected),
            )
            reopened_evidence = LocalEvidenceStore(root)
            return (
                reopened_world,
                reopened_evidence,
                reopened_world.verify(),
                reopened_evidence.get(artifact.sha256),
            )

        (
            (
                reopened_world,
                reopened_evidence,
                statistics,
                stored_crop,
            ),
            stage_measurements["reopen_and_verify"],
        ) = _measure(reopen_and_verify)

        def query_all() -> tuple[tuple[FrameRef, ...], tuple[EvidenceRef, ...]]:
            listed_frames: list[FrameRef] = []
            frame_cursor: tuple[int, str] | None = None
            while True:
                frame_page = reopened_world.list_run_frames(
                    committed.run_id,
                    after_stream_index=(None if frame_cursor is None else frame_cursor[0]),
                    after_decode_index=(None if frame_cursor is None else frame_cursor[1]),
                    limit=batch_size,
                )
                listed_frames.extend(frame_page)
                if len(frame_page) < batch_size:
                    break
                frame_cursor = (
                    frame_page[-1].stream_index,
                    frame_page[-1].decode_index,
                )
            listed_evidence: list[EvidenceRef] = []
            evidence_cursor: str | None = None
            while True:
                evidence_page = reopened_world.list_run_evidence(
                    committed.run_id,
                    after_evidence_id=evidence_cursor,
                    limit=batch_size,
                )
                listed_evidence.extend(evidence_page)
                if len(evidence_page) < batch_size:
                    break
                evidence_cursor = evidence_page[-1].evidence_id
            return tuple(listed_frames), tuple(listed_evidence)

        (listed_frames, listed_evidence), stage_measurements["indexed_queries"] = _measure(
            query_all
        )
        total_cpu_ns = max(1, time.process_time_ns() - started_total_cpu)
        total_wall_ns = max(1, time.perf_counter_ns() - started_total_wall)
        total_files, total_logical, total_allocated = _disk_usage(root)
        artifact_files, artifact_logical, artifact_allocated = _disk_usage(root / "artifacts")
        database_files, database_logical, database_allocated = _database_usage(root)
        artifact_state = reopened_evidence.inspect((artifact,))[0].state
        manifest_reopened = reopened_world.get(committed.run_id) == committed

    peak_rss = _peak_rss_bytes()
    actual_golden = {
        "candidate_index_sha256": hashlib.sha256(
            b"".join(dumps_record(frame_ref) + b"\n" for frame_ref in candidates)
        ).hexdigest(),
        "crop_aggregate_sha256": crop_aggregate_sha256,
        "crop_rgb24_bytes": len(crop_pixels),
        "crop_sha256": artifact.sha256,
        "evidence_ids_sha256": _aggregate_strings(tuple(item.evidence_id for item in evidence)),
        "full_frame_sha256": hashlib.sha256(frame).hexdigest(),
        "run_id": committed.run_id,
        "run_manifest_sha256": hashlib.sha256(dumps_record(committed)).hexdigest(),
        "sample_index_sha256": sample_index_sha256,
        "selected_frame_ids_sha256": _aggregate_strings(
            tuple(frame_ref.frame_id for frame_ref in selected)
        ),
        "source_id": source.source_id,
        "unique_artifact_count": len({item.artifact.sha256 for item in evidence}),
        "world_artifact_count": statistics.artifact_count,
        "world_intent_count": statistics.intent_count,
        "world_record_count": statistics.record_count,
    }
    checks = {
        "golden_matches": actual_golden == golden,
        "exact_candidate_count": len(candidates) == workload["candidate_frame_count"],
        "exact_sample_count": len(selected) == workload["sample_count"],
        "exact_near_five_fps_selection": all(
            int(frame_ref.decode_index) == index * 6 for index, frame_ref in enumerate(selected)
        ),
        "exact_source_pts": all(
            frame_ref.pts.value == str(int(frame_ref.decode_index) * 1000) for frame_ref in selected
        ),
        "crop_bytes_retrievable": stored_crop == crop_pixels,
        "artifact_integrity_valid": artifact_state is ArtifactState.VALID,
        "world_counts_match": statistics
        == WorldStoreStats(
            record_count=cast(int, golden["world_record_count"]),
            artifact_count=cast(int, golden["world_artifact_count"]),
            intent_count=cast(int, golden["world_intent_count"]),
        ),
        "manifest_reopened": manifest_reopened,
        "frame_index_matches": listed_frames == selected,
        "evidence_index_matches": {item.evidence_id for item in listed_evidence}
        == {item.evidence_id for item in evidence},
        "fixture_rights_and_privacy_proven": True,
        "temporary_outputs_removed": not root.exists(),
    }
    timing_measured = all(
        measurement["wall_ns"] > 0 and measurement["cpu_ns"] > 0
        for measurement in stage_measurements.values()
    )
    resource_checks = {
        "timing_measured": timing_measured,
        "rss_measured": peak_rss > 0,
        "rss_bounded": 0 < peak_rss <= _PEAK_RSS_LIMIT_BYTES,
        "disk_measured": total_files > 0 and total_logical > 0 and total_allocated > 0,
        "index_measured": database_files > 0 and database_logical > 0,
    }
    passed = profile_ok and all(checks.values()) and all(resource_checks.values())
    return {
        "schema": "visualworld.v01-benchmark-receipt",
        "schema_version": 1,
        "status": "pass" if passed else "fail",
        "provenance": {
            "fixture_manifest_sha256": _sha256(MANIFEST_PATH),
            "generated_input": True,
            "license_expression": "Apache-2.0",
            "perception_accuracy_claimed": False,
            "privacy_classification": "public-synthetic-no-personal-data",
            "source_revision": revision,
        },
        "implementation": {
            "benchmark_harness_sha256": _sha256(Path(__file__)),
            "executable_implementation": sys.implementation.name,
            "geometry_module_sha256": _sha256(ROOT / "src/visualworld/geometry.py"),
            "ingestion_module_sha256": _sha256(ROOT / "src/visualworld/ingestion.py"),
            "python_version": platform.python_version(),
            "sampling_module_sha256": _sha256(ROOT / "src/visualworld/sampling.py"),
            "sqlite_version": sqlite3.sqlite_version,
            "storage_module_sha256": _sha256(ROOT / "src/visualworld/storage.py"),
            "world_store_module_sha256": _sha256(ROOT / "src/visualworld/world_store.py"),
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
        "workload": workload,
        "configuration": configuration,
        "warmup": {
            "crop_count": configuration["warmup_crop_count"],
            "excluded_from_total": True,
            "sampling_candidate_count": configuration["warmup_sampling_candidates"],
        },
        "checks": checks,
        "metrics": {
            "cpu_utilization_milli_percent": total_cpu_ns * 100_000 // total_wall_ns,
            "frames_per_second_milli": len(selected) * 1_000_000_000_000 // total_wall_ns,
            "process_cpu_ns": total_cpu_ns,
            "real_time_factor_milli": source_seconds * 1_000_000_000_000 // total_wall_ns,
            "total_wall_ns": total_wall_ns,
        },
        "resources": {
            **resource_checks,
            "artifact_store_allocated_bytes": artifact_allocated,
            "artifact_store_file_count": artifact_files,
            "artifact_store_logical_bytes": artifact_logical,
            "combined_store_allocated_bytes": total_allocated,
            "combined_store_file_count": total_files,
            "combined_store_logical_bytes": total_logical,
            "metadata_index_allocated_bytes": database_allocated,
            "metadata_index_file_count": database_files,
            "metadata_index_logical_bytes": database_logical,
            "peak_rss_limit_bytes": _PEAK_RSS_LIMIT_BYTES,
            "process_peak_rss_bytes": peak_rss,
        },
        "measurements": {
            **stage_measurements,
            "metadata_transactions": {
                **stage_measurements["metadata_transactions"],
                "transaction_count": len(_batches(selected, batch_size))
                + len(_batches(evidence, batch_size)),
            },
            "sampling": {
                **stage_measurements["sampling"],
                "page_count": sample_page_count,
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    arguments = parser.parse_args()
    result = _run(arguments.work_root.resolve(strict=True), arguments.revision)
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": result["status"]}, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
