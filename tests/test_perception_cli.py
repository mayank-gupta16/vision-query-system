# SPDX-License-Identifier: Apache-2.0
"""Deterministic contract tests for the issue-77 perception CLI surface."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import visualworld.cli as cli
import visualworld.perception_cli as perception_cli
from visualworld.coordinator import (
    CoordinatorError,
    CoordinatorErrorCode,
    CoordinatorEvent,
    CoordinatorStage,
    EventStatus,
    PerceptionDisposition,
    PerceptionEvent,
    PerceptionRunResult,
    RecoveryReport,
)
from visualworld.evidence import BestFrameEvidenceSelector
from visualworld.ingestion import (
    Artifact,
    EvidenceRef,
    Fingerprint,
    FrameRef,
    Geometry,
    MediaTime,
    Producer,
    Rational,
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
)
from visualworld.media import MediaMetrics, MediaRuntime
from visualworld.perception import Observation, Tracklet, TrackPoint
from visualworld.ports import PerceptionResultState, PortError, PortErrorCode, PortKind
from visualworld.world_store import DeletionState, DeletionStatus, PersistedEvidenceSelection

GOLDENS = Path(__file__).with_name("goldens")


def _result(captured: pytest.CaptureFixture[str], *, error: bool = False) -> dict[str, Any]:
    streams = captured.readouterr()
    text = streams.err if error else streams.out
    assert (streams.out if error else streams.err) == ""
    assert text.endswith("\n")
    assert "\n" not in text[:-1]
    return cast(dict[str, Any], json.loads(text))


def _runtime_arguments(command: str = "validate-runtime") -> list[str]:
    return [
        "perception",
        command,
        "--media-runtime-root",
        "/private/runtime/media",
        "--perception-runtime-root",
        "/private/runtime/perception",
        "--original-frame-overlay-root",
        "/private/runtime/original",
    ]


def _run_arguments() -> list[str]:
    return [
        *_runtime_arguments("run"),
        "--store",
        "/private/store",
        "--source-root",
        "/private/source-root",
        "--source",
        "clips/input.mp4",
        "--model",
        "vehicle-detection-0201",
        "--device",
        "CPU",
        "--category",
        "vehicle",
    ]


def _graph() -> tuple[
    RunManifest,
    tuple[FrameRef, ...],
    tuple[Observation, ...],
    Tracklet,
    tuple[PersistedEvidenceSelection, ...],
    bytes,
]:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint("ab" * 32, "30000"),
        (SourceStream(0, 64, 48, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(source.source_id, 0, str(index), MediaTime(str(index * 200), time_base))
        for index in range(2)
    )
    detector = Producer("visualworld.test-detector", "1", "bc" * 32)
    observations = tuple(
        Observation.create(
            source.source_id,
            frame.frame_id,
            0,
            frame.pts,
            Geometry(64, 48, (4 + index, 5, 20 + index, 30), "inferred"),
            "vehicle",
            900_000 + index,
            detector,
        )
        for index, frame in enumerate(frames)
    )
    tracklet = Tracklet.create(
        source.source_id,
        0,
        "vehicle",
        tuple(TrackPoint.from_observation(item) for item in observations),
        "source_end",
        Producer("visualworld.test-tracker", "1", "cd" * 32),
    )
    manifest = RunManifest.create(
        source.source_id,
        (detector,),
        Sampling(Rational("5", "1")),
        "committed",
        RunOutputs("2", hashlib.sha256(b"sample-index").hexdigest()),
    )
    intents = BestFrameEvidenceSelector().plan(tracklet, observations).intents
    selections: list[PersistedEvidenceSelection] = []
    first_pixels = b""
    for intent in intents:
        x_min, y_min, x_max, y_max = intent.geometry.box_xyxy
        pixels = bytes([intent.rank]) * ((x_max - x_min) * (y_max - y_min) * 3)
        reference = EvidenceRef.create(
            intent.frame_id,
            Artifact(hashlib.sha256(pixels).hexdigest(), str(len(pixels))),
            intent.geometry,
        )
        selections.append(PersistedEvidenceSelection(manifest.run_id, intent, reference))
        if intent.rank == 1:
            first_pixels = pixels
    return manifest, frames, observations, tracklet, tuple(selections), first_pixels


def test_legacy_parser_and_all_v01_golden_bytes_remain_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = cli.build_parser()
    with pytest.raises(cli._UsageError):
        parser.parse_args(["perception"])

    assert cli.main([]) == 0
    assert capsys.readouterr() == ((GOLDENS / "cli-help.txt").read_text(), "")
    assert cli.main(["probe"]) == 0
    assert capsys.readouterr() == ((GOLDENS / "cli-probe.json").read_text(), "")
    assert cli.main(["--unknown"]) == 2
    assert capsys.readouterr() == ("", (GOLDENS / "cli-error.json").read_text())


def test_perception_parser_is_side_effect_free_and_has_exact_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = perception_cli.build_parser()
    assert vars(parser.parse_args([])) == {"command": None}
    assert capsys.readouterr() == ("", "")
    assert cli.main(["perception"]) == 0
    captured = capsys.readouterr()
    for command in (
        "validate-runtime",
        "run",
        "inspect",
        "export-evidence",
        "recover",
        "delete-source",
    ):
        assert command in captured.out
    assert captured.err == ""


def test_unsupported_platform_precedes_source_and_store_touches(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    touches: list[str] = []

    def unsupported() -> bool:
        touches.append("platform")
        return False

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("source/store must not be constructed")

    monkeypatch.setattr(perception_cli, "_platform_supported", unsupported)
    monkeypatch.setattr(perception_cli, "LocalVideoSource", forbidden)
    monkeypatch.setattr(perception_cli, "LocalEvidenceStore", forbidden)
    monkeypatch.setattr(perception_cli, "LocalWorldStore", forbidden)
    original_lstat = Path.lstat

    def checked_lstat(path: Path) -> Any:
        if str(path).startswith("/private/"):
            raise AssertionError("unsupported-platform path was statted")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", checked_lstat)

    assert cli.main(_run_arguments()) == 3
    value = _result(capsys, error=True)
    assert value["error"] == {
        "code": "unsupported",
        "operation": "platform",
        "retryable": False,
    }
    assert touches == ["platform"]
    assert "/private" not in json.dumps(value)


@pytest.mark.parametrize(
    ("option", "value"),
    [("--model", "other"), ("--device", "GPU"), ("--category", "person")],
)
def test_nonapproved_configuration_fails_before_preflight(
    option: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def forbidden(request: object, cancelled: object) -> None:
        del request, cancelled
        nonlocal called
        called = True

    monkeypatch.setattr(perception_cli, "_execute_run", forbidden)
    arguments = _run_arguments()
    arguments[arguments.index(option) + 1] = value

    assert cli.main(arguments) == 3
    result = _result(capsys, error=True)
    assert result["error"] == {
        "code": "unsupported",
        "operation": "configuration",
        "retryable": False,
    }
    assert called is False
    assert value not in json.dumps(result)


def test_all_runtime_closures_validate_in_order_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(perception_cli, "_platform_supported", lambda: True)
    monkeypatch.setattr(
        perception_cli,
        "verify_media_runtime",
        lambda runtime, *, cancelled=None: calls.append("media"),
    )
    monkeypatch.setattr(
        perception_cli,
        "verify_perception_runtime",
        lambda runtime, *, cancelled=None: calls.append("perception"),
    )
    monkeypatch.setattr(
        perception_cli,
        "verify_original_frame_runtime",
        lambda runtime, *, cancelled=None: calls.append("original-frame"),
    )
    paths = perception_cli._RuntimePaths(Path("/media"), Path("/model"), Path("/overlay"))

    validated = perception_cli._validated_runtimes(paths)

    assert isinstance(validated.media, MediaRuntime)
    assert calls == ["media", "perception", "original-frame"]


def test_run_probes_source_before_constructing_stores_and_reuses_cancel_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    observed_events: list[threading.Event] = []
    media = MediaRuntime(Path("/media"), Path("/media/worker/media_worker.py"))
    validated = perception_cli._ValidatedRuntimes(
        media,
        cast(Any, object()),
        cast(Any, object()),
    )
    monkeypatch.setattr(
        perception_cli,
        "_validated_runtimes",
        lambda paths, cancelled=None: validated,
    )

    class Video:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args
            order.append("source")
            observed_events.append(cast(threading.Event, kwargs["cancelled"]))

        def probe(self) -> None:
            order.append("probe")

    class EvidenceStore:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            order.append("evidence-store")

    class WorldStore:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            order.append("world-store")

    class Worker:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args
            observed_events.append(cast(threading.Event, kwargs["cancelled"]))

    class Reader(Worker):
        pass

    class Coordinator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def run_with_original_frames(self, *args: object, **kwargs: object) -> PerceptionRunResult:
            del args
            observed_events.append(cast(threading.Event, kwargs["cancelled"]))
            return PerceptionRunResult(PerceptionResultState.UNKNOWN, reason="test_unknown")

    monkeypatch.setattr(perception_cli, "LocalVideoSource", Video)
    monkeypatch.setattr(perception_cli, "LocalEvidenceStore", EvidenceStore)
    monkeypatch.setattr(perception_cli, "LocalWorldStore", WorldStore)
    monkeypatch.setattr(perception_cli, "IsolatedPerceptionWorker", Worker)
    monkeypatch.setattr(perception_cli, "OpenVinoVehicleDetector", lambda worker: worker)
    monkeypatch.setattr(perception_cli, "IsolatedOriginalFrameReader", Reader)
    monkeypatch.setattr(perception_cli, "PerceptionCoordinator", Coordinator)
    request = perception_cli._RunRequest(
        Path("/store"),
        Path("/source"),
        "clip.mp4",
        perception_cli._RuntimePaths(Path("/media"), Path("/model"), Path("/overlay")),
        0,
    )
    cancelled = threading.Event()

    result, video = perception_cli._execute_run(request, cancelled)

    assert result.reason == "test_unknown"
    assert cast(Any, video).__class__ is Video
    assert order == ["source", "probe", "evidence-store", "world-store"]
    assert observed_events == [cancelled, cancelled, cancelled, cancelled]


@pytest.mark.parametrize(
    ("state", "reason", "exit_code", "error_code"),
    [
        (PerceptionResultState.UNSUPPORTED, "unsupported_platform", 3, "unsupported"),
        (PerceptionResultState.UNKNOWN, "model_unavailable", 4, "unknown"),
        (PerceptionResultState.UNKNOWN, "timeout", 124, "timeout"),
        (PerceptionResultState.UNKNOWN, "cancelled", 130, "cancelled"),
    ],
)
def test_incomplete_run_results_have_stable_exits_and_redacted_payloads(
    state: PerceptionResultState,
    reason: str,
    exit_code: int,
    error_code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = PerceptionRunResult(state, reason=reason)
    video = cast(Any, object())
    monkeypatch.setattr(perception_cli, "_execute_run", lambda request, cancelled: (result, video))

    assert cli.main(_run_arguments()) == exit_code
    value = _result(capsys, error=True)
    assert value["error"]["code"] == error_code
    assert value["result"]["state"] == state.value
    assert value["result"]["reason"] == reason
    assert "/private" not in json.dumps(value)


def test_command_deadline_cancels_the_shared_event_and_maps_to_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ImmediateTimer:
        daemon = False

        def __init__(self, interval: int, function: Any) -> None:
            assert interval == perception_cli.PERCEPTION_RUN_TIMEOUT_SECONDS
            self._function = function

        def start(self) -> None:
            self._function()

        def cancel(self) -> None:
            pass

        def join(self) -> None:
            pass

    def cancelled_run(request: object, cancelled: threading.Event) -> None:
        del request
        assert cancelled.is_set()
        raise PortError(PortErrorCode.CANCELLED, PortKind.DETECTOR, "detect")

    monkeypatch.setattr(cast(Any, perception_cli).threading, "Timer", ImmediateTimer)
    monkeypatch.setattr(perception_cli, "_execute_run", cancelled_run)

    assert cli.main(_run_arguments()) == 124
    value = _result(capsys, error=True)
    assert value["error"] == {
        "code": "timeout",
        "operation": "run",
        "retryable": True,
    }


def test_command_deadline_rejects_result_returned_after_timer_fired(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ImmediateTimer:
        daemon = False

        def __init__(self, interval: int, function: Any) -> None:
            assert interval == perception_cli.PERCEPTION_RUN_TIMEOUT_SECONDS
            self._function = function

        def start(self) -> None:
            self._function()

        def cancel(self) -> None:
            pass

        def join(self) -> None:
            pass

    def late_result(
        request: object, cancelled: threading.Event
    ) -> tuple[PerceptionRunResult, object]:
        del request
        assert cancelled.is_set()
        return PerceptionRunResult(PerceptionResultState.UNKNOWN, reason="cancelled"), object()

    monkeypatch.setattr(cast(Any, perception_cli).threading, "Timer", ImmediateTimer)
    monkeypatch.setattr(perception_cli, "_execute_run", late_result)

    assert cli.main(_run_arguments()) == 124
    value = _result(capsys, error=True)
    assert value["error"] == {
        "code": "timeout",
        "operation": "run",
        "retryable": True,
    }


def test_complete_run_payload_is_bounded_metadata_without_pixels_or_paths() -> None:
    manifest, frames, observations, tracklet, selections, _ = _graph()
    evidence = tuple(cast(EvidenceRef, selection.evidence) for selection in selections)
    result = PerceptionRunResult(
        PerceptionResultState.COMPLETE,
        manifest,
        frames,
        observations,
        (tracklet,),
        tuple(selection.intent for selection in selections),
        PerceptionDisposition.COMMITTED,
        evidence=evidence,
    )

    class Video:
        metrics = MediaMetrics(12, 10, 4096, 30000, 2)

    payload = perception_cli._run_payload(result, cast(Any, Video()))
    encoded = json.dumps(payload)
    assert payload["run_id"] == manifest.run_id
    assert payload["counts"] == {
        "evidence": len(selections),
        "observations": 2,
        "samples": 2,
        "tracklets": 1,
    }
    assert "pixels" not in encoded
    assert "/private" not in encoded
    assert payload["stages"] == []
    assert "observation_ids" not in payload
    assert "tracklet_ids" not in payload
    assert "evidence_ids" not in payload


@pytest.mark.parametrize("kind", ["observations", "tracklets", "evidence"])
def test_inspection_pages_use_bounded_nonoverlapping_cursors_and_include_run_id(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest, _, observations, tracklet, selections, _ = _graph()
    root = tmp_path / "store"
    root.mkdir()

    class World:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == root

        def list_run_observations(self, run_id: str, **kwargs: object) -> tuple[Observation, ...]:
            assert run_id == manifest.run_id
            after_id = kwargs["after_observation_id"]
            start = (
                0
                if after_id is None
                else next(
                    index + 1
                    for index, item in enumerate(observations)
                    if item.observation_id == after_id
                )
            )
            return observations[start : start + cast(int, kwargs["limit"])]

        def list_run_tracklets(self, run_id: str, **kwargs: object) -> tuple[Tracklet, ...]:
            assert run_id == manifest.run_id
            return () if kwargs["after_tracklet_id"] is not None else (tracklet,)

        def list_selected_evidence(
            self, run_id: str, tracklet_id: str, **kwargs: object
        ) -> tuple[PersistedEvidenceSelection, ...]:
            assert run_id == manifest.run_id
            assert tracklet_id == tracklet.tracklet_id
            after_rank = kwargs["after_rank"]
            start = 0 if after_rank is None else cast(int, after_rank)
            return selections[start : start + cast(int, kwargs["limit"])]

    monkeypatch.setattr(perception_cli, "LocalWorldStore", World)
    arguments = [
        "perception",
        "inspect",
        "--store",
        str(root),
        "--run-id",
        manifest.run_id,
        "--kind",
        kind,
        "--limit",
        "1",
    ]
    if kind == "evidence":
        arguments.extend(["--tracklet-id", tracklet.tracklet_id])

    assert cli.main(arguments) == 0
    first = _result(capsys)["result"]
    assert first["run_id"] == manifest.run_id
    assert len(first["items"]) == 1
    if first["next"] is None:
        assert kind == "tracklets"
        return
    second_arguments = list(arguments)
    for name, value in first["next"].items():
        second_arguments.extend(["--" + name.replace("_", "-"), str(value)])
    assert cli.main(second_arguments) == 0
    second = _result(capsys)["result"]
    assert len(second["items"]) == 1
    assert first["items"] != second["items"]
    assert second["next"] is None


def test_export_only_writes_exact_materialized_selected_crop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest, _, _, tracklet, selections, pixels = _graph()
    selection = selections[0]
    reference = cast(EvidenceRef, selection.evidence)
    root = tmp_path / "store"
    root.mkdir()
    destination = tmp_path / "selected.rgb24"

    class World:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == root

        def list_selected_evidence(
            self, run_id: str, tracklet_id: str, **kwargs: object
        ) -> tuple[PersistedEvidenceSelection, ...]:
            assert run_id == manifest.run_id
            assert tracklet_id == tracklet.tracklet_id
            assert kwargs == {"after_rank": None, "limit": 1}
            return (selection,)

        def get(self, evidence_id: str) -> EvidenceRef:
            assert evidence_id == reference.evidence_id
            return reference

    class EvidenceStore:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == root

        def get(self, sha256: str) -> bytes:
            assert sha256 == reference.artifact.sha256
            return pixels

    monkeypatch.setattr(perception_cli, "LocalWorldStore", World)
    monkeypatch.setattr(perception_cli, "LocalEvidenceStore", EvidenceStore)

    assert (
        cli.main(
            [
                "perception",
                "export-evidence",
                "--store",
                str(root),
                "--run-id",
                manifest.run_id,
                "--tracklet-id",
                tracklet.tracklet_id,
                "--rank",
                "1",
                "--output",
                str(destination),
            ]
        )
        == 0
    )
    value = _result(capsys)
    assert destination.read_bytes() == pixels
    assert stat.S_IMODE(destination.stat().st_mode) == 0o400
    serialized = json.dumps(value)
    assert str(destination) not in serialized
    assert repr(pixels) not in serialized
    assert value["result"]["run_id"] == manifest.run_id


def test_recover_and_delete_source_emit_bounded_canonical_summaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    source_id = "src_" + "aa" * 32
    deletion = DeletionStatus(
        "del_" + "bb" * 32,
        DeletionState.COMPLETE,
        3,
        2,
        1,
        "2026-09-10T00:00:00Z",
    )

    class Store:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == root

    class Coordinator:
        def __init__(self, evidence: object, world: object) -> None:
            assert isinstance(evidence, Store)
            assert isinstance(world, Store)

        def recover(self) -> RecoveryReport:
            return RecoveryReport(1, 2, 3, 0, ())

        def delete_source(
            self, selected_source_id: str, *, deletion_id: str | None
        ) -> DeletionStatus:
            assert selected_source_id == source_id
            assert deletion_id == deletion.deletion_id
            return deletion

    monkeypatch.setattr(perception_cli, "LocalEvidenceStore", Store)
    monkeypatch.setattr(perception_cli, "LocalWorldStore", Store)
    monkeypatch.setattr(perception_cli, "IngestionCoordinator", Coordinator)

    assert cli.main(["perception", "recover", "--store", str(root)]) == 0
    recovered = _result(capsys)["result"]
    assert recovered == {
        "deletions_completed": 2,
        "events": [],
        "integrity_issues": 0,
        "runs_cleaned": 1,
        "schema": "visualworld.perception-cli.recovery",
        "schema_version": 1,
        "staging_entries_removed": 3,
    }

    assert (
        cli.main(
            [
                "perception",
                "delete-source",
                "--store",
                str(root),
                "--source-id",
                source_id,
                "--deletion-id",
                deletion.deletion_id,
            ]
        )
        == 0
    )
    deleted = _result(capsys)["result"]
    assert deleted["deletion"]["deletion_id"] == deletion.deletion_id
    assert deleted["deletion"]["state"] == "complete"
    assert str(root) not in json.dumps(deleted)


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [(PortErrorCode.TIMEOUT, 124), (PortErrorCode.CANCELLED, 130)],
)
def test_port_timeout_and_cancel_have_stable_exit_codes(
    code: PortErrorCode,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(arguments: argparse.Namespace) -> perception_cli._CommandResult:
        del arguments
        raise PortError(
            code, PortKind.VIDEO_SOURCE, "probe", retryable=code is PortErrorCode.TIMEOUT
        )

    monkeypatch.setattr(perception_cli, "_run_validate_runtime", fail)
    assert cli.main(_runtime_arguments()) == exit_code
    value = _result(capsys, error=True)
    assert value["error"]["code"] == code.value
    assert value["error"]["operation"] == "probe"


@pytest.mark.parametrize(
    ("arguments", "code", "operation"),
    [
        (
            [
                "perception",
                "inspect",
                "--store",
                "/private/missing-store",
                "--run-id",
                "run_" + "a" * 64,
                "--kind",
                "observations",
            ],
            "not_found",
            "store",
        ),
        (
            [
                "perception",
                "inspect",
                "--store",
                "/private/store",
                "--run-id",
                "run_" + "a" * 64,
                "--kind",
                "evidence",
            ],
            "invalid_request",
            "tracklet_id",
        ),
        (
            [
                "perception",
                "export-evidence",
                "--store",
                "/private/store",
                "--run-id",
                "run_" + "a" * 64,
                "--tracklet-id",
                "trk_" + "b" * 64,
                "--rank",
                "1",
                "--output",
                "relative.rgb24",
            ],
            "invalid_request",
            "output",
        ),
    ],
)
def test_legacy_path_and_argument_errors_keep_actionable_codes(
    arguments: list[str],
    code: str,
    operation: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    arguments = [
        str(store)
        if value == "/private/store"
        else str(tmp_path / "missing-store")
        if value == "/private/missing-store"
        else value
        for value in arguments
    ]

    assert cli.main(arguments) == 1
    value = _result(capsys, error=True)
    assert value["error"] == {
        "code": code,
        "operation": operation,
        "retryable": False,
    }


def test_output_cap_fails_closed_without_emitting_the_large_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(perception_cli, "MAX_PERCEPTION_CLI_OUTPUT_BYTES", 1)
    monkeypatch.setattr(
        perception_cli,
        "_run_validate_runtime",
        lambda arguments: perception_cli._CommandResult({"private": "x" * 10_000}),
    )

    assert cli.main(_runtime_arguments()) == 1
    value = _result(capsys, error=True)
    assert value["error"] == {
        "code": "limit_exceeded",
        "operation": "emit_output",
        "retryable": False,
    }
    assert "private" not in json.dumps(value)


@pytest.mark.parametrize(
    "source",
    ["/absolute.mp4", "../escape.mp4", "clips/../escape.mp4", r"clips\escape.mp4", "https://x"],
)
def test_hostile_source_names_fail_before_runtime_or_state(
    source: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def forbidden(request: object, cancelled: object) -> None:
        del request, cancelled
        nonlocal called
        called = True

    monkeypatch.setattr(perception_cli, "_execute_run", forbidden)
    arguments = _run_arguments()
    arguments[arguments.index("clips/input.mp4")] = source

    assert cli.main(arguments) == 1
    value = _result(capsys, error=True)
    assert value["error"]["code"] == "invalid_request"
    assert called is False
    assert source not in json.dumps(value)


def test_argument_helpers_and_platform_checks_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert perception_cli._integer(argparse.Namespace(value=3), "value", 0, 3) == 3
    for value in (True, -1, 4, "3"):
        with pytest.raises(perception_cli._PerceptionCliError) as raised:
            perception_cli._integer(argparse.Namespace(value=value), "value", 0, 3)
        assert raised.value.operation == "value"

    assert perception_cli._optional(argparse.Namespace(value=None), "value") is None
    assert perception_cli._optional(argparse.Namespace(value="cursor"), "value") == "cursor"
    with pytest.raises(perception_cli._PerceptionCliError):
        perception_cli._optional(argparse.Namespace(value=1), "value")

    for value in ("relative", "/private/bad\nname", "/" + "x" * 4097, "\udcff"):
        with pytest.raises(perception_cli._PerceptionCliError) as raised:
            perception_cli._absolute_argument(argparse.Namespace(value=value), "value")
        assert raised.value.operation == "value"

    for value in ("", "clips/bad\nname.mp4", "\udcff"):
        with pytest.raises(perception_cli._PerceptionCliError) as raised:
            perception_cli._relative_source(argparse.Namespace(source=value))
        assert raised.value.operation == "source"

    monkeypatch.setattr(cast(Any, perception_cli)._detection, "_supported_platform", lambda: 1)
    monkeypatch.setattr(cast(Any, perception_cli)._original, "_supported_platform", lambda: True)
    assert perception_cli._platform_supported() is False
    monkeypatch.setattr(
        cast(Any, perception_cli)._detection,
        "_supported_platform",
        lambda: (_ for _ in ()).throw(RuntimeError("private")),
    )
    assert perception_cli._platform_supported() is False


def test_event_summaries_aggregate_bounded_public_fields() -> None:
    events = (
        PerceptionEvent(CoordinatorStage.DETECT, EventStatus.SUCCEEDED, 7, 2),
        PerceptionEvent(CoordinatorStage.DETECT, EventStatus.SUCCEEDED, 11, 3),
        PerceptionEvent(CoordinatorStage.TRACK, EventStatus.FAILED, 13, 1),
    )

    assert perception_cli._run_stage_summaries(events) == [
        {
            "call_count": 2,
            "duration_ns": 18,
            "item_count": 5,
            "protocol_version": 1,
            "stage": "detect",
            "status": "succeeded",
        },
        {
            "call_count": 1,
            "duration_ns": 13,
            "item_count": 1,
            "protocol_version": 1,
            "stage": "track",
            "status": "failed",
        },
    ]
    run_id = "run_" + "a" * 64
    deletion_id = "del_" + "b" * 64
    assert (
        perception_cli._coordinator_event(
            CoordinatorEvent(
                CoordinatorStage.RECOVER_RUN,
                EventStatus.SUCCEEDED,
                5,
                1,
                run_id=run_id,
            )
        )["run_id"]
        == run_id
    )
    assert (
        perception_cli._coordinator_event(
            CoordinatorEvent(
                CoordinatorStage.RECOVER_DELETION,
                EventStatus.SUCCEEDED,
                6,
                1,
                deletion_id=deletion_id,
            )
        )["deletion_id"]
        == deletion_id
    )


def test_interrupt_scope_cancels_and_cleans_up_in_main_and_worker_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[Any] = []
    timer_actions: list[str] = []

    class Timer:
        daemon = False

        def __init__(self, interval: int, function: Any) -> None:
            assert interval == 2
            self.function = function

        def start(self) -> None:
            timer_actions.append("start")

        def cancel(self) -> None:
            timer_actions.append("cancel")

        def join(self) -> None:
            timer_actions.append("join")

    monkeypatch.setattr(cast(Any, perception_cli).threading, "Timer", Timer)
    monkeypatch.setattr(cast(Any, perception_cli).signal, "getsignal", lambda _signal: "previous")
    monkeypatch.setattr(
        cast(Any, perception_cli).signal,
        "signal",
        lambda _signal, handler: installed.append(handler),
    )
    with perception_cli._cancel_on_interrupt(timeout_seconds=2) as control:
        assert control.cancelled.is_set() is False
        installed[-1](2, None)
        assert control.cancelled.is_set() is True
    assert installed[-1] == "previous"
    assert timer_actions == ["start", "cancel", "join"]

    worker = object()
    monkeypatch.setattr(cast(Any, perception_cli).threading, "current_thread", lambda: worker)
    monkeypatch.setattr(cast(Any, perception_cli).threading, "main_thread", lambda: object())
    timer_actions.clear()
    with perception_cli._cancel_on_interrupt(timeout_seconds=2) as control:
        assert control.timed_out.is_set() is False
    assert timer_actions == ["start", "cancel", "join"]

    with (
        pytest.raises(ValueError, match="positive integer"),
        perception_cli._cancel_on_interrupt(timeout_seconds=0),
    ):
        pass


@pytest.mark.parametrize("failure_kind", ["port", "cli", "late", "cancelled"])
def test_runtime_validation_maps_deadlines_and_cancellation(
    failure_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = perception_cli._CancellationControl(threading.Event(), threading.Event())
    if failure_kind in {"port", "cli", "late"}:
        control.timed_out.set()
        control.cancelled.set()
    elif failure_kind == "cancelled":
        control.cancelled.set()

    class Scope:
        def __enter__(self) -> perception_cli._CancellationControl:
            return control

        def __exit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(perception_cli, "_cancel_on_interrupt", lambda **_kwargs: Scope())

    def validate(*args: object, **kwargs: object) -> None:
        del args, kwargs
        if failure_kind == "port":
            raise PortError(PortErrorCode.CANCELLED, PortKind.VIDEO_SOURCE, "probe")
        if failure_kind == "cli":
            raise perception_cli._PerceptionCliError("cancelled", "validate_runtime", exit_code=130)

    monkeypatch.setattr(perception_cli, "_validated_runtimes", validate)
    parsed = perception_cli.build_parser().parse_args(_runtime_arguments()[1:])
    with pytest.raises(perception_cli._PerceptionCliError) as raised:
        perception_cli._run_validate_runtime(parsed)
    if failure_kind in {"port", "cli", "late"}:
        assert (raised.value.code, raised.value.exit_code, raised.value.retryable) == (
            "timeout",
            124,
            True,
        )
    else:
        assert (raised.value.code, raised.value.exit_code) == ("cancelled", 130)


def test_runtime_validation_success_and_post_verifier_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actual_validate = perception_cli._validated_runtimes
    monkeypatch.setattr(
        perception_cli,
        "_validated_runtimes",
        lambda *args, **kwargs: cast(Any, object()),
    )
    assert cli.main(_runtime_arguments()) == 0
    assert _result(capsys)["result"]["status"] == "ready"

    monkeypatch.setattr(perception_cli, "_validated_runtimes", actual_validate)
    cancelled = threading.Event()
    monkeypatch.setattr(perception_cli, "_platform_supported", lambda: True)
    monkeypatch.setattr(perception_cli, "verify_media_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(perception_cli, "verify_perception_runtime", lambda *args, **kwargs: None)

    def cancel_after_overlay(*args: object, **kwargs: object) -> None:
        del args, kwargs
        cancelled.set()

    monkeypatch.setattr(perception_cli, "verify_original_frame_runtime", cancel_after_overlay)
    with pytest.raises(perception_cli._PerceptionCliError) as raised:
        perception_cli._validated_runtimes(
            perception_cli._RuntimePaths(Path("/media"), Path("/model"), Path("/overlay")),
            cancelled,
        )
    assert raised.value.code == "cancelled"


def test_inspection_rejects_mixed_or_partial_cursors_before_queries() -> None:
    class World:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"query must not run: {name}")

    base: dict[str, object] = {
        "stream_index": 0,
        "after_pts_value": None,
        "after_observation_id": None,
        "after_start_pts_value": None,
        "after_tracklet_id": None,
        "tracklet_id": None,
        "after_rank": None,
    }
    cases: list[tuple[Any, dict[str, object], str]] = [
        (perception_cli._inspect_observations, {"after_pts_value": "1"}, "observation_cursor"),
        (perception_cli._inspect_observations, {"after_rank": 1}, "inspect"),
        (perception_cli._inspect_tracklets, {"after_tracklet_id": "trk"}, "tracklet_cursor"),
        (perception_cli._inspect_tracklets, {"after_pts_value": "1"}, "inspect"),
        (perception_cli._inspect_evidence, {"tracklet_id": "trk", "after_rank": 9}, "after_rank"),
        (
            perception_cli._inspect_evidence,
            {"tracklet_id": "trk", "after_observation_id": "obs"},
            "inspect",
        ),
    ]
    for handler, changes, operation in cases:
        with pytest.raises(perception_cli._PerceptionCliError) as raised:
            handler(cast(Any, World()), argparse.Namespace(**(base | changes)), "run", 1)
        assert raised.value.operation == operation


def test_tracklet_inspection_emits_next_cursor_only_when_more_records_exist() -> None:
    record = SimpleNamespace(
        start_pts=SimpleNamespace(value="10"),
        tracklet_id="trk_" + "a" * 64,
        to_mapping=lambda: {"tracklet_id": "trk_" + "a" * 64},
    )

    class World:
        def list_run_tracklets(self, _run_id: str, **kwargs: object) -> tuple[Any, ...]:
            return (record,) if kwargs["after_tracklet_id"] is None else (object(),)

    result = perception_cli._inspect_tracklets(
        cast(Any, World()),
        argparse.Namespace(
            stream_index=0,
            after_start_pts_value=None,
            after_tracklet_id=None,
            after_pts_value=None,
            after_observation_id=None,
            tracklet_id=None,
            after_rank=None,
        ),
        "run_" + "b" * 64,
        1,
    )
    assert result.payload["next"] == {
        "after_start_pts_value": "10",
        "after_tracklet_id": record.tracklet_id,
    }


def test_export_rejects_missing_unmaterialized_and_mismatched_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _, _, tracklet, selections, pixels = _graph()
    selection = selections[0]
    reference = cast(EvidenceRef, selection.evidence)
    root = tmp_path / "store"
    root.mkdir()
    mode = ["missing"]

    class World:
        def __init__(self, _root: Path) -> None:
            pass

        def list_selected_evidence(self, *args: object, **kwargs: object) -> tuple[Any, ...]:
            del args, kwargs
            if mode[0] == "missing":
                return ()
            if mode[0] == "unmaterialized":
                return (PersistedEvidenceSelection(manifest.run_id, selection.intent, None),)
            return (selection,)

        def get(self, _evidence_id: str) -> object:
            return object() if mode[0] == "record-mismatch" else reference

    class EvidenceStore:
        def __init__(self, _root: Path) -> None:
            pass

        def get(self, _sha256: str) -> bytes:
            return pixels

    monkeypatch.setattr(perception_cli, "LocalWorldStore", World)
    monkeypatch.setattr(perception_cli, "LocalEvidenceStore", EvidenceStore)
    arguments = argparse.Namespace(
        store=str(root),
        output=str(tmp_path / "selected.rgb24"),
        run_id=manifest.run_id,
        tracklet_id=tracklet.tracklet_id,
        rank=1,
    )

    for selected_mode, code in (
        ("missing", "not_found"),
        ("unmaterialized", "not_found"),
        ("record-mismatch", "corrupt"),
    ):
        mode[0] = selected_mode
        with pytest.raises(perception_cli._PerceptionCliError) as raised:
            perception_cli._run_export_evidence(arguments)
        assert raised.value.code == code

    mode[0] = "artifact-mismatch"
    monkeypatch.setattr(
        perception_cli,
        "write_rgb24_crop",
        lambda *args, **kwargs: Artifact("0" * 64, "1"),
    )
    with pytest.raises(perception_cli._PerceptionCliError) as raised:
        perception_cli._run_export_evidence(arguments)
    assert raised.value.code == "corrupt"


@pytest.mark.parametrize(
    ("failure", "code", "operation", "exit_code"),
    [
        (KeyboardInterrupt(), "cancelled", "run", 130),
        (
            CoordinatorError(
                CoordinatorErrorCode.CONFLICT,
                CoordinatorStage.FINALIZE,
                retryable=True,
            ),
            "conflict",
            "finalize",
            1,
        ),
        (TypeError("private"), "invalid_request", "run", 1),
        (RuntimeError("private"), "operation_failed", "run", 1),
    ],
)
def test_main_maps_unexpected_handler_failures_without_private_details(
    failure: BaseException,
    code: str,
    operation: str,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Parser:
        def parse_args(self, _argv: object) -> argparse.Namespace:
            def fail(_arguments: argparse.Namespace) -> perception_cli._CommandResult:
                raise failure

            return argparse.Namespace(command="run", handler=fail)

    monkeypatch.setattr(perception_cli, "build_parser", Parser)
    assert perception_cli.main([]) == exit_code
    result = _result(capsys, error=True)
    assert result["error"]["code"] == code
    assert result["error"]["operation"] == operation
    assert "private" not in json.dumps(result)


def test_emit_and_parser_output_failures_have_stable_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_write(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise cli._OutputError("write failed")

    monkeypatch.setattr(cast(Any, perception_cli)._legacy, "_write_text", fail_write)
    monkeypatch.setattr(cast(Any, perception_cli)._legacy, "_write_document", fail_write)
    assert perception_cli._emit("perception.test", perception_cli._CommandResult({})) == 1
    assert (
        perception_cli._emit(
            "perception.test",
            perception_cli._failure_result("operation_failed", "test", 4),
        )
        == 4
    )

    class ParseOutputFailure:
        def parse_args(self, _argv: object) -> argparse.Namespace:
            raise cli._OutputError("write failed")

    monkeypatch.setattr(perception_cli, "build_parser", ParseOutputFailure)
    assert perception_cli.main([]) == 1
