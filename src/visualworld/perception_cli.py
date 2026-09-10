# SPDX-License-Identifier: Apache-2.0
"""Bounded canonical-JSON CLI for the approved v0.2 perception slice.

This module is reached only through the exact ``visualworld perception`` token.
The version-1 parser deliberately does not import it while serving legacy help,
version, or commands.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import FrameType
from typing import TextIO, cast

from visualworld import cli as _legacy
from visualworld import detection as _detection
from visualworld import original_frame_runtime as _original
from visualworld.coordinator import (
    CoordinatorError,
    CoordinatorEvent,
    IngestionCoordinator,
    PerceptionConfig,
    PerceptionCoordinator,
    PerceptionEvent,
    PerceptionRunResult,
)
from visualworld.detection import (
    DETECTOR_CONFIDENCE_FLOOR_MILLIONTHS,
    DETECTOR_CONFIGURATION_SHA256,
    DETECTOR_MANIFEST_SHA256,
    DETECTOR_MODEL_BIN_SHA256,
    DETECTOR_MODEL_XML_SHA256,
    DETECTOR_RUNTIME_CLOSURE_SHA256,
    DETECTOR_RUNTIME_ID,
    DETECTOR_WORKER_SHA256,
    IsolatedPerceptionWorker,
    OpenVinoVehicleDetector,
    PerceptionRuntime,
    verify_perception_runtime,
)
from visualworld.evidence import BestFrameEvidenceSelector
from visualworld.geometry import CropError, Rgb24Crop, write_rgb24_crop
from visualworld.ingestion import EvidenceRef, RecordValidationError
from visualworld.media import LocalVideoSource, MediaRuntime, verify_media_runtime
from visualworld.original_frame_runtime import (
    IsolatedOriginalFrameReader,
    OriginalFrameRuntime,
    verify_original_frame_runtime,
)
from visualworld.perception_materialization import EvidenceMaterializationConfig
from visualworld.ports import MAX_PORT_BATCH_ITEMS, PerceptionResultState, PortError, PortErrorCode
from visualworld.sampling import PtsFrameSampler
from visualworld.storage import LocalEvidenceStore
from visualworld.tracking import GlobalLastBoxTracker
from visualworld.world_store import DeletionStatus, LocalWorldStore, PersistedEvidenceSelection

PERCEPTION_CLI_SCHEMA_VERSION = 1
MAX_PERCEPTION_CLI_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_INSPECTION_PAGE_ITEMS = MAX_PORT_BATCH_ITEMS
PERCEPTION_RUN_TIMEOUT_SECONDS = 60

_MODEL = "vehicle-detection-0201"
_DEVICE = "CPU"
_CATEGORY = "vehicle"
_MEDIA_WORKER = Path("worker/media_worker.py")
_PERCEPTION_WORKER = Path("worker/perception_worker.py")
_ORIGINAL_FRAME_WORKER = Path("worker/original_frame_worker.py")


@dataclass(frozen=True, slots=True)
class _RuntimePaths:
    media: Path
    perception: Path
    original_frame: Path


@dataclass(frozen=True, slots=True)
class _ValidatedRuntimes:
    media: MediaRuntime
    perception: PerceptionRuntime
    original_frame: OriginalFrameRuntime


@dataclass(frozen=True, slots=True)
class _RunRequest:
    store: Path
    source_root: Path
    source_name: str
    runtimes: _RuntimePaths
    stream_index: int


@dataclass(frozen=True, slots=True)
class _CommandResult:
    payload: dict[str, object]
    exit_code: int = 0
    error_code: str | None = None
    operation: str | None = None
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class _CancellationControl:
    cancelled: threading.Event
    timed_out: threading.Event


class _PerceptionCliError(RuntimeError):
    """A bounded failure whose constructor accepts no untrusted detail."""

    def __init__(
        self,
        code: str,
        operation: str,
        *,
        exit_code: int = 1,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.operation = operation
        self.exit_code = exit_code
        self.retryable = retryable
        super().__init__(f"{code} at perception_cli.{operation}")


def _schema(name: str, **values: object) -> dict[str, object]:
    return {
        **values,
        "schema": f"visualworld.perception-cli.{name}",
        "schema_version": PERCEPTION_CLI_SCHEMA_VERSION,
    }


def _argument(arguments: argparse.Namespace, name: str) -> str:
    return _legacy._argument(arguments, name)


def _integer(arguments: argparse.Namespace, name: str, minimum: int, maximum: int) -> int:
    value = getattr(arguments, name, None)
    if type(value) is not int or not minimum <= value <= maximum:
        raise _PerceptionCliError("invalid_request", name)
    return value


def _absolute_argument(arguments: argparse.Namespace, name: str) -> Path:
    value = _argument(arguments, name)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise _PerceptionCliError("invalid_request", name) from None
    path = Path(value)
    if (
        not path.is_absolute()
        or path.name in {"", ".", ".."}
        or not encoded
        or len(encoded) > 4096
        or any(byte < 32 for byte in encoded)
    ):
        raise _PerceptionCliError("invalid_request", name)
    return path


def _relative_source(arguments: argparse.Namespace) -> str:
    value = _argument(arguments, "source")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise _PerceptionCliError("invalid_request", "source") from None
    path = PurePosixPath(value)
    if (
        not value
        or len(encoded) > 4096
        or any(byte < 32 for byte in encoded)
        or "\\" in value
        or "://" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise _PerceptionCliError("invalid_request", "source")
    return value


def _runtime_paths(arguments: argparse.Namespace) -> _RuntimePaths:
    return _RuntimePaths(
        _absolute_argument(arguments, "media_runtime_root"),
        _absolute_argument(arguments, "perception_runtime_root"),
        _absolute_argument(arguments, "original_frame_overlay_root"),
    )


def _configuration(arguments: argparse.Namespace) -> None:
    if (
        _argument(arguments, "model") != _MODEL
        or _argument(arguments, "device") != _DEVICE
        or _argument(arguments, "category") != _CATEGORY
    ):
        raise _PerceptionCliError(
            "unsupported",
            "configuration",
            exit_code=3,
        )


def _platform_supported() -> bool:
    try:
        detector = _detection._supported_platform()
        original = _original._supported_platform()
    except Exception:
        return False
    return type(detector) is bool and type(original) is bool and detector and original


def _validated_runtimes(
    paths: _RuntimePaths,
    cancelled: threading.Event | None = None,
) -> _ValidatedRuntimes:
    """Validate every closure before any local store or source is touched.

    The public media/perception validators and the frozen overlay verifier perform
    no acquisition; the caller supplies every absolute root.
    """

    if not _platform_supported():
        raise _PerceptionCliError("unsupported", "platform", exit_code=3)
    media = MediaRuntime(paths.media, paths.media / _MEDIA_WORKER)
    perception = PerceptionRuntime(paths.perception, media)
    original = OriginalFrameRuntime(
        media,
        paths.original_frame,
        paths.original_frame / _ORIGINAL_FRAME_WORKER,
    )
    verify_media_runtime(media, cancelled=cancelled)
    verify_perception_runtime(perception, cancelled=cancelled)
    verify_original_frame_runtime(original, cancelled=cancelled)
    if cancelled is not None and cancelled.is_set():
        raise _PerceptionCliError("cancelled", "validate_runtime", exit_code=130)
    return _ValidatedRuntimes(media, perception, original)


def _runtime_result() -> dict[str, object]:
    return _schema(
        "runtime-validation",
        category=_CATEGORY,
        confidence_floor_millionths=DETECTOR_CONFIDENCE_FLOOR_MILLIONTHS,
        device=_DEVICE,
        media={
            "manifest_sha256": _detection.MEDIA_MANIFEST_SHA256,
            "runtime_id": _detection.MEDIA_RUNTIME_ID,
            "tree_sha256": _detection.MEDIA_TREE_SHA256,
        },
        model={
            "bin_sha256": DETECTOR_MODEL_BIN_SHA256,
            "name": _MODEL,
            "xml_sha256": DETECTOR_MODEL_XML_SHA256,
        },
        original_frame={
            "manifest_sha256": _original._APPROVED_OVERLAY_MANIFEST_SHA256,
            "runtime_id": "visualworld-original-frame-overlay-v1",
            "worker_sha256": _original._APPROVED_OVERLAY_WORKER_SHA256,
        },
        perception={
            "configuration_sha256": DETECTOR_CONFIGURATION_SHA256,
            "manifest_sha256": DETECTOR_MANIFEST_SHA256,
            "runtime_closure_sha256": DETECTOR_RUNTIME_CLOSURE_SHA256,
            "runtime_id": DETECTOR_RUNTIME_ID,
            "worker_sha256": DETECTOR_WORKER_SHA256,
        },
        status="ready",
    )


def _run_stage_summaries(values: tuple[PerceptionEvent, ...]) -> list[dict[str, object]]:
    summaries: dict[tuple[str, str], dict[str, object]] = {}
    for value in values:
        key = (value.stage.value, value.status.value)
        summary = summaries.setdefault(
            key,
            {
                "call_count": 0,
                "duration_ns": 0,
                "item_count": 0,
                "protocol_version": value.protocol_version,
                "stage": value.stage.value,
                "status": value.status.value,
            },
        )
        summary["call_count"] = cast(int, summary["call_count"]) + 1
        summary["duration_ns"] = cast(int, summary["duration_ns"]) + value.duration_ns
        summary["item_count"] = cast(int, summary["item_count"]) + value.item_count
    return [summaries[key] for key in sorted(summaries)]


def _coordinator_event(value: CoordinatorEvent) -> dict[str, object]:
    result: dict[str, object] = {
        "duration_ns": value.duration_ns,
        "item_count": value.item_count,
        "protocol_version": value.protocol_version,
        "stage": value.stage.value,
        "status": value.status.value,
    }
    if value.run_id is not None:
        result["run_id"] = value.run_id
    if value.deletion_id is not None:
        result["deletion_id"] = value.deletion_id
    return result


@contextmanager
def _cancel_on_interrupt(*, timeout_seconds: int | None = None) -> Iterator[_CancellationControl]:
    cancelled = threading.Event()
    timed_out = threading.Event()
    timer: threading.Timer | None = None
    if timeout_seconds is not None:
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise ValueError("timeout must be a positive integer")

        def expire() -> None:
            timed_out.set()
            cancelled.set()

        timer = threading.Timer(timeout_seconds, expire)
        timer.daemon = True
        timer.start()
    if threading.current_thread() is not threading.main_thread():
        try:
            yield _CancellationControl(cancelled, timed_out)
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
        return
    previous = signal.getsignal(signal.SIGINT)

    def cancel(_signum: int, _frame: FrameType | None) -> None:
        cancelled.set()

    signal.signal(signal.SIGINT, cancel)
    try:
        yield _CancellationControl(cancelled, timed_out)
    finally:
        signal.signal(signal.SIGINT, previous)
        if timer is not None:
            timer.cancel()
            timer.join()


def _execute_run(
    request: _RunRequest, cancelled: threading.Event
) -> tuple[PerceptionRunResult, LocalVideoSource]:
    runtimes = _validated_runtimes(request.runtimes, cancelled)
    # The runtime preflight must stay before the source, and the complete source
    # probe must stay before either store. LocalVideoSource caches this decode,
    # so the coordinator's own probes reuse the exact validated snapshot.
    video = LocalVideoSource(
        request.source_root,
        request.source_name,
        runtimes.media,
        cancelled=cancelled,
    )
    video.probe()
    evidence = LocalEvidenceStore(request.store)
    world = LocalWorldStore(request.store)
    detector = OpenVinoVehicleDetector(
        IsolatedPerceptionWorker(
            request.source_root,
            request.source_name,
            runtimes.perception,
            cancelled=cancelled,
        )
    )
    original_frames = IsolatedOriginalFrameReader(
        request.source_root,
        request.source_name,
        runtimes.original_frame,
        cancelled=cancelled,
    )
    result = PerceptionCoordinator(evidence, world).run_with_original_frames(
        video,
        PtsFrameSampler(),
        detector,
        GlobalLastBoxTracker(requested_category=_CATEGORY),
        original_frames,
        BestFrameEvidenceSelector(),
        PerceptionConfig(stream_index=request.stream_index),
        EvidenceMaterializationConfig(),
        cancelled=cancelled,
    )
    return result, video


def _run_payload(result: PerceptionRunResult, video: LocalVideoSource) -> dict[str, object]:
    PerceptionRunResult.__post_init__(result)
    payload = _schema(
        "run",
        category=_CATEGORY,
        device=_DEVICE,
        model={
            "configuration_sha256": DETECTOR_CONFIGURATION_SHA256,
            "name": _MODEL,
            "runtime_id": DETECTOR_RUNTIME_ID,
        },
        reason=result.reason,
        stages=_run_stage_summaries(result.events),
        state=result.state.value,
    )
    if result.state is not PerceptionResultState.COMPLETE:
        return payload
    if result.manifest is None or result.disposition is None:
        raise _PerceptionCliError("corrupt", "run")
    metrics = video.metrics
    payload.update(
        {
            "counts": {
                "evidence": len(result.evidence),
                "observations": len(result.observations),
                "samples": len(result.frames),
                "tracklets": len(result.tracklets),
            },
            "disposition": result.disposition.value,
            "producers": [producer.to_mapping() for producer in result.manifest.producers],
            "resources": {
                "decode_cpu_ms": metrics.cpu_ms,
                "decode_frame_count": metrics.frame_count,
                "decode_memory_peak_bytes": metrics.memory_peak_bytes,
                "decode_wall_ms": metrics.wall_ms,
                "source_bytes": metrics.source_bytes,
            },
            "run_id": result.manifest.run_id,
            "sample_end_pts": result.frames[-1].pts.to_mapping() if result.frames else None,
            "sample_start_pts": result.frames[0].pts.to_mapping() if result.frames else None,
            "source_id": result.manifest.source_id,
        }
    )
    return payload


def _run_validate_runtime(arguments: argparse.Namespace) -> _CommandResult:
    with _cancel_on_interrupt(timeout_seconds=PERCEPTION_RUN_TIMEOUT_SECONDS) as control:
        try:
            _validated_runtimes(_runtime_paths(arguments), control.cancelled)
        except PortError as error:
            if error.code is PortErrorCode.CANCELLED and control.timed_out.is_set():
                raise _PerceptionCliError(
                    "timeout", "validate_runtime", exit_code=124, retryable=True
                ) from None
            raise
        except _PerceptionCliError as error:
            if error.code == "cancelled" and control.timed_out.is_set():
                raise _PerceptionCliError(
                    "timeout", "validate_runtime", exit_code=124, retryable=True
                ) from None
            raise
        if control.timed_out.is_set():
            raise _PerceptionCliError("timeout", "validate_runtime", exit_code=124, retryable=True)
        if control.cancelled.is_set():
            raise _PerceptionCliError("cancelled", "validate_runtime", exit_code=130)
    return _CommandResult(_runtime_result())


def _run_perception(arguments: argparse.Namespace) -> _CommandResult:
    _configuration(arguments)
    request = _RunRequest(
        _absolute_argument(arguments, "store"),
        _absolute_argument(arguments, "source_root"),
        _relative_source(arguments),
        _runtime_paths(arguments),
        _integer(arguments, "stream_index", 0, 2**31 - 1),
    )
    with _cancel_on_interrupt(timeout_seconds=PERCEPTION_RUN_TIMEOUT_SECONDS) as control:
        try:
            result, video = _execute_run(request, control.cancelled)
        except PortError as error:
            if error.code is PortErrorCode.CANCELLED and control.timed_out.is_set():
                raise _PerceptionCliError("timeout", "run", exit_code=124, retryable=True) from None
            raise
        except _PerceptionCliError as error:
            if error.code == "cancelled" and control.timed_out.is_set():
                raise _PerceptionCliError("timeout", "run", exit_code=124, retryable=True) from None
            raise
        if control.timed_out.is_set():
            raise _PerceptionCliError("timeout", "run", exit_code=124, retryable=True)
        if control.cancelled.is_set():
            raise _PerceptionCliError("cancelled", "run", exit_code=130)
    payload = _run_payload(result, video)
    if result.state is PerceptionResultState.COMPLETE:
        return _CommandResult(payload)
    reason = result.reason or "perception_unavailable"
    if reason == "cancelled":
        return _CommandResult(payload, 130, "cancelled", "run")
    if reason == "timeout":
        return _CommandResult(payload, 124, "timeout", "run", True)
    if result.state is PerceptionResultState.UNSUPPORTED:
        return _CommandResult(payload, 3, "unsupported", "run")
    return _CommandResult(payload, 4, "unknown", "run")


def _optional(arguments: argparse.Namespace, name: str) -> str | None:
    value = getattr(arguments, name, None)
    if value is None:
        return None
    if type(value) is not str:
        raise _PerceptionCliError("invalid_request", name)
    return value


def _inspection_page(
    kind: str,
    run_id: str,
    items: list[dict[str, object]],
    next_cursor: dict[str, object] | None,
) -> _CommandResult:
    return _CommandResult(
        _schema(
            "inspection-page",
            items=items,
            kind=kind,
            next=next_cursor,
            run_id=run_id,
        )
    )


def _inspect_observations(
    world: LocalWorldStore,
    arguments: argparse.Namespace,
    run_id: str,
    limit: int,
) -> _CommandResult:
    after_pts = _optional(arguments, "after_pts_value")
    after_id = _optional(arguments, "after_observation_id")
    if (after_pts is None) != (after_id is None):
        raise _PerceptionCliError("invalid_request", "observation_cursor")
    if (
        any(
            _optional(arguments, name) is not None
            for name in ("after_start_pts_value", "after_tracklet_id", "tracklet_id")
        )
        or getattr(arguments, "after_rank", None) is not None
    ):
        raise _PerceptionCliError("invalid_request", "inspect")
    stream_index = _integer(arguments, "stream_index", 0, 2**31 - 1)
    records = world.list_run_observations(
        run_id,
        stream_index=stream_index,
        after_pts_value=after_pts,
        after_observation_id=after_id,
        limit=limit,
    )
    next_cursor: dict[str, object] | None = None
    if len(records) == limit:
        last = records[-1]
        following = world.list_run_observations(
            run_id,
            stream_index=stream_index,
            after_pts_value=last.pts.value,
            after_observation_id=last.observation_id,
            limit=1,
        )
        if following:
            next_cursor = {
                "after_observation_id": last.observation_id,
                "after_pts_value": last.pts.value,
            }
    return _inspection_page(
        "observations",
        run_id,
        [record.to_mapping() for record in records],
        next_cursor,
    )


def _inspect_tracklets(
    world: LocalWorldStore,
    arguments: argparse.Namespace,
    run_id: str,
    limit: int,
) -> _CommandResult:
    after_pts = _optional(arguments, "after_start_pts_value")
    after_id = _optional(arguments, "after_tracklet_id")
    if (after_pts is None) != (after_id is None):
        raise _PerceptionCliError("invalid_request", "tracklet_cursor")
    if (
        any(
            _optional(arguments, name) is not None
            for name in ("after_pts_value", "after_observation_id", "tracklet_id")
        )
        or getattr(arguments, "after_rank", None) is not None
    ):
        raise _PerceptionCliError("invalid_request", "inspect")
    stream_index = _integer(arguments, "stream_index", 0, 2**31 - 1)
    records = world.list_run_tracklets(
        run_id,
        stream_index=stream_index,
        after_start_pts_value=after_pts,
        after_tracklet_id=after_id,
        limit=limit,
    )
    next_cursor: dict[str, object] | None = None
    if len(records) == limit:
        last = records[-1]
        following = world.list_run_tracklets(
            run_id,
            stream_index=stream_index,
            after_start_pts_value=last.start_pts.value,
            after_tracklet_id=last.tracklet_id,
            limit=1,
        )
        if following:
            next_cursor = {
                "after_start_pts_value": last.start_pts.value,
                "after_tracklet_id": last.tracklet_id,
            }
    return _inspection_page(
        "tracklets",
        run_id,
        [record.to_mapping() for record in records],
        next_cursor,
    )


def _selection_mapping(value: PersistedEvidenceSelection) -> dict[str, object]:
    PersistedEvidenceSelection.__post_init__(value)
    return {
        "evidence": value.evidence.to_mapping() if value.evidence is not None else None,
        "intent": value.intent.to_mapping(),
        "run_id": value.run_id,
    }


def _inspect_evidence(
    world: LocalWorldStore,
    arguments: argparse.Namespace,
    run_id: str,
    limit: int,
) -> _CommandResult:
    tracklet_id = _argument(arguments, "tracklet_id")
    after_rank = getattr(arguments, "after_rank", None)
    if after_rank is not None and (type(after_rank) is not int or not 1 <= after_rank <= 8):
        raise _PerceptionCliError("invalid_request", "after_rank")
    if any(
        _optional(arguments, name) is not None
        for name in (
            "after_pts_value",
            "after_observation_id",
            "after_start_pts_value",
            "after_tracklet_id",
        )
    ):
        raise _PerceptionCliError("invalid_request", "inspect")
    records = world.list_selected_evidence(
        run_id,
        tracklet_id,
        after_rank=after_rank,
        limit=limit,
    )
    next_cursor: dict[str, object] | None = None
    if len(records) == limit:
        last = records[-1]
        following = world.list_selected_evidence(
            run_id,
            tracklet_id,
            after_rank=last.intent.rank,
            limit=1,
        )
        if following:
            next_cursor = {"after_rank": last.intent.rank}
    return _inspection_page(
        "evidence",
        run_id,
        [_selection_mapping(record) for record in records],
        next_cursor,
    )


def _run_inspect(arguments: argparse.Namespace) -> _CommandResult:
    root = _legacy._store_path(_argument(arguments, "store"), must_exist=True)
    run_id = _argument(arguments, "run_id")
    kind = _argument(arguments, "kind")
    limit = _integer(arguments, "limit", 1, MAX_INSPECTION_PAGE_ITEMS)
    handlers: dict[
        str,
        Callable[[LocalWorldStore, argparse.Namespace, str, int], _CommandResult],
    ] = {
        "evidence": _inspect_evidence,
        "observations": _inspect_observations,
        "tracklets": _inspect_tracklets,
    }
    handler = handlers.get(kind)
    if handler is None:
        raise _PerceptionCliError("invalid_request", "kind")
    # Validate conditional syntax before a read-only command opens or
    # initializes the store implementation.
    if kind == "evidence":
        _argument(arguments, "tracklet_id")
    elif _optional(arguments, "tracklet_id") is not None:
        raise _PerceptionCliError("invalid_request", "inspect")
    world = LocalWorldStore(root)
    return handler(world, arguments, run_id, limit)


def _destination(root: Path, arguments: argparse.Namespace) -> Path:
    destination = _legacy._output_path(_argument(arguments, "output"))
    try:
        if destination.resolve(strict=False).is_relative_to(root.resolve(strict=True)):
            raise _PerceptionCliError("invalid_request", "output")
    except OSError:
        raise _PerceptionCliError("operation_failed", "output") from None
    return destination


def _run_export_evidence(arguments: argparse.Namespace) -> _CommandResult:
    root = _legacy._store_path(_argument(arguments, "store"), must_exist=True)
    destination = _destination(root, arguments)
    run_id = _argument(arguments, "run_id")
    tracklet_id = _argument(arguments, "tracklet_id")
    rank = _integer(arguments, "rank", 1, 8)
    world = LocalWorldStore(root)
    selections = world.list_selected_evidence(
        run_id,
        tracklet_id,
        after_rank=None if rank == 1 else rank - 1,
        limit=1,
    )
    if len(selections) != 1 or selections[0].intent.rank != rank:
        raise _PerceptionCliError("not_found", "selected_evidence")
    selection = selections[0]
    reference = selection.evidence
    if reference is None or reference.geometry is None:
        raise _PerceptionCliError("not_found", "selected_evidence")
    record = world.get(reference.evidence_id)
    if type(record) is not EvidenceRef or record != reference:
        raise _PerceptionCliError("corrupt", "selected_evidence")
    content = LocalEvidenceStore(root).get(reference.artifact.sha256)
    x_min, y_min, x_max, y_max = reference.geometry.box_xyxy
    crop = Rgb24Crop(x_max - x_min, y_max - y_min, content)
    artifact = write_rgb24_crop(
        crop,
        artifact_root=destination.parent,
        relative_destination=destination.name,
    )
    if artifact != reference.artifact:
        raise _PerceptionCliError("corrupt", "selected_evidence")
    return _CommandResult(
        _schema(
            "evidence-export",
            evidence=reference.to_mapping(),
            export={
                "bytes": int(artifact.bytes),
                "height": crop.height,
                "media_type": artifact.media_type,
                "sha256": artifact.sha256,
                "width": crop.width,
            },
            intent=selection.intent.to_mapping(),
            run_id=run_id,
        )
    )


def _run_recover(arguments: argparse.Namespace) -> _CommandResult:
    root = _legacy._store_path(_argument(arguments, "store"), must_exist=True)
    report = IngestionCoordinator(LocalEvidenceStore(root), LocalWorldStore(root)).recover()
    return _CommandResult(
        _schema(
            "recovery",
            deletions_completed=report.deletions_completed,
            events=[_coordinator_event(event) for event in report.events],
            integrity_issues=report.integrity_issues,
            runs_cleaned=report.runs_cleaned,
            staging_entries_removed=report.staging_entries_removed,
        )
    )


def _deletion_mapping(value: DeletionStatus) -> dict[str, object]:
    DeletionStatus.__post_init__(value)
    return {
        "artifact_count": value.artifact_count,
        "completed_at_utc": value.completed_at_utc,
        "deletion_id": value.deletion_id,
        "protocol_version": value.protocol_version,
        "record_count": value.record_count,
        "shared_retention_count": value.shared_retention_count,
        "state": value.state.value,
    }


def _run_delete_source(arguments: argparse.Namespace) -> _CommandResult:
    root = _legacy._store_path(_argument(arguments, "store"), must_exist=True)
    deletion_id = _optional(arguments, "deletion_id")
    status = IngestionCoordinator(LocalEvidenceStore(root), LocalWorldStore(root)).delete_source(
        _argument(arguments, "source_id"),
        deletion_id=deletion_id,
    )
    return _CommandResult(_schema("source-deletion", deletion=_deletion_mapping(status)))


def _add_runtime_roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--media-runtime-root", required=True, metavar="ABSOLUTE_ROOT")
    parser.add_argument("--perception-runtime-root", required=True, metavar="ABSOLUTE_ROOT")
    parser.add_argument("--original-frame-overlay-root", required=True, metavar="ABSOLUTE_ROOT")


def _add_store(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", required=True, metavar="ABSOLUTE_STORE")


def build_parser() -> argparse.ArgumentParser:
    """Create the v0.2 namespace parser without touching runtime or store state."""

    parser = _legacy._SafeArgumentParser(
        prog="visualworld perception",
        description="VisualWorld bounded offline v0.2 perception CLI.",
        epilog="Commands emit canonical JSON; no command downloads runtime artifacts.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", title="commands")

    validate = commands.add_parser(
        "validate-runtime",
        help="validate the approved installed media, model, and frame runtimes",
        allow_abbrev=False,
    )
    _add_runtime_roots(validate)
    validate.set_defaults(handler=_run_validate_runtime)

    run = commands.add_parser(
        "run",
        help="run one authorized local-video perception job",
        allow_abbrev=False,
    )
    _add_store(run)
    run.add_argument("--source-root", required=True, metavar="ABSOLUTE_ROOT")
    run.add_argument("--source", required=True, metavar="RELATIVE_SOURCE")
    _add_runtime_roots(run)
    run.add_argument("--model", required=True, metavar="MODEL")
    run.add_argument("--device", required=True, metavar="DEVICE")
    run.add_argument("--category", required=True, metavar="CATEGORY")
    run.add_argument("--stream-index", type=int, default=0, metavar="INDEX")
    run.set_defaults(handler=_run_perception)

    inspect = commands.add_parser(
        "inspect",
        help="page committed observations, tracklets, or selected evidence",
        allow_abbrev=False,
    )
    _add_store(inspect)
    inspect.add_argument("--run-id", required=True, metavar="RUN_ID")
    inspect.add_argument("--kind", required=True, metavar="KIND")
    inspect.add_argument("--limit", type=int, default=32, metavar="COUNT")
    inspect.add_argument("--stream-index", type=int, default=0, metavar="INDEX")
    inspect.add_argument("--after-pts-value", metavar="PTS")
    inspect.add_argument("--after-observation-id", metavar="OBSERVATION_ID")
    inspect.add_argument("--after-start-pts-value", metavar="PTS")
    inspect.add_argument("--after-tracklet-id", metavar="TRACKLET_ID")
    inspect.add_argument("--tracklet-id", metavar="TRACKLET_ID")
    inspect.add_argument("--after-rank", type=int, metavar="RANK")
    inspect.set_defaults(handler=_run_inspect)

    export = commands.add_parser(
        "export-evidence",
        help="export one materialized run-owned selected RGB24 crop",
        allow_abbrev=False,
    )
    _add_store(export)
    export.add_argument("--run-id", required=True, metavar="RUN_ID")
    export.add_argument("--tracklet-id", required=True, metavar="TRACKLET_ID")
    export.add_argument("--rank", required=True, type=int, metavar="RANK")
    export.add_argument("--output", required=True, metavar="ABSOLUTE_FILE")
    export.set_defaults(handler=_run_export_evidence)

    recover = commands.add_parser(
        "recover",
        help="resume bounded interrupted local-store operations",
        allow_abbrev=False,
    )
    _add_store(recover)
    recover.set_defaults(handler=_run_recover)

    delete = commands.add_parser(
        "delete-source",
        help="delete one source and its run-owned perception graph and evidence",
        allow_abbrev=False,
    )
    _add_store(delete)
    delete.add_argument("--source-id", required=True, metavar="SOURCE_ID")
    delete.add_argument("--deletion-id", metavar="DELETION_ID")
    delete.set_defaults(handler=_run_delete_source)
    return parser


def _failure_result(
    code: str,
    operation: str,
    exit_code: int,
    *,
    retryable: bool = False,
    payload: dict[str, object] | None = None,
) -> _CommandResult:
    return _CommandResult(payload or {}, exit_code, code, operation, retryable)


def _port_failure(error: PortError) -> _CommandResult:
    exits = {
        PortErrorCode.CANCELLED: 130,
        PortErrorCode.TIMEOUT: 124,
        PortErrorCode.UNSUPPORTED: 3,
    }
    return _failure_result(
        error.code.value,
        error.operation,
        exits.get(error.code, 1),
        retryable=error.retryable,
    )


def _document(command: str, result: _CommandResult) -> dict[str, object]:
    if result.exit_code == 0:
        return _legacy._success(command, result.payload)
    document = _legacy._failure(
        command,
        result.error_code or "operation_failed",
        result.operation or command.replace("-", "_"),
        retryable=result.retryable,
    )
    if result.payload:
        document["result"] = result.payload
    return document


def _emit(command: str, result: _CommandResult) -> int:
    document = _document(command, result)
    encoded = _legacy.canonical_json(document) + "\n"
    if len(encoded.encode("ascii")) > MAX_PERCEPTION_CLI_OUTPUT_BYTES:
        result = _failure_result("limit_exceeded", "emit_output", 1)
        document = _document(command, result)
        encoded = _legacy.canonical_json(document) + "\n"
    stream: TextIO = sys.stdout if result.exit_code == 0 else sys.stderr
    try:
        _legacy._write_text(encoded, stream)
    except _legacy._OutputError:
        if result.exit_code != 0:
            return result.exit_code
        failure = _failure_result("operation_failed", "emit_output", 1)
        with suppress(_legacy._OutputError):
            _legacy._write_document(_document(command, failure), sys.stderr)
        return 1
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Run one bounded v0.2 namespace command with stable output discipline."""

    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
    except _legacy._OutputError:
        return _emit("perception.arguments", _failure_result("operation_failed", "emit_output", 1))
    except _legacy._UsageError:
        return _emit(
            "perception.arguments",
            _failure_result("usage_error", "parse_arguments", 2),
        )
    command_value = getattr(arguments, "command", None)
    if command_value is None:
        try:
            parser.print_help()
        except _legacy._OutputError:
            return _emit(
                "perception.arguments",
                _failure_result("operation_failed", "emit_output", 1),
            )
        return 0
    handler = getattr(arguments, "handler", None)
    if type(command_value) is not str or not callable(handler):
        return _emit(
            "perception.arguments",
            _failure_result("usage_error", "parse_arguments", 2),
        )
    command = f"perception.{command_value}"
    selected = cast(Callable[[argparse.Namespace], _CommandResult], handler)
    try:
        result = selected(arguments)
    except KeyboardInterrupt:
        result = _failure_result("cancelled", command_value.replace("-", "_"), 130)
    except _PerceptionCliError as error:
        result = _failure_result(
            error.code,
            error.operation,
            error.exit_code,
            retryable=error.retryable,
        )
    except _legacy._CliError as error:
        result = _failure_result(
            error.code,
            error.operation,
            1,
            retryable=error.retryable,
        )
    except CoordinatorError as error:
        result = _failure_result(
            error.code.value,
            error.stage.value,
            1,
            retryable=error.retryable,
        )
    except PortError as error:
        result = _port_failure(error)
    except (CropError, RecordValidationError, TypeError, ValueError):
        result = _failure_result("invalid_request", command_value.replace("-", "_"), 1)
    except Exception:
        result = _failure_result("operation_failed", command_value.replace("-", "_"), 1)
    return _emit(command, result)


__all__ = [
    "MAX_INSPECTION_PAGE_ITEMS",
    "MAX_PERCEPTION_CLI_OUTPUT_BYTES",
    "PERCEPTION_CLI_SCHEMA_VERSION",
    "PERCEPTION_RUN_TIMEOUT_SECONDS",
    "build_parser",
    "main",
]
