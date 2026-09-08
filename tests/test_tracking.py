# SPDX-License-Identifier: Apache-2.0
"""Contract, hostile-input, paging, and benchmark goldens for the v0.2 tracker."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

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
from visualworld.perception import FrameDiscontinuity, Observation
from visualworld.ports import (
    FakeTracker,
    PerceptionResultState,
    PortCall,
    PortError,
    PortErrorCode,
    PortKind,
    Tracker,
    TrackingResult,
)
from visualworld.tracking import (
    ASSOCIATION_IOU_BASIS_POINTS,
    CUT_THRESHOLD_BASIS_POINTS,
    MAX_MISSED_SAMPLES,
    TRACKER_CONFIGURATION_SHA256,
    TRACKING_FPS,
    GlobalLastBoxTracker,
    ResumableTracker,
    TrackingCursor,
    TrackingDiagnostics,
    TrackingLimits,
    _maximum_assignment,
    rgb24_discontinuity_basis_points,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v02_tracking_metrics as research  # noqa: E402

BOX = (10, 10, 30, 30)
DETECTOR_PRODUCER = Producer("visualworld.test-detector", "1", "dd" * 32)


def _source(
    *,
    digest: str = "aa" * 32,
    width: int = 100,
    height: int = 80,
    time_base: TimeBase | None = None,
) -> Source:
    selected_time_base = TimeBase("1", "1000") if time_base is None else time_base
    return Source.create(
        Fingerprint(digest, "1000"),
        (SourceStream(0, width, height, 0, selected_time_base),),
    )


def _frames(source: Source, count: int, *, step: int = 200) -> tuple[FrameRef, ...]:
    time_base = source.streams[0].time_base
    return tuple(
        FrameRef.create(
            source.source_id,
            0,
            str(index),
            MediaTime(str(index * step), time_base),
        )
        for index in range(count)
    )


def _observation(
    source: Source,
    frame: FrameRef,
    box: tuple[int, int, int, int],
    *,
    confidence: int = 950_000,
) -> Observation:
    stream = source.streams[0]
    return Observation.create(
        source.source_id,
        frame.frame_id,
        frame.stream_index,
        frame.pts,
        Geometry(stream.width, stream.height, box, "inferred"),
        "vehicle",
        confidence,
        DETECTOR_PRODUCER,
    )


def _scores(
    frames: tuple[FrameRef, ...],
    values: tuple[int, ...] | None = None,
) -> tuple[FrameDiscontinuity, ...]:
    selected = (0,) * len(frames) if values is None else values
    return tuple(
        FrameDiscontinuity.from_frame(frame, score)
        for frame, score in zip(frames, selected, strict=True)
    )


def test_one_shot_tracker_satisfies_contract_and_fake_substitution() -> None:
    source = _source()
    frames = _frames(source, 4)
    observations = tuple(
        _observation(source, frame, (10 + index, 10, 30 + index, 30))
        for index, frame in enumerate(frames)
    )
    discontinuities = _scores(frames)
    tracker = GlobalLastBoxTracker()

    result = tracker.track(
        source,
        frames,
        observations,
        discontinuities=discontinuities,
    )

    assert isinstance(tracker, Tracker)
    assert isinstance(tracker, ResumableTracker)
    assert result.state is PerceptionResultState.COMPLETE
    assert len(result.tracklets) == 1
    tracklet = result.tracklets[0]
    assert [point.observation_id for point in tracklet.points] == [
        observation.observation_id for observation in observations
    ]
    assert tracklet.termination_reason == "source_end"
    assert tracklet.producer == tracker.producer
    assert tracker.producer.configuration_sha256 == TRACKER_CONFIGURATION_SHA256
    assert tracker.descriptor.implementation == "visualworld.global-last-iou-tracker"
    assert tracker.descriptor.deterministic is True
    assert tracker.descriptor.offline is True
    assert tracker.descriptor.allowed_effects == ()
    assert tracker.calls == (PortCall(PortKind.TRACKER, "track", 4),)

    fake = FakeTracker(result)
    assert (
        fake.track(
            source,
            frames,
            observations,
            discontinuities=discontinuities,
        )
        == result
    )


def test_frozen_configuration_and_integer_constants_are_exact() -> None:
    assert ASSOCIATION_IOU_BASIS_POINTS == 1_000
    assert CUT_THRESHOLD_BASIS_POINTS == 1_500
    assert MAX_MISSED_SAMPLES == 5
    assert TRACKING_FPS == 5
    assert (
        TRACKER_CONFIGURATION_SHA256
        == "e5a8fd8bdd43abeeb93b1602668be2436ce8f6c8553d9002c10038526e41ece2"
    )


def test_rgb24_cut_score_matches_frozen_every_fourth_pixel_rule() -> None:
    black = bytes(24)
    changed_sample = bytes([255, 255, 255]) + bytes(21)
    changed_unsampled = bytes(3) + bytes([255, 255, 255]) + bytes(18)

    assert rgb24_discontinuity_basis_points(None, black, width=8, height=1) == 0
    assert rgb24_discontinuity_basis_points(black, changed_sample, width=8, height=1) == 5_000
    assert rgb24_discontinuity_basis_points(black, changed_unsampled, width=8, height=1) == 0
    assert rgb24_discontinuity_basis_points(black, bytes([255]) * 24, width=8, height=1) == 10_000

    for current, width, height in (
        (bytearray(24), 8, 1),
        (bytes(23), 8, 1),
        (bytes(24), True, 1),
        (bytes(24), 0, 1),
    ):
        with pytest.raises(ValueError, match="invalid_rgb24_frame"):
            rgb24_discontinuity_basis_points(
                None,
                cast(bytes, current),
                width=width,
                height=height,
            )

    with pytest.raises(ValueError, match="invalid_rgb24_frame"):
        rgb24_discontinuity_basis_points(bytes(23), black, width=8, height=1)


def test_exact_global_assignment_and_ties_are_deterministic() -> None:
    assert _maximum_assignment(((1, 1), (1, 1))) == ((0, 0), (1, 1))
    assert _maximum_assignment(((1,), (2,))) == ((1, 0),)
    assert _maximum_assignment(((10, 8), (9, 0))) == ((0, 1), (1, 0))
    with pytest.raises(ValueError, match="invalid_assignment"):
        _maximum_assignment(((1,), (1, 2)))
    with pytest.raises(ValueError, match="invalid_assignment"):
        _maximum_assignment(((cast(int, float("nan")),),))


def test_five_misses_reassociate_but_sixth_terminates_before_reacquisition() -> None:
    source = _source()
    frames = _frames(source, 8)
    observations = (
        _observation(source, frames[0], BOX),
        _observation(source, frames[6], BOX),
        _observation(source, frames[7], BOX),
    )
    result = GlobalLastBoxTracker().track(
        source,
        frames,
        observations,
        discontinuities=_scores(frames),
    )

    assert len(result.tracklets) == 1
    assert [point.frame_id for point in result.tracklets[0].points] == [
        frames[0].frame_id,
        frames[6].frame_id,
        frames[7].frame_id,
    ]

    expired_observations = (
        _observation(source, frames[0], BOX),
        _observation(source, frames[7], BOX),
    )
    expired = GlobalLastBoxTracker().track(
        source,
        frames,
        expired_observations,
        discontinuities=_scores(frames),
    )
    assert [tracklet.termination_reason for tracklet in expired.tracklets] == [
        "miss_timeout",
        "source_end",
    ]
    assert [len(tracklet.points) for tracklet in expired.tracklets] == [1, 1]


def test_cut_terminates_before_first_post_cut_association() -> None:
    source = _source()
    frames = _frames(source, 3)
    observations = tuple(_observation(source, frame, BOX) for frame in frames)
    result = GlobalLastBoxTracker().track(
        source,
        frames,
        observations,
        discontinuities=_scores(frames, (0, 1_499, 1_500)),
    )

    assert [tracklet.termination_reason for tracklet in result.tracklets] == [
        "cut",
        "source_end",
    ]
    assert [
        tuple(point.frame_id for point in tracklet.points) for tracklet in result.tracklets
    ] == [
        (frames[0].frame_id, frames[1].frame_id),
        (frames[2].frame_id,),
    ]


def test_resumed_pages_equal_one_shot_across_occlusion_and_empty_final_page() -> None:
    source = _source()
    frames = _frames(source, 8)
    observations = (
        _observation(source, frames[0], BOX),
        _observation(source, frames[6], BOX),
    )
    scores = _scores(frames)
    expected = GlobalLastBoxTracker().track(
        source,
        frames,
        observations,
        discontinuities=scores,
    )
    tracker = GlobalLastBoxTracker()
    first = tracker.track_page(
        source,
        frames[:4],
        tuple(
            item
            for item in observations
            if item.frame_id in {frame.frame_id for frame in frames[:4]}
        ),
        discontinuities=scores[:4],
    )
    assert first.finished is False and first.tracklets == () and first.cursor is not None
    second = tracker.track_page(
        source,
        frames[4:],
        tuple(
            item
            for item in observations
            if item.frame_id in {frame.frame_id for frame in frames[4:]}
        ),
        discontinuities=scores[4:],
        cursor=first.cursor,
    )
    assert second.finished is False and second.cursor is not None
    final = tracker.track_page(
        source,
        (),
        (),
        discontinuities=(),
        cursor=second.cursor,
        end_of_stream=True,
    )
    assert (*first.tracklets, *second.tracklets, *final.tracklets) == expected.tracklets
    assert final.diagnostics == second.cursor.diagnostics.__class__(
        frame_count=8,
        observation_count=2,
        created_tracks=1,
        maximum_active_tracks=1,
        detected_cut_frames=0,
        cut_terminations=0,
        miss_timeout_terminations=0,
        source_end_terminations=1,
    )
    assert final.cursor is not None and final.cursor.finished is True
    with pytest.raises(TypeError):
        replace(final.cursor, finished=False)


def test_missing_scores_and_out_of_boundary_category_or_rate_are_explicit() -> None:
    source = _source()
    frames = _frames(source, 2)
    observations = tuple(_observation(source, frame, BOX) for frame in frames)

    unknown = GlobalLastBoxTracker().track(source, frames, observations)
    assert unknown == TrackingResult(
        PerceptionResultState.UNKNOWN,
        reason="cut_scores_unavailable",
    )

    unsupported_rate_frames = _frames(source, 2, step=250)
    unsupported_rate = GlobalLastBoxTracker().track(
        source,
        unsupported_rate_frames,
        tuple(_observation(source, frame, BOX) for frame in unsupported_rate_frames),
        discontinuities=_scores(unsupported_rate_frames),
    )
    assert unsupported_rate == TrackingResult(
        PerceptionResultState.UNSUPPORTED,
        reason="sampling_rate_unsupported",
    )

    unsupported_category = GlobalLastBoxTracker(requested_category="animal").track(
        source,
        frames,
        observations,
        discontinuities=_scores(frames),
    )
    assert unsupported_category == TrackingResult(
        PerceptionResultState.UNSUPPORTED,
        reason="category_unsupported",
    )


def test_hostile_records_pixels_and_mixed_scope_fail_closed_and_redacted() -> None:
    source = _source()
    frames = _frames(source, 2)
    observation = _observation(source, frames[0], BOX)
    tracker = GlobalLastBoxTracker()
    marker = b"private-pixels-never-print"

    hostile_cases: tuple[tuple[object, object, object], ...] = (
        (cast(tuple[FrameRef, ...], list(frames)), (observation,), _scores(frames)),
        (frames, cast(tuple[Observation, ...], [observation]), _scores(frames)),
        (frames, (observation,), cast(tuple[FrameDiscontinuity, ...], (marker, marker))),
        (frames[::-1], (observation,), _scores(frames)[::-1]),
    )
    for hostile_frames, hostile_observations, hostile_scores in hostile_cases:
        with pytest.raises(PortError) as raised:
            tracker.track(
                source,
                cast(tuple[FrameRef, ...], hostile_frames),
                cast(tuple[Observation, ...], hostile_observations),
                discontinuities=cast(tuple[FrameDiscontinuity, ...], hostile_scores),
            )
        assert raised.value.code in {PortErrorCode.INVALID_REQUEST, PortErrorCode.CONFLICT}
        assert marker not in str(raised.value).encode()
        assert source.source_id not in str(raised.value)

    class VendorFingerprint(Fingerprint):
        pass

    vendor_fingerprint = VendorFingerprint(
        source.fingerprint.digest,
        source.fingerprint.bytes,
    )
    hostile_source = Source(source.source_id, vendor_fingerprint, source.streams)
    with pytest.raises(PortError) as raised:
        tracker.track(
            hostile_source,
            frames,
            (observation,),
            discontinuities=_scores(frames),
        )
    assert raised.value.code is PortErrorCode.INVALID_REQUEST

    class VendorMediaTime(MediaTime):
        pass

    vendor_pts = VendorMediaTime("0", source.streams[0].time_base)
    hostile_frame = FrameRef.create(source.source_id, 0, "0", vendor_pts)
    with pytest.raises(PortError) as raised:
        tracker.track(source, (hostile_frame,), ())
    assert raised.value.code is PortErrorCode.INVALID_REQUEST

    class VendorTimeBase(TimeBase):
        pass

    nested_vendor_pts = MediaTime("0", VendorTimeBase("1", "1000"))
    nested_hostile_frame = FrameRef.create(source.source_id, 0, "0", nested_vendor_pts)
    with pytest.raises(PortError) as raised:
        tracker.track(source, (nested_hostile_frame,), ())
    assert raised.value.code is PortErrorCode.INVALID_REQUEST

    class StatefulMediaTime(MediaTime):
        calls = 0

        def to_mapping(self) -> dict[str, object]:
            type(self).calls += 1
            if type(self).calls > 2:
                raise OSError("/private/frame-source.mov")
            return super().to_mapping()

    stateful_pts = StatefulMediaTime("0", source.streams[0].time_base)
    stateful_frame = FrameRef.create(source.source_id, 0, "0", stateful_pts)
    with pytest.raises(ValueError) as frame_error:
        FrameDiscontinuity.from_frame(stateful_frame, 0)
    assert "/private/frame-source.mov" not in str(frame_error.value)

    other = _source(digest="bb" * 32)
    other_frame = _frames(other, 1)[0]
    with pytest.raises(PortError, match="conflict"):
        tracker.track(
            source,
            frames,
            (_observation(other, other_frame, BOX),),
            discontinuities=_scores(frames),
        )

    wrong_geometry = Observation.create(
        source.source_id,
        frames[0].frame_id,
        0,
        frames[0].pts,
        Geometry(101, 80, BOX, "inferred"),
        "vehicle",
        950_000,
        tracker.producer,
    )
    with pytest.raises(PortError, match="conflict"):
        tracker.track(
            source,
            frames,
            (wrong_geometry,),
            discontinuities=_scores(frames),
        )


def test_numeric_and_resource_bounds_reject_without_partial_progress() -> None:
    source = _source(width=10_000, height=100)
    frames = _frames(source, 3)
    with pytest.raises(ValueError):
        TrackingLimits(max_page_frames=True)
    hostile_limits = TrackingLimits()
    object.__setattr__(hostile_limits, "max_page_observations", 1_000_000)
    with pytest.raises(ValueError):
        GlobalLastBoxTracker(limits=hostile_limits)
    owned_limits = TrackingLimits()
    owned_tracker = GlobalLastBoxTracker(limits=owned_limits)
    object.__setattr__(owned_limits, "max_page_frames", 1)
    assert owned_tracker.descriptor.max_batch_items == 64
    assert (
        owned_tracker.track(source, frames, (), discontinuities=_scores(frames)).state
        is PerceptionResultState.COMPLETE
    )
    with pytest.raises(ValueError):
        FrameDiscontinuity.from_frame(frames[0], cast(int, float("nan")))
    with pytest.raises(ValueError):
        FrameDiscontinuity.from_frame(frames[0], cast(int, True))

    limited = GlobalLastBoxTracker(limits=TrackingLimits(max_track_points=2))
    observations = tuple(_observation(source, frame, BOX) for frame in frames)
    with pytest.raises(PortError) as raised:
        limited.track(
            source,
            frames,
            observations,
            discontinuities=_scores(frames),
        )
    assert raised.value.code is PortErrorCode.LIMIT_EXCEEDED
    assert limited.calls == ()

    one_frame = frames[:1]
    too_many = tuple(
        _observation(
            source,
            one_frame[0],
            (index * 100, 0, index * 100 + 50, 50),
            confidence=900_000 + index,
        )
        for index in range(65)
    )
    with pytest.raises(PortError) as raised:
        GlobalLastBoxTracker().track(
            source,
            one_frame,
            too_many,
            discontinuities=_scores(one_frame),
        )
    assert raised.value.code is PortErrorCode.LIMIT_EXCEEDED


def test_empty_clip_and_bounded_request_conflicts_are_explicit() -> None:
    source = _source()
    frames = _frames(source, 7)
    empty = GlobalLastBoxTracker().track(source, (), (), discontinuities=())
    assert empty == TrackingResult(PerceptionResultState.COMPLETE)

    with pytest.raises(ValueError):
        TrackingLimits(max_total_frames=63)
    with pytest.raises(ValueError):
        TrackingDiagnostics(-1, 0, 0, 0, 0, 0, 0, 0)

    bounded = GlobalLastBoxTracker(limits=TrackingLimits(max_page_frames=1, max_total_frames=1))
    with pytest.raises(PortError) as raised:
        bounded.track(source, frames[:2], (), discontinuities=_scores(frames[:2]))
    assert raised.value.code is PortErrorCode.LIMIT_EXCEEDED

    first = bounded.track_page(source, frames[:1], (), discontinuities=_scores(frames[:1]))
    assert first.cursor is not None
    with pytest.raises(PortError) as raised:
        bounded.track_page(
            source,
            frames[1:2],
            (),
            discontinuities=_scores(frames[1:2]),
            cursor=first.cursor,
        )
    assert raised.value.code is PortErrorCode.LIMIT_EXCEEDED

    duration_bounded = GlobalLastBoxTracker(limits=TrackingLimits(max_duration_seconds=1))
    with pytest.raises(PortError) as raised:
        duration_bounded.track(source, frames, (), discontinuities=_scores(frames))
    assert raised.value.code is PortErrorCode.LIMIT_EXCEEDED

    with pytest.raises(PortError) as raised:
        GlobalLastBoxTracker().track_page(source, (), (), discontinuities=())
    assert raised.value.code is PortErrorCode.INVALID_REQUEST

    with pytest.raises(PortError) as raised:
        GlobalLastBoxTracker().track(
            source,
            frames[:2],
            (),
            discontinuities=_scores(frames[:1]),
        )
    assert raised.value.code is PortErrorCode.INVALID_REQUEST

    with pytest.raises(PortError) as raised:
        GlobalLastBoxTracker().track(
            source,
            frames[:1],
            cast(tuple[Observation, ...], (b"pixels",)),
            discontinuities=_scores(frames[:1]),
        )
    assert raised.value.code is PortErrorCode.INVALID_REQUEST

    with pytest.raises(PortError) as raised:
        GlobalLastBoxTracker().track(
            source,
            (frames[0], frames[0]),
            (),
            discontinuities=_scores((frames[0], frames[0])),
        )
    assert raised.value.code is PortErrorCode.CONFLICT

    wrong_stream_frame = FrameRef.create(source.source_id, 1, "0", frames[0].pts)
    with pytest.raises(PortError) as raised:
        GlobalLastBoxTracker().track(
            source, (wrong_stream_frame,), (), discontinuities=_scores((wrong_stream_frame,))
        )
    assert raised.value.code is PortErrorCode.CONFLICT

    with pytest.raises(PortError) as raised:
        GlobalLastBoxTracker().track(
            source,
            frames[:1],
            (),
            discontinuities=_scores(frames[:1], (1,)),
        )
    assert raised.value.code is PortErrorCode.CONFLICT

    other_frame = _frames(_source(digest="bb" * 32), 1)[0]
    with pytest.raises(PortError) as raised:
        GlobalLastBoxTracker().track(
            source,
            frames[:1],
            (),
            discontinuities=_scores((other_frame,)),
        )
    assert raised.value.code is PortErrorCode.CONFLICT


def test_cursor_factory_and_resume_scope_fail_closed() -> None:
    source = _source()
    frames = _frames(source, 2)
    tracker = GlobalLastBoxTracker()
    first = tracker.track_page(
        source,
        frames[:1],
        (_observation(source, frames[0], BOX),),
        discontinuities=_scores(frames[:1]),
    )
    assert first.cursor is not None
    cursor = first.cursor

    with pytest.raises(TypeError):
        TrackingCursor(
            _factory=object(),
            source_id=cursor.source_id,
            stream_index=cursor.stream_index,
            time_base=cursor.time_base,
            source_stream=cursor.source_stream,
            limits=cursor.limits,
            first_pts=cursor.first_pts,
            last_frame=cursor.last_frame,
            active_tracks=cursor.active_tracks,
            next_ordinal=cursor.next_ordinal,
            diagnostics=cursor.diagnostics,
        )

    source_without_streams = Source(source.source_id, source.fingerprint)
    with pytest.raises(PortError) as raised:
        tracker.track_page(
            source_without_streams,
            (),
            (),
            discontinuities=(),
            cursor=cursor,
            end_of_stream=True,
        )
    assert raised.value.code is PortErrorCode.CONFLICT

    changed_stream_source = Source(
        source.source_id,
        source.fingerprint,
        (SourceStream(0, 200, 160, 0, source.streams[0].time_base),),
    )
    with pytest.raises(PortError) as raised:
        tracker.track_page(
            changed_stream_source,
            frames[1:],
            (),
            discontinuities=_scores(frames[1:]),
            cursor=cursor,
        )
    assert raised.value.code is PortErrorCode.CONFLICT

    changed_observation = _observation(
        changed_stream_source,
        frames[1],
        (20, 20, 40, 40),
    )
    with pytest.raises(PortError) as raised:
        tracker.track_page(
            changed_stream_source,
            frames[1:],
            (changed_observation,),
            discontinuities=_scores(frames[1:]),
            cursor=cursor,
            end_of_stream=True,
        )
    assert raised.value.code is PortErrorCode.CONFLICT
    assert changed_stream_source.source_id not in str(raised.value)

    finished = tracker.track_page(
        source,
        frames[1:],
        (),
        discontinuities=_scores(frames[1:]),
        cursor=cursor,
        end_of_stream=True,
    )
    assert finished.cursor is not None
    with pytest.raises(PortError) as raised:
        tracker.track_page(
            source,
            (),
            (),
            discontinuities=(),
            cursor=finished.cursor,
            end_of_stream=True,
        )
    assert raised.value.code is PortErrorCode.CONFLICT

    active_bounded = GlobalLastBoxTracker(
        limits=TrackingLimits(max_active_tracks=1, max_total_tracks=2)
    )
    observations = (
        _observation(source, frames[0], BOX),
        _observation(source, frames[1], (60, 10, 80, 30)),
    )
    with pytest.raises(PortError) as raised:
        active_bounded.track(
            source,
            frames,
            observations,
            discontinuities=_scores(frames),
        )
    assert raised.value.code is PortErrorCode.LIMIT_EXCEEDED


class _HostileCancellation(threading.Event):
    def is_set(self) -> bool:
        raise OSError("/private/camera.mov")


def test_cancellation_publishes_no_call_or_cursor_and_retry_is_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    frames = _frames(source, 4)
    observations = tuple(_observation(source, frame, BOX) for frame in frames)

    for hostile in (cast(threading.Event, b"/private/raw.mov"), _HostileCancellation()):
        tracker = GlobalLastBoxTracker()
        with pytest.raises(PortError) as raised:
            tracker.track_page(
                source,
                frames,
                observations,
                discontinuities=_scores(frames),
                end_of_stream=True,
                cancelled=hostile,
            )
        assert raised.value.code is PortErrorCode.INVALID_REQUEST
        assert "/private/" not in str(raised.value)
        assert tracker.calls == ()

    overridden = threading.Event()
    object.__setattr__(
        overridden,
        "is_set",
        lambda: (_ for _ in ()).throw(OSError("/private/overridden.mov")),
    )
    assert (
        GlobalLastBoxTracker()
        .track_page(
            source,
            frames,
            observations,
            discontinuities=_scores(frames),
            end_of_stream=True,
            cancelled=overridden,
        )
        .finished
        is True
    )

    corrupted = threading.Event()
    object.__setattr__(corrupted, "_flag", "/private/corrupted.mov")
    with pytest.raises(PortError) as raised:
        GlobalLastBoxTracker().track_page(
            source,
            frames,
            observations,
            discontinuities=_scores(frames),
            end_of_stream=True,
            cancelled=corrupted,
        )
    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert "/private/" not in str(raised.value)

    tracker = GlobalLastBoxTracker()
    cancelled = threading.Event()
    original_check = GlobalLastBoxTracker._check_cancelled
    check_count = 0

    def cancel_during_second_frame(event: threading.Event | None, operation: str) -> None:
        nonlocal check_count
        check_count += 1
        if check_count == 4:
            assert event is not None
            event.set()
        original_check(event, operation)

    monkeypatch.setattr(
        GlobalLastBoxTracker,
        "_check_cancelled",
        staticmethod(cancel_during_second_frame),
    )

    with pytest.raises(PortError) as raised:
        tracker.track_page(
            source,
            frames,
            observations,
            discontinuities=_scores(frames),
            end_of_stream=True,
            cancelled=cancelled,
        )
    assert raised.value.code is PortErrorCode.CANCELLED
    assert tracker.calls == ()

    retry = tracker.track_page(
        source,
        frames,
        observations,
        discontinuities=_scores(frames),
        end_of_stream=True,
    )
    expected = GlobalLastBoxTracker().track(
        source,
        frames,
        observations,
        discontinuities=_scores(frames),
    )
    assert retry.tracklets == expected.tracklets


def test_production_adapter_reproduces_frozen_research_tracker_partitions() -> None:
    raw = cast(
        dict[str, Any],
        json.loads((ROOT / "fixtures/v02-tracking-research/results/raw-results.json").read_bytes()),
    )
    repetitions = cast(list[dict[str, Any]], raw["detection_repetitions"])
    sequences = cast(list[dict[str, Any]], repetitions[0]["representative_predictions"])
    assert len(sequences) == 10

    for sequence in sequences:
        sequence_id = cast(str, sequence["sequence_id"])
        frame_values = cast(list[dict[str, Any]], sequence["frames"])
        source = _source(
            digest=hashlib.sha256(sequence_id.encode()).hexdigest(),
            width=640_000,
            height=360_000,
        )
        frames = _frames(source, len(frame_values))
        observations: list[Observation] = []
        discontinuities: list[FrameDiscontinuity] = []
        reference = research.GeometryTracker(
            association="global",
            motion="last",
            iou_threshold_basis_points=1_000,
            max_missed_samples=5,
            width_milli=640_000,
            height_milli=360_000,
        )
        reference_membership: dict[tuple[int, tuple[int, int, int, int]], str] = {}
        for index, (frame, frame_value) in enumerate(zip(frames, frame_values, strict=True)):
            score = cast(int, frame_value["cut_score_basis_points"])
            boxes = tuple(
                cast(tuple[int, int, int, int], tuple(box))
                for box in cast(list[list[int]], frame_value["detections"])
            )
            discontinuities.append(FrameDiscontinuity.from_frame(frame, score))
            observations.extend(_observation(source, frame, box) for box in boxes)
            if score >= 1_500:
                reference.reset()
            for track_id, box in reference.update(boxes, cast(int, frame_value["pts_ms"])):
                reference_membership[(index, box)] = track_id
        reference.finish()

        result = GlobalLastBoxTracker().track(
            source,
            frames,
            tuple(observations),
            discontinuities=tuple(discontinuities),
        )
        production_membership: dict[tuple[int, tuple[int, int, int, int]], str] = {}
        frame_indexes = {frame.frame_id: index for index, frame in enumerate(frames)}
        for tracklet in result.tracklets:
            for point in tracklet.points:
                production_membership[(frame_indexes[point.frame_id], point.geometry.box_xyxy)] = (
                    tracklet.tracklet_id
                )

        assert set(production_membership) == set(reference_membership)
        production_to_reference: dict[str, str] = {}
        reference_to_production: dict[str, str] = {}
        for key, production_id in production_membership.items():
            reference_id = reference_membership[key]
            assert production_to_reference.setdefault(production_id, reference_id) == reference_id
            assert reference_to_production.setdefault(reference_id, production_id) == production_id
        terminations = {
            reason: sum(tracklet.termination_reason == reason for tracklet in result.tracklets)
            for reason in ("cut", "miss_timeout", "source_end")
        }
        assert terminations == reference.termination_counts
