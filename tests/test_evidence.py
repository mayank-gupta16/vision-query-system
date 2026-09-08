# SPDX-License-Identifier: Apache-2.0
"""Golden and boundary tests for deterministic best-frame evidence selection."""

from __future__ import annotations

import builtins
import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import replace
from typing import NoReturn, cast

import pytest

import visualworld.evidence as evidence_module
from visualworld.evidence import (
    DEFAULT_EVIDENCE_SELECTOR_CONFIGURATION_SHA256,
    DEFAULT_MAX_SELECTED_OBSERVATIONS,
    MAX_EVIDENCE_RGB24_FRAME_BYTES,
    MAX_SELECTED_OBSERVATIONS,
    BestFrameEvidenceSelector,
    DetailResolution,
    EvidenceCropResult,
    EvidenceIntent,
    EvidenceNeed,
    EvidencePlanResult,
    EvidenceScore,
    EvidenceSelectionLimits,
    MaterializedEvidence,
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
from visualworld.ports import (
    EvidenceSelector,
    FakeEvidenceSelector,
    FakeEvidenceStore,
    PerceptionResultState,
    PortCall,
    PortError,
    PortErrorCode,
    PortKind,
)


def _values(
    boxes: tuple[tuple[int, int, int, int], ...],
    confidences: tuple[int, ...],
    *,
    width: int = 100,
    height: int = 100,
    termination: str = "source_end",
    pts_values: tuple[int, ...] | None = None,
) -> tuple[Source, tuple[FrameRef, ...], tuple[Observation, ...], Tracklet]:
    assert len(boxes) == len(confidences)
    selected_pts = tuple(index * 200 for index in range(len(boxes)))
    if pts_values is not None:
        assert len(pts_values) == len(boxes)
        selected_pts = pts_values
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint("aa" * 32, "30000"),
        (SourceStream(0, width, height, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(source.source_id, 0, str(index), MediaTime(str(pts), time_base))
        for index, pts in enumerate(selected_pts)
    )
    detector = Producer("visualworld.test-detector", "1", "bb" * 32)
    observations = tuple(
        Observation.create(
            source.source_id,
            frame.frame_id,
            0,
            frame.pts,
            Geometry(width, height, box, "inferred"),
            "vehicle",
            confidence,
            detector,
        )
        for frame, box, confidence in zip(frames, boxes, confidences, strict=True)
    )
    tracklet = Tracklet.create(
        source.source_id,
        0,
        "vehicle",
        tuple(TrackPoint.from_observation(observation) for observation in observations),
        termination,
        Producer("visualworld.test-tracker", "1", "cc" * 32),
    )
    return source, frames, observations, tracklet


def _golden_values() -> tuple[Source, tuple[FrameRef, ...], tuple[Observation, ...], Tracklet]:
    return _values(
        (
            (0, 10, 90, 90),
            (10, 10, 70, 70),
            (10, 10, 20, 20),
            (10, 10, 70, 70),
            (10, 10, 80, 80),
        ),
        (1_000_000, 980_000, 980_000, 980_000, 970_000),
    )


def test_golden_order_preserves_integer_scores_and_tie_breaks() -> None:
    _, _, observations, tracklet = _golden_values()
    selector = BestFrameEvidenceSelector(
        limits=EvidenceSelectionLimits(max_selected_observations=5)
    )

    plan = selector.plan(tracklet, observations)

    assert plan.state is PerceptionResultState.COMPLETE
    assert plan.observation_ids == (
        "obs_a4d6ca3ca374fde862a6df9c29f23ee0c43340ce53fe4aae52ff11e70530a3f0",
        "obs_37c1e35d1f8574fc9116e07a82cbbb19d099547aebb856a61d39cffc9a4ec299",
        "obs_b26815932e0b172f15a8e2de1dd53ed3ad759ff4cfaf7dd8decef40345798493",
        "obs_ca151265bf96ff8604189e77564a19313000355236d7776748aaeaba5c8e20ef",
        "obs_c1efe40547f4bcaaf848e03a5172ab26d56bedf1b59d2739caeb2bf1b515bb3f",
    )
    assert (
        hashlib.sha256(
            json.dumps(plan.to_mapping(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        == "790903e36bf33229bf4846682099460b57d9f0faa1ed311fc22870c81493bf6d"
    )
    first, symmetric, tiny, large_lower_confidence, boundary = plan.intents
    assert first.rank == 1
    assert first.score.to_mapping() == {
        "components": {
            "boundary_touch_count": 0,
            "confidence_millionths": 980_000,
            "source_area_pixels": 10_000,
            "visible_area_millionths": 360_000,
            "visible_area_pixels": 3_600,
        },
        "tie_break": {
            "midpoint_distance_seconds_x2": {"denominator": 5, "numerator": 2},
            "observation_id": observations[1].observation_id,
            "point_count": 5,
            "point_index": 1,
            "pts": observations[1].pts.to_mapping(),
        },
    }
    assert symmetric.score.midpoint_distance_seconds_x2_numerator == 2
    assert symmetric.score.midpoint_distance_seconds_x2_denominator == 5
    assert symmetric.score.point_index == 3
    assert tiny.score.visible_area_pixels == 100
    assert large_lower_confidence.score.visible_area_pixels == 4_900
    assert boundary.score.boundary_touch_count == 1
    assert boundary.score.confidence_millionths == 1_000_000
    assert selector.calls == (PortCall(PortKind.EVIDENCE_SELECTOR, "plan", 5),)


def test_stable_port_is_bounded_pixel_free_and_protocol_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, observations, tracklet = _golden_values()
    selector = BestFrameEvidenceSelector()
    attempts: list[str] = []

    def denied(*_: object, **__: object) -> NoReturn:
        attempts.append("pixel-or-file-access")
        raise AssertionError("selection attempted an ambient or pixel operation")

    monkeypatch.setattr(evidence_module, "extract_rgb24_crop", denied)
    monkeypatch.setattr(builtins, "open", denied)

    selected = selector.select(tracklet, observations)

    assert isinstance(selector, EvidenceSelector)
    assert DEFAULT_MAX_SELECTED_OBSERVATIONS == 3
    assert DEFAULT_EVIDENCE_SELECTOR_CONFIGURATION_SHA256 == (
        "98cca604cf1880af26173fb2210eedb5b9a5734dd7970e9024e734d0531a1de5"
    )
    assert selected.observation_ids == tuple(
        observations[index].observation_id for index in (1, 3, 2)
    )
    assert selected.state is PerceptionResultState.COMPLETE
    assert selector.descriptor.deterministic
    assert selector.descriptor.offline
    assert selector.descriptor.allowed_effects == ()
    assert selector.descriptor.max_payload_bytes == MAX_EVIDENCE_RGB24_FRAME_BYTES
    assert selector.calls == (PortCall(PortKind.EVIDENCE_SELECTOR, "select", 5),)
    assert attempts == []


def test_repetition_and_adapter_substitution_preserve_selection_and_components() -> None:
    _, _, observations, tracklet = _golden_values()
    first = BestFrameEvidenceSelector(limits=EvidenceSelectionLimits(5))
    substitute = BestFrameEvidenceSelector(limits=EvidenceSelectionLimits(5))

    first_runs = tuple(first.plan(tracklet, tuple(reversed(observations))) for _ in range(3))
    substitute_plan = substitute.plan(tracklet, observations)
    concrete_result = substitute.select(tracklet, observations)
    fake_result = FakeEvidenceSelector(concrete_result).select(tracklet, observations)

    assert all(result.observation_ids == substitute_plan.observation_ids for result in first_runs)
    assert all(result.to_mapping() == substitute_plan.to_mapping() for result in first_runs)
    assert fake_result == concrete_result
    assert fake_result.observation_ids == substitute_plan.observation_ids
    assert first.producer == substitute.producer


def test_actual_rational_pts_not_frame_ordinal_breaks_temporal_ties() -> None:
    _, _, observations, tracklet = _values(
        ((10, 10, 20, 20),) * 4,
        (900_000,) * 4,
        pts_values=(0, 100, 200, 1_000),
    )

    plan = BestFrameEvidenceSelector(limits=EvidenceSelectionLimits(4)).plan(tracklet, observations)

    assert plan.observation_ids == tuple(
        observations[index].observation_id for index in (2, 1, 0, 3)
    )
    assert (
        plan.intents[0].score.midpoint_distance_seconds_x2_numerator,
        plan.intents[0].score.midpoint_distance_seconds_x2_denominator,
    ) == (3, 5)


@pytest.mark.parametrize("maximum", [1, 2, 3, MAX_SELECTED_OBSERVATIONS])
def test_configured_selection_count_is_strictly_bounded(maximum: int) -> None:
    boxes = tuple((10 + index, 10, 20 + index, 20) for index in range(12))
    _, _, observations, tracklet = _values(boxes, (900_000,) * len(boxes))

    result = BestFrameEvidenceSelector(limits=EvidenceSelectionLimits(maximum)).plan(
        tracklet, observations
    )

    assert len(result.intents) == maximum
    assert tuple(intent.rank for intent in result.intents) == tuple(range(1, maximum + 1))


@pytest.mark.parametrize("maximum", [0, 9, True, 1.0, "3"])
def test_invalid_selection_bounds_fail_closed(maximum: object) -> None:
    with pytest.raises(ValueError, match="supported bound"):
        EvidenceSelectionLimits(cast(int, maximum))


def test_caller_owned_limits_are_snapshotted() -> None:
    _, _, observations, tracklet = _golden_values()
    limits = EvidenceSelectionLimits(2)
    selector = BestFrameEvidenceSelector(limits=limits)
    original_producer = selector.producer
    object.__setattr__(limits, "max_selected_observations", MAX_SELECTED_OBSERVATIONS)

    result = selector.plan(tracklet, observations)

    assert len(result.intents) == 2
    assert selector.producer == original_producer


@pytest.mark.parametrize("termination", ["cut", "miss_timeout", "source_end"])
def test_one_point_and_every_completed_termination_are_supported(termination: str) -> None:
    _, _, observations, tracklet = _values(
        ((1, 1, 3, 3),),
        (950_000,),
        width=4,
        height=3,
        termination=termination,
    )

    result = BestFrameEvidenceSelector().plan(tracklet, observations)

    assert result.observation_ids == (observations[0].observation_id,)
    assert result.intents[0].score.midpoint_distance_seconds_x2_numerator == 0


def test_empty_or_incomplete_tracklet_cannot_be_selected() -> None:
    _, _, observations, tracklet = _golden_values()
    object.__setattr__(tracklet, "points", ())

    with pytest.raises(PortError) as raised:
        BestFrameEvidenceSelector().select(tracklet, observations)

    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert raised.value.__context__ is None


def test_missing_duplicate_and_mismatched_observations_fail_closed() -> None:
    _, _, observations, tracklet = _golden_values()
    selector = BestFrameEvidenceSelector()
    mismatched = Observation.create(
        observations[0].source_id,
        observations[0].frame_id,
        observations[0].stream_index,
        observations[0].pts,
        Geometry(100, 100, (1, 1, 2, 2), "inferred"),
        "vehicle",
        observations[0].confidence_millionths,
        observations[0].producer,
    )

    for supplied in (
        observations[:-1],
        (*observations[:-1], observations[0]),
        (mismatched, *observations[1:]),
    ):
        with pytest.raises(PortError) as raised:
            selector.plan(tracklet, supplied)
        assert raised.value.code is PortErrorCode.CONFLICT
        assert raised.value.__context__ is None


def test_explicit_original_crop_is_byte_exact_and_ready_for_existing_cas() -> None:
    _, _, observations, tracklet = _values(
        ((1, 1, 4, 3),),
        (950_000,),
        width=4,
        height=3,
    )
    selector = BestFrameEvidenceSelector()
    intent = selector.plan(tracklet, observations).intents[0]
    frame = bytes(range(4 * 3 * 3))
    expected = frame[15:24] + frame[27:36]

    result = selector.materialize(
        intent,
        tracklet,
        observations,
        frame,
        need=EvidenceNeed.INSPECTION,
        detail_resolution=DetailResolution.UNKNOWN,
    )

    assert result.state is PerceptionResultState.COMPLETE
    materialized = cast(MaterializedEvidence, result.materialized)
    assert materialized.crop.pixels == expected
    assert materialized.reference.frame_id == observations[0].frame_id
    assert materialized.reference.geometry == observations[0].geometry
    assert materialized.reference.artifact == materialized.crop.artifact()
    assert materialized.reference.retention == "derived_private"
    assert materialized.intent.deletion_owner == "coordinator_source_cascade"
    assert materialized.need is EvidenceNeed.INSPECTION
    assert materialized.detail_resolution is DetailResolution.UNKNOWN
    store = FakeEvidenceStore(max_payload_bytes=len(expected))
    assert store.put(materialized.reference.artifact, materialized.crop.pixels) == (
        materialized.reference.artifact
    )
    assert store.get(materialized.reference.artifact.sha256) == expected
    assert selector.calls == (
        PortCall(PortKind.EVIDENCE_SELECTOR, "plan", 1),
        PortCall(PortKind.EVIDENCE_SELECTOR, "materialize", 1),
    )


def test_unavailable_unresolvable_and_unassessed_detail_return_unknown_without_crop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, observations, tracklet = _values(
        ((1, 1, 4, 3),),
        (950_000,),
        width=4,
        height=3,
    )
    selector = BestFrameEvidenceSelector()
    intent = selector.plan(tracklet, observations).intents[0]
    attempts: list[str] = []

    def denied(*_: object, **__: object) -> NoReturn:
        attempts.append("crop")
        raise AssertionError("crop should not run")

    monkeypatch.setattr(evidence_module, "extract_rgb24_crop", denied)

    unavailable = selector.materialize(
        intent,
        tracklet,
        observations,
        None,
        need=EvidenceNeed.INSPECTION,
        detail_resolution=DetailResolution.UNKNOWN,
    )
    unresolvable = selector.materialize(
        intent,
        tracklet,
        observations,
        b"private pixels that must not be inspected",
        need=EvidenceNeed.DOWNSTREAM_DETAIL,
        detail_resolution=DetailResolution.UNRESOLVABLE,
    )
    unassessed = selector.materialize(
        intent,
        tracklet,
        observations,
        b"private pixels that must not be inspected",
        need=EvidenceNeed.DOWNSTREAM_DETAIL,
        detail_resolution=DetailResolution.UNKNOWN,
    )

    assert (unavailable.state, unavailable.reason) == (
        PerceptionResultState.UNKNOWN,
        "source_pixels_unavailable",
    )
    assert unavailable.to_mapping() == {
        "reason": "source_pixels_unavailable",
        "state": "unknown",
    }
    assert (unresolvable.state, unresolvable.reason) == (
        PerceptionResultState.UNKNOWN,
        "detail_unresolvable",
    )
    assert (unassessed.state, unassessed.reason) == (
        PerceptionResultState.UNKNOWN,
        "detail_resolution_unknown",
    )
    assert attempts == []


def test_pixels_and_sensitive_content_never_enter_repr_metadata_or_errors() -> None:
    _, _, observations, tracklet = _values(
        ((1, 1, 4, 3),),
        (950_000,),
        width=4,
        height=3,
    )
    selector = BestFrameEvidenceSelector()
    plan = selector.plan(tracklet, observations)
    marker = b"private/path\nplate=SECRET"
    frame = (marker * 2)[: 4 * 3 * 3]
    result = selector.materialize(
        plan.intents[0],
        tracklet,
        observations,
        frame,
        need=EvidenceNeed.INSPECTION,
        detail_resolution=DetailResolution.RESOLVABLE,
    )

    rendered = repr(result) + json.dumps(plan.to_mapping()) + json.dumps(result.to_mapping())
    assert marker.decode(errors="ignore") not in rendered
    assert frame.hex() not in rendered
    assert "crop=Rgb24Crop" in repr(result)
    assert "pixels=" not in repr(cast(MaterializedEvidence, result.materialized).crop)

    with pytest.raises(PortError) as raised:
        selector.materialize(
            plan.intents[0],
            tracklet,
            observations,
            marker,
            need=EvidenceNeed.INSPECTION,
            detail_resolution=DetailResolution.RESOLVABLE,
        )
    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert raised.value.__context__ is None
    assert marker.decode(errors="ignore") not in str(raised.value)


def test_foreign_or_mutated_intent_and_hostile_types_fail_closed() -> None:
    _, _, observations, tracklet = _golden_values()
    first = BestFrameEvidenceSelector(limits=EvidenceSelectionLimits(1))
    second = BestFrameEvidenceSelector(limits=EvidenceSelectionLimits(2))
    intent = first.plan(tracklet, observations).intents[0]

    with pytest.raises(PortError) as foreign:
        second.materialize(
            intent,
            tracklet,
            observations,
            None,
            need=EvidenceNeed.INSPECTION,
            detail_resolution=DetailResolution.UNKNOWN,
        )
    assert foreign.value.code is PortErrorCode.CONFLICT

    object.__setattr__(intent, "frame_id", "private/path\nsecret")
    with pytest.raises(PortError) as mutated:
        first.materialize(
            intent,
            tracklet,
            observations,
            None,
            need=EvidenceNeed.INSPECTION,
            detail_resolution=DetailResolution.UNKNOWN,
        )
    assert mutated.value.code is PortErrorCode.INVALID_REQUEST
    assert mutated.value.__context__ is None
    assert "private" not in str(mutated.value)

    with pytest.raises(PortError, match="limit_exceeded"):
        first.select(tracklet, cast(tuple[Observation, ...], list(observations)))
    with pytest.raises(PortError, match="invalid_request"):
        first.select(cast(Tracklet, object()), observations)
    with pytest.raises(PortError, match="invalid_request"):
        first.select(tracklet, (cast(Observation, object()),))
    with pytest.raises(ValueError, match="limits"):
        BestFrameEvidenceSelector(limits=cast(EvidenceSelectionLimits, object()))
    with pytest.raises(PortError, match="invalid_request"):
        first.materialize(
            cast(EvidenceIntent, object()),
            tracklet,
            observations,
            None,
            need=EvidenceNeed.INSPECTION,
            detail_resolution=DetailResolution.UNKNOWN,
        )
    with pytest.raises(PortError, match="invalid_request"):
        first.materialize(
            first.plan(tracklet, observations).intents[0],
            tracklet,
            observations,
            None,
            need=cast(EvidenceNeed, "inspection"),
            detail_resolution=DetailResolution.UNKNOWN,
        )
    with pytest.raises(PortError, match="invalid_request"):
        first.materialize(
            first.plan(tracklet, observations).intents[0],
            tracklet,
            observations,
            cast(bytes, bytearray(b"private pixels")),
            need=EvidenceNeed.INSPECTION,
            detail_resolution=DetailResolution.RESOLVABLE,
        )


def test_materialization_rejects_a_frame_beyond_the_declared_byte_bound() -> None:
    _, _, observations, tracklet = _values(
        ((1, 0, 2, 1),),
        (950_000,),
        width=MAX_EVIDENCE_RGB24_FRAME_BYTES,
        height=1,
    )
    selector = BestFrameEvidenceSelector()
    intent = selector.plan(tracklet, observations).intents[0]

    with pytest.raises(PortError) as raised:
        selector.materialize(
            intent,
            tracklet,
            observations,
            b"x",
            need=EvidenceNeed.INSPECTION,
            detail_resolution=DetailResolution.RESOLVABLE,
        )

    assert raised.value.code is PortErrorCode.LIMIT_EXCEEDED
    assert raised.value.__context__ is None


def test_public_evidence_values_reject_inconsistent_or_partial_states() -> None:
    _, _, observations, tracklet = _values(
        ((1, 1, 4, 3),),
        (950_000,),
        width=4,
        height=3,
    )
    plan = BestFrameEvidenceSelector().plan(tracklet, observations)
    intent = plan.intents[0]
    score = intent.score
    invalid: tuple[Callable[[], object], ...] = (
        lambda: EvidenceSelectionLimits(0),
        lambda: EvidenceScore(
            score.boundary_touch_count,
            score.confidence_millionths,
            score.visible_area_pixels,
            score.source_area_pixels,
            score.visible_area_millionths + 1,
            score.midpoint_distance_seconds_x2_numerator,
            score.midpoint_distance_seconds_x2_denominator,
            score.point_index,
            score.point_count,
            score.pts,
            score.observation_id,
        ),
        lambda: EvidencePlanResult(PerceptionResultState.COMPLETE, (intent,), "unexpected"),
        lambda: EvidencePlanResult(PerceptionResultState.UNKNOWN),
        lambda: EvidencePlanResult(
            PerceptionResultState.UNKNOWN,
            (intent,),
            "source_pixels_unavailable",
        ),
        lambda: EvidenceCropResult(PerceptionResultState.COMPLETE),
        lambda: EvidenceCropResult(
            PerceptionResultState.UNKNOWN,
            reason="private/path",
        ),
        lambda: EvidenceIntent(
            2,
            intent.tracklet_id,
            intent.observation_id,
            intent.source_id,
            intent.frame_id,
            intent.stream_index,
            intent.pts,
            intent.geometry,
            intent.score,
            intent.selector,
            deletion_owner="artifact_store",
        ),
    )
    for construct in invalid:
        with pytest.raises(ValueError):
            construct()


def test_score_and_intent_values_reject_hostile_nested_fields() -> None:
    _, _, observations, tracklet = _values(
        ((1, 1, 4, 3),),
        (950_000,),
        width=4,
        height=3,
    )
    intent = BestFrameEvidenceSelector().plan(tracklet, observations).intents[0]
    score = intent.score
    invalid_scores: tuple[Callable[[], object], ...] = (
        lambda: replace(score, boundary_touch_count=5),
        lambda: replace(score, confidence_millionths=-1),
        lambda: replace(score, visible_area_pixels=0),
        lambda: replace(score, point_count=0),
        lambda: replace(score, point_index=1),
        lambda: replace(score, midpoint_distance_seconds_x2_denominator=2),
        lambda: replace(score, pts=cast(MediaTime, object())),
        lambda: replace(score, observation_id="private/path"),
        lambda: replace(score, confidence_millionths=cast(int, True)),
    )
    for construct in invalid_scores:
        with pytest.raises(ValueError):
            construct()

    valid_other_area = replace(
        score,
        visible_area_pixels=5,
        visible_area_millionths=416_666,
    )
    valid_other_id = replace(score, observation_id="obs_" + "00" * 32)
    invalid_intents: tuple[Callable[[], object], ...] = (
        lambda: replace(intent, rank=0),
        lambda: replace(intent, source_id="private/path"),
        lambda: replace(intent, stream_index=cast(int, True)),
        lambda: replace(intent, pts=cast(MediaTime, object())),
        lambda: replace(intent, geometry=cast(Geometry, object())),
        lambda: replace(intent, score=cast(EvidenceScore, object())),
        lambda: replace(intent, selector=cast(Producer, object())),
        lambda: replace(intent, score=valid_other_id),
        lambda: replace(intent, score=valid_other_area),
    )
    for construct in invalid_intents:
        with pytest.raises(ValueError):
            construct()


def test_hostile_nested_producer_strings_cannot_forge_selector_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EqualToEverything(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

        __hash__ = str.__hash__

    _, _, observations, tracklet = _values(
        ((1, 1, 4, 3),),
        (950_000,),
        width=4,
        height=3,
    )
    issuer = BestFrameEvidenceSelector(limits=EvidenceSelectionLimits(1))
    receiver = BestFrameEvidenceSelector(limits=EvidenceSelectionLimits(2))
    intent = issuer.plan(tracklet, observations).intents[0]
    object.__setattr__(
        intent,
        "selector",
        Producer(
            EqualToEverything("private.producer"),
            EqualToEverything("private"),
            EqualToEverything("00" * 32),
        ),
    )
    attempts: list[str] = []

    def denied(*_: object, **__: object) -> NoReturn:
        attempts.append("materialized")
        raise AssertionError("forged intent reached materialization")

    monkeypatch.setattr(evidence_module, "extract_rgb24_crop", denied)

    with pytest.raises(PortError) as raised:
        receiver.materialize(
            intent,
            tracklet,
            observations,
            bytes(range(4 * 3 * 3)),
            need=EvidenceNeed.INSPECTION,
            detail_resolution=DetailResolution.RESOLVABLE,
        )

    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert raised.value.__context__ is None
    assert "private" not in str(raised.value)
    assert attempts == []


def test_valid_shaped_intent_and_rank_substitutions_cannot_materialize() -> None:
    _, _, observations, tracklet = _golden_values()
    selector = BestFrameEvidenceSelector()
    intent = selector.plan(tracklet, observations).intents[0]
    score = intent.score
    altered_pts = MediaTime("123", intent.pts.time_base)
    altered_observation_id = "obs_" + "44" * 32
    forged = (
        replace(intent, frame_id="frm_" + "11" * 32),
        replace(intent, source_id="src_" + "22" * 32),
        replace(intent, tracklet_id="trk_" + "33" * 32),
        replace(
            intent,
            observation_id=altered_observation_id,
            score=replace(score, observation_id=altered_observation_id),
        ),
        replace(intent, stream_index=1),
        replace(intent, rank=2),
        replace(
            intent,
            geometry=Geometry(100, 100, (20, 20, 80, 80), "inferred"),
        ),
        replace(intent, pts=altered_pts, score=replace(score, pts=altered_pts)),
        replace(intent, score=replace(score, confidence_millionths=1)),
        replace(
            intent,
            score=replace(
                score,
                midpoint_distance_seconds_x2_numerator=1,
                midpoint_distance_seconds_x2_denominator=2,
            ),
        ),
        replace(intent, score=replace(score, point_index=2)),
        replace(
            intent,
            selector=Producer(
                "visualworld.best-frame-evidence-selector",
                "1",
                "55" * 32,
            ),
        ),
    )

    for altered in forged:
        with pytest.raises(PortError) as raised:
            selector.materialize(
                altered,
                tracklet,
                observations,
                bytes(100 * 100 * 3),
                need=EvidenceNeed.INSPECTION,
                detail_resolution=DetailResolution.RESOLVABLE,
            )
        assert raised.value.code is PortErrorCode.CONFLICT
        assert raised.value.__context__ is None


def test_hostile_nested_exception_is_sanitized_before_dynamic_access() -> None:
    attempts: list[str] = []

    class ExplodingTimeBase:
        def to_mapping(self) -> object:
            attempts.append("dynamic-access")
            raise RuntimeError("private/path\nsecret")

    _, _, observations, tracklet = _values(
        ((1, 1, 4, 3),),
        (950_000,),
        width=4,
        height=3,
    )
    selector = BestFrameEvidenceSelector()
    intent = selector.plan(tracklet, observations).intents[0]
    object.__setattr__(intent.pts, "time_base", ExplodingTimeBase())

    with pytest.raises(PortError) as raised:
        selector.materialize(
            intent,
            tracklet,
            observations,
            bytes(range(4 * 3 * 3)),
            need=EvidenceNeed.INSPECTION,
            detail_resolution=DetailResolution.RESOLVABLE,
        )

    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert raised.value.__context__ is None
    assert "private" not in str(raised.value)
    assert attempts == []


def test_materialize_sanitizes_hostile_base_exception_from_crop_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HostileFailure(BaseException):
        pass

    _, _, observations, tracklet = _values(
        ((1, 1, 4, 3),),
        (950_000,),
        width=4,
        height=3,
    )
    selector = BestFrameEvidenceSelector()
    intent = selector.plan(tracklet, observations).intents[0]

    def fail(*_: object, **__: object) -> NoReturn:
        raise HostileFailure("private/path\nsecret")

    monkeypatch.setattr(evidence_module, "extract_rgb24_crop", fail)

    with pytest.raises(PortError) as raised:
        selector.materialize(
            intent,
            tracklet,
            observations,
            bytes(range(4 * 3 * 3)),
            need=EvidenceNeed.INSPECTION,
            detail_resolution=DetailResolution.RESOLVABLE,
        )

    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert raised.value.__context__ is None
    assert "private" not in str(raised.value)


def test_forged_score_integers_are_bounded_before_derived_arithmetic() -> None:
    _, _, observations, tracklet = _values(
        ((1, 1, 4, 3),),
        (950_000,),
        width=4,
        height=3,
    )
    score = BestFrameEvidenceSelector().plan(tracklet, observations).intents[0].score

    invalid_scores: tuple[Callable[[], EvidenceScore], ...] = (
        lambda: replace(
            score,
            source_area_pixels=2**63,
            visible_area_pixels=2**63,
            visible_area_millionths=1_000_000,
        ),
        lambda: replace(
            score,
            midpoint_distance_seconds_x2_numerator=2**200,
            midpoint_distance_seconds_x2_denominator=1,
        ),
    )
    for construct in invalid_scores:
        with pytest.raises(ValueError):
            construct()


def test_hostile_lifecycle_string_subclasses_are_rejected_before_equality() -> None:
    class EqualToEverything(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

    _, _, observations, tracklet = _values(
        ((1, 1, 4, 3),),
        (950_000,),
        width=4,
        height=3,
    )
    intent = BestFrameEvidenceSelector().plan(tracklet, observations).intents[0]

    hostile = EqualToEverything("private")
    invalid_intents: tuple[Callable[[], EvidenceIntent], ...] = (
        lambda: replace(intent, kind=hostile),
        lambda: replace(intent, retention=hostile),
        lambda: replace(intent, deletion_owner=hostile),
    )
    for construct in invalid_intents:
        with pytest.raises(ValueError):
            construct()


@pytest.mark.parametrize("operation", ("plan", "select"))
def test_selection_operations_sanitize_concurrent_nested_mutation(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    dynamic_access: list[str] = []

    class ExplodingGeometry:
        def to_mapping(self) -> object:
            dynamic_access.append("to_mapping")
            raise RuntimeError("private/path\nsecret")

    _, _, observations, tracklet = _golden_values()
    target = observations[0]
    entered_snapshot = threading.Event()
    mutation_finished = threading.Event()
    failures: list[str] = []
    first_copy = True
    original_copy_time = evidence_module._copy_time

    def pause_first_time_copy(value: MediaTime) -> MediaTime:
        nonlocal first_copy
        if first_copy:
            first_copy = False
            entered_snapshot.set()
            if not mutation_finished.wait(timeout=2):
                failures.append("mutation timed out")
        return original_copy_time(value)

    def mutate_during_snapshot() -> None:
        if not entered_snapshot.wait(timeout=2):
            failures.append("snapshot timed out")
            mutation_finished.set()
            return
        object.__setattr__(target, "geometry", ExplodingGeometry())
        mutation_finished.set()

    monkeypatch.setattr(evidence_module, "_copy_time", pause_first_time_copy)
    mutator = threading.Thread(target=mutate_during_snapshot)
    mutator.start()
    selector = BestFrameEvidenceSelector()

    try:
        with pytest.raises(PortError) as raised:
            getattr(selector, operation)(tracklet, observations)
    finally:
        mutation_finished.set()
        mutator.join(timeout=2)

    assert not mutator.is_alive()
    assert failures == []
    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert raised.value.__context__ is None
    assert "private" not in str(raised.value)
    assert dynamic_access == []
    assert selector.calls == ()


@pytest.mark.parametrize("operation", ("plan", "select"))
def test_selection_operations_sanitize_hostile_base_exception(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    class HostileFailure(BaseException):
        pass

    _, _, observations, tracklet = _golden_values()

    def fail(*_: object, **__: object) -> NoReturn:
        raise HostileFailure("private/path\nsecret")

    monkeypatch.setattr(evidence_module, "_score", fail)
    selector = BestFrameEvidenceSelector()

    with pytest.raises(PortError) as raised:
        getattr(selector, operation)(tracklet, observations)

    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert raised.value.__context__ is None
    assert "private" not in str(raised.value)
    assert selector.calls == ()
