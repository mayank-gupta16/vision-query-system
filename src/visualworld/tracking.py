# SPDX-License-Identifier: Apache-2.0
"""Deterministic clip-local tracking over pixel-free detector observations."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from typing import Protocol, runtime_checkable

from visualworld.ingestion import (
    Fingerprint,
    FrameRef,
    MediaTime,
    Producer,
    Source,
    SourceStream,
    TimeBase,
)
from visualworld.perception import (
    MAX_TRACK_POINTS,
    FrameDiscontinuity,
    Observation,
    Tracklet,
    TrackPoint,
)
from visualworld.ports import (
    MAX_PERCEPTION_OBSERVATIONS,
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    PerceptionResultState,
    PortCall,
    PortError,
    PortErrorCode,
    PortKind,
    Tracker,
    TrackingResult,
)

ASSOCIATION_IOU_BASIS_POINTS = 1_000
CUT_THRESHOLD_BASIS_POINTS = 1_500
MAX_MISSED_SAMPLES = 5
TRACKING_FPS = 5
MAX_RGB24_BYTES = 128 * 1024 * 1024

_SOURCE_ID_RE = re.compile(r"src_[0-9a-f]{64}\Z")
_CATEGORY_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_CURSOR_FACTORY = object()
_CONFIGURATION = {
    "association": "global_maximum_total_iou",
    "box_prediction": "last_detector_supported",
    "category": "vehicle",
    "cut_score": "mean_absolute_rgb_delta_every_fourth_pixel_basis_points",
    "cut_threshold_basis_points": CUT_THRESHOLD_BASIS_POINTS,
    "iou_threshold_basis_points": ASSOCIATION_IOU_BASIS_POINTS,
    "max_missed_samples": MAX_MISSED_SAMPLES,
    "sampling_fps": TRACKING_FPS,
    "schema": "visualworld.global-last-iou-tracker-configuration",
    "schema_version": 1,
    "tie_break": "active_ordinal_then_box_then_observation_id",
}
TRACKER_CONFIGURATION_SHA256 = hashlib.sha256(
    json.dumps(_CONFIGURATION, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
).hexdigest()
TRACKER_PRODUCER = Producer(
    "visualworld.global-last-iou-tracker",
    "1",
    TRACKER_CONFIGURATION_SHA256,
)


def _error(code: PortErrorCode, operation: str) -> PortError:
    return PortError(code, PortKind.TRACKER, operation)


def _time(value: MediaTime) -> Fraction:
    return Fraction(
        int(value.value) * int(value.time_base.numerator),
        int(value.time_base.denominator),
    )


def _validate_time_base(value: TimeBase) -> None:
    if (
        type(value) is not TimeBase
        or type(value.numerator) is not str
        or type(value.denominator) is not str
    ):
        raise ValueError("invalid time base")
    TimeBase.__post_init__(value)


def _validate_producer(value: Producer) -> None:
    if (
        type(value) is not Producer
        or type(value.name) is not str
        or type(value.version) is not str
        or type(value.configuration_sha256) is not str
    ):
        raise ValueError("invalid producer")
    Producer.__post_init__(value)


def _validate_media_time(value: MediaTime) -> None:
    if (
        type(value) is not MediaTime
        or type(value.value) is not str
        or type(value.basis) is not str
        or (value.estimate_method is not None and type(value.estimate_method) is not str)
        or (value.estimate_producer is not None and type(value.estimate_producer) is not Producer)
    ):
        raise ValueError("invalid media time")
    _validate_time_base(value.time_base)
    if value.estimate_producer is not None:
        _validate_producer(value.estimate_producer)
    MediaTime.__post_init__(value)


def _validate_source_stream(value: SourceStream) -> None:
    if type(value) is not SourceStream or type(value.media_type) is not str:
        raise ValueError("invalid source stream")
    _validate_time_base(value.time_base)
    SourceStream.__post_init__(value)


def _copy_source_stream(value: SourceStream) -> SourceStream:
    _validate_source_stream(value)
    return SourceStream(
        value.stream_index,
        value.width,
        value.height,
        value.rotation_degrees,
        TimeBase(value.time_base.numerator, value.time_base.denominator),
        value.media_type,
    )


def _validate_source(value: Source) -> None:
    if (
        type(value) is not Source
        or type(value.source_id) is not str
        or type(value.fingerprint) is not Fingerprint
        or type(value.fingerprint.digest) is not str
        or type(value.fingerprint.bytes) is not str
        or type(value.fingerprint.algorithm) is not str
        or type(value.streams) is not tuple
        or not all(type(stream) is SourceStream for stream in value.streams)
    ):
        raise ValueError("invalid source")
    Fingerprint.__post_init__(value.fingerprint)
    for stream in value.streams:
        _validate_source_stream(stream)
    Source.__post_init__(value)


def _validate_frame(value: FrameRef) -> None:
    if (
        type(value) is not FrameRef
        or type(value.frame_id) is not str
        or type(value.source_id) is not str
        or type(value.decode_index) is not str
        or type(value.pts) is not MediaTime
        or (value.duration is not None and type(value.duration) is not MediaTime)
        or (value.key_frame is not None and type(value.key_frame) is not bool)
    ):
        raise ValueError("invalid frame")
    _validate_media_time(value.pts)
    if value.duration is not None:
        _validate_media_time(value.duration)
    FrameRef.__post_init__(value)


def _copy_frame(value: FrameRef) -> FrameRef:
    _validate_frame(value)
    return FrameRef.from_mapping(value.to_mapping())


def _copy_track_point_from_observation(value: Observation) -> TrackPoint:
    if type(value) is not Observation:
        raise ValueError("invalid observation")
    Observation.__post_init__(value)
    owned_observation = Observation.from_mapping(value.to_mapping())
    return TrackPoint.from_observation(owned_observation)


def _copy_track_point(value: TrackPoint) -> TrackPoint:
    if type(value) is not TrackPoint:
        raise ValueError("invalid track point")
    TrackPoint.__post_init__(value)
    return TrackPoint.from_mapping(value.to_mapping())


def rgb24_discontinuity_basis_points(
    previous: bytes | None,
    current: bytes,
    *,
    width: int,
    height: int,
) -> int:
    """Return the frozen every-fourth-pixel mean absolute RGB delta."""

    if (
        type(current) is not bytes
        or (previous is not None and type(previous) is not bytes)
        or type(width) is not int
        or type(height) is not int
        or not 1 <= width <= 2**31 - 1
        or not 1 <= height <= 2**31 - 1
    ):
        raise ValueError("invalid_rgb24_frame")
    expected = width * height * 3
    if expected > MAX_RGB24_BYTES or len(current) != expected:
        raise ValueError("invalid_rgb24_frame")
    if previous is None:
        return 0
    if len(previous) != expected:
        raise ValueError("invalid_rgb24_frame")
    total = 0
    samples = 0
    for offset in range(0, expected, 12):
        total += abs(previous[offset] - current[offset])
        total += abs(previous[offset + 1] - current[offset + 1])
        total += abs(previous[offset + 2] - current[offset + 2])
        samples += 3
    return total * 10_000 // (samples * 255)


def _iou_basis_points(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> int:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    union = first_area + second_area - intersection
    return 0 if union <= 0 else intersection * 10_000 // union


def _maximum_assignment(weights: tuple[tuple[int, ...], ...]) -> tuple[tuple[int, int], ...]:
    """Return the exact deterministic maximum-weight rectangular assignment."""

    if not weights or not weights[0]:
        return ()
    column_count = len(weights[0])
    if (
        len(weights) > MAX_PORT_BATCH_ITEMS
        or column_count > MAX_PORT_BATCH_ITEMS
        or any(len(row) != column_count for row in weights)
        or any(
            type(value) is not int or not 0 <= value <= 10_000 for row in weights for value in row
        )
    ):
        raise ValueError("invalid_assignment")
    row_count = len(weights)
    transposed = row_count > column_count
    matrix = (
        tuple(
            tuple(weights[row][column] for row in range(row_count))
            for column in range(column_count)
        )
        if transposed
        else weights
    )
    rows = len(matrix)
    columns = len(matrix[0])
    maximum = max(max(row) for row in matrix)
    costs = tuple(tuple(maximum - value for value in row) for row in matrix)
    row_potential = [0] * (rows + 1)
    column_potential = [0] * (columns + 1)
    matched_row = [0] * (columns + 1)
    predecessor = [0] * (columns + 1)
    infinity = 10**18
    for row_index in range(1, rows + 1):
        matched_row[0] = row_index
        column_zero = 0
        minimums = [infinity] * (columns + 1)
        used = [False] * (columns + 1)
        while True:
            used[column_zero] = True
            current_row = matched_row[column_zero]
            delta = infinity
            next_column = 0
            for column_index in range(1, columns + 1):
                if used[column_index]:
                    continue
                current = (
                    costs[current_row - 1][column_index - 1]
                    - row_potential[current_row]
                    - column_potential[column_index]
                )
                if current < minimums[column_index]:
                    minimums[column_index] = current
                    predecessor[column_index] = column_zero
                if minimums[column_index] < delta:
                    delta = minimums[column_index]
                    next_column = column_index
            for column_index in range(columns + 1):
                if used[column_index]:
                    row_potential[matched_row[column_index]] += delta
                    column_potential[column_index] -= delta
                else:
                    minimums[column_index] -= delta
            column_zero = next_column
            if matched_row[column_zero] == 0:
                break
        while True:
            previous = predecessor[column_zero]
            matched_row[column_zero] = matched_row[previous]
            column_zero = previous
            if column_zero == 0:
                break
    pairs = tuple(
        (matched_row[column] - 1, column - 1)
        for column in range(1, columns + 1)
        if matched_row[column]
    )
    if transposed:
        pairs = tuple((column, row) for row, column in pairs)
    return tuple(sorted(pairs))


@dataclass(frozen=True, slots=True)
class TrackingLimits:
    """Fail-closed bounds for one-shot and resumable tracking."""

    max_page_frames: int = MAX_PORT_BATCH_ITEMS
    max_page_observations: int = MAX_PERCEPTION_OBSERVATIONS
    max_total_frames: int = 18_000
    max_total_observations: int = 100_000
    max_active_tracks: int = MAX_PORT_BATCH_ITEMS
    max_total_tracks: int = MAX_PORT_BATCH_ITEMS
    max_track_points: int = MAX_TRACK_POINTS
    max_duration_seconds: int = 60 * 60

    def __post_init__(self) -> None:
        values = (
            self.max_page_frames,
            self.max_page_observations,
            self.max_total_frames,
            self.max_total_observations,
            self.max_active_tracks,
            self.max_total_tracks,
            self.max_track_points,
            self.max_duration_seconds,
        )
        if any(type(value) is not int for value in values):
            raise ValueError("tracking limits are outside the v1 bounds")
        if (
            not 1 <= self.max_page_frames <= MAX_PORT_BATCH_ITEMS
            or not 1 <= self.max_page_observations <= MAX_PERCEPTION_OBSERVATIONS
            or not self.max_page_frames <= self.max_total_frames <= 100_000
            or not self.max_page_observations <= self.max_total_observations <= 1_000_000
            or not 1 <= self.max_active_tracks <= MAX_PORT_BATCH_ITEMS
            or not self.max_active_tracks <= self.max_total_tracks <= MAX_PORT_BATCH_ITEMS
            or not 1 <= self.max_track_points <= MAX_TRACK_POINTS
            or not 1 <= self.max_duration_seconds <= 24 * 60 * 60
        ):
            raise ValueError("tracking limits are outside the v1 bounds")


def _copy_tracking_limits(value: TrackingLimits) -> TrackingLimits:
    if type(value) is not TrackingLimits:
        raise ValueError("limits must use TrackingLimits")
    return TrackingLimits(
        max_page_frames=value.max_page_frames,
        max_page_observations=value.max_page_observations,
        max_total_frames=value.max_total_frames,
        max_total_observations=value.max_total_observations,
        max_active_tracks=value.max_active_tracks,
        max_total_tracks=value.max_total_tracks,
        max_track_points=value.max_track_points,
        max_duration_seconds=value.max_duration_seconds,
    )


@dataclass(frozen=True, slots=True)
class TrackingDiagnostics:
    """Bounded pixel-free counters for a tracking prefix."""

    frame_count: int
    observation_count: int
    created_tracks: int
    maximum_active_tracks: int
    detected_cut_frames: int
    cut_terminations: int
    miss_timeout_terminations: int
    source_end_terminations: int

    def __post_init__(self) -> None:
        values = (
            self.frame_count,
            self.observation_count,
            self.created_tracks,
            self.maximum_active_tracks,
            self.detected_cut_frames,
            self.cut_terminations,
            self.miss_timeout_terminations,
            self.source_end_terminations,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("tracking diagnostics are invalid")


@dataclass(frozen=True, slots=True)
class _ActiveTrack:
    ordinal: int
    points: tuple[TrackPoint, ...]
    missed_samples: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.ordinal) is not int
            or self.ordinal < 1
            or type(self.points) is not tuple
            or not self.points
            or len(self.points) > MAX_TRACK_POINTS
            or not all(type(point) is TrackPoint for point in self.points)
            or type(self.missed_samples) is not int
            or not 0 <= self.missed_samples <= MAX_MISSED_SAMPLES
        ):
            raise ValueError("active track is invalid")
        for point in self.points:
            TrackPoint.__post_init__(point)


def _copy_active_track(value: _ActiveTrack) -> _ActiveTrack:
    if type(value) is not _ActiveTrack:
        raise ValueError("invalid active track")
    value.__post_init__()
    return _ActiveTrack(
        value.ordinal,
        tuple(_copy_track_point(point) for point in value.points),
        value.missed_samples,
    )


def _copy_tracking_diagnostics(value: TrackingDiagnostics) -> TrackingDiagnostics:
    if type(value) is not TrackingDiagnostics:
        raise ValueError("invalid tracking diagnostics")
    return TrackingDiagnostics(
        value.frame_count,
        value.observation_count,
        value.created_tracks,
        value.maximum_active_tracks,
        value.detected_cut_frames,
        value.cut_terminations,
        value.miss_timeout_terminations,
        value.source_end_terminations,
    )


def _cursor_integrity_sha256(cursor: TrackingCursor) -> str:
    payload = {
        "active_tracks": [
            {
                "missed_samples": track.missed_samples,
                "ordinal": track.ordinal,
                "points": [point.to_mapping() for point in track.points],
            }
            for track in cursor.active_tracks
        ],
        "diagnostics": {
            "created_tracks": cursor.diagnostics.created_tracks,
            "cut_terminations": cursor.diagnostics.cut_terminations,
            "detected_cut_frames": cursor.diagnostics.detected_cut_frames,
            "frame_count": cursor.diagnostics.frame_count,
            "maximum_active_tracks": cursor.diagnostics.maximum_active_tracks,
            "miss_timeout_terminations": cursor.diagnostics.miss_timeout_terminations,
            "observation_count": cursor.diagnostics.observation_count,
            "source_end_terminations": cursor.diagnostics.source_end_terminations,
        },
        "finished": cursor.finished,
        "first_pts": cursor.first_pts.to_mapping(),
        "last_frame": cursor.last_frame.to_mapping(),
        "limits": {
            "max_active_tracks": cursor.limits.max_active_tracks,
            "max_duration_seconds": cursor.limits.max_duration_seconds,
            "max_page_frames": cursor.limits.max_page_frames,
            "max_page_observations": cursor.limits.max_page_observations,
            "max_total_frames": cursor.limits.max_total_frames,
            "max_total_observations": cursor.limits.max_total_observations,
            "max_total_tracks": cursor.limits.max_total_tracks,
            "max_track_points": cursor.limits.max_track_points,
        },
        "next_ordinal": cursor.next_ordinal,
        "source_id": cursor.source_id,
        "source_stream": cursor.source_stream.to_mapping(),
        "stream_index": cursor.stream_index,
        "time_base": cursor.time_base.to_mapping(),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class TrackingCursor:
    """Opaque immutable continuation state for an unfinished clip prefix."""

    source_id: str
    stream_index: int
    time_base: TimeBase
    source_stream: SourceStream
    limits: TrackingLimits
    first_pts: MediaTime
    last_frame: FrameRef
    active_tracks: tuple[_ActiveTrack, ...]
    next_ordinal: int
    diagnostics: TrackingDiagnostics
    _integrity_sha256: str
    finished: bool = False

    def __init__(
        self,
        *,
        _factory: object,
        source_id: str,
        stream_index: int,
        time_base: TimeBase,
        source_stream: SourceStream,
        limits: TrackingLimits,
        first_pts: MediaTime,
        last_frame: FrameRef,
        active_tracks: tuple[_ActiveTrack, ...],
        next_ordinal: int,
        diagnostics: TrackingDiagnostics,
        finished: bool = False,
    ) -> None:
        if _factory is not _CURSOR_FACTORY:
            raise TypeError("TrackingCursor values are issued only by GlobalLastBoxTracker")
        for name, value in (
            ("source_id", source_id),
            ("stream_index", stream_index),
            ("time_base", time_base),
            ("source_stream", source_stream),
            ("limits", limits),
            ("first_pts", first_pts),
            ("last_frame", last_frame),
            ("active_tracks", active_tracks),
            ("next_ordinal", next_ordinal),
            ("diagnostics", diagnostics),
            ("finished", finished),
        ):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_integrity_sha256", _cursor_integrity_sha256(self))
        self.__post_init__()

    def __post_init__(self) -> None:
        if (
            type(self.source_id) is not str
            or not _SOURCE_ID_RE.fullmatch(self.source_id)
            or type(self.stream_index) is not int
            or not 0 <= self.stream_index <= 2**31 - 1
            or type(self.time_base) is not TimeBase
            or type(self.source_stream) is not SourceStream
            or type(self.limits) is not TrackingLimits
            or type(self.first_pts) is not MediaTime
            or type(self.last_frame) is not FrameRef
            or type(self.active_tracks) is not tuple
            or not all(type(track) is _ActiveTrack for track in self.active_tracks)
            or type(self.next_ordinal) is not int
            or type(self.diagnostics) is not TrackingDiagnostics
            or type(self.finished) is not bool
            or type(self._integrity_sha256) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", self._integrity_sha256)
        ):
            raise ValueError("tracking cursor is invalid")
        _validate_time_base(self.time_base)
        _validate_source_stream(self.source_stream)
        _validate_media_time(self.first_pts)
        _validate_frame(self.last_frame)
        self.limits.__post_init__()
        self.diagnostics.__post_init__()
        for track in self.active_tracks:
            track.__post_init__()
        ordinals = [track.ordinal for track in self.active_tracks]
        termination_count = (
            self.diagnostics.cut_terminations
            + self.diagnostics.miss_timeout_terminations
            + self.diagnostics.source_end_terminations
        )
        if (
            self.last_frame.source_id != self.source_id
            or self.last_frame.stream_index != self.stream_index
            or self.last_frame.pts.time_base != self.time_base
            or self.source_stream.stream_index != self.stream_index
            or self.source_stream.time_base != self.time_base
            or self.first_pts.time_base != self.time_base
            or len(ordinals) != len(set(ordinals))
            or len(self.active_tracks) > self.limits.max_active_tracks
            or self.next_ordinal != self.diagnostics.created_tracks + 1
            or self.diagnostics.maximum_active_tracks > self.limits.max_active_tracks
            or self.diagnostics.maximum_active_tracks < len(self.active_tracks)
            or self.diagnostics.maximum_active_tracks > self.diagnostics.created_tracks
            or self.diagnostics.created_tracks > self.limits.max_total_tracks
            or self.diagnostics.created_tracks > self.diagnostics.observation_count
            or self.diagnostics.frame_count < 1
            or self.diagnostics.frame_count > self.limits.max_total_frames
            or self.diagnostics.observation_count > self.limits.max_total_observations
            or self.diagnostics.detected_cut_frames > self.diagnostics.frame_count
            or self.diagnostics.created_tracks != len(self.active_tracks) + termination_count
            or sum(len(track.points) for track in self.active_tracks)
            > self.diagnostics.observation_count
            or (
                _time(self.last_frame.pts) - _time(self.first_pts)
                != Fraction(self.diagnostics.frame_count - 1, TRACKING_FPS)
            )
            or (
                _time(self.last_frame.pts) - _time(self.first_pts)
                > self.limits.max_duration_seconds
            )
            or (self.finished and self.active_tracks)
            or (not self.finished and self.diagnostics.source_end_terminations)
        ):
            raise ValueError("tracking cursor conflicts with its bound prefix")
        observation_ids: set[str] = set()
        for track in self.active_tracks:
            point_ids = [point.observation_id for point in track.points]
            if (
                track.ordinal >= self.next_ordinal
                or len(track.points) > self.limits.max_track_points
                or track.points[-1].category != "vehicle"
                or len(point_ids) != len(set(point_ids))
                or observation_ids.intersection(point_ids)
                or any(
                    _time(current.pts) <= _time(previous.pts)
                    for previous, current in pairwise(track.points)
                )
                or (
                    _time(self.last_frame.pts) - _time(track.points[-1].pts)
                    != Fraction(track.missed_samples, TRACKING_FPS)
                )
                or any(
                    point.source_id != self.source_id
                    or point.stream_index != self.stream_index
                    or point.pts.time_base != self.time_base
                    or point.geometry.source_width != self.source_stream.width
                    or point.geometry.source_height != self.source_stream.height
                    for point in track.points
                )
            ):
                raise ValueError("tracking cursor conflicts with its active tracks")
            observation_ids.update(point_ids)
        if self._integrity_sha256 != _cursor_integrity_sha256(self):
            raise ValueError("tracking cursor integrity check failed")


@dataclass(frozen=True, slots=True)
class TrackingPage:
    """Completed tracklets and continuation state for one bounded page."""

    state: PerceptionResultState
    tracklets: tuple[Tracklet, ...]
    cursor: TrackingCursor | None
    finished: bool
    diagnostics: TrackingDiagnostics
    reason: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.state) is not PerceptionResultState
            or type(self.tracklets) is not tuple
            or len(self.tracklets) > MAX_PORT_BATCH_ITEMS
            or not all(type(tracklet) is Tracklet for tracklet in self.tracklets)
            or type(self.finished) is not bool
            or type(self.diagnostics) is not TrackingDiagnostics
        ):
            raise ValueError("tracking page is invalid")
        for tracklet in self.tracklets:
            Tracklet.__post_init__(tracklet)
        tracklet_ids = [tracklet.tracklet_id for tracklet in self.tracklets]
        if len(tracklet_ids) != len(set(tracklet_ids)):
            raise ValueError("tracking page contains duplicate tracklets")
        self.diagnostics.__post_init__()
        if self.state is PerceptionResultState.COMPLETE:
            if self.reason is not None:
                raise ValueError("complete tracking page cannot have a reason")
            if self.cursor is None:
                if self.tracklets or not self.finished:
                    raise ValueError("tracking page cursor is missing")
            elif type(self.cursor) is not TrackingCursor:
                raise ValueError("tracking page cursor is invalid")
            else:
                self.cursor.__post_init__()
                if (
                    self.cursor.finished != self.finished
                    or self.cursor.diagnostics != self.diagnostics
                ):
                    raise ValueError("tracking page cursor is inconsistent")
        elif (
            self.tracklets
            or self.cursor is not None
            or not self.finished
            or type(self.reason) is not str
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.reason)
        ):
            raise ValueError("incomplete tracking page is invalid")


@runtime_checkable
class ResumableTracker(Tracker, Protocol):
    def track_page(
        self,
        source: Source,
        frames: tuple[FrameRef, ...],
        observations: tuple[Observation, ...],
        *,
        discontinuities: tuple[FrameDiscontinuity, ...],
        cursor: TrackingCursor | None = None,
        end_of_stream: bool = False,
        cancelled: threading.Event | None = None,
    ) -> TrackingPage: ...


class GlobalLastBoxTracker:
    """Frozen global-IoU vehicle tracker selected by the v0.2 benchmark."""

    def __init__(
        self,
        *,
        requested_category: str = "vehicle",
        limits: TrackingLimits | None = None,
    ) -> None:
        if type(requested_category) is not str or not _CATEGORY_RE.fullmatch(requested_category):
            raise ValueError("requested category is invalid")
        supplied_limits = TrackingLimits() if limits is None else limits
        selected_limits = _copy_tracking_limits(supplied_limits)
        self._requested_category = requested_category
        self._limits = selected_limits
        self._calls: list[PortCall] = []
        self._descriptor = CapabilityDescriptor(
            PortKind.TRACKER,
            "visualworld.global-last-iou-tracker",
            "1",
            deterministic=True,
            offline=True,
            max_batch_items=selected_limits.max_page_frames,
        )

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    @property
    def calls(self) -> tuple[PortCall, ...]:
        return tuple(self._calls)

    @property
    def producer(self) -> Producer:
        return TRACKER_PRODUCER

    @staticmethod
    def _check_cancelled(cancelled: threading.Event | None, operation: str) -> None:
        if cancelled is None:
            return
        if type(cancelled) is not threading.Event:
            raise _error(PortErrorCode.INVALID_REQUEST, operation)
        try:
            state = threading.Event.is_set(cancelled)
        except Exception:
            raise _error(PortErrorCode.INVALID_REQUEST, operation) from None
        if type(state) is not bool:
            raise _error(PortErrorCode.INVALID_REQUEST, operation)
        if state:
            raise _error(PortErrorCode.CANCELLED, operation)

    def track(
        self,
        source: Source,
        frames: tuple[FrameRef, ...],
        observations: tuple[Observation, ...],
        *,
        discontinuities: tuple[FrameDiscontinuity, ...] | None = None,
    ) -> TrackingResult:
        try:
            page = self._track_page(
                source,
                frames,
                observations,
                discontinuities=discontinuities,
                cursor=None,
                end_of_stream=True,
                cancelled=None,
                operation="track",
            )
            result = TrackingResult(page.state, page.tracklets, page.reason)
        except (TypeError, ValueError):
            raise _error(PortErrorCode.INVALID_REQUEST, "track") from None
        self._calls.append(PortCall(PortKind.TRACKER, "track", len(frames)))
        return result

    def track_page(
        self,
        source: Source,
        frames: tuple[FrameRef, ...],
        observations: tuple[Observation, ...],
        *,
        discontinuities: tuple[FrameDiscontinuity, ...],
        cursor: TrackingCursor | None = None,
        end_of_stream: bool = False,
        cancelled: threading.Event | None = None,
    ) -> TrackingPage:
        try:
            page = self._track_page(
                source,
                frames,
                observations,
                discontinuities=discontinuities,
                cursor=cursor,
                end_of_stream=end_of_stream,
                cancelled=cancelled,
                operation="track_page",
            )
        except (TypeError, ValueError):
            raise _error(PortErrorCode.INVALID_REQUEST, "track_page") from None
        self._calls.append(PortCall(PortKind.TRACKER, "track_page", len(frames)))
        return page

    def _track_page(
        self,
        source: Source,
        frames: tuple[FrameRef, ...],
        observations: tuple[Observation, ...],
        *,
        discontinuities: tuple[FrameDiscontinuity, ...] | None,
        cursor: TrackingCursor | None,
        end_of_stream: bool,
        cancelled: threading.Event | None,
        operation: str,
    ) -> TrackingPage:
        self._check_cancelled(cancelled, operation)
        validated = self._validate_inputs(
            source,
            frames,
            observations,
            discontinuities,
            cursor,
            end_of_stream,
            cancelled,
            operation,
        )
        by_frame, selected_discontinuities, rate_supported, selected_stream = validated
        base_diagnostics = (
            _copy_tracking_diagnostics(cursor.diagnostics)
            if cursor is not None
            else TrackingDiagnostics(0, 0, 0, 0, 0, 0, 0, 0)
        )
        if self._requested_category != "vehicle":
            return TrackingPage(
                PerceptionResultState.UNSUPPORTED,
                (),
                None,
                True,
                base_diagnostics,
                "category_unsupported",
            )
        if not rate_supported:
            return TrackingPage(
                PerceptionResultState.UNSUPPORTED,
                (),
                None,
                True,
                base_diagnostics,
                "sampling_rate_unsupported",
            )
        if discontinuities is None and frames:
            return TrackingPage(
                PerceptionResultState.UNKNOWN,
                (),
                None,
                True,
                base_diagnostics,
                "cut_scores_unavailable",
            )

        active = {
            track.ordinal: _copy_active_track(track)
            for track in (() if cursor is None else cursor.active_tracks)
        }
        next_ordinal = 1 if cursor is None else cursor.next_ordinal
        created_tracks = base_diagnostics.created_tracks
        maximum_active = base_diagnostics.maximum_active_tracks
        cut_frames = base_diagnostics.detected_cut_frames
        cut_terminations = base_diagnostics.cut_terminations
        miss_terminations = base_diagnostics.miss_timeout_terminations
        source_terminations = base_diagnostics.source_end_terminations
        completed: list[Tracklet] = []

        for frame, discontinuity in zip(frames, selected_discontinuities, strict=True):
            self._check_cancelled(cancelled, operation)
            if discontinuity.score_basis_points >= CUT_THRESHOLD_BASIS_POINTS:
                cut_frames += 1
                for ordinal in sorted(active):
                    completed.append(self._complete(active[ordinal], "cut"))
                cut_terminations += len(active)
                active.clear()

            detections = tuple(
                sorted(
                    by_frame[frame.frame_id],
                    key=lambda item: (item.geometry.box_xyxy, item.observation_id),
                )
            )
            active_rows = tuple(active[ordinal] for ordinal in sorted(active))
            weights = tuple(
                tuple(
                    _iou_basis_points(
                        track.points[-1].geometry.box_xyxy,
                        observation.geometry.box_xyxy,
                    )
                    for observation in detections
                )
                for track in active_rows
            )
            pairs = (
                tuple(
                    (row, column)
                    for row, column in _maximum_assignment(weights)
                    if weights[row][column] >= ASSOCIATION_IOU_BASIS_POINTS
                )
                if weights and detections
                else ()
            )
            matched_ordinals: set[int] = set()
            matched_detections: set[int] = set()
            for row, column in pairs:
                track = active_rows[row]
                if len(track.points) >= self._limits.max_track_points:
                    raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
                active[track.ordinal] = _ActiveTrack(
                    track.ordinal,
                    (*track.points, _copy_track_point_from_observation(detections[column])),
                )
                matched_ordinals.add(track.ordinal)
                matched_detections.add(column)

            for ordinal in sorted(tuple(active)):
                if ordinal in matched_ordinals:
                    continue
                track = active[ordinal]
                missed = track.missed_samples + 1
                if missed > MAX_MISSED_SAMPLES:
                    completed.append(self._complete(track, "miss_timeout"))
                    miss_terminations += 1
                    del active[ordinal]
                else:
                    active[ordinal] = _ActiveTrack(ordinal, track.points, missed)

            for column, observation in enumerate(detections):
                if column in matched_detections:
                    continue
                if (
                    created_tracks >= self._limits.max_total_tracks
                    or len(active) >= self._limits.max_active_tracks
                ):
                    raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
                active[next_ordinal] = _ActiveTrack(
                    next_ordinal,
                    (_copy_track_point_from_observation(observation),),
                )
                next_ordinal += 1
                created_tracks += 1
            maximum_active = max(maximum_active, len(active))

        if end_of_stream:
            for ordinal in sorted(active):
                completed.append(self._complete(active[ordinal], "source_end"))
            source_terminations += len(active)
            active.clear()

        diagnostics = TrackingDiagnostics(
            base_diagnostics.frame_count + len(frames),
            base_diagnostics.observation_count + len(observations),
            created_tracks,
            maximum_active,
            cut_frames,
            cut_terminations,
            miss_terminations,
            source_terminations,
        )
        if len(completed) > MAX_PORT_BATCH_ITEMS:
            raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
        if not frames and cursor is None:
            return TrackingPage(
                PerceptionResultState.COMPLETE,
                (),
                None,
                True,
                diagnostics,
            )
        if frames:
            last_frame = frames[-1]
        elif cursor is not None:
            last_frame = cursor.last_frame
        else:
            raise AssertionError("validated tracking page has no position")
        if selected_stream is None:
            raise AssertionError("validated tracking page has no source stream")
        first_pts = frames[0].pts if cursor is None else cursor.first_pts
        cursor_stream = _copy_source_stream(selected_stream)
        next_cursor = TrackingCursor(
            _factory=_CURSOR_FACTORY,
            source_id=source.source_id,
            stream_index=last_frame.stream_index,
            time_base=cursor_stream.time_base,
            source_stream=cursor_stream,
            limits=_copy_tracking_limits(self._limits),
            first_pts=MediaTime.from_mapping(first_pts.to_mapping()),
            last_frame=_copy_frame(last_frame),
            active_tracks=tuple(_copy_active_track(active[ordinal]) for ordinal in sorted(active)),
            next_ordinal=next_ordinal,
            diagnostics=diagnostics,
            finished=end_of_stream,
        )
        return TrackingPage(
            PerceptionResultState.COMPLETE,
            tuple(completed),
            next_cursor,
            end_of_stream,
            diagnostics,
        )

    def _validate_inputs(
        self,
        source: Source,
        frames: tuple[FrameRef, ...],
        observations: tuple[Observation, ...],
        discontinuities: tuple[FrameDiscontinuity, ...] | None,
        cursor: TrackingCursor | None,
        end_of_stream: bool,
        cancelled: threading.Event | None,
        operation: str,
    ) -> tuple[
        dict[str, tuple[Observation, ...]],
        tuple[FrameDiscontinuity, ...],
        bool,
        SourceStream | None,
    ]:
        if (
            type(source) is not Source
            or type(frames) is not tuple
            or type(observations) is not tuple
            or (discontinuities is not None and type(discontinuities) is not tuple)
            or (cursor is not None and type(cursor) is not TrackingCursor)
            or type(end_of_stream) is not bool
            or (cancelled is not None and type(cancelled) is not threading.Event)
        ):
            raise _error(PortErrorCode.INVALID_REQUEST, operation)
        if (
            len(frames) > self._limits.max_page_frames
            or len(observations) > self._limits.max_page_observations
        ):
            raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
        if not frames:
            if (
                observations
                or (discontinuities is not None and discontinuities != ())
                or not end_of_stream
            ):
                raise _error(PortErrorCode.INVALID_REQUEST, operation)
        elif discontinuities is not None and len(discontinuities) != len(frames):
            raise _error(PortErrorCode.INVALID_REQUEST, operation)
        try:
            _validate_source(source)
            for frame in frames:
                _validate_frame(frame)
            for observation in observations:
                if type(observation) is not Observation:
                    raise ValueError
                Observation.__post_init__(observation)
            for item in () if discontinuities is None else discontinuities:
                if type(item) is not FrameDiscontinuity:
                    raise ValueError
                FrameDiscontinuity.__post_init__(item)
            if cursor is not None:
                cursor.__post_init__()
        except (TypeError, ValueError):
            raise _error(PortErrorCode.INVALID_REQUEST, operation) from None
        if cursor is not None and (
            cursor.finished or cursor.source_id != source.source_id or cursor.limits != self._limits
        ):
            raise _error(PortErrorCode.CONFLICT, operation)

        streams = {source_stream.stream_index: source_stream for source_stream in source.streams}
        frame_ids = {frame.frame_id for frame in frames}
        positions = {(frame.stream_index, frame.decode_index) for frame in frames}
        if len(frame_ids) != len(frames) or len(positions) != len(frames):
            raise _error(PortErrorCode.CONFLICT, operation)
        stream: SourceStream | None
        if frames:
            stream_index = frames[0].stream_index
            stream = streams.get(stream_index)
            if stream is None or any(
                frame.source_id != source.source_id
                or frame.stream_index != stream_index
                or frame.pts.time_base != stream.time_base
                or (frame.duration is not None and frame.duration.time_base != stream.time_base)
                for frame in frames
            ):
                raise _error(PortErrorCode.CONFLICT, operation)
            if cursor is not None and (
                cursor.stream_index != stream_index or cursor.source_stream != stream
            ):
                raise _error(PortErrorCode.CONFLICT, operation)
        elif cursor is not None:
            stream_index = cursor.stream_index
            stream = streams.get(stream_index)
            if stream is None or stream != cursor.source_stream:
                raise _error(PortErrorCode.CONFLICT, operation)
        else:
            stream_index = 0
            stream = None

        ordered_frames = (() if cursor is None else (cursor.last_frame,)) + frames
        for previous, current in pairwise(ordered_frames):
            if _time(current.pts) <= _time(previous.pts) or int(current.decode_index) <= int(
                previous.decode_index
            ):
                raise _error(PortErrorCode.CONFLICT, operation)
        rate_supported = all(
            _time(current.pts) - _time(previous.pts) == Fraction(1, TRACKING_FPS)
            for previous, current in pairwise(ordered_frames)
        )

        selected_discontinuities = () if discontinuities is None else discontinuities
        if selected_discontinuities:
            if cursor is None and selected_discontinuities[0].score_basis_points != 0:
                raise _error(PortErrorCode.CONFLICT, operation)
            if any(
                item.source_id != frame.source_id
                or item.frame_id != frame.frame_id
                or item.stream_index != frame.stream_index
                or item.pts != frame.pts
                for item, frame in zip(selected_discontinuities, frames, strict=True)
            ):
                raise _error(PortErrorCode.CONFLICT, operation)

        by_frame_lists: dict[str, list[Observation]] = {frame.frame_id: [] for frame in frames}
        observation_ids: set[str] = set()
        for observation in observations:
            observation_frame = next(
                (candidate for candidate in frames if candidate.frame_id == observation.frame_id),
                None,
            )
            if (
                observation_frame is None
                or observation.observation_id in observation_ids
                or observation.source_id != source.source_id
                or observation.stream_index != stream_index
                or observation.pts != observation_frame.pts
                or stream is None
                or observation.geometry.source_width != stream.width
                or observation.geometry.source_height != stream.height
            ):
                raise _error(PortErrorCode.CONFLICT, operation)
            observation_ids.add(observation.observation_id)
            by_frame_lists[observation_frame.frame_id].append(observation)
        if any(len(items) > self._limits.max_active_tracks for items in by_frame_lists.values()):
            raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)

        prior_frames = 0 if cursor is None else cursor.diagnostics.frame_count
        prior_observations = 0 if cursor is None else cursor.diagnostics.observation_count
        if (
            prior_frames + len(frames) > self._limits.max_total_frames
            or prior_observations + len(observations) > self._limits.max_total_observations
        ):
            raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
        if frames:
            first_pts = frames[0].pts if cursor is None else cursor.first_pts
            if _time(frames[-1].pts) - _time(first_pts) > self._limits.max_duration_seconds:
                raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
        self._check_cancelled(cancelled, operation)
        return (
            {frame_id: tuple(items) for frame_id, items in by_frame_lists.items()},
            selected_discontinuities,
            rate_supported,
            stream,
        )

    @staticmethod
    def _complete(track: _ActiveTrack, reason: str) -> Tracklet:
        first = track.points[0]
        return Tracklet.create(
            first.source_id,
            first.stream_index,
            first.category,
            track.points,
            reason,
            TRACKER_PRODUCER,
        )


__all__ = [
    "ASSOCIATION_IOU_BASIS_POINTS",
    "CUT_THRESHOLD_BASIS_POINTS",
    "MAX_MISSED_SAMPLES",
    "MAX_RGB24_BYTES",
    "TRACKER_CONFIGURATION_SHA256",
    "TRACKER_PRODUCER",
    "TRACKING_FPS",
    "GlobalLastBoxTracker",
    "ResumableTracker",
    "TrackingCursor",
    "TrackingDiagnostics",
    "TrackingLimits",
    "TrackingPage",
    "rgb24_discontinuity_basis_points",
]
