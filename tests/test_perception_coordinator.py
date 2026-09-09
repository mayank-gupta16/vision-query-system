# SPDX-License-Identifier: Apache-2.0
"""End-to-end contracts for the restart-safe v0.2 perception coordinator."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest

from visualworld.coordinator import (
    CommitBoundary,
    CoordinatorError,
    CoordinatorErrorCode,
    CoordinatorStage,
    EventStatus,
    FrameDiscontinuityResult,
    PerceptionConfig,
    PerceptionCoordinator,
    PerceptionDisposition,
    PerceptionEvent,
    PerceptionRunResult,
    new_deletion_id,
)
from visualworld.detection import (
    DetectionProvenance,
    FixturePerceptionWorker,
    OpenVinoVehicleDetector,
    PerceptionWorkerResult,
    WorkerDetection,
    WorkerFrame,
)
from visualworld.evidence import (
    BestFrameEvidenceSelector,
    EvidencePlanResult,
    dumps_evidence_intent,
)
from visualworld.ingestion import (
    Fingerprint,
    FrameRef,
    Geometry,
    MediaTime,
    Producer,
    Source,
    SourceStream,
    TimeBase,
    dumps_record,
)
from visualworld.perception import (
    FrameDiscontinuity,
    Observation,
    Tracklet,
    TrackPoint,
    dumps_perception_record,
)
from visualworld.ports import (
    CapabilityDescriptor,
    DetectionResult,
    Effect,
    PerceptionResultState,
    PortError,
    PortErrorCode,
    PortKind,
)
from visualworld.sampling import PtsFrameSampler, SamplingLimits
from visualworld.storage import LocalEvidenceStore
from visualworld.tracking import (
    GlobalLastBoxTracker,
    TrackingDiagnostics,
    TrackingLimits,
    TrackingPage,
)
from visualworld.world_store import LocalWorldStore

_T = TypeVar("_T")


class PagedVideo:
    def __init__(self, source: Source, frames: tuple[FrameRef, ...]) -> None:
        self.descriptor = CapabilityDescriptor(PortKind.VIDEO_SOURCE, "test-video", "1", True, True)
        self.source = source
        self.frames = frames
        self.limits: list[int] = []

    def probe(self) -> Source:
        return self.source

    def read_frames(
        self, *, stream_index: int, after_decode_index: str | None = None, limit: int = 64
    ) -> tuple[FrameRef, ...]:
        self.limits.append(limit)
        after = -1 if after_decode_index is None else int(after_decode_index)
        return tuple(
            frame
            for frame in self.frames
            if frame.stream_index == stream_index and int(frame.decode_index) > after
        )[:limit]


class DeterministicDetector:
    descriptor = CapabilityDescriptor(PortKind.DETECTOR, "test-detector", "1", True, True)
    producer = Producer("test-detector", "1", "ab" * 32)

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


class Discontinuities:
    descriptor = CapabilityDescriptor(PortKind.FRAME_DISCONTINUITY, "test-score", "1", True, True)
    producer = Producer("test-score", "1", "cd" * 32)

    def __init__(self, state: PerceptionResultState = PerceptionResultState.COMPLETE) -> None:
        self.state = state
        self.previous: list[FrameRef | None] = []

    def score(
        self, _source: Source, previous_selected: FrameRef | None, frames: tuple[FrameRef, ...]
    ) -> FrameDiscontinuityResult:
        self.previous.append(previous_selected)
        if self.state is not PerceptionResultState.COMPLETE:
            return FrameDiscontinuityResult(self.state, reason="scores_unavailable")
        return FrameDiscontinuityResult.complete(
            tuple(FrameDiscontinuity.from_frame(frame, 0) for frame in frames)
        )


class PatternDetector(DeterministicDetector):
    def __init__(self, detected_indexes: set[int]) -> None:
        self.detected_indexes = detected_indexes

    def detect(self, source: Source, frames: tuple[FrameRef, ...]) -> DetectionResult:
        selected = tuple(
            frame for frame in frames if int(frame.decode_index) in self.detected_indexes
        )
        return super().detect(source, selected)


class ScoredDiscontinuities(Discontinuities):
    def __init__(self, scores: dict[int, int]) -> None:
        super().__init__()
        self.scores = scores

    def score(
        self, _source: Source, previous_selected: FrameRef | None, frames: tuple[FrameRef, ...]
    ) -> FrameDiscontinuityResult:
        self.previous.append(previous_selected)
        return FrameDiscontinuityResult.complete(
            tuple(
                FrameDiscontinuity.from_frame(frame, self.scores.get(int(frame.decode_index), 0))
                for frame in frames
            )
        )


class OutcomeDetector(DeterministicDetector):
    def __init__(self, state: PerceptionResultState) -> None:
        self.state = state

    def detect(self, _source: Source, _frames: tuple[FrameRef, ...]) -> DetectionResult:
        return DetectionResult(self.state, reason="detection_unavailable")


class OutcomeTracker:
    def __init__(self, state: PerceptionResultState) -> None:
        concrete = GlobalLastBoxTracker()
        self.descriptor = concrete.descriptor
        self.producer = concrete.producer
        self.state = state

    def track_page(self, *_args: object, **_kwargs: object) -> TrackingPage:
        return TrackingPage(
            self.state,
            (),
            None,
            True,
            TrackingDiagnostics(0, 0, 0, 0, 0, 0, 0, 0),
            "tracking_unavailable",
        )


class OutcomeSelector:
    def __init__(self, state: PerceptionResultState) -> None:
        concrete = BestFrameEvidenceSelector()
        self.descriptor = concrete.descriptor
        self.producer = concrete.producer
        self.state = state

    def plan(self, *_args: object) -> EvidencePlanResult:
        return EvidencePlanResult(self.state, reason="selection_unavailable")


class RaisingDetector(DeterministicDetector):
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def detect(self, _source: Source, _frames: tuple[FrameRef, ...]) -> DetectionResult:
        raise self.error


class ReplayDetector:
    def __init__(self, concrete: OpenVinoVehicleDetector, result: DetectionResult) -> None:
        self.descriptor = concrete.descriptor
        self.producer = concrete.producer
        self.result = result

    def detect(self, _source: Source, _frames: tuple[FrameRef, ...]) -> DetectionResult:
        return self.result


class _AdapterTrigger:
    operation: str

    def after(self, value: _T) -> _T:
        raise NotImplementedError


class _CancellationTrigger(_AdapterTrigger):
    def __init__(self, event: threading.Event, mode: str, port: PortKind, operation: str) -> None:
        self.event = event
        self.mode = mode
        self.port = port
        self.operation = operation

    def after(self, value: _T) -> _T:
        if self.mode == "port":
            raise PortError(PortErrorCode.CANCELLED, self.port, self.operation)
        self.event.set()
        return value


class _ManualClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        return self.value


class _BudgetOrTimeoutTrigger(_AdapterTrigger):
    def __init__(
        self,
        mode: str,
        port: PortKind,
        operation: str,
        clock: _ManualClock,
    ) -> None:
        self.mode = mode
        self.port = port
        self.operation = operation
        self.clock = clock

    def after(self, value: _T) -> _T:
        if self.mode == "timeout":
            raise PortError(
                PortErrorCode.TIMEOUT,
                self.port,
                self.operation,
                retryable=True,
            )
        self.clock.value = 2
        return value


class CancellingVideo(PagedVideo):
    def __init__(
        self,
        source: Source,
        frames: tuple[FrameRef, ...],
        trigger: _AdapterTrigger,
    ) -> None:
        super().__init__(source, frames)
        self.trigger = trigger

    def probe(self) -> Source:
        source = super().probe()
        return self.trigger.after(source) if self.trigger.operation == "probe" else source

    def read_frames(
        self, *, stream_index: int, after_decode_index: str | None = None, limit: int = 64
    ) -> tuple[FrameRef, ...]:
        frames = super().read_frames(
            stream_index=stream_index,
            after_decode_index=after_decode_index,
            limit=limit,
        )
        if self.trigger.operation == "read_frames":
            return self.trigger.after(frames)
        return frames


class CancellingSampler:
    def __init__(self, trigger: _AdapterTrigger) -> None:
        self.inner = PtsFrameSampler()
        self.descriptor = self.inner.descriptor
        self.trigger = trigger

    def sample_page(self, *args: Any, **kwargs: Any) -> Any:
        return self.trigger.after(self.inner.sample_page(*args, **kwargs))


class CancellingDetector(DeterministicDetector):
    def __init__(self, trigger: _AdapterTrigger) -> None:
        self.trigger = trigger

    def detect(self, source: Source, frames: tuple[FrameRef, ...]) -> DetectionResult:
        return self.trigger.after(super().detect(source, frames))


class CancellingDiscontinuities(Discontinuities):
    def __init__(self, trigger: _AdapterTrigger) -> None:
        super().__init__()
        self.trigger = trigger

    def score(
        self, source: Source, previous_selected: FrameRef | None, frames: tuple[FrameRef, ...]
    ) -> FrameDiscontinuityResult:
        return self.trigger.after(super().score(source, previous_selected, frames))


class CancellingTracker:
    def __init__(self, trigger: _AdapterTrigger) -> None:
        self.inner = GlobalLastBoxTracker()
        self.descriptor = self.inner.descriptor
        self.producer = self.inner.producer
        self.trigger = trigger

    def track_page(self, *args: Any, **kwargs: Any) -> Any:
        return self.trigger.after(self.inner.track_page(*args, **kwargs))


class CancellingSelector:
    def __init__(self, trigger: _AdapterTrigger) -> None:
        self.inner = BestFrameEvidenceSelector()
        self.descriptor = self.inner.descriptor
        self.producer = self.inner.producer
        self.trigger = trigger

    def plan(self, *args: Any, **kwargs: Any) -> Any:
        return self.trigger.after(self.inner.plan(*args, **kwargs))


class WrongGeometryDetector(DeterministicDetector):
    def detect(self, source: Source, frames: tuple[FrameRef, ...]) -> DetectionResult:
        return DetectionResult(
            PerceptionResultState.COMPLETE,
            tuple(
                Observation.create(
                    source.source_id,
                    frame.frame_id,
                    frame.stream_index,
                    frame.pts,
                    Geometry(11, 10, (1, 1, 6, 6), "inferred"),
                    "vehicle",
                    900_000,
                    self.producer,
                )
                for frame in frames
            ),
        )


class ReorderedDiscontinuities(Discontinuities):
    def score(
        self, source: Source, previous_selected: FrameRef | None, frames: tuple[FrameRef, ...]
    ) -> FrameDiscontinuityResult:
        complete = super().score(source, previous_selected, frames)
        return replace(complete, discontinuities=tuple(reversed(complete.discontinuities)))


class WrongPointTracker:
    def __init__(self) -> None:
        self.inner = GlobalLastBoxTracker()
        self.descriptor = self.inner.descriptor
        self.producer = self.inner.producer

    def track_page(self, *args: Any, **kwargs: Any) -> TrackingPage:
        page = self.inner.track_page(*args, **kwargs)
        if not page.tracklets:
            return page
        original = page.tracklets[0]
        first = original.points[0]
        wrong_point = TrackPoint(
            first.observation_id,
            first.source_id,
            first.frame_id,
            first.stream_index,
            first.pts,
            Geometry(10, 10, (0, 0, 5, 5), "inferred"),
            first.category,
        )
        wrong = Tracklet.create(
            original.source_id,
            original.stream_index,
            original.category,
            (wrong_point, *original.points[1:]),
            original.termination_reason,
            original.producer,
        )
        return replace(page, tracklets=(wrong, *page.tracklets[1:]))


class WrongContextSelector:
    def __init__(self) -> None:
        self.inner = BestFrameEvidenceSelector()
        self.descriptor = self.inner.descriptor
        self.producer = self.inner.producer

    def plan(self, *args: Any, **kwargs: Any) -> EvidencePlanResult:
        plan = self.inner.plan(*args, **kwargs)
        wrong = replace(plan.intents[0], source_id="src_" + "f" * 64)
        return replace(plan, intents=(wrong, *plan.intents[1:]))


def _source_and_frames(
    label: bytes, count: int, *, width: int = 10, height: int = 10
) -> tuple[Source, tuple[FrameRef, ...]]:
    basis = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(hashlib.sha256(label).hexdigest(), str(count)),
        (SourceStream(0, width, height, 0, basis),),
    )
    frames = tuple(
        FrameRef.create(source.source_id, 0, str(index), MediaTime(str(index * 200), basis))
        for index in range(count)
    )
    return source, frames


def _run(
    root: Path,
    source: Source,
    frames: tuple[FrameRef, ...],
    *,
    detector: Any | None = None,
    tracker: Any | None = None,
    discontinuities: Any | None = None,
    selector: Any | None = None,
    video: PagedVideo | None = None,
    config: PerceptionConfig | None = None,
    cancelled: threading.Event | None = None,
    fault_hook: Callable[[CommitBoundary, str], None] | None = None,
    event_sink: Callable[[PerceptionEvent], None] | None = None,
    clock_ns: Callable[[], int] | None = None,
) -> tuple[PerceptionCoordinator, LocalWorldStore, Any]:
    world = LocalWorldStore(root)
    coordinator = PerceptionCoordinator(
        LocalEvidenceStore(root),
        world,
        event_sink=event_sink,
        **({} if clock_ns is None else {"clock_ns": clock_ns}),
    )
    selected_config = config or PerceptionConfig(
        page_candidates=min(64, max(2, len(frames))),
        max_pages=max(1, len(frames)),
        max_samples=max(1, len(frames)),
        max_candidates=max(1, len(frames)),
    )
    result = coordinator.run(
        video or PagedVideo(source, frames),
        PtsFrameSampler(),
        detector or DeterministicDetector(),
        tracker or GlobalLastBoxTracker(),
        discontinuities or Discontinuities(),
        selector or BestFrameEvidenceSelector(),
        selected_config,
        cancelled=cancelled,
        fault_hook=fault_hook,
    )
    return coordinator, world, result


def test_two_page_run_publishes_hidden_v2_graph_atomically(tmp_path: Path) -> None:
    basis = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(hashlib.sha256(b"source").hexdigest(), "6"),
        (SourceStream(0, 10, 10, 0, basis),),
    )
    frames = tuple(
        FrameRef.create(source.source_id, 0, str(index), MediaTime(str(index * 200), basis))
        for index in range(4)
    )
    coordinator = PerceptionCoordinator(
        LocalEvidenceStore(tmp_path / "store"),
        LocalWorldStore(tmp_path / "store"),
    )
    result = coordinator.run(
        PagedVideo(source, frames),
        PtsFrameSampler(limits=SamplingLimits(max_page_candidates=3)),
        DeterministicDetector(),
        GlobalLastBoxTracker(limits=TrackingLimits(max_page_frames=2)),
        Discontinuities(),
        BestFrameEvidenceSelector(),
        PerceptionConfig(page_candidates=2, max_pages=4, max_samples=4),
    )

    assert result.state is PerceptionResultState.COMPLETE
    assert result.manifest is not None
    assert result.frames == frames
    assert len(result.observations) == len(frames)
    assert len(result.tracklets) == 1
    assert result.tracklets[0].termination_reason == "source_end"
    assert result.intents
    replay = coordinator.run(
        PagedVideo(source, frames),
        PtsFrameSampler(limits=SamplingLimits(max_page_candidates=3)),
        DeterministicDetector(),
        GlobalLastBoxTracker(limits=TrackingLimits(max_page_frames=2)),
        Discontinuities(),
        BestFrameEvidenceSelector(),
        PerceptionConfig(page_candidates=2, max_pages=4, max_samples=4),
    )
    assert replay.disposition is PerceptionDisposition.ALREADY_COMMITTED
    assert replay.manifest == result.manifest
    assert replay.observations == result.observations
    assert replay.tracklets == result.tracklets
    assert replay.intents == result.intents
    assert {event.stage.value for event in result.events} >= {
        "probe",
        "sample",
        "detect",
        "discontinuity",
        "track",
        "select",
        "perception_metadata",
    }
    reopened = LocalWorldStore(tmp_path / "store")
    assert (
        reopened.list_run_observations(result.manifest.run_id, stream_index=0, limit=8)
        == result.observations
    )
    assert (
        reopened.list_run_tracklets(result.manifest.run_id, stream_index=0, limit=8)
        == result.tracklets
    )


def test_overlap_reserves_a_slot_and_exposes_previous_selected_frame(tmp_path: Path) -> None:
    basis = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(hashlib.sha256(b"long-source").hexdigest(), "65"),
        (SourceStream(0, 10, 10, 0, basis),),
    )
    frames = tuple(
        FrameRef.create(source.source_id, 0, str(index), MediaTime(str(index * 200), basis))
        for index in range(65)
    )
    video = PagedVideo(source, frames)
    scores = Discontinuities()

    coordinator = PerceptionCoordinator(
        LocalEvidenceStore(tmp_path / "store"), LocalWorldStore(tmp_path / "store")
    )
    with pytest.raises(CoordinatorError) as raised:
        coordinator.run(
            video,
            PtsFrameSampler(),
            DeterministicDetector(),
            GlobalLastBoxTracker(),
            scores,
            BestFrameEvidenceSelector(),
            PerceptionConfig(page_candidates=64, max_pages=2, max_samples=65, max_candidates=65),
        )

    assert raised.value.code is CoordinatorErrorCode.LIMIT_EXCEEDED
    assert scores.previous == [None, frames[63]]
    assert video.limits == [64, 63, 63]
    assert all(limit <= 64 for limit in video.limits)
    assert LocalWorldStore(tmp_path / "store").pending_runs(limit=8) == ()


def test_unknown_discontinuities_publish_no_graph(tmp_path: Path) -> None:
    basis = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(hashlib.sha256(b"unknown-source").hexdigest(), "2"),
        (SourceStream(0, 10, 10, 0, basis),),
    )
    frames = tuple(
        FrameRef.create(source.source_id, 0, str(index), MediaTime(str(index * 200), basis))
        for index in range(2)
    )
    world = LocalWorldStore(tmp_path / "store")
    result = PerceptionCoordinator(LocalEvidenceStore(tmp_path / "store"), world).run(
        PagedVideo(source, frames),
        PtsFrameSampler(),
        DeterministicDetector(),
        GlobalLastBoxTracker(),
        Discontinuities(PerceptionResultState.UNKNOWN),
        BestFrameEvidenceSelector(),
        PerceptionConfig(max_samples=2, max_candidates=2),
    )

    assert result.state is PerceptionResultState.UNKNOWN
    assert result.reason == "scores_unavailable"
    assert world.pending_runs(limit=8) == ()


def test_empty_source_publishes_a_valid_empty_run(tmp_path: Path) -> None:
    basis = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(hashlib.sha256(b"empty-source").hexdigest(), "0"),
        (SourceStream(0, 10, 10, 0, basis),),
    )
    result = PerceptionCoordinator(
        LocalEvidenceStore(tmp_path / "store"), LocalWorldStore(tmp_path / "store")
    ).run(
        PagedVideo(source, ()),
        PtsFrameSampler(),
        DeterministicDetector(),
        GlobalLastBoxTracker(),
        Discontinuities(),
        BestFrameEvidenceSelector(),
        PerceptionConfig(max_samples=1, max_candidates=1),
    )
    assert result.state is PerceptionResultState.COMPLETE
    assert result.frames == ()
    assert result.observations == ()
    assert result.tracklets == ()
    assert result.intents == ()


def test_empty_source_cannot_publish_control_records_past_zero_byte_budget(
    tmp_path: Path,
) -> None:
    source, _ = _source_and_frames(b"zero-byte-empty-source", 0)
    root = tmp_path / "store"
    world = LocalWorldStore(root)
    with pytest.raises(CoordinatorError) as raised:
        _run(
            root,
            source,
            (),
            config=PerceptionConfig(
                max_samples=1,
                max_candidates=1,
                max_metadata_bytes=0,
            ),
        )
    assert raised.value.code is CoordinatorErrorCode.LIMIT_EXCEEDED
    assert raised.value.stage is CoordinatorStage.PROBE
    assert world.pending_runs(limit=8) == ()
    with pytest.raises(PortError) as missing_source:
        world.get(source.source_id)
    assert missing_source.value.code is PortErrorCode.NOT_FOUND


def test_metadata_budget_includes_final_manifest_and_accepts_exact_boundary(
    tmp_path: Path,
) -> None:
    source, frames = _source_and_frames(b"exact-metadata-budget", 1)
    _, _, baseline = _run(tmp_path / "baseline", source, frames)
    assert baseline.manifest is not None
    exact_bytes = (
        len(dumps_record(source))
        + len(dumps_record(baseline.manifest))
        + sum(len(dumps_record(item)) for item in baseline.frames)
        + sum(len(dumps_perception_record(item)) for item in baseline.observations)
        + sum(len(dumps_perception_record(item)) for item in baseline.tracklets)
        + sum(len(dumps_evidence_intent(item)) for item in baseline.intents)
    )

    with pytest.raises(CoordinatorError) as below:
        _run(
            tmp_path / "below",
            source,
            frames,
            config=PerceptionConfig(
                max_samples=1,
                max_candidates=1,
                max_metadata_bytes=exact_bytes - 1,
            ),
        )
    assert below.value.code is CoordinatorErrorCode.LIMIT_EXCEEDED
    assert below.value.stage is CoordinatorStage.FINALIZE

    _, _, exact = _run(
        tmp_path / "exact",
        source,
        frames,
        config=PerceptionConfig(
            max_samples=1,
            max_candidates=1,
            max_metadata_bytes=exact_bytes,
        ),
    )
    assert exact.state is PerceptionResultState.COMPLETE


def test_paged_cut_and_miss_timeout_preserve_exact_trajectory_points(tmp_path: Path) -> None:
    cut_source, cut_frames = _source_and_frames(b"cut-source", 4)
    _, _, cut = _run(
        tmp_path / "cut",
        cut_source,
        cut_frames,
        discontinuities=ScoredDiscontinuities({2: 1_500}),
        config=PerceptionConfig(page_candidates=2, max_pages=4, max_samples=4, max_candidates=4),
    )
    assert [tracklet.termination_reason for tracklet in cut.tracklets] == ["cut", "source_end"]
    assert tuple(
        point.frame_id for tracklet in cut.tracklets for point in tracklet.points
    ) == tuple(frame.frame_id for frame in cut_frames)

    miss_source, miss_frames = _source_and_frames(b"miss-source", 8)
    _, _, miss = _run(
        tmp_path / "miss",
        miss_source,
        miss_frames,
        detector=PatternDetector({0}),
        config=PerceptionConfig(page_candidates=4, max_pages=4, max_samples=8, max_candidates=8),
    )
    assert len(miss.tracklets) == 1
    assert miss.tracklets[0].termination_reason == "miss_timeout"
    assert tuple(point.frame_id for point in miss.tracklets[0].points) == (miss_frames[0].frame_id,)


@pytest.mark.parametrize(
    "boundary",
    [
        CommitBoundary.PREPARED,
        CommitBoundary.FRAME_METADATA_RECORDED,
        CommitBoundary.OBSERVATIONS_RECORDED,
        CommitBoundary.TRACKLETS_AND_INTENTS_RECORDED,
        CommitBoundary.RUN_COMMITTED,
    ],
)
def test_every_perception_durable_boundary_replays_without_partial_visibility(
    tmp_path: Path, boundary: CommitBoundary
) -> None:
    source, frames = _source_and_frames(b"fault-source", 3)
    root = tmp_path / boundary.value
    run_ids: list[str] = []

    class SimulatedCrash(RuntimeError):
        pass

    def crash(selected: CommitBoundary, run_id: str) -> None:
        if selected is boundary:
            run_ids.append(run_id)
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        _run(root, source, frames, fault_hook=crash)
    assert len(run_ids) == 1
    world = LocalWorldStore(root)
    if boundary is not CommitBoundary.RUN_COMMITTED:
        with pytest.raises(PortError) as hidden:
            world.list_run_frames(run_ids[0], limit=8)
        assert hidden.value.code is PortErrorCode.NOT_FOUND

    _, _, replay = _run(root, source, frames)
    assert replay.state is PerceptionResultState.COMPLETE
    assert replay.manifest is not None and replay.manifest.run_id == run_ids[0]
    expected_disposition = (
        PerceptionDisposition.ALREADY_COMMITTED
        if boundary is CommitBoundary.RUN_COMMITTED
        else PerceptionDisposition.COMMITTED
    )
    assert replay.disposition is expected_disposition


@pytest.mark.parametrize(
    "boundary",
    [
        CommitBoundary.PREPARED,
        CommitBoundary.FRAME_METADATA_RECORDED,
        CommitBoundary.OBSERVATIONS_RECORDED,
        CommitBoundary.TRACKLETS_AND_INTENTS_RECORDED,
        CommitBoundary.RUN_COMMITTED,
    ],
)
def test_cancellation_at_each_durable_boundary_is_restart_safe(
    tmp_path: Path, boundary: CommitBoundary
) -> None:
    source, frames = _source_and_frames(b"cancel-boundary-source", 3)
    root = tmp_path / boundary.value
    cancelled = threading.Event()

    def cancel(selected: CommitBoundary, _run_id: str) -> None:
        if selected is boundary:
            cancelled.set()

    _, world, result = _run(root, source, frames, cancelled=cancelled, fault_hook=cancel)
    if boundary is CommitBoundary.RUN_COMMITTED:
        assert result.state is PerceptionResultState.COMPLETE
        assert result.manifest is not None
        assert world.get(result.manifest.run_id) == result.manifest
        return

    assert result.state is PerceptionResultState.UNKNOWN
    assert result.reason == "cancelled"
    assert result.events[-1].status is EventStatus.CANCELLED
    run_id = result.events[-1].run_id
    assert run_id is not None
    with pytest.raises(PortError) as hidden:
        world.list_run_frames(run_id, limit=8)
    assert hidden.value.code is PortErrorCode.NOT_FOUND
    cancelled.clear()
    _, _, replay = _run(root, source, frames)
    assert replay.state is PerceptionResultState.COMPLETE
    assert replay.manifest is not None and replay.manifest.run_id == run_id


def test_adapter_cancellation_timeout_and_hostile_diagnostics_are_redacted(tmp_path: Path) -> None:
    source, frames = _source_and_frames(b"adapter-failure-source", 2)
    cancellation = PortError(
        PortErrorCode.CANCELLED,
        PortKind.DETECTOR,
        "detect",
    )
    _, world, cancelled = _run(
        tmp_path / "cancelled", source, frames, detector=RaisingDetector(cancellation)
    )
    assert cancelled.state is PerceptionResultState.UNKNOWN
    assert cancelled.reason == "cancelled"
    assert cancelled.events[-1].status is EventStatus.CANCELLED
    assert world.pending_runs(limit=8) == ()

    captured: list[PerceptionEvent] = []
    hostile_detail = "/private/source.mov RGB24 sensitive-marker"
    with pytest.raises(CoordinatorError) as failed:
        _run(
            tmp_path / "failed",
            source,
            frames,
            detector=RaisingDetector(RuntimeError(hostile_detail)),
            event_sink=captured.append,
        )
    assert failed.value.code is CoordinatorErrorCode.OPERATION_FAILED
    assert failed.value.stage.value == "detect"
    assert hostile_detail not in str(failed.value)
    assert hostile_detail not in repr(captured)
    assert captured[-1].status is EventStatus.FAILED

    timeout = PortError(
        PortErrorCode.TIMEOUT,
        PortKind.DETECTOR,
        "detect",
        retryable=True,
    )
    with pytest.raises(CoordinatorError) as timed_out:
        _run(tmp_path / "timeout", source, frames, detector=RaisingDetector(timeout))
    assert timed_out.value.code is CoordinatorErrorCode.OPERATION_FAILED
    assert timed_out.value.retryable is True


@pytest.mark.parametrize(
    ("name", "port", "operation", "expected_stage"),
    [
        ("video", PortKind.VIDEO_SOURCE, "probe", "probe"),
        ("sampler", PortKind.FRAME_SAMPLER, "sample_page", "sample"),
        ("detector", PortKind.DETECTOR, "detect", "detect"),
        (
            "discontinuity",
            PortKind.FRAME_DISCONTINUITY,
            "score",
            "discontinuity",
        ),
        ("tracker", PortKind.TRACKER, "track_page", "track"),
        ("selector", PortKind.EVIDENCE_SELECTOR, "plan", "select"),
    ],
)
@pytest.mark.parametrize("mode", ["port", "flag"])
def test_cancellation_before_and_after_every_adapter_call_is_redacted(
    tmp_path: Path,
    name: str,
    port: PortKind,
    operation: str,
    expected_stage: str,
    mode: str,
) -> None:
    source, frames = _source_and_frames(b"all-adapter-cancellation", 2)
    cancelled = threading.Event()
    trigger = _CancellationTrigger(cancelled, mode, port, operation)
    adapters: dict[str, Any] = {}
    if name == "video":
        adapters["video"] = CancellingVideo(source, frames, trigger)
    elif name == "sampler":
        adapters["sampler"] = CancellingSampler(trigger)
    elif name == "detector":
        adapters["detector"] = CancellingDetector(trigger)
    elif name == "discontinuity":
        adapters["discontinuities"] = CancellingDiscontinuities(trigger)
    elif name == "tracker":
        adapters["tracker"] = CancellingTracker(trigger)
    else:
        adapters["selector"] = CancellingSelector(trigger)

    root = tmp_path / f"{name}-{mode}"
    world = LocalWorldStore(root)
    coordinator = PerceptionCoordinator(LocalEvidenceStore(root), world)
    result = coordinator.run(
        adapters.get("video", PagedVideo(source, frames)),
        adapters.get("sampler", PtsFrameSampler()),
        adapters.get("detector", DeterministicDetector()),
        adapters.get("tracker", GlobalLastBoxTracker()),
        adapters.get("discontinuities", Discontinuities()),
        adapters.get("selector", BestFrameEvidenceSelector()),
        PerceptionConfig(max_samples=2, max_candidates=2),
        cancelled=cancelled,
    )
    assert result.state is PerceptionResultState.UNKNOWN
    assert result.reason == "cancelled"
    assert result.events[-1].stage.value == expected_stage
    assert result.events[-1].status is EventStatus.CANCELLED
    assert world.pending_runs(limit=8) == ()


@pytest.mark.parametrize(
    ("name", "port", "operation", "expected_stage"),
    [
        ("video_probe", PortKind.VIDEO_SOURCE, "probe", CoordinatorStage.PROBE),
        ("video_read", PortKind.VIDEO_SOURCE, "read_frames", CoordinatorStage.PROBE),
        ("sampler", PortKind.FRAME_SAMPLER, "sample_page", CoordinatorStage.SAMPLE),
        ("detector", PortKind.DETECTOR, "detect", CoordinatorStage.DETECT),
        (
            "discontinuity",
            PortKind.FRAME_DISCONTINUITY,
            "score",
            CoordinatorStage.DISCONTINUITY,
        ),
        ("tracker", PortKind.TRACKER, "track_page", CoordinatorStage.TRACK),
        ("selector", PortKind.EVIDENCE_SELECTOR, "plan", CoordinatorStage.SELECT),
    ],
)
@pytest.mark.parametrize("mode", ["budget", "timeout"])
def test_every_adapter_call_honors_cooperative_budget_and_timeout_failures(
    tmp_path: Path,
    name: str,
    port: PortKind,
    operation: str,
    expected_stage: CoordinatorStage,
    mode: str,
) -> None:
    source, frames = _source_and_frames(b"adapter-boundary-" + name.encode(), 1)
    clock = _ManualClock()
    trigger = _BudgetOrTimeoutTrigger(mode, port, operation, clock)
    video: Any = PagedVideo(source, frames)
    sampler: Any = PtsFrameSampler()
    detector: Any = DeterministicDetector()
    tracker: Any = GlobalLastBoxTracker()
    discontinuities: Any = Discontinuities()
    selector: Any = BestFrameEvidenceSelector()
    if name.startswith("video_"):
        video = CancellingVideo(source, frames, trigger)
    elif name == "sampler":
        sampler = CancellingSampler(trigger)
    elif name == "detector":
        detector = CancellingDetector(trigger)
    elif name == "discontinuity":
        discontinuities = CancellingDiscontinuities(trigger)
    elif name == "tracker":
        tracker = CancellingTracker(trigger)
    else:
        selector = CancellingSelector(trigger)

    root = tmp_path / f"{name}-{mode}"
    world = LocalWorldStore(root)
    events: list[PerceptionEvent] = []
    coordinator = PerceptionCoordinator(
        LocalEvidenceStore(root),
        world,
        event_sink=events.append,
        clock_ns=clock,
    )
    with pytest.raises(CoordinatorError) as raised:
        coordinator.run(
            video,
            sampler,
            detector,
            tracker,
            discontinuities,
            selector,
            PerceptionConfig(
                max_samples=1,
                max_candidates=1,
                max_duration_ns=1,
            ),
        )
    assert raised.value.code is (
        CoordinatorErrorCode.OPERATION_FAILED
        if mode == "timeout"
        else CoordinatorErrorCode.LIMIT_EXCEEDED
    )
    assert raised.value.stage is expected_stage
    assert raised.value.retryable is (mode == "timeout")
    assert events[-1].stage is expected_stage
    assert events[-1].status is EventStatus.FAILED
    assert world.pending_runs(limit=8) == ()
    with pytest.raises(PortError) as missing_source:
        world.get(source.source_id)
    assert missing_source.value.code is PortErrorCode.NOT_FOUND


def test_already_cancelled_run_does_not_call_any_adapter(tmp_path: Path) -> None:
    source, frames = _source_and_frames(b"pre-cancelled", 2)
    event = threading.Event()
    event.set()
    video = PagedVideo(source, frames)
    _, world, result = _run(tmp_path / "store", source, frames, video=video, cancelled=event)
    assert result.state is PerceptionResultState.UNKNOWN
    assert result.events[-1].status is EventStatus.CANCELLED
    assert video.limits == []
    assert world.pending_runs(limit=8) == ()


def test_unknown_and_unsupported_adapter_results_publish_nothing(tmp_path: Path) -> None:
    source, frames = _source_and_frames(b"outcome-source", 2)
    cases = (
        (
            "detect",
            {"detector": OutcomeDetector(PerceptionResultState.UNKNOWN)},
            "detection_unavailable",
        ),
        (
            "discontinuity",
            {"discontinuities": Discontinuities(PerceptionResultState.UNSUPPORTED)},
            "scores_unavailable",
        ),
        (
            "track",
            {"tracker": OutcomeTracker(PerceptionResultState.UNKNOWN)},
            "tracking_unavailable",
        ),
        (
            "select",
            {"selector": OutcomeSelector(PerceptionResultState.UNSUPPORTED)},
            "selection_unavailable",
        ),
    )
    for name, adapters, reason in cases:
        _, world, result = _run(tmp_path / name, source, frames, **adapters)
        assert result.state is not PerceptionResultState.COMPLETE
        assert result.reason == reason
        assert world.pending_runs(limit=8) == ()


@pytest.mark.parametrize(
    ("name", "adapters", "stage"),
    [
        ("geometry", {"detector": WrongGeometryDetector()}, "detect"),
        (
            "discontinuity_order",
            {"discontinuities": ReorderedDiscontinuities()},
            "discontinuity",
        ),
        ("track_point", {"tracker": WrongPointTracker()}, "track"),
        ("intent_context", {"selector": WrongContextSelector()}, "select"),
    ],
)
def test_hostile_cross_adapter_outputs_are_rejected_before_persistence(
    tmp_path: Path, name: str, adapters: dict[str, Any], stage: str
) -> None:
    source, frames = _source_and_frames(b"hostile-output-" + name.encode(), 2)
    with pytest.raises(CoordinatorError) as raised:
        _run(tmp_path / name, source, frames, **adapters)
    assert raised.value.code is CoordinatorErrorCode.CONFLICT
    assert raised.value.stage.value == stage
    assert LocalWorldStore(tmp_path / name).pending_runs(limit=8) == ()


def test_changed_source_record_on_reprobe_and_concurrent_deletion_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, frames = _source_and_frames(b"mutable-source", 2)
    changed, _ = _source_and_frames(b"changed-source", 2)

    class MutatingVideo(PagedVideo):
        def __init__(self) -> None:
            super().__init__(source, frames)
            self.probes = 0

        def probe(self) -> Source:
            self.probes += 1
            return source if self.probes == 1 else changed

    with pytest.raises(CoordinatorError) as mutated:
        _run(tmp_path / "mutation", source, frames, video=MutatingVideo())
    assert mutated.value.code is CoordinatorErrorCode.CONFLICT
    assert LocalWorldStore(tmp_path / "mutation").pending_runs(limit=8) == ()

    root = tmp_path / "deletion"
    world = LocalWorldStore(root)
    coordinator = PerceptionCoordinator(LocalEvidenceStore(root), world)
    commit_for_run = world.commit_for_run
    deletion_started = False

    def delete_before_frames(
        run_id: str,
        records: tuple[FrameRef, ...],
        *,
        evidence_session: Any = None,
    ) -> None:
        nonlocal deletion_started
        if not deletion_started:
            deletion_started = True
            world.begin_source_deletion(
                source.source_id,
                new_deletion_id(),
                evidence_session=evidence_session,
            )
        commit_for_run(run_id, records, evidence_session=evidence_session)

    monkeypatch.setattr(world, "commit_for_run", delete_before_frames)

    with pytest.raises(CoordinatorError) as deleted:
        coordinator.run(
            PagedVideo(source, frames),
            PtsFrameSampler(),
            DeterministicDetector(),
            GlobalLastBoxTracker(),
            Discontinuities(),
            BestFrameEvidenceSelector(),
            PerceptionConfig(max_samples=2, max_candidates=2),
        )
    assert deleted.value.code is CoordinatorErrorCode.CONFLICT
    assert deleted.value.stage.value == "perception_metadata"


def test_declared_item_byte_time_and_aggregate_limits_fail_closed(tmp_path: Path) -> None:
    source, frames = _source_and_frames(b"bounded-source", 2)
    video = PagedVideo(source, frames)
    video.descriptor = replace(video.descriptor, max_batch_items=1)
    with pytest.raises(CoordinatorError) as items:
        _run(tmp_path / "items", source, frames, video=video)
    assert items.value.code is CoordinatorErrorCode.LIMIT_EXCEEDED

    byte_detector = DeterministicDetector()
    byte_detector.descriptor = replace(byte_detector.descriptor, max_payload_bytes=1)
    with pytest.raises(CoordinatorError) as payload:
        _run(tmp_path / "payload", source, frames, detector=byte_detector)
    assert payload.value.code is CoordinatorErrorCode.LIMIT_EXCEEDED

    with pytest.raises(CoordinatorError) as observations:
        _run(
            tmp_path / "observations",
            source,
            frames,
            config=PerceptionConfig(max_samples=2, max_candidates=2, max_observations=0),
        )
    assert observations.value.code is CoordinatorErrorCode.LIMIT_EXCEEDED

    with pytest.raises(CoordinatorError) as metadata:
        _run(
            tmp_path / "metadata",
            source,
            frames,
            config=PerceptionConfig(
                max_samples=2,
                max_candidates=2,
                max_metadata_bytes=0,
            ),
        )
    assert metadata.value.code is CoordinatorErrorCode.LIMIT_EXCEEDED

    clock_value = 0

    def slow_clock() -> int:
        nonlocal clock_value
        clock_value += 2
        return clock_value

    with pytest.raises(CoordinatorError) as duration:
        _run(
            tmp_path / "duration",
            source,
            frames,
            config=PerceptionConfig(
                max_samples=2,
                max_candidates=2,
                max_duration_ns=1,
            ),
            clock_ns=slow_clock,
        )
    assert duration.value.code is CoordinatorErrorCode.LIMIT_EXCEEDED


@pytest.mark.parametrize(
    ("name", "frame_count", "config", "stage"),
    [
        (
            "candidates",
            2,
            PerceptionConfig(page_candidates=2, max_candidates=1, max_samples=2),
            "probe",
        ),
        (
            "pages",
            3,
            PerceptionConfig(
                page_candidates=2,
                max_pages=1,
                max_candidates=3,
                max_samples=3,
            ),
            "probe",
        ),
        (
            "samples",
            2,
            PerceptionConfig(page_candidates=2, max_candidates=2, max_samples=1),
            "sample",
        ),
        (
            "tracklets",
            2,
            PerceptionConfig(max_candidates=2, max_samples=2, max_tracklets=0),
            "track",
        ),
        (
            "intents",
            2,
            PerceptionConfig(max_candidates=2, max_samples=2, max_intents=0),
            "select",
        ),
    ],
)
def test_each_aggregate_limit_reports_the_exact_bounded_stage(
    tmp_path: Path,
    name: str,
    frame_count: int,
    config: PerceptionConfig,
    stage: str,
) -> None:
    source, frames = _source_and_frames(b"aggregate-" + name.encode(), frame_count)
    with pytest.raises(CoordinatorError) as raised:
        _run(tmp_path / name, source, frames, config=config)
    assert raised.value.code is CoordinatorErrorCode.LIMIT_EXCEEDED
    assert raised.value.stage.value == stage
    assert LocalWorldStore(tmp_path / name).pending_runs(limit=8) == ()


def test_more_than_one_store_page_is_verified_without_truncation(tmp_path: Path) -> None:
    source, frames = _source_and_frames(b"seventy-source", 70)
    _, world, result = _run(
        tmp_path / "store",
        source,
        frames,
        discontinuities=ScoredDiscontinuities({35: 1_500}),
        config=PerceptionConfig(
            page_candidates=64,
            max_pages=2,
            max_samples=70,
            max_candidates=70,
            max_observations=70,
            max_tracklets=2,
            max_intents=6,
        ),
    )
    assert result.state is PerceptionResultState.COMPLETE
    assert len(result.observations) == 70
    assert [len(tracklet.points) for tracklet in result.tracklets] == [35, 35]
    assert result.manifest is not None
    first = world.list_run_observations(result.manifest.run_id, stream_index=0, limit=64)
    second = world.list_run_observations(
        result.manifest.run_id,
        stream_index=0,
        after_pts_value=first[-1].pts.value,
        after_observation_id=first[-1].observation_id,
        limit=64,
    )
    assert len(first) == 64 and len(second) == 6


def test_openvino_fixture_and_contract_substitute_persist_identical_graphs(tmp_path: Path) -> None:
    source, frames = _source_and_frames(b"openvino-source", 2, width=640, height=360)
    stream = source.streams[0]
    output = PerceptionWorkerResult(
        DetectionProvenance(source.fingerprint.digest, int(source.fingerprint.bytes)),
        stream.stream_index,
        stream.width,
        stream.height,
        stream.rotation_degrees,
        stream.time_base,
        tuple(
            WorkerFrame(
                frame.decode_index,
                frame.pts,
                frame.duration,
                bool(frame.key_frame),
                (WorkerDetection((250_000, 250_000, 500_000, 500_000), 975_000),),
            )
            for frame in frames
        ),
    )
    concrete = OpenVinoVehicleDetector(FixturePerceptionWorker(output))
    expected_detection = concrete.detect(source, frames)
    concrete = OpenVinoVehicleDetector(FixturePerceptionWorker(output))
    _, _, concrete_result = _run(tmp_path / "concrete", source, frames, detector=concrete)
    _, _, substitute_result = _run(
        tmp_path / "substitute",
        source,
        frames,
        detector=ReplayDetector(concrete, expected_detection),
    )
    assert concrete_result.manifest == substitute_result.manifest
    assert tuple(map(dumps_perception_record, concrete_result.observations)) == tuple(
        map(dumps_perception_record, substitute_result.observations)
    )
    assert tuple(map(dumps_perception_record, concrete_result.tracklets)) == tuple(
        map(dumps_perception_record, substitute_result.tracklets)
    )
    assert tuple(map(dumps_evidence_intent, concrete_result.intents)) == tuple(
        map(dumps_evidence_intent, substitute_result.intents)
    )


def test_perception_graph_matches_stable_canonical_golden(tmp_path: Path) -> None:
    source, frames = _source_and_frames(b"golden-source", 4)
    _, _, result = _run(
        tmp_path / "store",
        source,
        frames,
        config=PerceptionConfig(page_candidates=2, max_pages=4, max_samples=4, max_candidates=4),
    )
    assert result.manifest is not None
    actual = {
        "frames": [dumps_record(item).decode() for item in result.frames],
        "intents": [dumps_evidence_intent(item).decode() for item in result.intents],
        "manifest": dumps_record(result.manifest).decode(),
        "observations": [dumps_perception_record(item).decode() for item in result.observations],
        "tracklets": [dumps_perception_record(item).decode() for item in result.tracklets],
    }
    expected = json.loads(
        (Path(__file__).parent / "goldens" / "perception-coordinator-v1.json").read_text()
    )
    assert actual == expected


@pytest.mark.parametrize(
    "build",
    [
        lambda: FrameDiscontinuityResult(cast(Any, "complete")),
        lambda: FrameDiscontinuityResult(PerceptionResultState.COMPLETE, cast(Any, [])),
        lambda: FrameDiscontinuityResult(PerceptionResultState.COMPLETE, cast(Any, (object(),))),
        lambda: FrameDiscontinuityResult(PerceptionResultState.COMPLETE, reason="unexpected"),
        lambda: FrameDiscontinuityResult(PerceptionResultState.UNKNOWN),
        lambda: FrameDiscontinuityResult(PerceptionResultState.UNKNOWN, reason="Not Stable"),
    ],
)
def test_discontinuity_results_reject_ambiguous_or_unbounded_states(
    build: Callable[[], Any],
) -> None:
    with pytest.raises(ValueError):
        build()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("page_candidates", 1),
        ("max_pages", 0),
        ("max_samples", 0),
        ("max_candidates", 0),
        ("max_observations", -1),
        ("max_tracklets", -1),
        ("max_intents", -1),
        ("max_metadata_bytes", -1),
        ("max_duration_ns", 0),
        ("stream_index", -1),
        ("protocol_version", 2),
        ("max_pages", True),
    ],
)
def test_perception_configuration_rejects_every_unbounded_dimension(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        replace(PerceptionConfig(), **{field: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"stage": "probe"},
        {"status": "succeeded"},
        {"duration_ns": 0},
        {"item_count": -1},
        {"run_id": "run_private-path"},
        {"protocol_version": 2},
    ],
)
def test_perception_events_reject_unstable_or_unbounded_values(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(
            PerceptionEvent(CoordinatorStage.PROBE, EventStatus.SUCCEEDED, 1, 0),
            **changes,
        )


def test_perception_results_never_mix_incomplete_state_with_graph_outputs(
    tmp_path: Path,
) -> None:
    source, frames = _source_and_frames(b"result-invariant", 1)
    _, _, complete = _run(tmp_path / "store", source, frames)

    with pytest.raises(ValueError):
        PerceptionRunResult(cast(Any, "complete"))
    with pytest.raises(ValueError):
        PerceptionRunResult(
            PerceptionResultState.UNKNOWN,
            frames=cast(Any, [frames[0]]),
            reason="unknown",
        )
    with pytest.raises(ValueError):
        PerceptionRunResult(PerceptionResultState.COMPLETE)
    with pytest.raises(ValueError):
        replace(complete, disposition=None)
    with pytest.raises(ValueError):
        replace(complete, reason="unexpected")
    with pytest.raises(ValueError):
        PerceptionRunResult(
            PerceptionResultState.UNKNOWN,
            manifest=complete.manifest,
            reason="unknown",
        )
    with pytest.raises(ValueError):
        PerceptionRunResult(PerceptionResultState.UNKNOWN, reason="Not Stable")


def test_coordinator_constructor_and_run_arguments_fail_closed(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    with pytest.raises(ValueError):
        PerceptionCoordinator(LocalEvidenceStore(first), LocalWorldStore(second))
    with pytest.raises(ValueError):
        PerceptionCoordinator(
            LocalEvidenceStore(first),
            LocalWorldStore(first),
            event_sink=cast(Any, object()),
        )
    with pytest.raises(ValueError):
        PerceptionCoordinator(
            LocalEvidenceStore(first),
            LocalWorldStore(first),
            clock_ns=cast(Any, object()),
        )

    source, frames = _source_and_frames(b"invalid-run-argument", 1)
    coordinator = PerceptionCoordinator(LocalEvidenceStore(first), LocalWorldStore(first))
    adapters = (
        PagedVideo(source, frames),
        PtsFrameSampler(),
        DeterministicDetector(),
        GlobalLastBoxTracker(),
        Discontinuities(),
        BestFrameEvidenceSelector(),
    )
    with pytest.raises(CoordinatorError) as config_error:
        coordinator.run(*adapters, cast(Any, object()))
    assert config_error.value.code is CoordinatorErrorCode.INVALID_REQUEST
    with pytest.raises(CoordinatorError) as cancellation_error:
        coordinator.run(*adapters, PerceptionConfig(), cancelled=cast(Any, object()))
    assert cancellation_error.value.code is CoordinatorErrorCode.INVALID_REQUEST
    with pytest.raises(CoordinatorError) as hook_error:
        coordinator.run(*adapters, PerceptionConfig(), fault_hook=cast(Any, object()))
    assert hook_error.value.code is CoordinatorErrorCode.INVALID_REQUEST


@pytest.mark.parametrize(
    ("mutation", "stage"),
    [
        ("wrong_port", "probe"),
        ("nondeterministic", "probe"),
        ("online", "probe"),
        ("effect", "probe"),
        ("wrong_producer", "detect"),
        ("invalid_producer", "detect"),
        ("raising_descriptor", "probe"),
        ("raising_producer", "detect"),
    ],
)
def test_untrusted_adapter_identity_is_validated_before_calls(
    tmp_path: Path, mutation: str, stage: str
) -> None:
    source, frames = _source_and_frames(b"adapter-identity-" + mutation.encode(), 1)
    detector: Any = DeterministicDetector()
    if mutation == "wrong_port":
        detector.descriptor = replace(detector.descriptor, port=PortKind.TRACKER)
    elif mutation == "nondeterministic":
        detector.descriptor = replace(detector.descriptor, deterministic=False)
    elif mutation == "online":
        detector.descriptor = replace(detector.descriptor, offline=False)
    elif mutation == "effect":
        effect_descriptor = replace(detector.descriptor)
        object.__setattr__(effect_descriptor, "allowed_effects", (Effect.NETWORK,))
        detector.descriptor = effect_descriptor
    elif mutation == "wrong_producer":
        detector.producer = object()
    elif mutation == "invalid_producer":
        invalid_producer = replace(detector.producer)
        object.__setattr__(invalid_producer, "configuration_sha256", "invalid")
        detector.producer = invalid_producer
    elif mutation == "raising_descriptor":

        class RaisingDescriptor:
            producer = DeterministicDetector.producer

            @property
            def descriptor(self) -> CapabilityDescriptor:
                raise RuntimeError("private descriptor detail")

            def detect(self, source: Source, frames: tuple[FrameRef, ...]) -> DetectionResult:
                return DeterministicDetector().detect(source, frames)

        detector = RaisingDescriptor()
    else:

        class RaisingProducer:
            descriptor = DeterministicDetector.descriptor

            @property
            def producer(self) -> Producer:
                raise RuntimeError("private producer detail")

            def detect(self, source: Source, frames: tuple[FrameRef, ...]) -> DetectionResult:
                return DeterministicDetector().detect(source, frames)

        detector = RaisingProducer()

    with pytest.raises(CoordinatorError) as raised:
        _run(tmp_path / mutation, source, frames, detector=detector)
    assert raised.value.code is CoordinatorErrorCode.INVALID_REQUEST
    assert raised.value.stage.value == stage


@pytest.mark.parametrize(
    ("clock", "code"),
    [
        (lambda: "1", CoordinatorErrorCode.INVALID_REQUEST),
        (lambda: -1, CoordinatorErrorCode.INVALID_REQUEST),
        (
            lambda: (_ for _ in ()).throw(RuntimeError("private clock detail")),
            CoordinatorErrorCode.OPERATION_FAILED,
        ),
    ],
)
def test_untrusted_clock_values_are_redacted(
    tmp_path: Path, clock: Callable[[], Any], code: CoordinatorErrorCode
) -> None:
    source, frames = _source_and_frames(b"bad-clock", 1)
    with pytest.raises(CoordinatorError) as raised:
        _run(tmp_path / code.value, source, frames, clock_ns=clock)
    assert raised.value.code is code
    assert "private" not in str(raised.value)


@pytest.mark.parametrize(
    ("port_code", "expected"),
    [
        (PortErrorCode.LIMIT_EXCEEDED, CoordinatorErrorCode.LIMIT_EXCEEDED),
        (PortErrorCode.INVALID_REQUEST, CoordinatorErrorCode.INVALID_REQUEST),
        (PortErrorCode.NOT_FOUND, CoordinatorErrorCode.CONFLICT),
        (PortErrorCode.CONFLICT, CoordinatorErrorCode.CONFLICT),
        (PortErrorCode.CORRUPT, CoordinatorErrorCode.CORRUPT),
        (PortErrorCode.STORAGE_FAILED, CoordinatorErrorCode.OPERATION_FAILED),
    ],
)
def test_adapter_port_failures_map_to_stable_coordinator_errors(
    tmp_path: Path, port_code: PortErrorCode, expected: CoordinatorErrorCode
) -> None:
    source, frames = _source_and_frames(b"port-map-" + port_code.value.encode(), 1)
    error = PortError(port_code, PortKind.DETECTOR, "detect", retryable=True)
    with pytest.raises(CoordinatorError) as raised:
        _run(tmp_path / port_code.value, source, frames, detector=RaisingDetector(error))
    assert raised.value.code is expected
    assert raised.value.stage is CoordinatorStage.DETECT
    assert raised.value.retryable is True


def test_event_sink_failures_cannot_change_a_committed_run(tmp_path: Path) -> None:
    source, frames = _source_and_frames(b"event-sink-failure", 1)
    calls = 0

    def rejecting_sink(_event: PerceptionEvent) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("sink unavailable")

    _, _, result = _run(tmp_path / "store", source, frames, event_sink=rejecting_sink)
    assert result.state is PerceptionResultState.COMPLETE
    assert calls == len(result.events)


@pytest.mark.parametrize(
    ("stage", "bad_value"),
    [
        ("sample", object()),
        ("detect", object()),
        ("discontinuity", object()),
        ("track", object()),
        ("select", object()),
    ],
)
def test_wrong_adapter_result_types_are_rejected_before_persistence(
    tmp_path: Path, stage: str, bad_value: object
) -> None:
    source, frames = _source_and_frames(b"wrong-result-" + stage.encode(), 1)

    class WrongSampler:
        descriptor = PtsFrameSampler().descriptor

        def sample_page(self, *_args: Any, **_kwargs: Any) -> object:
            return bad_value

    class WrongDetector(DeterministicDetector):
        def detect(self, _source: Source, _frames: tuple[FrameRef, ...]) -> Any:
            return bad_value

    class WrongDiscontinuities(Discontinuities):
        def score(self, *_args: Any, **_kwargs: Any) -> Any:
            return bad_value

    class WrongTracker:
        descriptor = GlobalLastBoxTracker().descriptor
        producer = GlobalLastBoxTracker().producer

        def track_page(self, *_args: Any, **_kwargs: Any) -> object:
            return bad_value

    class WrongSelector:
        descriptor = BestFrameEvidenceSelector().descriptor
        producer = BestFrameEvidenceSelector().producer

        def plan(self, *_args: Any, **_kwargs: Any) -> object:
            return bad_value

    adapters: dict[str, Any] = {
        "detector": DeterministicDetector(),
        "tracker": GlobalLastBoxTracker(),
        "discontinuities": Discontinuities(),
        "selector": BestFrameEvidenceSelector(),
    }
    sampler: Any = PtsFrameSampler()
    if stage == "sample":
        sampler = WrongSampler()
    elif stage == "detect":
        adapters["detector"] = WrongDetector()
    elif stage == "discontinuity":
        adapters["discontinuities"] = WrongDiscontinuities()
    elif stage == "track":
        adapters["tracker"] = WrongTracker()
    else:
        adapters["selector"] = WrongSelector()

    world = LocalWorldStore(tmp_path / stage)
    coordinator = PerceptionCoordinator(LocalEvidenceStore(tmp_path / stage), world)
    with pytest.raises(CoordinatorError) as raised:
        coordinator.run(
            PagedVideo(source, frames),
            sampler,
            adapters["detector"],
            adapters["tracker"],
            adapters["discontinuities"],
            adapters["selector"],
            PerceptionConfig(max_samples=1, max_candidates=1),
        )
    assert raised.value.code is CoordinatorErrorCode.INVALID_REQUEST
    assert raised.value.stage.value == stage
    assert world.pending_runs(limit=8) == ()


@pytest.mark.parametrize(
    "stage",
    ["sample", "detect", "discontinuity", "track", "select"],
)
def test_mutated_adapter_result_records_are_revalidated_before_persistence(
    tmp_path: Path, stage: str
) -> None:
    source, frames = _source_and_frames(b"mutated-result-" + stage.encode(), 1)

    class MutatedSampler:
        def __init__(self) -> None:
            self.inner = PtsFrameSampler()
            self.descriptor = self.inner.descriptor

        def sample_page(self, *args: Any, **kwargs: Any) -> Any:
            result = self.inner.sample_page(*args, **kwargs)
            object.__setattr__(result, "frames", list(result.frames))
            return result

    class MutatedDetector(DeterministicDetector):
        def detect(self, source: Source, frames: tuple[FrameRef, ...]) -> DetectionResult:
            result = super().detect(source, frames)
            object.__setattr__(result, "observations", list(result.observations))
            return result

    class MutatedDiscontinuities(Discontinuities):
        def score(self, *args: Any, **kwargs: Any) -> FrameDiscontinuityResult:
            result = super().score(*args, **kwargs)
            object.__setattr__(result, "discontinuities", list(result.discontinuities))
            return result

    class MutatedTracker:
        def __init__(self) -> None:
            self.inner = GlobalLastBoxTracker()
            self.descriptor = self.inner.descriptor
            self.producer = self.inner.producer

        def track_page(self, *args: Any, **kwargs: Any) -> TrackingPage:
            result = self.inner.track_page(*args, **kwargs)
            object.__setattr__(result, "tracklets", list(result.tracklets))
            return result

    class MutatedSelector:
        def __init__(self) -> None:
            self.inner = BestFrameEvidenceSelector()
            self.descriptor = self.inner.descriptor
            self.producer = self.inner.producer

        def plan(self, *args: Any, **kwargs: Any) -> EvidencePlanResult:
            result = self.inner.plan(*args, **kwargs)
            object.__setattr__(result, "intents", list(result.intents))
            return result

    adapters: dict[str, Any] = {
        "sampler": PtsFrameSampler(),
        "detector": DeterministicDetector(),
        "tracker": GlobalLastBoxTracker(),
        "discontinuities": Discontinuities(),
        "selector": BestFrameEvidenceSelector(),
    }
    adapter_key = {
        "sample": "sampler",
        "detect": "detector",
        "discontinuity": "discontinuities",
        "track": "tracker",
        "select": "selector",
    }[stage]
    adapters[adapter_key] = {
        "sample": MutatedSampler(),
        "detect": MutatedDetector(),
        "discontinuity": MutatedDiscontinuities(),
        "track": MutatedTracker(),
        "select": MutatedSelector(),
    }[stage]

    world = LocalWorldStore(tmp_path / stage)
    coordinator = PerceptionCoordinator(LocalEvidenceStore(tmp_path / stage), world)
    with pytest.raises(CoordinatorError) as raised:
        coordinator.run(
            PagedVideo(source, frames),
            adapters["sampler"],
            adapters["detector"],
            adapters["tracker"],
            adapters["discontinuities"],
            adapters["selector"],
            PerceptionConfig(max_samples=1, max_candidates=1),
        )
    assert raised.value.code is CoordinatorErrorCode.INVALID_REQUEST
    assert raised.value.stage.value == stage
    assert world.pending_runs(limit=8) == ()


def test_non_vehicle_observations_return_explicit_unsupported(tmp_path: Path) -> None:
    source, frames = _source_and_frames(b"unsupported-category", 1)

    class GenericDetector(DeterministicDetector):
        def detect(self, source: Source, frames: tuple[FrameRef, ...]) -> DetectionResult:
            vehicle = super().detect(source, frames).observations[0]
            return DetectionResult(
                PerceptionResultState.COMPLETE,
                (
                    Observation.create(
                        vehicle.source_id,
                        vehicle.frame_id,
                        vehicle.stream_index,
                        vehicle.pts,
                        vehicle.geometry,
                        "pedestrian",
                        vehicle.confidence_millionths,
                        vehicle.producer,
                    ),
                ),
            )

    _, world, result = _run(tmp_path / "store", source, frames, detector=GenericDetector())
    assert result.state is PerceptionResultState.UNSUPPORTED
    assert result.reason == "category_unsupported"
    assert world.pending_runs(limit=8) == ()


def test_complete_selector_result_requires_at_least_one_evidence_intent(tmp_path: Path) -> None:
    source, frames = _source_and_frames(b"empty-evidence-plan", 1)

    class EmptySelector:
        def __init__(self) -> None:
            concrete = BestFrameEvidenceSelector()
            self.descriptor = concrete.descriptor
            self.producer = concrete.producer

        def plan(self, *_args: Any) -> EvidencePlanResult:
            return EvidencePlanResult(PerceptionResultState.COMPLETE)

    with pytest.raises(CoordinatorError) as raised:
        _run(tmp_path / "store", source, frames, selector=EmptySelector())
    assert raised.value.code is CoordinatorErrorCode.CONFLICT
    assert raised.value.stage is CoordinatorStage.SELECT
    assert LocalWorldStore(tmp_path / "store").pending_runs(limit=8) == ()


def test_malformed_sources_and_frame_pages_are_rejected(tmp_path: Path) -> None:
    source, frames = _source_and_frames(b"malformed-source", 1)

    class WrongProbe(PagedVideo):
        def probe(self) -> Any:
            return object()

    with pytest.raises(CoordinatorError) as wrong_probe:
        _run(tmp_path / "probe", source, frames, video=WrongProbe(source, frames))
    assert wrong_probe.value.stage is CoordinatorStage.PROBE

    class WrongPage(PagedVideo):
        def read_frames(self, **_kwargs: Any) -> Any:
            return [frames[0]]

    with pytest.raises(CoordinatorError) as wrong_page:
        _run(tmp_path / "page", source, frames, video=WrongPage(source, frames))
    assert wrong_page.value.code is CoordinatorErrorCode.CONFLICT

    with pytest.raises(CoordinatorError) as missing_stream:
        _run(
            tmp_path / "stream",
            source,
            frames,
            config=PerceptionConfig(stream_index=1, max_samples=1, max_candidates=1),
        )
    assert missing_stream.value.code is CoordinatorErrorCode.CONFLICT


def test_post_commit_verification_detects_corrupt_store_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, frames = _source_and_frames(b"corrupt-read", 2)
    root = tmp_path / "store"
    world = LocalWorldStore(root)
    coordinator = PerceptionCoordinator(LocalEvidenceStore(root), world)
    original = world.list_run_frames
    finalized = False
    original_finalize = world.finalize_run

    def finalize(*args: Any, **kwargs: Any) -> None:
        nonlocal finalized
        original_finalize(*args, **kwargs)
        finalized = True

    def corrupt_frames(*args: Any, **kwargs: Any) -> tuple[FrameRef, ...]:
        page = original(*args, **kwargs)
        return page[:-1] if finalized else page

    monkeypatch.setattr(world, "finalize_run", finalize)
    monkeypatch.setattr(world, "list_run_frames", corrupt_frames)
    with pytest.raises(CoordinatorError) as raised:
        coordinator.run(
            PagedVideo(source, frames),
            PtsFrameSampler(),
            DeterministicDetector(),
            GlobalLastBoxTracker(),
            Discontinuities(),
            BestFrameEvidenceSelector(),
            PerceptionConfig(max_samples=2, max_candidates=2),
        )
    assert raised.value.code is CoordinatorErrorCode.CONFLICT
    assert raised.value.stage is CoordinatorStage.VERIFY


def test_post_commit_verification_maps_store_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, frames = _source_and_frames(b"verification-read-error", 1)
    root = tmp_path / "store"
    world = LocalWorldStore(root)
    coordinator = PerceptionCoordinator(LocalEvidenceStore(root), world)
    original_finalize = world.finalize_run
    finalized = False

    def finalize(*args: Any, **kwargs: Any) -> None:
        nonlocal finalized
        original_finalize(*args, **kwargs)
        finalized = True

    def fail_read(*_args: Any, **_kwargs: Any) -> tuple[FrameRef, ...]:
        if finalized:
            raise PortError(PortErrorCode.CORRUPT, PortKind.WORLD_STORE, "list_run_frames")
        return ()

    monkeypatch.setattr(world, "finalize_run", finalize)
    monkeypatch.setattr(world, "list_run_frames", fail_read)
    with pytest.raises(CoordinatorError) as raised:
        coordinator.run(
            PagedVideo(source, frames),
            PtsFrameSampler(),
            DeterministicDetector(),
            GlobalLastBoxTracker(),
            Discontinuities(),
            BestFrameEvidenceSelector(),
            PerceptionConfig(max_samples=1, max_candidates=1),
        )
    assert raised.value.code is CoordinatorErrorCode.CORRUPT
    assert raised.value.stage is CoordinatorStage.VERIFY


@pytest.mark.parametrize(
    "method_name",
    [
        "list_run_frames",
        "list_run_observations",
        "list_run_tracklets",
        "list_selected_evidence",
    ],
)
def test_post_commit_verification_enforces_bounds_on_every_store_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method_name: str
) -> None:
    source, frames = _source_and_frames(b"verification-bound-" + method_name.encode(), 1)
    root = tmp_path / method_name
    world = LocalWorldStore(root)
    coordinator = PerceptionCoordinator(LocalEvidenceStore(root), world)
    original = getattr(world, method_name)

    def overfull_page(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
        page = original(*args, **kwargs)
        return (*page, *page)

    monkeypatch.setattr(world, method_name, overfull_page)
    with pytest.raises(CoordinatorError) as raised:
        coordinator.run(
            PagedVideo(source, frames),
            PtsFrameSampler(),
            DeterministicDetector(),
            GlobalLastBoxTracker(),
            Discontinuities(),
            BestFrameEvidenceSelector(),
            PerceptionConfig(
                max_samples=1,
                max_candidates=1,
                max_observations=1,
                max_tracklets=1,
                max_intents=1,
            ),
        )
    assert raised.value.code is CoordinatorErrorCode.LIMIT_EXCEEDED
    assert raised.value.stage is CoordinatorStage.VERIFY


def test_frame_page_time_base_must_match_the_probed_stream(tmp_path: Path) -> None:
    source, _ = _source_and_frames(b"wrong-time-base", 1)
    wrong_frame = FrameRef.create(
        source.source_id,
        0,
        "0",
        MediaTime("0", TimeBase("1", "90000")),
    )

    class WrongTimeBaseVideo(PagedVideo):
        def read_frames(self, **_kwargs: Any) -> tuple[FrameRef, ...]:
            return (wrong_frame,)

    with pytest.raises(CoordinatorError) as raised:
        _run(
            tmp_path / "store",
            source,
            (wrong_frame,),
            video=WrongTimeBaseVideo(source, (wrong_frame,)),
        )
    assert raised.value.code is CoordinatorErrorCode.CONFLICT
    assert raised.value.stage is CoordinatorStage.PROBE


@pytest.mark.parametrize("bad_result", [1, RuntimeError("clock unavailable")])
def test_cancellation_probe_rejects_invalid_event_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_result: int | RuntimeError,
) -> None:
    source, frames = _source_and_frames(b"invalid-event", 1)
    cancelled = threading.Event()

    def invalid_is_set(_event: threading.Event) -> Any:
        if isinstance(bad_result, BaseException):
            raise bad_result
        return bad_result

    monkeypatch.setattr(threading.Event, "is_set", invalid_is_set)
    with pytest.raises(CoordinatorError) as raised:
        _run(tmp_path / "store", source, frames, cancelled=cancelled)
    assert raised.value.code is CoordinatorErrorCode.INVALID_REQUEST
    assert "clock unavailable" not in str(raised.value)
