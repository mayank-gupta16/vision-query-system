# SPDX-License-Identifier: Apache-2.0
"""Coordinator contracts for exact original-frame evidence publication."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from visualworld.coordinator import (
    CommitBoundary,
    CoordinatorError,
    CoordinatorErrorCode,
    CoordinatorStage,
    EvidenceMaterializationConfig,
    IngestionCoordinator,
    PerceptionConfig,
    PerceptionCoordinator,
    PerceptionDisposition,
    PerceptionRunResult,
)
from visualworld.evidence import (
    BestFrameEvidenceSelector,
    DetailResolution,
    EvidenceCropResult,
    EvidenceNeed,
)
from visualworld.frame_access import (
    FakeOriginalFrameReader,
    OriginalFrame,
    OriginalFrameReadResult,
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
)
from visualworld.perception import Observation, Tracklet, TrackPoint
from visualworld.perception_materialization import (
    EvidenceMaterializationResult,
    OriginalFrameDiscontinuityProvider,
    OriginalFrameMaterializer,
    materialization_producer,
)
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


class _PagedVideo:
    descriptor = CapabilityDescriptor(PortKind.VIDEO_SOURCE, "test-video", "1", True, True)

    def __init__(self, source: Source, frames: tuple[FrameRef, ...]) -> None:
        self.source = source
        self.frames = frames

    def probe(self) -> Source:
        return self.source

    def read_frames(
        self, *, stream_index: int, after_decode_index: str | None = None, limit: int = 64
    ) -> tuple[FrameRef, ...]:
        after = -1 if after_decode_index is None else int(after_decode_index)
        return tuple(
            frame
            for frame in self.frames
            if frame.stream_index == stream_index and int(frame.decode_index) > after
        )[:limit]


class _Detector:
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


def _fixture() -> tuple[Source, tuple[FrameRef, ...], tuple[OriginalFrame, ...]]:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(hashlib.sha256(b"materialized-source").hexdigest(), "4"),
        (SourceStream(0, 10, 10, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(source.source_id, 0, str(index), MediaTime(str(index * 200), time_base))
        for index in range(4)
    )
    pixels = (bytes(300), bytes(300), bytes([255]) * 300, bytes([255]) * 300)
    return (
        source,
        frames,
        tuple(
            OriginalFrame(source, frame, content)
            for frame, content in zip(frames, pixels, strict=True)
        ),
    )


def _run(
    root: Path,
    *,
    materialization: EvidenceMaterializationConfig | None = None,
    fault_hook: Callable[[CommitBoundary, str], None] | None = None,
    reader_state: PerceptionResultState = PerceptionResultState.COMPLETE,
    reader_reason: str | None = None,
) -> tuple[LocalEvidenceStore, LocalWorldStore, PerceptionRunResult]:
    source, frames, originals = _fixture()
    evidence = LocalEvidenceStore(root)
    world = LocalWorldStore(root)
    coordinator = PerceptionCoordinator(evidence, world)
    result = coordinator.run_with_original_frames(
        _PagedVideo(source, frames),
        PtsFrameSampler(),
        _Detector(),
        GlobalLastBoxTracker(),
        FakeOriginalFrameReader(
            source,
            originals if reader_state is PerceptionResultState.COMPLETE else (),
            state=reader_state,
            reason=reader_reason,
        ),
        BestFrameEvidenceSelector(),
        PerceptionConfig(
            page_candidates=2,
            max_pages=4,
            max_samples=4,
            max_candidates=4,
            max_observations=4,
            max_tracklets=2,
            max_intents=4,
        ),
        materialization or EvidenceMaterializationConfig(),
        fault_hook=fault_hook,
    )
    return evidence, world, result


def test_original_frames_score_across_pages_and_publish_exact_crops(tmp_path: Path) -> None:
    evidence_store, world, result = _run(tmp_path / "store")

    assert result.state is PerceptionResultState.COMPLETE
    assert result.disposition is PerceptionDisposition.COMMITTED
    assert result.manifest is not None
    assert [item.termination_reason for item in result.tracklets] == ["cut", "source_end"]
    assert len(result.evidence) == len(result.intents) == 4
    producer_names = {producer.name for producer in result.manifest.producers}
    assert "visualworld.perception-original-frame-reader" in producer_names
    assert "visualworld.fake.original-frame-reader" in producer_names
    assert "visualworld.perception-evidence-materialization" in producer_names
    black_crop = bytes(5 * 5 * 3)
    white_crop = bytes([255]) * (5 * 5 * 3)
    expected_crops = {
        hashlib.sha256(black_crop).hexdigest(): black_crop,
        hashlib.sha256(white_crop).hexdigest(): white_crop,
    }
    assert {item.artifact.sha256 for item in result.evidence} == set(expected_crops)
    for tracklet in result.tracklets:
        selections = world.list_selected_evidence(
            result.manifest.run_id, tracklet.tracklet_id, limit=8
        )
        assert all(selection.evidence is not None for selection in selections)
        for selection in selections:
            assert selection.evidence is not None
            assert selection.intent.source_id == result.manifest.source_id
            geometry = selection.evidence.geometry
            assert geometry is not None
            assert geometry.box_xyxy == (1, 1, 6, 6)
            assert (
                evidence_store.get(selection.evidence.artifact.sha256)
                == expected_crops[selection.evidence.artifact.sha256]
            )

    _, _, replay = _run(tmp_path / "store")
    assert replay.state is PerceptionResultState.COMPLETE
    assert replay.disposition is PerceptionDisposition.ALREADY_COMMITTED
    assert replay.evidence == result.evidence


def test_unresolved_materialization_publishes_nothing(tmp_path: Path) -> None:
    _, world, result = _run(
        tmp_path / "unknown",
        materialization=EvidenceMaterializationConfig(
            need=EvidenceNeed.DOWNSTREAM_DETAIL,
            detail_resolution=DetailResolution.UNKNOWN,
        ),
    )

    assert result.state is PerceptionResultState.UNKNOWN
    assert result.reason == "detail_resolution_unknown"
    assert result.evidence == ()
    assert world.pending_runs(limit=8) == ()
    stats = world.verify()
    assert stats.observation_count == stats.tracklet_count == stats.selection_count == 0


def test_materialized_source_deletion_removes_graph_and_cas(tmp_path: Path) -> None:
    evidence_store, world, result = _run(tmp_path / "delete")
    assert result.manifest is not None
    artifacts = tuple(item.artifact for item in result.evidence)

    deletion = IngestionCoordinator(evidence_store, world).delete_source(result.manifest.source_id)

    assert deletion.state.value == "complete"
    stats = world.verify()
    assert stats.observation_count == stats.tracklet_count == stats.selection_count == 0
    with pytest.raises(PortError) as missing_source:
        world.get(result.manifest.source_id)
    assert missing_source.value.code is PortErrorCode.NOT_FOUND
    for artifact in artifacts:
        with pytest.raises(PortError) as missing:
            evidence_store.get(artifact.sha256)
        assert missing.value.code is PortErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (PerceptionResultState.UNKNOWN, "source_pixels_unavailable"),
        (PerceptionResultState.UNSUPPORTED, "decoder_unsupported"),
    ],
)
def test_incomplete_original_frame_read_publishes_nothing(
    tmp_path: Path, state: PerceptionResultState, reason: str
) -> None:
    _, world, result = _run(
        tmp_path / state.value,
        reader_state=state,
        reader_reason=reason,
    )

    assert result.state is state
    assert result.reason == reason
    assert world.pending_runs(limit=8) == ()
    assert world.verify().selection_count == 0


def test_aggregate_crop_limit_fails_before_persistence(tmp_path: Path) -> None:
    with pytest.raises(CoordinatorError) as raised:
        _run(
            tmp_path / "bounded",
            materialization=EvidenceMaterializationConfig(max_total_evidence_bytes=299),
        )

    assert raised.value.code is CoordinatorErrorCode.LIMIT_EXCEEDED
    assert raised.value.stage is CoordinatorStage.CROP
    world = LocalWorldStore(tmp_path / "bounded")
    assert world.pending_runs(limit=8) == ()
    assert world.verify().selection_count == 0


def test_duplicate_evidence_references_are_rejected_before_persistence() -> None:
    source, frames, originals = _fixture()
    frame = frames[0]
    geometry = Geometry(10, 10, (1, 1, 6, 6), "inferred")
    detector = _Detector.producer
    observations = tuple(
        Observation.create(
            source.source_id,
            frame.frame_id,
            frame.stream_index,
            frame.pts,
            geometry,
            "vehicle",
            confidence,
            detector,
        )
        for confidence in (900_000, 800_000)
    )
    tracker = GlobalLastBoxTracker().producer
    tracklets = tuple(
        Tracklet.create(
            source.source_id,
            frame.stream_index,
            "vehicle",
            (TrackPoint.from_observation(observation),),
            "source_end",
            tracker,
        )
        for observation in observations
    )
    selector = BestFrameEvidenceSelector()
    intents = tuple(
        selector.plan(tracklet, (observation,)).intents[0]
        for tracklet, observation in zip(tracklets, observations, strict=True)
    )
    reader = FakeOriginalFrameReader(source, originals)
    materializer = OriginalFrameMaterializer(
        reader,
        reader.descriptor,
        selector,
        EvidenceMaterializationConfig(),
    )

    with pytest.raises(PortError) as raised:
        materializer.materialize(source, frames, observations, tracklets, intents)
    assert raised.value.code is PortErrorCode.CONFLICT


@pytest.mark.parametrize(
    "boundary",
    [
        CommitBoundary.STAGED,
        CommitBoundary.INTENTS_RECORDED,
        CommitBoundary.ARTIFACTS_PROMOTED,
        CommitBoundary.EVIDENCE_METADATA_RECORDED,
    ],
)
def test_materialized_publication_boundaries_replay_atomically(
    tmp_path: Path, boundary: CommitBoundary
) -> None:
    root = tmp_path / boundary.value
    run_ids: list[str] = []

    class _Crash(RuntimeError):
        pass

    def crash(selected: CommitBoundary, run_id: str) -> None:
        if selected is boundary:
            run_ids.append(run_id)
            raise _Crash

    with pytest.raises(_Crash):
        _run(root, fault_hook=crash)
    assert len(run_ids) == 1
    with pytest.raises(PortError) as hidden:
        LocalWorldStore(root).list_run_frames(run_ids[0], limit=8)
    assert hidden.value.code is PortErrorCode.NOT_FOUND

    _, _, replay = _run(root)
    assert replay.state is PerceptionResultState.COMPLETE
    assert replay.disposition is PerceptionDisposition.COMMITTED
    assert replay.manifest is not None and replay.manifest.run_id == run_ids[0]
    assert len(replay.evidence) == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"need": cast(Any, "inspection")},
        {"detail_resolution": cast(Any, "unknown")},
        {"max_total_original_frame_bytes": 2},
        {"max_total_evidence_bytes": -1},
        {"protocol_version": 2},
    ],
)
def test_materialization_configuration_rejects_non_contract_values(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="configuration"):
        replace(EvidenceMaterializationConfig(), **cast(Any, changes))
    with pytest.raises(ValueError, match="configuration"):
        materialization_producer(cast(Any, object()))


def test_materialization_result_rejects_invalid_state_payload_combinations() -> None:
    invalid: tuple[Callable[[], object], ...] = (
        lambda: EvidenceMaterializationResult(cast(Any, "complete")),
        lambda: EvidenceMaterializationResult(PerceptionResultState.COMPLETE, cast(Any, [])),
        lambda: EvidenceMaterializationResult(PerceptionResultState.COMPLETE, reason="unexpected"),
        lambda: EvidenceMaterializationResult(PerceptionResultState.UNKNOWN),
    )
    for build in invalid:
        with pytest.raises(ValueError):
            build()


def _single_case() -> tuple[
    Source,
    tuple[FrameRef, ...],
    tuple[OriginalFrame, ...],
    tuple[Observation, ...],
    tuple[Tracklet, ...],
    tuple[object, ...],
]:
    source, frames, originals = _fixture()
    frame = frames[0]
    observation = Observation.create(
        source.source_id,
        frame.frame_id,
        frame.stream_index,
        frame.pts,
        Geometry(10, 10, (1, 1, 6, 6), "inferred"),
        "vehicle",
        900_000,
        _Detector.producer,
    )
    tracklet = Tracklet.create(
        source.source_id,
        frame.stream_index,
        "vehicle",
        (TrackPoint.from_observation(observation),),
        "source_end",
        GlobalLastBoxTracker().producer,
    )
    selector = BestFrameEvidenceSelector()
    intent = selector.plan(tracklet, (observation,)).intents[0]
    return source, frames, originals, (observation,), (tracklet,), (intent,)


def test_discontinuity_adapter_validates_reader_results_and_bounds() -> None:
    source, frames, originals, _, _, _ = _single_case()
    reader = FakeOriginalFrameReader(source, originals)
    wrong_descriptor = CapabilityDescriptor(
        PortKind.DETECTOR, "wrong", "1", deterministic=True, offline=True
    )
    with pytest.raises(ValueError, match="discontinuity"):
        OriginalFrameDiscontinuityProvider(
            reader, wrong_descriptor, EvidenceMaterializationConfig()
        )

    provider = OriginalFrameDiscontinuityProvider(
        reader, reader.descriptor, EvidenceMaterializationConfig()
    )
    assert provider.score(source, None, ()).discontinuities == ()
    bounded_descriptor = CapabilityDescriptor(
        PortKind.ORIGINAL_FRAME_READER,
        "bounded",
        "1",
        deterministic=True,
        offline=True,
        max_batch_items=1,
    )
    bounded = OriginalFrameDiscontinuityProvider(
        reader, bounded_descriptor, EvidenceMaterializationConfig()
    )
    with pytest.raises(PortError) as too_many:
        bounded.score(source, frames[0], frames[1:2])
    assert too_many.value.code is PortErrorCode.LIMIT_EXCEEDED

    class Reader:
        def __init__(self, result: object) -> None:
            self.result = result

        def read(self, *_args: object, **_kwargs: object) -> object:
            return self.result

    invalid_result = object.__new__(OriginalFrameReadResult)
    object.__setattr__(invalid_result, "state", PerceptionResultState.COMPLETE)
    object.__setattr__(invalid_result, "frames", cast(Any, []))
    object.__setattr__(invalid_result, "reason", None)
    cases = (
        (object(), PortErrorCode.INVALID_REQUEST),
        (invalid_result, PortErrorCode.INVALID_REQUEST),
        (OriginalFrameReadResult.complete(originals[1:2]), PortErrorCode.CONFLICT),
    )
    for result, code in cases:
        invalid = OriginalFrameDiscontinuityProvider(
            cast(Any, Reader(result)), reader.descriptor, EvidenceMaterializationConfig()
        )
        with pytest.raises(PortError) as raised:
            invalid.score(source, None, frames[:1])
        assert raised.value.code is code


def test_materializer_rejects_incomplete_and_inconsistent_dependencies() -> None:
    source, frames, originals, observations, tracklets, raw_intents = _single_case()
    intent = cast(Any, raw_intents[0])
    reader = FakeOriginalFrameReader(source, originals)
    selector = BestFrameEvidenceSelector()
    with pytest.raises(ValueError, match="materializer"):
        OriginalFrameMaterializer(
            reader, reader.descriptor, object(), EvidenceMaterializationConfig()
        )

    unresolved = OriginalFrameMaterializer(
        reader,
        reader.descriptor,
        selector,
        EvidenceMaterializationConfig(detail_resolution=DetailResolution.UNRESOLVABLE),
    )
    result = unresolved.materialize(source, frames[:1], observations, tracklets, (intent,))
    assert result.state is PerceptionResultState.UNKNOWN
    assert result.reason == "detail_unresolvable"

    materializer = OriginalFrameMaterializer(
        reader, reader.descriptor, selector, EvidenceMaterializationConfig()
    )
    conflicts = (
        (frames[:1] * 2, observations, tracklets),
        ((), observations, tracklets),
        (frames[:1], (), tracklets),
    )
    for selected_frames, selected_observations, selected_tracklets in conflicts:
        with pytest.raises(PortError) as raised:
            materializer.materialize(
                source,
                selected_frames,
                selected_observations,
                selected_tracklets,
                (intent,),
            )
        assert raised.value.code is PortErrorCode.CONFLICT

    unavailable = FakeOriginalFrameReader(
        source,
        (),
        state=PerceptionResultState.UNSUPPORTED,
        reason="decoder_unsupported",
    )
    incomplete = OriginalFrameMaterializer(
        unavailable, unavailable.descriptor, selector, EvidenceMaterializationConfig()
    ).materialize(source, frames[:1], observations, tracklets, (intent,))
    assert incomplete.state is PerceptionResultState.UNSUPPORTED


def test_materializer_rejects_malformed_or_mismatched_selector_results() -> None:
    source, frames, originals, observations, tracklets, raw_intents = _single_case()
    intent = cast(Any, raw_intents[0])
    reader = FakeOriginalFrameReader(source, originals)

    class Selector:
        def __init__(self, result: object) -> None:
            self.result = result

        def materialize(self, *_args: object, **_kwargs: object) -> object:
            return self.result

    malformed = object.__new__(EvidenceCropResult)
    object.__setattr__(malformed, "state", PerceptionResultState.COMPLETE)
    object.__setattr__(malformed, "materialized", None)
    object.__setattr__(malformed, "reason", None)
    incomplete = EvidenceCropResult(
        PerceptionResultState.UNKNOWN, reason="detail_resolution_unknown"
    )
    valid = BestFrameEvidenceSelector().materialize(
        intent,
        tracklets[0],
        observations,
        originals[0].pixels,
        need=EvidenceNeed.INSPECTION,
        detail_resolution=DetailResolution.UNKNOWN,
    )
    assert valid.materialized is not None
    mismatched_intent = replace(intent, selector=Producer("alternate-selector", "1", "ab" * 32))
    cases = (
        (object(), intent, PortErrorCode.INVALID_REQUEST),
        (malformed, intent, PortErrorCode.INVALID_REQUEST),
        (incomplete, intent, None),
        (valid, mismatched_intent, PortErrorCode.CONFLICT),
    )
    for selected_result, selected_intent, code in cases:
        materializer = OriginalFrameMaterializer(
            reader,
            reader.descriptor,
            Selector(selected_result),
            EvidenceMaterializationConfig(),
        )
        if code is None:
            result = materializer.materialize(
                source, frames[:1], observations, tracklets, (selected_intent,)
            )
            assert result.state is PerceptionResultState.UNKNOWN
        else:
            with pytest.raises(PortError) as raised:
                materializer.materialize(
                    source, frames[:1], observations, tracklets, (selected_intent,)
                )
            assert raised.value.code is code
