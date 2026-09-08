# SPDX-License-Identifier: Apache-2.0
"""Contract tests for v0.2 perception ports and deterministic fakes."""

from __future__ import annotations

import builtins
import socket
import sqlite3
import subprocess
from typing import NoReturn, cast

import pytest

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
    MAX_PERCEPTION_OBSERVATIONS,
    DetectionResult,
    Detector,
    EvidenceSelectionResult,
    EvidenceSelector,
    FakeDetector,
    FakeEvidenceSelector,
    FakeTracker,
    PerceptionResultState,
    PortCall,
    PortError,
    PortErrorCode,
    PortKind,
    Tracker,
    TrackingResult,
)


def values() -> tuple[Source, tuple[FrameRef, ...], tuple[Observation, ...], Tracklet]:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint("aa" * 32, "10"),
        (SourceStream(0, 64, 48, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(source.source_id, 0, str(index), MediaTime(str(index * 200), time_base))
        for index in range(2)
    )
    detector = Producer("visualworld.fake-detector", "1", "bb" * 32)
    observations = tuple(
        Observation.create(
            source.source_id,
            frame.frame_id,
            frame.stream_index,
            frame.pts,
            Geometry(64, 48, (4 + index, 5, 20 + index, 30), "inferred"),
            "vehicle",
            950_000,
            detector,
        )
        for index, frame in enumerate(frames)
    )
    tracklet = Tracklet.create(
        source.source_id,
        0,
        "vehicle",
        tuple(TrackPoint.from_observation(observation) for observation in observations),
        "source_end",
        Producer("visualworld.fake-tracker", "1", "cc" * 32),
    )
    return source, frames, observations, tracklet


def test_perception_fakes_satisfy_their_protocols_and_are_instrumented() -> None:
    source, frames, observations, tracklet = values()
    detection = DetectionResult(PerceptionResultState.COMPLETE, observations)
    tracking = TrackingResult(PerceptionResultState.COMPLETE, (tracklet,))
    selection = EvidenceSelectionResult(
        PerceptionResultState.COMPLETE,
        (observations[1].observation_id,),
    )
    detector = FakeDetector(detection)
    tracker = FakeTracker(tracking)
    selector = FakeEvidenceSelector(selection)

    assert isinstance(detector, Detector)
    assert isinstance(tracker, Tracker)
    assert isinstance(selector, EvidenceSelector)
    assert detector.detect(source, frames) == detection
    assert tracker.track(source, frames, observations) == tracking
    assert selector.select(tracklet, observations) == selection
    assert detector.calls == (PortCall(PortKind.DETECTOR, "detect", 2),)
    assert tracker.calls == (PortCall(PortKind.TRACKER, "track", 2),)
    assert selector.calls == (PortCall(PortKind.EVIDENCE_SELECTOR, "select", 2),)
    assert {item.descriptor.port for item in (detector, tracker, selector)} == {
        PortKind.DETECTOR,
        PortKind.TRACKER,
        PortKind.EVIDENCE_SELECTOR,
    }
    assert all(item.descriptor.offline for item in (detector, tracker, selector))


def test_detector_frame_bound_allows_multiple_observations_per_frame() -> None:
    source, frames, observations, _ = values()
    template = observations[0]
    many = tuple(
        Observation.create(
            template.source_id,
            template.frame_id,
            template.stream_index,
            template.pts,
            template.geometry,
            template.category,
            index,
            template.producer,
        )
        for index in range(65)
    )
    detector = FakeDetector(DetectionResult(PerceptionResultState.COMPLETE, many))

    assert MAX_PERCEPTION_OBSERVATIONS == 4_096
    assert detector.detect(source, frames[:1]).observations == many
    assert detector.calls == (PortCall(PortKind.DETECTOR, "detect", 1),)


def test_perception_results_represent_complete_unknown_and_unsupported_explicitly() -> None:
    assert DetectionResult(PerceptionResultState.COMPLETE).observations == ()
    unknown = DetectionResult(PerceptionResultState.UNKNOWN, reason="source_pixels_unavailable")
    unsupported = TrackingResult(
        PerceptionResultState.UNSUPPORTED,
        reason="class_outside_measured_boundary",
    )
    assert unknown.reason == "source_pixels_unavailable"
    assert unsupported.reason == "class_outside_measured_boundary"

    invalid = (
        lambda: DetectionResult(PerceptionResultState.COMPLETE, reason="unexpected"),
        lambda: DetectionResult(PerceptionResultState.UNKNOWN),
        lambda: DetectionResult(
            PerceptionResultState.UNSUPPORTED,
            values()[2],
            "unsupported_platform",
        ),
        lambda: TrackingResult(cast(PerceptionResultState, "complete")),
        lambda: EvidenceSelectionResult(
            PerceptionResultState.COMPLETE,
            (values()[2][0].observation_id, values()[2][0].observation_id),
        ),
        lambda: DetectionResult(
            PerceptionResultState.COMPLETE,
            cast(tuple[Observation, ...], list(values()[2])),
        ),
        lambda: DetectionResult(
            PerceptionResultState.COMPLETE,
            (cast(Observation, object()),),
        ),
        lambda: DetectionResult(
            PerceptionResultState.COMPLETE,
            (values()[2][0], values()[2][0]),
        ),
        lambda: TrackingResult(
            PerceptionResultState.COMPLETE,
            cast(tuple[Tracklet, ...], list((values()[3],))),
        ),
        lambda: TrackingResult(
            PerceptionResultState.COMPLETE,
            (cast(Tracklet, object()),),
        ),
        lambda: TrackingResult(
            PerceptionResultState.COMPLETE,
            (values()[3], values()[3]),
        ),
        lambda: EvidenceSelectionResult(
            PerceptionResultState.COMPLETE,
            cast(tuple[str, ...], list((values()[2][0].observation_id,))),
        ),
        lambda: EvidenceSelectionResult(PerceptionResultState.COMPLETE, ("bad",)),
        lambda: DetectionResult(PerceptionResultState.UNKNOWN, reason="bad reason"),
        lambda: DetectionResult(
            PerceptionResultState.COMPLETE,
            (values()[2][0],) * (MAX_PERCEPTION_OBSERVATIONS + 1),
        ),
    )
    for construct in invalid:
        with pytest.raises(ValueError):
            construct()


def test_perception_fakes_preserve_explicit_incomplete_states_without_partial_outputs() -> None:
    source, frames, observations, tracklet = values()
    detection = FakeDetector(
        DetectionResult(PerceptionResultState.UNKNOWN, reason="source_pixels_unavailable")
    ).detect(source, frames)
    tracking = FakeTracker(
        TrackingResult(
            PerceptionResultState.UNSUPPORTED,
            reason="class_outside_measured_boundary",
        )
    ).track(source, frames, observations)
    selection = FakeEvidenceSelector(
        EvidenceSelectionResult(
            PerceptionResultState.UNKNOWN,
            reason="source_pixels_unavailable",
        )
    ).select(tracklet, observations)

    assert detection == DetectionResult(
        PerceptionResultState.UNKNOWN,
        reason="source_pixels_unavailable",
    )
    assert tracking.tracklets == ()
    assert selection.observation_ids == ()


def test_perception_fakes_reject_mismatched_sources_frames_and_outputs() -> None:
    source, frames, observations, tracklet = values()
    other_source = Source.create(
        Fingerprint("dd" * 32, "10"),
        source.streams,
    )

    with pytest.raises(PortError, match="conflict"):
        FakeDetector(DetectionResult(PerceptionResultState.COMPLETE, observations)).detect(
            other_source, frames
        )
    with pytest.raises(PortError, match="conflict"):
        FakeTracker(TrackingResult(PerceptionResultState.COMPLETE, (tracklet,))).track(
            source, frames[:1], observations
        )
    with pytest.raises(PortError, match="conflict"):
        FakeTracker(TrackingResult(PerceptionResultState.COMPLETE)).track(
            source, frames, observations
        )
    with pytest.raises(PortError, match="conflict"):
        FakeEvidenceSelector(
            EvidenceSelectionResult(
                PerceptionResultState.COMPLETE,
                ("obs_" + "00" * 32,),
            )
        ).select(tracklet, observations)
    with pytest.raises(PortError, match="conflict"):
        FakeEvidenceSelector(
            EvidenceSelectionResult(
                PerceptionResultState.COMPLETE,
                (observations[0].observation_id,),
            )
        ).select(tracklet, observations[:1])
    with pytest.raises(PortError, match="limit_exceeded"):
        FakeDetector(DetectionResult(PerceptionResultState.COMPLETE)).detect(
            source, cast(tuple[FrameRef, ...], list(frames))
        )
    with pytest.raises(PortError, match="invalid_request"):
        FakeDetector(DetectionResult(PerceptionResultState.COMPLETE)).detect(
            source, (cast(FrameRef, object()),)
        )
    with pytest.raises(PortError, match="invalid_request"):
        FakeDetector(cast(DetectionResult, object()))
    with pytest.raises(PortError, match="invalid_request"):
        FakeTracker(cast(TrackingResult, object()))
    with pytest.raises(PortError, match="invalid_request"):
        FakeEvidenceSelector(cast(EvidenceSelectionResult, object()))
    with pytest.raises(PortError, match="invalid_request"):
        FakeEvidenceSelector(EvidenceSelectionResult(PerceptionResultState.COMPLETE)).select(
            cast(Tracklet, object()), observations
        )
    with pytest.raises(PortError, match="limit_exceeded"):
        FakeEvidenceSelector(EvidenceSelectionResult(PerceptionResultState.COMPLETE)).select(
            tracklet, cast(tuple[Observation, ...], list(observations))
        )


def test_perception_fakes_do_not_touch_ambient_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    source, frames, observations, tracklet = values()
    detector = FakeDetector(DetectionResult(PerceptionResultState.COMPLETE, observations))
    tracker = FakeTracker(TrackingResult(PerceptionResultState.COMPLETE, (tracklet,)))
    selector = FakeEvidenceSelector(
        EvidenceSelectionResult(
            PerceptionResultState.COMPLETE,
            (observations[0].observation_id,),
        )
    )
    attempts: list[str] = []

    def denied(*_: object, **__: object) -> NoReturn:
        attempts.append("ambient-effect")
        raise AssertionError("ambient effect attempted")

    monkeypatch.setattr(builtins, "open", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(sqlite3, "connect", denied)
    monkeypatch.setattr(subprocess, "run", denied)

    assert detector.detect(source, frames).state is PerceptionResultState.COMPLETE
    assert tracker.track(source, frames, observations).state is PerceptionResultState.COMPLETE
    assert selector.select(tracklet, observations).state is PerceptionResultState.COMPLETE
    assert attempts == []


def test_perception_port_errors_remain_structured_and_redacted() -> None:
    source, frames, observations, _ = values()
    untrusted = "private/path\nsecret"
    result = DetectionResult(PerceptionResultState.UNKNOWN, reason="not_available")
    detector = FakeDetector(result)

    with pytest.raises(PortError) as raised:
        detector.detect(cast(Source, untrusted), frames)
    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert raised.value.port is PortKind.DETECTOR
    assert untrusted not in str(raised.value)

    with pytest.raises(PortError) as raised_tracker:
        FakeTracker(TrackingResult(PerceptionResultState.COMPLETE)).track(
            source,
            frames,
            cast(tuple[Observation, ...], [*observations]),
        )
    assert raised_tracker.value.code is PortErrorCode.LIMIT_EXCEEDED
