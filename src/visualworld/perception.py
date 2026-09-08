# SPDX-License-Identifier: Apache-2.0
"""Version-1, vendor-neutral observation and clip-local tracklet records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO, ClassVar

from visualworld.ingestion import (
    MAX_I31,
    MAX_RECORD_BYTES,
    AffineCoefficients,
    FrameRef,
    Geometry,
    MediaTime,
    Producer,
    ProducerSpace,
    Rational,
    TimeBase,
    _array,
    _bounded_int,
    _canonical_bytes,
    _exact_fields,
    _fail,
    _general_string,
    _identifier,
    _parse_json,
    _record_fields,
    _token,
    _typed_id,
    compare_media_time,
)

MAX_TRACK_POINTS = 64
MAX_CONFIDENCE_MILLIONTHS = 1_000_000
MAX_DISCONTINUITY_BASIS_POINTS = 10_000
_CATEGORIES = frozenset({"vehicle"})
_TERMINATION_REASONS = frozenset({"cut", "miss_timeout", "source_end"})


def _plain_string(value: object, path: str) -> str:
    if type(value) is not str:
        _fail("expected_string", path)
    return _general_string(value, path)


def _plain_time_base(value: object, path: str) -> TimeBase:
    if type(value) is not TimeBase:
        _fail("invalid_time_base", path)
    _plain_string(value.numerator, f"{path}.numerator")
    _plain_string(value.denominator, f"{path}.denominator")
    TimeBase.__post_init__(value)
    return value


def _plain_producer(value: object, path: str) -> Producer:
    if type(value) is not Producer:
        _fail("invalid_producer", path)
    _plain_string(value.name, f"{path}.name")
    _plain_string(value.version, f"{path}.version")
    _plain_string(value.configuration_sha256, f"{path}.configuration_sha256")
    Producer.__post_init__(value)
    return value


def _plain_media_time(value: object, path: str) -> MediaTime:
    if type(value) is not MediaTime:
        _fail("invalid_pts", path)
    _plain_string(value.value, f"{path}.value")
    _plain_string(value.basis, f"{path}.basis")
    _plain_time_base(value.time_base, f"{path}.time_base")
    if value.estimate_method is not None:
        _plain_string(value.estimate_method, f"{path}.estimate.method")
    if value.estimate_producer is not None:
        _plain_producer(value.estimate_producer, f"{path}.estimate.producer")
    MediaTime.__post_init__(value)
    return value


def _plain_frame_ref(value: object, path: str) -> FrameRef:
    if type(value) is not FrameRef:
        _fail("invalid_frame", path)
    _plain_string(value.frame_id, f"{path}.frame_id")
    _plain_string(value.source_id, f"{path}.source_id")
    _plain_string(value.decode_index, f"{path}.decode_index")
    _bounded_int(value.stream_index, 0, MAX_I31, f"{path}.stream_index")
    _plain_media_time(value.pts, f"{path}.pts")
    if value.duration is not None:
        _plain_media_time(value.duration, f"{path}.duration")
    if value.key_frame is not None and type(value.key_frame) is not bool:
        _fail("invalid_key_frame", f"{path}.key_frame")
    FrameRef.__post_init__(value)
    return value


def _plain_rational(value: object, path: str) -> Rational:
    if type(value) is not Rational:
        _fail("invalid_affine_coefficient", path)
    _plain_string(value.numerator, f"{path}.numerator")
    _plain_string(value.denominator, f"{path}.denominator")
    Rational.__post_init__(value)
    return value


def _plain_geometry(value: object, path: str) -> Geometry:
    if type(value) is not Geometry:
        _fail("invalid_geometry", path)
    if type(value.box_xyxy) is not tuple:
        _fail("invalid_box", f"{path}.box_xyxy")
    _plain_string(value.measurement, f"{path}.measurement")
    _plain_string(value.transform_kind, f"{path}.transform_to_source.kind")
    _plain_string(value.space, f"{path}.space")
    if value.producer_space is not None:
        if type(value.producer_space) is not ProducerSpace:
            _fail("invalid_producer_space", f"{path}.producer_space")
        if type(value.producer_space.box_xyxy) is not tuple:
            _fail("invalid_box", f"{path}.producer_space.box_xyxy")
        ProducerSpace.__post_init__(value.producer_space)
    if value.coefficients is not None:
        if type(value.coefficients) is not AffineCoefficients:
            _fail("invalid_affine_coefficients", f"{path}.transform_to_source.coefficients")
        for name in ("a", "b", "c", "d", "e", "f"):
            _plain_rational(
                getattr(value.coefficients, name),
                f"{path}.transform_to_source.coefficients.{name}",
            )
        AffineCoefficients.__post_init__(value.coefficients)
    Geometry.__post_init__(value)
    return value


class _PerceptionRecord:
    schema: ClassVar[str]
    schema_version: ClassVar[int] = 1
    identity_version: ClassVar[int] = 1

    def to_mapping(self) -> dict[str, object]:
        raise NotImplementedError

    def identity_projection(self) -> dict[str, object]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class FrameDiscontinuity:
    """Pixel-free image-change score bound to one exact sampled frame."""

    source_id: str
    frame_id: str
    stream_index: int
    pts: MediaTime
    score_basis_points: int

    def __post_init__(self) -> None:
        _plain_string(self.source_id, "frame_discontinuity.source_id")
        _plain_string(self.frame_id, "frame_discontinuity.frame_id")
        _typed_id(self.source_id, "src", "frame_discontinuity.source_id")
        _typed_id(self.frame_id, "frm", "frame_discontinuity.frame_id")
        _bounded_int(
            self.stream_index,
            0,
            MAX_I31,
            "frame_discontinuity.stream_index",
        )
        _plain_media_time(self.pts, "frame_discontinuity.pts")
        _bounded_int(
            self.score_basis_points,
            0,
            MAX_DISCONTINUITY_BASIS_POINTS,
            "frame_discontinuity.score_basis_points",
        )

    @classmethod
    def from_frame(cls, frame: FrameRef, score_basis_points: int) -> FrameDiscontinuity:
        _plain_frame_ref(frame, "frame")
        return cls(
            frame.source_id,
            frame.frame_id,
            frame.stream_index,
            frame.pts,
            score_basis_points,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "frame_id": self.frame_id,
            "pts": self.pts.to_mapping(),
            "score_basis_points": self.score_basis_points,
            "source_id": self.source_id,
            "stream_index": self.stream_index,
        }


@dataclass(frozen=True, slots=True)
class Observation(_PerceptionRecord):
    """One detector-supported object-like claim at an exact source time."""

    observation_id: str
    source_id: str
    frame_id: str
    stream_index: int
    pts: MediaTime
    geometry: Geometry
    category: str
    confidence_millionths: int
    producer: Producer

    schema: ClassVar[str] = "visualworld.observation"

    def __post_init__(self) -> None:
        _plain_string(self.observation_id, "observation_id")
        _plain_string(self.source_id, "source_id")
        _plain_string(self.frame_id, "frame_id")
        _typed_id(self.observation_id, "obs", "observation_id")
        _typed_id(self.source_id, "src", "source_id")
        _typed_id(self.frame_id, "frm", "frame_id")
        _bounded_int(self.stream_index, 0, MAX_I31, "stream_index")
        _plain_media_time(self.pts, "pts")
        _plain_geometry(self.geometry, "geometry")
        _plain_string(self.category, "category")
        _token(self.category, "category", _CATEGORIES)
        _bounded_int(
            self.confidence_millionths,
            0,
            MAX_CONFIDENCE_MILLIONTHS,
            "confidence_millionths",
        )
        _plain_producer(self.producer, "producer")
        if self.observation_id != _identifier("obs", self.identity_projection()):
            _fail("identifier_mismatch", "observation_id")

    @classmethod
    def create(
        cls,
        source_id: str,
        frame_id: str,
        stream_index: int,
        pts: MediaTime,
        geometry: Geometry,
        category: str,
        confidence_millionths: int,
        producer: Producer,
    ) -> Observation:
        _plain_string(source_id, "source_id")
        _plain_string(frame_id, "frame_id")
        _plain_string(category, "category")
        _plain_media_time(pts, "pts")
        _plain_geometry(geometry, "geometry")
        _plain_producer(producer, "producer")
        projection: dict[str, object] = {
            "category": category,
            "confidence_millionths": confidence_millionths,
            "frame_id": frame_id,
            "geometry": geometry.to_mapping(),
            "identity_version": 1,
            "producer": producer.to_mapping(),
            "pts": pts.to_mapping(),
            "source_id": source_id,
            "stream_index": stream_index,
        }
        return cls(
            observation_id=_identifier("obs", projection),
            source_id=source_id,
            frame_id=frame_id,
            stream_index=stream_index,
            pts=pts,
            geometry=geometry,
            category=category,
            confidence_millionths=confidence_millionths,
            producer=producer,
        )

    def identity_projection(self) -> dict[str, object]:
        return {
            "category": self.category,
            "confidence_millionths": self.confidence_millionths,
            "frame_id": self.frame_id,
            "geometry": self.geometry.to_mapping(),
            "identity_version": 1,
            "producer": self.producer.to_mapping(),
            "pts": self.pts.to_mapping(),
            "source_id": self.source_id,
            "stream_index": self.stream_index,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self.identity_projection(),
            "observation_id": self.observation_id,
            "schema": self.schema,
            "schema_version": 1,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Observation:
        item = _record_fields(
            value,
            {
                "category",
                "confidence_millionths",
                "frame_id",
                "geometry",
                "observation_id",
                "producer",
                "pts",
                "source_id",
                "stream_index",
            },
            cls.schema,
        )
        return cls(
            observation_id=_general_string(item["observation_id"], "observation_id"),
            source_id=_general_string(item["source_id"], "source_id"),
            frame_id=_general_string(item["frame_id"], "frame_id"),
            stream_index=_bounded_int(item["stream_index"], 0, MAX_I31, "stream_index"),
            pts=MediaTime.from_mapping(item["pts"], "pts"),
            geometry=Geometry.from_mapping(item["geometry"]),
            category=_token(item["category"], "category"),
            confidence_millionths=_bounded_int(
                item["confidence_millionths"],
                0,
                MAX_CONFIDENCE_MILLIONTHS,
                "confidence_millionths",
            ),
            producer=Producer.from_mapping(item["producer"]),
        )


@dataclass(frozen=True, slots=True)
class TrackPoint:
    """A pixel-free trajectory point bound to one observation and frame."""

    observation_id: str
    source_id: str
    frame_id: str
    stream_index: int
    pts: MediaTime
    geometry: Geometry
    category: str

    def __post_init__(self) -> None:
        _plain_string(self.observation_id, "track_point.observation_id")
        _plain_string(self.source_id, "track_point.source_id")
        _plain_string(self.frame_id, "track_point.frame_id")
        _typed_id(self.observation_id, "obs", "track_point.observation_id")
        _typed_id(self.source_id, "src", "track_point.source_id")
        _typed_id(self.frame_id, "frm", "track_point.frame_id")
        _bounded_int(self.stream_index, 0, MAX_I31, "track_point.stream_index")
        _plain_media_time(self.pts, "track_point.pts")
        _plain_geometry(self.geometry, "track_point.geometry")
        _plain_string(self.category, "track_point.category")
        _token(self.category, "track_point.category", _CATEGORIES)

    @classmethod
    def from_observation(cls, observation: Observation) -> TrackPoint:
        if type(observation) is not Observation:
            _fail("invalid_observation", "observation")
        Observation.__post_init__(observation)
        return cls(
            observation.observation_id,
            observation.source_id,
            observation.frame_id,
            observation.stream_index,
            observation.pts,
            observation.geometry,
            observation.category,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "category": self.category,
            "frame_id": self.frame_id,
            "geometry": self.geometry.to_mapping(),
            "observation_id": self.observation_id,
            "pts": self.pts.to_mapping(),
            "source_id": self.source_id,
            "stream_index": self.stream_index,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "point") -> TrackPoint:
        item = _exact_fields(
            value,
            {
                "category",
                "frame_id",
                "geometry",
                "observation_id",
                "pts",
                "source_id",
                "stream_index",
            },
            path,
        )
        return cls(
            observation_id=_general_string(item["observation_id"], f"{path}.observation_id"),
            source_id=_general_string(item["source_id"], f"{path}.source_id"),
            frame_id=_general_string(item["frame_id"], f"{path}.frame_id"),
            stream_index=_bounded_int(item["stream_index"], 0, MAX_I31, f"{path}.stream_index"),
            pts=MediaTime.from_mapping(item["pts"], f"{path}.pts"),
            geometry=Geometry.from_mapping(item["geometry"], f"{path}.geometry"),
            category=_token(item["category"], f"{path}.category"),
        )


@dataclass(frozen=True, slots=True)
class Tracklet(_PerceptionRecord):
    """Completed short-term continuity within one source clip."""

    tracklet_id: str
    source_id: str
    stream_index: int
    category: str
    points: tuple[TrackPoint, ...]
    termination_reason: str
    producer: Producer
    identity_scope: str = "source_clip"
    continuity: str = "inferred"

    schema: ClassVar[str] = "visualworld.tracklet"

    def __post_init__(self) -> None:
        _plain_string(self.tracklet_id, "tracklet_id")
        _plain_string(self.source_id, "source_id")
        _typed_id(self.tracklet_id, "trk", "tracklet_id")
        _typed_id(self.source_id, "src", "source_id")
        _bounded_int(self.stream_index, 0, MAX_I31, "stream_index")
        _plain_string(self.category, "category")
        _token(self.category, "category", _CATEGORIES)
        if type(self.points) is not tuple:
            _fail("track_points_must_be_tuple", "points")
        if not self.points:
            _fail("track_points_required", "points")
        if len(self.points) > MAX_TRACK_POINTS:
            _fail("too_many_track_points", "points")
        if not all(type(point) is TrackPoint for point in self.points):
            _fail("invalid_track_point", "points")
        for point in self.points:
            TrackPoint.__post_init__(point)
            if (
                point.source_id != self.source_id
                or point.stream_index != self.stream_index
                or point.category != self.category
            ):
                _fail("track_point_scope_mismatch", "points")
        observation_ids = [point.observation_id for point in self.points]
        frame_ids = [point.frame_id for point in self.points]
        if len(set(observation_ids)) != len(observation_ids):
            _fail("duplicate_observation", "points")
        if len(set(frame_ids)) != len(frame_ids):
            _fail("duplicate_frame", "points")
        if any(
            compare_media_time(self.points[index].pts, self.points[index + 1].pts) >= 0
            for index in range(len(self.points) - 1)
        ):
            _fail("unordered_track_points", "points")
        dimensions = {
            (point.geometry.source_width, point.geometry.source_height) for point in self.points
        }
        if len(dimensions) != 1:
            _fail("mixed_source_geometry", "points")
        _plain_string(self.termination_reason, "termination.reason")
        _token(self.termination_reason, "termination.reason", _TERMINATION_REASONS)
        _plain_producer(self.producer, "producer")
        _plain_string(self.identity_scope, "identity_scope")
        _token(self.identity_scope, "identity_scope", frozenset({"source_clip"}))
        _plain_string(self.continuity, "continuity")
        _token(self.continuity, "continuity", frozenset({"inferred"}))
        if self.tracklet_id != _identifier("trk", self.identity_projection()):
            _fail("identifier_mismatch", "tracklet_id")

    @property
    def start_pts(self) -> MediaTime:
        return self.points[0].pts

    @property
    def end_pts(self) -> MediaTime:
        return self.points[-1].pts

    @classmethod
    def create(
        cls,
        source_id: str,
        stream_index: int,
        category: str,
        points: tuple[TrackPoint, ...],
        termination_reason: str,
        producer: Producer,
    ) -> Tracklet:
        _plain_string(source_id, "source_id")
        _plain_string(category, "category")
        _plain_string(termination_reason, "termination.reason")
        if type(points) is not tuple:
            _fail("track_points_must_be_tuple", "points")
        if len(points) > MAX_TRACK_POINTS:
            _fail("too_many_track_points", "points")
        if not all(type(point) is TrackPoint for point in points):
            _fail("invalid_track_point", "points")
        for point in points:
            TrackPoint.__post_init__(point)
        _plain_producer(producer, "producer")
        projection: dict[str, object] = {
            "category": category,
            "continuity": "inferred",
            "identity_scope": "source_clip",
            "identity_version": 1,
            "points": [point.to_mapping() for point in points],
            "producer": producer.to_mapping(),
            "source_id": source_id,
            "stream_index": stream_index,
            "termination": {"reason": termination_reason},
        }
        return cls(
            tracklet_id=_identifier("trk", projection),
            source_id=source_id,
            stream_index=stream_index,
            category=category,
            points=points,
            termination_reason=termination_reason,
            producer=producer,
        )

    def identity_projection(self) -> dict[str, object]:
        return {
            "category": self.category,
            "continuity": self.continuity,
            "identity_scope": self.identity_scope,
            "identity_version": 1,
            "points": [point.to_mapping() for point in self.points],
            "producer": self.producer.to_mapping(),
            "source_id": self.source_id,
            "stream_index": self.stream_index,
            "termination": {"reason": self.termination_reason},
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self.identity_projection(),
            "end_pts": self.end_pts.to_mapping(),
            "schema": self.schema,
            "schema_version": 1,
            "start_pts": self.start_pts.to_mapping(),
            "tracklet_id": self.tracklet_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Tracklet:
        item = _record_fields(
            value,
            {
                "category",
                "continuity",
                "end_pts",
                "identity_scope",
                "points",
                "producer",
                "source_id",
                "start_pts",
                "stream_index",
                "termination",
                "tracklet_id",
            },
            cls.schema,
        )
        point_values = _array(item["points"], "points")
        if len(point_values) > MAX_TRACK_POINTS:
            _fail("too_many_track_points", "points")
        points = tuple(
            TrackPoint.from_mapping(point, f"points[{index}]")
            for index, point in enumerate(point_values)
        )
        if not points:
            _fail("track_points_required", "points")
        start_pts = MediaTime.from_mapping(item["start_pts"], "start_pts")
        end_pts = MediaTime.from_mapping(item["end_pts"], "end_pts")
        if start_pts != points[0].pts:
            _fail("start_pts_mismatch", "start_pts")
        if end_pts != points[-1].pts:
            _fail("end_pts_mismatch", "end_pts")
        termination = _exact_fields(item["termination"], {"reason"}, "termination")
        return cls(
            tracklet_id=_general_string(item["tracklet_id"], "tracklet_id"),
            source_id=_general_string(item["source_id"], "source_id"),
            stream_index=_bounded_int(item["stream_index"], 0, MAX_I31, "stream_index"),
            category=_token(item["category"], "category"),
            points=points,
            termination_reason=_token(termination["reason"], "termination.reason"),
            producer=Producer.from_mapping(item["producer"]),
            identity_scope=_token(item["identity_scope"], "identity_scope"),
            continuity=_token(item["continuity"], "continuity"),
        )


PerceptionRecord = Observation | Tracklet


def dumps_perception_record(record: PerceptionRecord) -> bytes:
    """Serialize one validated perception record as bounded canonical JSON."""
    if type(record) not in {Observation, Tracklet}:
        _fail("unsupported_perception_record")
    record.__post_init__()
    return _canonical_bytes(record.to_mapping())


def identity_perception_bytes(record: PerceptionRecord) -> bytes:
    """Return the canonical identity projection used for a perception ID."""
    if type(record) not in {Observation, Tracklet}:
        _fail("unsupported_perception_record")
    record.__post_init__()
    return _canonical_bytes(record.identity_projection())


def loads_perception_record(data: bytes) -> PerceptionRecord:
    """Parse one bounded strict-JSON v1 perception record."""
    if type(data) is not bytes:
        _fail("record_must_be_bytes")
    value = _parse_json(data)
    schema = _token(value.get("schema"), "schema")
    version = _bounded_int(value.get("schema_version"), 1, MAX_I31, "schema_version")
    if version != 1:
        _fail("unknown_schema_version", "schema_version")
    readers: dict[str, Callable[[object], PerceptionRecord]] = {
        Observation.schema: Observation.from_mapping,
        Tracklet.schema: Tracklet.from_mapping,
    }
    reader = readers.get(schema)
    if reader is None:
        _fail("unknown_schema", "schema")
    return reader(value)


def load_perception_record(reader: BinaryIO) -> PerceptionRecord:
    """Read at most 256 KiB plus one byte before parsing a perception record."""
    chunks: list[bytes] = []
    remaining = MAX_RECORD_BYTES + 1
    read_failed = False
    try:
        while remaining > 0:
            chunk = reader.read(remaining)
            if type(chunk) is not bytes:
                _fail("record_reader_must_be_binary")
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        read_failed = True
    if read_failed:
        _fail("record_read_failed")
    return loads_perception_record(b"".join(chunks))


__all__ = [
    "MAX_CONFIDENCE_MILLIONTHS",
    "MAX_DISCONTINUITY_BASIS_POINTS",
    "MAX_TRACK_POINTS",
    "FrameDiscontinuity",
    "Observation",
    "PerceptionRecord",
    "TrackPoint",
    "Tracklet",
    "dumps_perception_record",
    "identity_perception_bytes",
    "load_perception_record",
    "loads_perception_record",
]
