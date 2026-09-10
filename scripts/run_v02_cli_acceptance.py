#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise the issue-77 CLI in fresh processes through its deterministic seam.

This runner proves application composition, canonical command behavior, replay,
pagination, selected-crop export, recovery, deletion, and no-egress behavior. It
does not claim native Linux decode or OpenVINO inference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, cast

from visualworld.coordinator import PerceptionConfig, PerceptionCoordinator, PerceptionRunResult
from visualworld.evidence import BestFrameEvidenceSelector
from visualworld.frame_access import FakeOriginalFrameReader, OriginalFrame
from visualworld.ingestion import (
    Fingerprint,
    FrameRef,
    Geometry,
    MediaTime,
    Producer,
    Source,
    SourceStream,
    TimeBase,
)
from visualworld.media import MediaMetrics
from visualworld.perception import Observation
from visualworld.perception_materialization import EvidenceMaterializationConfig
from visualworld.ports import (
    CapabilityDescriptor,
    DetectionResult,
    PerceptionResultState,
    PortError,
    PortErrorCode,
    PortKind,
)
from visualworld.sampling import PtsFrameSampler
from visualworld.storage import LocalEvidenceStore
from visualworld.tracking import GlobalLastBoxTracker
from visualworld.world_store import LocalWorldStore

ROOT = Path(__file__).resolve().parents[1]
_MAX_COMMAND_SECONDS = 10


class _FixtureVideo:
    descriptor = CapabilityDescriptor(PortKind.VIDEO_SOURCE, "acceptance-video", "1", True, True)
    metrics = MediaMetrics(1, 1, 4096, 4, 4)

    def __init__(self, source: Source, frames: tuple[FrameRef, ...]) -> None:
        self._source = source
        self._frames = frames

    def probe(self) -> Source:
        return self._source

    def read_frames(
        self,
        *,
        stream_index: int,
        after_decode_index: str | None = None,
        limit: int = 64,
    ) -> tuple[FrameRef, ...]:
        after = -1 if after_decode_index is None else int(after_decode_index)
        return tuple(
            frame
            for frame in self._frames
            if frame.stream_index == stream_index and int(frame.decode_index) > after
        )[:limit]


class _FixtureDetector:
    descriptor = CapabilityDescriptor(PortKind.DETECTOR, "acceptance-detector", "1", True, True)
    producer = Producer("acceptance-detector", "1", "ab" * 32)

    def detect(self, source: Source, frames: tuple[FrameRef, ...]) -> DetectionResult:
        return DetectionResult(
            PerceptionResultState.COMPLETE,
            tuple(
                Observation.create(
                    source.source_id,
                    frame.frame_id,
                    frame.stream_index,
                    frame.pts,
                    Geometry(10, 10, (1, 1, 6, 6), "inferred"),
                    "vehicle",
                    900_000,
                    self.producer,
                )
                for frame in frames
            ),
        )


def _fixture() -> tuple[Source, tuple[FrameRef, ...], tuple[OriginalFrame, ...]]:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(hashlib.sha256(b"issue-77-acceptance-source").hexdigest(), "4"),
        (SourceStream(0, 10, 10, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(source.source_id, 0, str(index), MediaTime(str(index * 200), time_base))
        for index in range(4)
    )
    pixels = (bytes(300), bytes(300), bytes([255]) * 300, bytes([255]) * 300)
    originals = tuple(
        OriginalFrame(source, frame, content) for frame, content in zip(frames, pixels, strict=True)
    )
    return source, frames, originals


def _fixture_execute(request: object, cancelled: object) -> tuple[PerceptionRunResult, object]:
    """Substitute only the already-tested native adapters inside this harness."""

    import visualworld.perception_cli as perception_cli

    if type(request) is not perception_cli._RunRequest:
        raise ValueError("invalid fixture request")
    source, frames, originals = _fixture()
    video = _FixtureVideo(source, frames)
    result = PerceptionCoordinator(
        LocalEvidenceStore(request.store),
        LocalWorldStore(request.store),
    ).run_with_original_frames(
        video,
        PtsFrameSampler(),
        _FixtureDetector(),
        GlobalLastBoxTracker(),
        FakeOriginalFrameReader(source, originals),
        BestFrameEvidenceSelector(),
        PerceptionConfig(
            page_candidates=2,
            max_pages=4,
            max_samples=4,
            max_candidates=4,
            max_observations=4,
            max_tracklets=2,
            max_intents=4,
            stream_index=request.stream_index,
        ),
        EvidenceMaterializationConfig(),
        cancelled=cast(Any, cancelled),
    )
    return result, video


def _fixture_command(arguments: list[str]) -> int:
    import visualworld.cli as cli
    import visualworld.perception_cli as perception_cli

    scenario = "success"
    if len(arguments) >= 2 and arguments[0] == "--scenario":
        scenario = arguments[1]
        arguments = arguments[2:]

    class DeniedNetworkSocket(socket.socket):
        def connect(self, address: object) -> None:
            del address
            raise OSError("network denied by acceptance harness")

        def connect_ex(self, address: object) -> int:
            del address
            raise OSError("network denied by acceptance harness")

    def deny_network(*_arguments: object, **_keywords: object) -> object:
        raise OSError("network denied by acceptance harness")

    cast(Any, socket).socket = DeniedNetworkSocket
    cast(Any, socket).create_connection = deny_network
    cast(Any, socket).getaddrinfo = deny_network
    if scenario == "success":
        cast(Any, perception_cli)._execute_run = _fixture_execute
    elif scenario in {"runtime-absent", "runtime-tampered"}:
        cast(Any, perception_cli)._platform_supported = lambda: True

        def reject_runtime(*_arguments: object, **_keywords: object) -> None:
            raise PortError(
                PortErrorCode.ISOLATION_UNAVAILABLE,
                PortKind.VIDEO_SOURCE,
                "probe",
            )

        cast(Any, perception_cli).verify_media_runtime = reject_runtime
    elif scenario == "unsupported-platform":
        cast(Any, perception_cli)._platform_supported = lambda: False
    elif scenario == "hostile-worker-output":

        def reject_run(*_arguments: object, **_keywords: object) -> None:
            from visualworld import media

            media._decode_output(
                media._WorkerRun(
                    b'{"status":"ok","status":"ok"}\n',
                    b"",
                    0,
                    0,
                    0,
                    0,
                ),
                "0" * 64,
                0,
                media.MediaLimits(),
            )

        cast(Any, perception_cli)._execute_run = reject_run
    elif scenario == "elapsed-timeout":
        cast(Any, perception_cli).PERCEPTION_RUN_TIMEOUT_SECONDS = 1

        def late_result(
            _request: object, cancelled: threading.Event
        ) -> tuple[PerceptionRunResult, object]:
            if not cancelled.wait(2):
                raise AssertionError("command timer did not cancel the operation")
            return PerceptionRunResult(PerceptionResultState.UNKNOWN, reason="cancelled"), object()

        cast(Any, perception_cli)._execute_run = late_result
    elif scenario == "sigint":

        def interrupted_result(
            _request: object, cancelled: threading.Event
        ) -> tuple[PerceptionRunResult, object]:
            os.kill(os.getpid(), signal.SIGINT)
            if not cancelled.is_set():
                raise AssertionError("SIGINT did not reach the command cancellation event")
            return PerceptionRunResult(PerceptionResultState.UNKNOWN, reason="cancelled"), object()

        cast(Any, perception_cli)._execute_run = interrupted_result
    elif scenario == "bounded-output":
        cast(Any, perception_cli).MAX_PERCEPTION_CLI_OUTPUT_BYTES = 1
        cast(Any, perception_cli)._run_validate_runtime = lambda _arguments: (
            perception_cli._CommandResult({"private": "x" * 10_000})
        )
    elif scenario == "must-not-execute":

        def forbidden(*_arguments: object, **_keywords: object) -> None:
            raise AssertionError("invalid input reached execution")

        cast(Any, perception_cli)._execute_run = forbidden
    else:
        raise ValueError("unknown fixture scenario")
    return cli.main(["perception", *arguments])


def _child_peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    return value if platform.system() == "Darwin" else value * 1024


def _disk_bytes(root: Path) -> int:
    total = 0
    for directory, _, names in os.walk(root):
        for name in names:
            metadata = (Path(directory) / name).stat(follow_symlinks=False)
            if metadata.st_mode & 0o170000 == 0o100000:
                total += metadata.st_size
    return total


def _invoke(
    arguments: list[str],
    *,
    environment: dict[str, str],
    expected_exit: int,
    scenario: str = "success",
) -> tuple[dict[str, Any], int, str]:
    started = time.perf_counter_ns()
    completed = subprocess.run(
        [
            sys.executable,
            os.fspath(Path(__file__)),
            "--fixture-command",
            "--scenario",
            scenario,
            *arguments,
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        text=True,
        capture_output=True,
        timeout=_MAX_COMMAND_SECONDS,
    )
    elapsed = max(1, time.perf_counter_ns() - started)
    selected = completed.stdout if expected_exit == 0 else completed.stderr
    other = completed.stderr if expected_exit == 0 else completed.stdout
    if completed.returncode != expected_exit or other or selected.count("\n") != 1:
        raise ValueError("perception CLI command contract failed")
    try:
        document = json.loads(selected)
    except (TypeError, ValueError) as error:
        raise ValueError("perception CLI output was not JSON") from error
    if type(document) is not dict:
        raise ValueError("perception CLI output was not an object")
    canonical = (
        json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    )
    if selected != canonical:
        raise ValueError("perception CLI output was not canonical")
    return cast(dict[str, Any], document), elapsed, completed.stdout + completed.stderr


def _payload(document: dict[str, Any]) -> dict[str, Any]:
    payload = document.get("result")
    if type(payload) is not dict:
        raise ValueError("perception CLI result was invalid")
    return cast(dict[str, Any], payload)


def _run(work_root: Path) -> dict[str, object]:
    environment = {
        key: value for key, value in os.environ.items() if key not in {"PYTHONHOME", "VIRTUAL_ENV"}
    }
    environment["PYTHONPATH"] = "src"
    elapsed: list[int] = []
    rendered: list[str] = []

    with tempfile.TemporaryDirectory(prefix="visualworld-v02-cli-", dir=work_root) as temporary:
        private = Path(temporary)
        store = private / "store"
        output_root = private / "exports"
        output_root.mkdir(mode=0o700)
        output = output_root / "selected.rgb24"
        runtimes = [
            "--media-runtime-root",
            os.fspath(private / "missing-media"),
            "--perception-runtime-root",
            os.fspath(private / "missing-perception"),
            "--original-frame-overlay-root",
            os.fspath(private / "missing-overlay"),
        ]
        run_arguments = [
            "run",
            "--store",
            os.fspath(store),
            "--source-root",
            os.fspath(private / "missing-source-root"),
            "--source",
            "synthetic-v1/cfr.mov",
            *runtimes,
            "--model",
            "vehicle-detection-0201",
            "--device",
            "CPU",
            "--category",
            "vehicle",
        ]

        first, wall, text = _invoke(run_arguments, environment=environment, expected_exit=0)
        elapsed.append(wall)
        rendered.append(text)
        second, wall, text = _invoke(run_arguments, environment=environment, expected_exit=0)
        elapsed.append(wall)
        rendered.append(text)
        first_result = _payload(first)
        second_result = _payload(second)
        run_id = cast(str, first_result["run_id"])
        source_id = cast(str, first_result["source_id"])

        observation_page, wall, text = _invoke(
            [
                "inspect",
                "--store",
                os.fspath(store),
                "--run-id",
                run_id,
                "--kind",
                "observations",
                "--limit",
                "1",
            ],
            environment=environment,
            expected_exit=0,
        )
        elapsed.append(wall)
        rendered.append(text)
        observation_result = _payload(observation_page)
        cursor = cast(dict[str, object], observation_result["next"])
        observation_page_2, wall, text = _invoke(
            [
                "inspect",
                "--store",
                os.fspath(store),
                "--run-id",
                run_id,
                "--kind",
                "observations",
                "--limit",
                "1",
                "--after-pts-value",
                cast(str, cursor["after_pts_value"]),
                "--after-observation-id",
                cast(str, cursor["after_observation_id"]),
            ],
            environment=environment,
            expected_exit=0,
        )
        elapsed.append(wall)
        rendered.append(text)

        tracklets, wall, text = _invoke(
            [
                "inspect",
                "--store",
                os.fspath(store),
                "--run-id",
                run_id,
                "--kind",
                "tracklets",
                "--limit",
                "2",
            ],
            environment=environment,
            expected_exit=0,
        )
        elapsed.append(wall)
        rendered.append(text)
        tracklet_items = cast(list[dict[str, Any]], _payload(tracklets)["items"])
        tracklet_id = cast(str, tracklet_items[0]["tracklet_id"])
        evidence, wall, text = _invoke(
            [
                "inspect",
                "--store",
                os.fspath(store),
                "--run-id",
                run_id,
                "--kind",
                "evidence",
                "--tracklet-id",
                tracklet_id,
                "--limit",
                "1",
            ],
            environment=environment,
            expected_exit=0,
        )
        elapsed.append(wall)
        rendered.append(text)
        exported, wall, text = _invoke(
            [
                "export-evidence",
                "--store",
                os.fspath(store),
                "--run-id",
                run_id,
                "--tracklet-id",
                tracklet_id,
                "--rank",
                "1",
                "--output",
                os.fspath(output),
            ],
            environment=environment,
            expected_exit=0,
        )
        elapsed.append(wall)
        rendered.append(text)
        failure_documents: dict[str, dict[str, Any]] = {}
        hostile_parent = private / "hostile-parent"
        hostile_parent.symlink_to(output_root, target_is_directory=True)
        fifo = output_root / "fifo.rgb24"
        os.mkfifo(fifo, mode=0o600)
        export_failures = (
            ("existing_output", output),
            ("output_inside_store", store / "selected.rgb24"),
            ("symlink_output_parent", hostile_parent / "selected.rgb24"),
            ("fifo_output", fifo),
        )
        for name, hostile_output in export_failures:
            document, wall, text = _invoke(
                [
                    "export-evidence",
                    "--store",
                    os.fspath(store),
                    "--run-id",
                    run_id,
                    "--tracklet-id",
                    tracklet_id,
                    "--rank",
                    "1",
                    "--output",
                    os.fspath(hostile_output),
                ],
                environment=environment,
                expected_exit=1,
            )
            if document.get("error", {}).get("code") != "invalid_request":
                raise ValueError("hostile export destination was not rejected")
            failure_documents[name] = document
            elapsed.append(wall)
            rendered.append(text)
        recovered, wall, text = _invoke(
            ["recover", "--store", os.fspath(store)],
            environment=environment,
            expected_exit=0,
        )
        elapsed.append(wall)
        rendered.append(text)
        deleted, wall, text = _invoke(
            ["delete-source", "--store", os.fspath(store), "--source-id", source_id],
            environment=environment,
            expected_exit=0,
        )
        elapsed.append(wall)
        rendered.append(text)
        absent, wall, text = _invoke(
            [
                "inspect",
                "--store",
                os.fspath(store),
                "--run-id",
                run_id,
                "--kind",
                "observations",
            ],
            environment=environment,
            expected_exit=1,
        )
        elapsed.append(wall)
        rendered.append(text)

        failure_cases = (
            ("runtime_absent", "runtime-absent", run_arguments, 1, "isolation_unavailable"),
            (
                "runtime_tampered",
                "runtime-tampered",
                run_arguments,
                1,
                "isolation_unavailable",
            ),
            (
                "unsupported_platform",
                "unsupported-platform",
                run_arguments,
                3,
                "unsupported",
            ),
            (
                "unsupported_configuration",
                "must-not-execute",
                [*run_arguments[:-1], "person"],
                3,
                "unsupported",
            ),
            (
                "hostile_path",
                "must-not-execute",
                [
                    *run_arguments[: run_arguments.index("synthetic-v1/cfr.mov")],
                    "../escape.mov",
                    *run_arguments[run_arguments.index("synthetic-v1/cfr.mov") + 1 :],
                ],
                1,
                "invalid_request",
            ),
            (
                "hostile_worker_output",
                "hostile-worker-output",
                run_arguments,
                1,
                "decode_failed",
            ),
            ("timeout", "elapsed-timeout", run_arguments, 124, "timeout"),
            ("cancelled", "sigint", run_arguments, 130, "cancelled"),
            (
                "bounded_output",
                "bounded-output",
                ["validate-runtime", *runtimes],
                1,
                "limit_exceeded",
            ),
        )
        for name, scenario, arguments, expected_exit, expected_code in failure_cases:
            document, wall, text = _invoke(
                arguments,
                environment=environment,
                expected_exit=expected_exit,
                scenario=scenario,
            )
            if document.get("error", {}).get("code") != expected_code:
                raise ValueError("perception CLI stable failure mapping failed")
            failure_documents[name] = document
            elapsed.append(wall)
            rendered.append(text)

        first_items = cast(list[object], observation_result["items"])
        second_items = cast(list[object], _payload(observation_page_2)["items"])
        evidence_items = cast(list[dict[str, Any]], _payload(evidence)["items"])
        export_payload = _payload(exported)
        checks = {
            "canonical_success_and_error_documents": all(
                document.get("schema") == "visualworld.cli-result"
                and document.get("schema_version") == 1
                for document in (
                    first,
                    second,
                    observation_page,
                    observation_page_2,
                    tracklets,
                    evidence,
                    exported,
                    recovered,
                    deleted,
                    absent,
                    *failure_documents.values(),
                )
            ),
            "complete_then_idempotent_replay": first_result["disposition"] == "committed"
            and second_result["disposition"] == "already_committed"
            and first_result["run_id"] == second_result["run_id"],
            "fresh_process_pagination": len(first_items) == len(second_items) == 1
            and first_items[0] != second_items[0],
            "tracklet_termination_and_source_time": len(tracklet_items) == 2
            and all(
                "termination" in item
                and "points" in item
                and "start_pts" in item
                and "end_pts" in item
                for item in tracklet_items
            ),
            "selected_evidence_has_original_geometry_and_provenance": len(evidence_items) == 1
            and "geometry" in evidence_items[0]["intent"]
            and "selector" in evidence_items[0]["intent"],
            "exact_selected_crop_exported": output.read_bytes() == bytes(75)
            and export_payload["export"]["bytes"] == 75,
            "recovery_and_deletion_completed": _payload(recovered)["integrity_issues"] == 0
            and _payload(deleted)["deletion"]["state"] == "complete",
            "deleted_graph_is_not_visible": absent["error"]["code"] == "not_found",
            "fresh_process_failures_are_stable": set(failure_documents)
            == {
                "bounded_output",
                "cancelled",
                "existing_output",
                "fifo_output",
                "hostile_path",
                "hostile_worker_output",
                "output_inside_store",
                "runtime_absent",
                "runtime_tampered",
                "symlink_output_parent",
                "timeout",
                "unsupported_configuration",
                "unsupported_platform",
            },
            "paths_redacted": os.fspath(private) not in "".join(rendered),
        }
        store_bytes = _disk_bytes(store)

    resources = {
        "all_commands_under_ten_seconds": all(
            0 < value <= _MAX_COMMAND_SECONDS * 1_000_000_000 for value in elapsed
        ),
        "child_peak_rss_bounded": 0 < _child_peak_rss_bytes() <= 512 * 1024 * 1024,
        "store_logical_bytes_bounded": store_bytes <= 16 * 1024 * 1024,
    }
    passed = all(checks.values()) and all(resources.values())
    return {
        "checks": checks,
        "implementation": {
            "cli_sha256": hashlib.sha256(
                (ROOT / "src/visualworld/perception_cli.py").read_bytes()
            ).hexdigest(),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "native_linux_runtime_executed": False,
        "profile": {
            "machine": platform.machine(),
            "os": platform.system(),
            "python": platform.python_version(),
        },
        "resources": resources,
        "schema": "visualworld.perception-cli-acceptance-receipt",
        "schema_version": 1,
        "status": "pass" if passed else "fail",
        "workload": {
            "command_count": len(elapsed),
            "deterministic_adapter_seam": True,
            "runtime_failure_boundary_seam": True,
            "network_allowed": False,
        },
    }


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--fixture-command":
        return _fixture_command(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = _run(arguments.work_root.resolve(strict=True))
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": result["status"]}, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
