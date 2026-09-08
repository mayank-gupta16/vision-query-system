# SPDX-License-Identifier: Apache-2.0
"""Deterministic best-frame planning and explicit original-pixel materialization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import BinaryIO, cast

from visualworld.geometry import CropError, Rgb24Crop, extract_rgb24_crop
from visualworld.ingestion import (
    MAX_I31,
    MAX_RECORD_BYTES,
    AffineCoefficients,
    Artifact,
    EvidenceRef,
    Geometry,
    MediaTime,
    Producer,
    ProducerSpace,
    Rational,
    TimeBase,
    _canonical_bytes,
    _exact_fields,
    _parse_json,
)
from visualworld.perception import (
    MAX_CONFIDENCE_MILLIONTHS,
    Observation,
    Tracklet,
    TrackPoint,
)
from visualworld.ports import (
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    EvidenceSelectionResult,
    PerceptionResultState,
    PortCall,
    PortError,
    PortErrorCode,
    PortKind,
)

DEFAULT_MAX_SELECTED_OBSERVATIONS = 3
MAX_SELECTED_OBSERVATIONS = 8
MAX_EVIDENCE_RGB24_FRAME_BYTES = 128 * 1024 * 1024
_MILLION = 1_000_000
_OBSERVATION_ID_RE = re.compile(r"obs_[0-9a-f]{64}\Z")
_TRACKLET_ID_RE = re.compile(r"trk_[0-9a-f]{64}\Z")
_SOURCE_ID_RE = re.compile(r"src_[0-9a-f]{64}\Z")
_FRAME_ID_RE = re.compile(r"frm_[0-9a-f]{64}\Z")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_KIND = "original_frame"
_RETENTION = "derived_private"
_DELETION_OWNER = "coordinator_source_cascade"
# Geometry dimensions are ingestion-bounded signed-31-bit positive integers.
_MAX_SOURCE_AREA_PIXELS = MAX_I31 * MAX_I31
# ``abs(2*t - start - end)`` combines three i64*u32/u32 media times. A common
# denominator is below 2**96 and the resulting absolute numerator below 2**161.
_MAX_MIDPOINT_DISTANCE_NUMERATOR_BITS = 161
_MAX_MIDPOINT_DISTANCE_DENOMINATOR_BITS = 96


def _error(code: PortErrorCode, operation: str) -> PortError:
    return PortError(code, PortKind.EVIDENCE_SELECTOR, operation)


def _copy_producer(value: Producer) -> Producer:
    if type(value) is not Producer:
        raise ValueError("invalid producer")
    fields = (value.name, value.version, value.configuration_sha256)
    if any(type(item) is not str for item in fields):
        raise ValueError("invalid producer")
    name, version, configuration_sha256 = fields
    return Producer(name, version, configuration_sha256)


def _copy_time(value: MediaTime) -> MediaTime:
    if type(value) is not MediaTime:
        raise ValueError("invalid time")
    fields = (
        value.value,
        value.time_base,
        value.basis,
        value.estimate_method,
        value.estimate_producer,
    )
    time_value, time_base, basis, estimate_method, estimate_producer = fields
    if (
        type(time_value) is not str
        or type(basis) is not str
        or type(time_base) is not TimeBase
        or (estimate_method is not None and type(estimate_method) is not str)
        or (estimate_producer is not None and type(estimate_producer) is not Producer)
    ):
        raise ValueError("invalid time")
    time_base_fields = (time_base.numerator, time_base.denominator)
    if any(type(item) is not str for item in time_base_fields):
        raise ValueError("invalid time")
    time_base_numerator, time_base_denominator = time_base_fields
    owned_estimate_producer = (
        None if estimate_producer is None else _copy_producer(estimate_producer)
    )
    return MediaTime(
        time_value,
        TimeBase(time_base_numerator, time_base_denominator),
        basis,
        estimate_method,
        owned_estimate_producer,
    )


def _copy_rational(value: Rational) -> Rational:
    if type(value) is not Rational:
        raise ValueError("invalid rational")
    fields = (value.numerator, value.denominator)
    if any(type(item) is not str for item in fields):
        raise ValueError("invalid rational")
    numerator, denominator = fields
    return Rational(numerator, denominator)


def _copy_geometry(value: Geometry) -> Geometry:
    if type(value) is not Geometry:
        raise ValueError("invalid geometry")
    fields = (
        value.source_width,
        value.source_height,
        value.box_xyxy,
        value.measurement,
        value.transform_kind,
        value.producer_space,
        value.coefficients,
        value.space,
    )
    (
        source_width,
        source_height,
        box_xyxy,
        measurement,
        transform_kind,
        supplied_space,
        supplied_coefficients,
        space,
    ) = fields
    if (
        type(source_width) is not int
        or type(source_height) is not int
        or type(box_xyxy) is not tuple
        or len(box_xyxy) != 4
        or any(type(coordinate) is not int for coordinate in box_xyxy)
        or type(measurement) is not str
        or type(transform_kind) is not str
        or type(space) is not str
    ):
        raise ValueError("invalid geometry")
    producer_space: ProducerSpace | None = None
    if supplied_space is not None:
        if type(supplied_space) is not ProducerSpace:
            raise ValueError("invalid geometry")
        space_fields = (
            supplied_space.width,
            supplied_space.height,
            supplied_space.box_xyxy,
        )
        producer_width, producer_height, producer_box = space_fields
        if (
            type(producer_width) is not int
            or type(producer_height) is not int
            or type(producer_box) is not tuple
            or len(producer_box) != 4
            or any(type(coordinate) is not int for coordinate in producer_box)
        ):
            raise ValueError("invalid geometry")
        producer_space = ProducerSpace(
            producer_width,
            producer_height,
            producer_box,
        )
    coefficients: AffineCoefficients | None = None
    if supplied_coefficients is not None:
        if type(supplied_coefficients) is not AffineCoefficients:
            raise ValueError("invalid geometry")
        coefficient_values = (
            supplied_coefficients.a,
            supplied_coefficients.b,
            supplied_coefficients.c,
            supplied_coefficients.d,
            supplied_coefficients.e,
            supplied_coefficients.f,
        )
        coefficients = AffineCoefficients(
            *(_copy_rational(coefficient) for coefficient in coefficient_values)
        )
    return Geometry(
        source_width,
        source_height,
        box_xyxy,
        measurement,
        transform_kind,
        producer_space,
        coefficients,
        space,
    )


def _copy_observation(value: Observation) -> Observation:
    if type(value) is not Observation:
        raise ValueError("invalid observation")
    fields = (
        value.observation_id,
        value.source_id,
        value.frame_id,
        value.stream_index,
        value.pts,
        value.geometry,
        value.category,
        value.confidence_millionths,
        value.producer,
    )
    (
        observation_id,
        source_id,
        frame_id,
        stream_index,
        pts,
        geometry,
        category,
        confidence_millionths,
        producer,
    ) = fields
    if (
        type(stream_index) is not int
        or type(confidence_millionths) is not int
        or any(type(item) is not str for item in (observation_id, source_id, frame_id, category))
    ):
        raise ValueError("invalid observation")
    return Observation(
        observation_id,
        source_id,
        frame_id,
        stream_index,
        _copy_time(pts),
        _copy_geometry(geometry),
        category,
        confidence_millionths,
        _copy_producer(producer),
    )


def _copy_track_point(value: TrackPoint) -> TrackPoint:
    if type(value) is not TrackPoint:
        raise ValueError("invalid track point")
    fields = (
        value.observation_id,
        value.source_id,
        value.frame_id,
        value.stream_index,
        value.pts,
        value.geometry,
        value.category,
    )
    observation_id, source_id, frame_id, stream_index, pts, geometry, category = fields
    if type(stream_index) is not int or any(
        type(item) is not str for item in (observation_id, source_id, frame_id, category)
    ):
        raise ValueError("invalid track point")
    return TrackPoint(
        observation_id,
        source_id,
        frame_id,
        stream_index,
        _copy_time(pts),
        _copy_geometry(geometry),
        category,
    )


def _copy_tracklet(value: Tracklet) -> Tracklet:
    if type(value) is not Tracklet:
        raise ValueError("invalid tracklet")
    fields = (
        value.tracklet_id,
        value.source_id,
        value.stream_index,
        value.category,
        value.points,
        value.termination_reason,
        value.producer,
        value.identity_scope,
        value.continuity,
    )
    (
        tracklet_id,
        source_id,
        stream_index,
        category,
        points,
        termination_reason,
        producer,
        identity_scope,
        continuity,
    ) = fields
    if (
        type(stream_index) is not int
        or type(points) is not tuple
        or not 1 <= len(points) <= MAX_PORT_BATCH_ITEMS
        or not all(type(point) is TrackPoint for point in points)
        or any(
            type(item) is not str
            for item in (
                tracklet_id,
                source_id,
                category,
                termination_reason,
                identity_scope,
                continuity,
            )
        )
    ):
        raise ValueError("invalid tracklet")
    return Tracklet(
        tracklet_id,
        source_id,
        stream_index,
        category,
        tuple(_copy_track_point(point) for point in points),
        termination_reason,
        _copy_producer(producer),
        identity_scope,
        continuity,
    )


def _copy_artifact(value: Artifact) -> Artifact:
    if type(value) is not Artifact:
        raise ValueError("invalid artifact")
    fields = (value.sha256, value.bytes, value.media_type)
    if any(type(item) is not str for item in fields):
        raise ValueError("invalid artifact")
    sha256, byte_count, media_type = fields
    return Artifact(sha256, byte_count, media_type)


def _copy_evidence_ref(value: EvidenceRef) -> EvidenceRef:
    if type(value) is not EvidenceRef:
        raise ValueError("invalid evidence reference")
    fields = (
        value.evidence_id,
        value.frame_id,
        value.artifact,
        value.geometry,
        value.kind,
        value.retention,
    )
    evidence_id, frame_id, artifact, geometry, kind, retention = fields
    if any(
        type(item) is not str
        for item in (
            evidence_id,
            frame_id,
            kind,
            retention,
        )
    ):
        raise ValueError("invalid evidence reference")
    if geometry is not None and type(geometry) is not Geometry:
        raise ValueError("invalid evidence reference")
    return EvidenceRef(
        evidence_id,
        frame_id,
        _copy_artifact(artifact),
        None if geometry is None else _copy_geometry(geometry),
        kind,
        retention,
    )


def _copy_crop(value: Rgb24Crop) -> Rgb24Crop:
    if type(value) is not Rgb24Crop:
        raise ValueError("invalid evidence crop")
    fields = (value.width, value.height, value.pixels)
    width, height, pixels = fields
    if type(width) is not int or type(height) is not int or type(pixels) is not bytes:
        raise ValueError("invalid evidence crop")
    return Rgb24Crop(width, height, pixels)


@dataclass(frozen=True, slots=True)
class EvidenceSelectionLimits:
    """Small output bound for one completed tracklet."""

    max_selected_observations: int = DEFAULT_MAX_SELECTED_OBSERVATIONS

    def __post_init__(self) -> None:
        if (
            type(self.max_selected_observations) is not int
            or not 1 <= self.max_selected_observations <= MAX_SELECTED_OBSERVATIONS
        ):
            raise ValueError("max_selected_observations is outside the supported bound")


def _configuration(max_selected_observations: int) -> dict[str, object]:
    return {
        "deletion_owner": _DELETION_OWNER,
        "evidence_kind": _KIND,
        "max_selected_observations": max_selected_observations,
        "ranking": [
            "boundary_touch_count_ascending",
            "confidence_millionths_descending",
            "visible_area_millionths_descending",
            "visible_area_pixels_descending",
            "midpoint_distance_seconds_x2_ascending",
            "point_index_ascending",
            "observation_id_ascending",
        ],
        "retention": _RETENTION,
        "schema": "visualworld.best-frame-evidence-selector-configuration",
        "schema_version": 1,
    }


def _producer(max_selected_observations: int) -> Producer:
    content = json.dumps(
        _configuration(max_selected_observations),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return Producer(
        "visualworld.best-frame-evidence-selector",
        "1",
        hashlib.sha256(content).hexdigest(),
    )


DEFAULT_EVIDENCE_SELECTOR_CONFIGURATION_SHA256 = _producer(
    DEFAULT_MAX_SELECTED_OBSERVATIONS
).configuration_sha256


@dataclass(frozen=True, slots=True)
class EvidenceScore:
    """Integer-only ranking components and deterministic time/identity tie-breaks."""

    boundary_touch_count: int
    confidence_millionths: int
    visible_area_pixels: int
    source_area_pixels: int
    visible_area_millionths: int
    midpoint_distance_seconds_x2_numerator: int
    midpoint_distance_seconds_x2_denominator: int
    point_index: int
    point_count: int
    pts: MediaTime
    observation_id: str

    def __post_init__(self) -> None:
        integers = (
            self.boundary_touch_count,
            self.confidence_millionths,
            self.visible_area_pixels,
            self.source_area_pixels,
            self.visible_area_millionths,
            self.midpoint_distance_seconds_x2_numerator,
            self.midpoint_distance_seconds_x2_denominator,
            self.point_index,
            self.point_count,
        )
        if any(type(value) is not int for value in integers):
            raise ValueError("evidence score components must be integers")
        if not 0 <= self.boundary_touch_count <= 4:
            raise ValueError("boundary touch count is invalid")
        if not 0 <= self.confidence_millionths <= MAX_CONFIDENCE_MILLIONTHS:
            raise ValueError("confidence is invalid")
        if not (
            1 <= self.visible_area_pixels <= self.source_area_pixels <= _MAX_SOURCE_AREA_PIXELS
        ):
            raise ValueError("visible area is invalid")
        if not 0 <= self.visible_area_millionths <= _MILLION:
            raise ValueError("visible area ratio is invalid")
        if self.visible_area_millionths != (
            self.visible_area_pixels * _MILLION // self.source_area_pixels
        ):
            raise ValueError("visible area ratio is inconsistent")
        if not 1 <= self.point_count <= MAX_PORT_BATCH_ITEMS:
            raise ValueError("point count is invalid")
        if not 0 <= self.point_index < self.point_count:
            raise ValueError("point index is invalid")
        if (
            self.midpoint_distance_seconds_x2_numerator < 0
            or self.midpoint_distance_seconds_x2_denominator <= 0
            or self.midpoint_distance_seconds_x2_numerator.bit_length()
            > _MAX_MIDPOINT_DISTANCE_NUMERATOR_BITS
            or self.midpoint_distance_seconds_x2_denominator.bit_length()
            > _MAX_MIDPOINT_DISTANCE_DENOMINATOR_BITS
        ):
            raise ValueError("midpoint distance is invalid")
        midpoint_distance = Fraction(
            self.midpoint_distance_seconds_x2_numerator,
            self.midpoint_distance_seconds_x2_denominator,
        )
        if (
            midpoint_distance.numerator != self.midpoint_distance_seconds_x2_numerator
            or midpoint_distance.denominator != self.midpoint_distance_seconds_x2_denominator
        ):
            raise ValueError("midpoint distance is not normalized")
        owned_pts = _copy_time(self.pts)
        if type(self.observation_id) is not str or not _OBSERVATION_ID_RE.fullmatch(
            self.observation_id
        ):
            raise ValueError("observation identifier is invalid")
        object.__setattr__(self, "pts", owned_pts)

    def rank_key(self) -> tuple[int, int, int, int, Fraction, int, str]:
        """Return the frozen lexicographic ranking key; lower is better."""

        self.__post_init__()
        return (
            self.boundary_touch_count,
            -self.confidence_millionths,
            -self.visible_area_millionths,
            -self.visible_area_pixels,
            Fraction(
                self.midpoint_distance_seconds_x2_numerator,
                self.midpoint_distance_seconds_x2_denominator,
            ),
            self.point_index,
            self.observation_id,
        )

    def to_mapping(self) -> dict[str, object]:
        self.__post_init__()
        return {
            "components": {
                "boundary_touch_count": self.boundary_touch_count,
                "confidence_millionths": self.confidence_millionths,
                "source_area_pixels": self.source_area_pixels,
                "visible_area_millionths": self.visible_area_millionths,
                "visible_area_pixels": self.visible_area_pixels,
            },
            "tie_break": {
                "midpoint_distance_seconds_x2": {
                    "denominator": self.midpoint_distance_seconds_x2_denominator,
                    "numerator": self.midpoint_distance_seconds_x2_numerator,
                },
                "observation_id": self.observation_id,
                "point_count": self.point_count,
                "point_index": self.point_index,
                "pts": self.pts.to_mapping(),
            },
        }

    @classmethod
    def from_mapping(cls, value: object) -> EvidenceScore:
        item = _exact_fields(value, {"components", "tie_break"}, "score")
        components = _exact_fields(
            item["components"],
            {
                "boundary_touch_count",
                "confidence_millionths",
                "source_area_pixels",
                "visible_area_millionths",
                "visible_area_pixels",
            },
            "score.components",
        )
        tie_break = _exact_fields(
            item["tie_break"],
            {
                "midpoint_distance_seconds_x2",
                "observation_id",
                "point_count",
                "point_index",
                "pts",
            },
            "score.tie_break",
        )
        midpoint = _exact_fields(
            tie_break["midpoint_distance_seconds_x2"],
            {"denominator", "numerator"},
            "score.tie_break.midpoint_distance_seconds_x2",
        )
        integers = cast(
            tuple[int, int, int, int, int, int, int, int, int],
            (
                components["boundary_touch_count"],
                components["confidence_millionths"],
                components["visible_area_pixels"],
                components["source_area_pixels"],
                components["visible_area_millionths"],
                midpoint["numerator"],
                midpoint["denominator"],
                tie_break["point_index"],
                tie_break["point_count"],
            ),
        )
        if any(type(number) is not int for number in integers):
            raise ValueError("evidence score components must be integers")
        observation_id = tie_break["observation_id"]
        if type(observation_id) is not str:
            raise ValueError("observation identifier is invalid")
        return cls(
            *integers,
            MediaTime.from_mapping(tie_break["pts"], "score.tie_break.pts"),
            observation_id,
        )


@dataclass(frozen=True, slots=True)
class EvidenceIntent:
    """Pixel-free request to materialize one selected source-coordinate crop."""

    rank: int
    tracklet_id: str
    observation_id: str
    source_id: str
    frame_id: str
    stream_index: int
    pts: MediaTime
    geometry: Geometry
    score: EvidenceScore
    selector: Producer
    kind: str = _KIND
    retention: str = _RETENTION
    deletion_owner: str = _DELETION_OWNER

    def __post_init__(self) -> None:
        if type(self.rank) is not int or not 1 <= self.rank <= MAX_SELECTED_OBSERVATIONS:
            raise ValueError("evidence rank is invalid")
        identifiers = (
            (self.tracklet_id, _TRACKLET_ID_RE),
            (self.observation_id, _OBSERVATION_ID_RE),
            (self.source_id, _SOURCE_ID_RE),
            (self.frame_id, _FRAME_ID_RE),
        )
        if any(
            type(value) is not str or not pattern.fullmatch(value) for value, pattern in identifiers
        ):
            raise ValueError("evidence intent identifier is invalid")
        if type(self.stream_index) is not int or not 0 <= self.stream_index <= 2**31 - 1:
            raise ValueError("stream index is invalid")
        if any(
            type(value) is not str for value in (self.kind, self.retention, self.deletion_owner)
        ):
            raise ValueError("evidence lifecycle policy is invalid")
        owned_pts = _copy_time(self.pts)
        owned_geometry = _copy_geometry(self.geometry)
        if type(self.score) is not EvidenceScore:
            raise ValueError("evidence score is invalid")
        owned_score = _copy_score(self.score)
        owned_selector = _copy_producer(self.selector)
        if owned_score.observation_id != self.observation_id or owned_score.pts != owned_pts:
            raise ValueError("evidence score scope is inconsistent")
        x_min, y_min, x_max, y_max = owned_geometry.box_xyxy
        visible_area = (x_max - x_min) * (y_max - y_min)
        source_area = owned_geometry.source_width * owned_geometry.source_height
        boundary_touches = sum(
            (
                x_min == 0,
                y_min == 0,
                x_max == owned_geometry.source_width,
                y_max == owned_geometry.source_height,
            )
        )
        if (
            owned_score.boundary_touch_count != boundary_touches
            or owned_score.visible_area_pixels != visible_area
            or owned_score.source_area_pixels != source_area
        ):
            raise ValueError("evidence score geometry is inconsistent")
        if (
            self.kind != _KIND
            or self.retention != _RETENTION
            or self.deletion_owner != _DELETION_OWNER
        ):
            raise ValueError("evidence lifecycle policy is invalid")
        object.__setattr__(self, "pts", owned_pts)
        object.__setattr__(self, "geometry", owned_geometry)
        object.__setattr__(self, "score", owned_score)
        object.__setattr__(self, "selector", owned_selector)

    def to_mapping(self) -> dict[str, object]:
        """Return complete metadata without retrieving or embedding pixels."""

        self.__post_init__()
        return {
            "deletion_owner": self.deletion_owner,
            "frame_id": self.frame_id,
            "geometry": self.geometry.to_mapping(),
            "kind": self.kind,
            "observation_id": self.observation_id,
            "pts": self.pts.to_mapping(),
            "rank": self.rank,
            "retention": self.retention,
            "schema": "visualworld.evidence_intent",
            "schema_version": 1,
            "score": self.score.to_mapping(),
            "selector": self.selector.to_mapping(),
            "source_id": self.source_id,
            "stream_index": self.stream_index,
            "tracklet_id": self.tracklet_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> EvidenceIntent:
        item = _exact_fields(
            value,
            {
                "deletion_owner",
                "frame_id",
                "geometry",
                "kind",
                "observation_id",
                "pts",
                "rank",
                "retention",
                "schema",
                "schema_version",
                "score",
                "selector",
                "source_id",
                "stream_index",
                "tracklet_id",
            },
            "$",
        )
        schema = item["schema"]
        schema_version = item["schema_version"]
        if (
            type(schema) is not str
            or schema != "visualworld.evidence_intent"
            or type(schema_version) is not int
            or schema_version != 1
        ):
            raise ValueError("unsupported evidence intent schema")
        scalar_strings = (
            item["tracklet_id"],
            item["observation_id"],
            item["source_id"],
            item["frame_id"],
            item["kind"],
            item["retention"],
            item["deletion_owner"],
        )
        if any(type(text) is not str for text in scalar_strings):
            raise ValueError("evidence intent scalar is invalid")
        if type(item["rank"]) is not int or type(item["stream_index"]) is not int:
            raise ValueError("evidence intent integer is invalid")
        return cls(
            item["rank"],
            cast(str, item["tracklet_id"]),
            cast(str, item["observation_id"]),
            cast(str, item["source_id"]),
            cast(str, item["frame_id"]),
            item["stream_index"],
            MediaTime.from_mapping(item["pts"], "pts"),
            Geometry.from_mapping(item["geometry"]),
            EvidenceScore.from_mapping(item["score"]),
            Producer.from_mapping(item["selector"], "selector"),
            cast(str, item["kind"]),
            cast(str, item["retention"]),
            cast(str, item["deletion_owner"]),
        )


def dumps_evidence_intent(intent: EvidenceIntent) -> bytes:
    """Serialize one validated metadata-only evidence intent canonically."""

    if type(intent) is not EvidenceIntent:
        raise ValueError("unsupported evidence intent")
    intent.__post_init__()
    return _canonical_bytes(intent.to_mapping())


def loads_evidence_intent(data: bytes) -> EvidenceIntent:
    """Parse one bounded strict-JSON evidence intent."""

    if type(data) is not bytes:
        raise ValueError("evidence intent must be bytes")
    return EvidenceIntent.from_mapping(_parse_json(data))


def load_evidence_intent(reader: BinaryIO) -> EvidenceIntent:
    """Read at most 256 KiB plus one byte before parsing an evidence intent."""

    chunks: list[bytes] = []
    remaining = MAX_RECORD_BYTES + 1
    read_failed = False
    try:
        while remaining > 0:
            chunk = reader.read(remaining)
            if type(chunk) is not bytes:
                raise ValueError("evidence intent reader must be binary")
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        read_failed = True
    if read_failed:
        raise ValueError("evidence intent read failed")
    return loads_evidence_intent(b"".join(chunks))


@dataclass(frozen=True, slots=True)
class EvidencePlanResult:
    """Detailed metadata-only companion to the stable selector port result."""

    state: PerceptionResultState
    intents: tuple[EvidenceIntent, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not PerceptionResultState:
            raise ValueError("state must use PerceptionResultState")
        if type(self.intents) is not tuple or len(self.intents) > MAX_SELECTED_OBSERVATIONS:
            raise ValueError("evidence intents exceed the supported bound")
        if not all(type(intent) is EvidenceIntent for intent in self.intents):
            raise ValueError("evidence intents are invalid")
        for intent in self.intents:
            intent.__post_init__()
        if len({intent.observation_id for intent in self.intents}) != len(self.intents):
            raise ValueError("evidence intent observations must be unique")
        if tuple(intent.rank for intent in self.intents) != tuple(range(1, len(self.intents) + 1)):
            raise ValueError("evidence intent ranks must be contiguous")
        if self.state is PerceptionResultState.COMPLETE:
            if self.reason is not None:
                raise ValueError("complete evidence plan cannot have a reason")
        elif self.intents or type(self.reason) is not str or not _REASON_RE.fullmatch(self.reason):
            raise ValueError("incomplete evidence plan requires a stable reason")

    @property
    def observation_ids(self) -> tuple[str, ...]:
        return tuple(intent.observation_id for intent in self.intents)

    def to_mapping(self) -> dict[str, object]:
        self.__post_init__()
        result: dict[str, object] = {
            "intents": [intent.to_mapping() for intent in self.intents],
            "state": self.state.value,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result


class EvidenceNeed(StrEnum):
    INSPECTION = "inspection"
    DOWNSTREAM_DETAIL = "downstream_detail"


class DetailResolution(StrEnum):
    RESOLVABLE = "resolvable"
    UNRESOLVABLE = "unresolvable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MaterializedEvidence:
    """One exact crop and reference; nested ``Rgb24Crop`` keeps pixels out of repr."""

    intent: EvidenceIntent
    need: EvidenceNeed
    detail_resolution: DetailResolution
    reference: EvidenceRef
    crop: Rgb24Crop

    def __post_init__(self) -> None:
        if type(self.intent) is not EvidenceIntent:
            raise ValueError("evidence intent is invalid")
        if type(self.need) is not EvidenceNeed:
            raise ValueError("evidence need is invalid")
        if type(self.detail_resolution) is not DetailResolution:
            raise ValueError("detail resolution is invalid")
        if type(self.reference) is not EvidenceRef:
            raise ValueError("evidence reference is invalid")
        if type(self.crop) is not Rgb24Crop:
            raise ValueError("evidence crop is invalid")
        owned_intent = _copy_intent(self.intent)
        owned_reference = _copy_evidence_ref(self.reference)
        owned_crop = _copy_crop(self.crop)
        if (
            owned_reference.frame_id != owned_intent.frame_id
            or owned_reference.geometry != owned_intent.geometry
            or owned_reference.kind != owned_intent.kind
            or owned_reference.retention != owned_intent.retention
            or owned_reference.artifact != owned_crop.artifact()
        ):
            raise ValueError("materialized evidence is inconsistent")
        object.__setattr__(self, "intent", owned_intent)
        object.__setattr__(self, "reference", owned_reference)
        object.__setattr__(self, "crop", owned_crop)

    def to_mapping(self) -> dict[str, object]:
        """Return the reference and request metadata, never the crop bytes."""

        self.__post_init__()
        return {
            "intent": self.intent.to_mapping(),
            "need": self.need.value,
            "detail_resolution": self.detail_resolution.value,
            "reference": self.reference.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class EvidenceCropResult:
    state: PerceptionResultState
    materialized: MaterializedEvidence | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not PerceptionResultState:
            raise ValueError("state must use PerceptionResultState")
        if self.state is PerceptionResultState.COMPLETE:
            if type(self.materialized) is not MaterializedEvidence or self.reason is not None:
                raise ValueError("complete crop result requires materialized evidence")
            self.materialized.__post_init__()
        elif (
            self.materialized is not None
            or type(self.reason) is not str
            or not _REASON_RE.fullmatch(self.reason)
        ):
            raise ValueError("incomplete crop result requires a stable reason")

    def to_mapping(self) -> dict[str, object]:
        """Return metadata only even when the crop has been materialized."""

        self.__post_init__()
        result: dict[str, object] = {"state": self.state.value}
        if self.materialized is not None:
            result["materialized"] = self.materialized.to_mapping()
        if self.reason is not None:
            result["reason"] = self.reason
        return result


def _media_seconds(value: MediaTime) -> Fraction:
    return Fraction(
        int(value.value) * int(value.time_base.numerator),
        int(value.time_base.denominator),
    )


def _score(
    observation: Observation,
    point_index: int,
    point_count: int,
    start_pts: MediaTime,
    end_pts: MediaTime,
) -> EvidenceScore:
    geometry = observation.geometry
    x_min, y_min, x_max, y_max = geometry.box_xyxy
    visible_area = (x_max - x_min) * (y_max - y_min)
    source_area = geometry.source_width * geometry.source_height
    boundary_touches = sum(
        (
            x_min == 0,
            y_min == 0,
            x_max == geometry.source_width,
            y_max == geometry.source_height,
        )
    )
    midpoint_distance_x2 = abs(
        2 * _media_seconds(observation.pts) - _media_seconds(start_pts) - _media_seconds(end_pts)
    )
    return EvidenceScore(
        boundary_touches,
        observation.confidence_millionths,
        visible_area,
        source_area,
        visible_area * _MILLION // source_area,
        midpoint_distance_x2.numerator,
        midpoint_distance_x2.denominator,
        point_index,
        point_count,
        _copy_time(observation.pts),
        observation.observation_id,
    )


def _copy_score(value: EvidenceScore) -> EvidenceScore:
    if type(value) is not EvidenceScore:
        raise ValueError("invalid evidence score")
    fields = (
        value.boundary_touch_count,
        value.confidence_millionths,
        value.visible_area_pixels,
        value.source_area_pixels,
        value.visible_area_millionths,
        value.midpoint_distance_seconds_x2_numerator,
        value.midpoint_distance_seconds_x2_denominator,
        value.point_index,
        value.point_count,
        value.pts,
        value.observation_id,
    )
    (
        boundary_touch_count,
        confidence_millionths,
        visible_area_pixels,
        source_area_pixels,
        visible_area_millionths,
        midpoint_numerator,
        midpoint_denominator,
        point_index,
        point_count,
        pts,
        observation_id,
    ) = fields
    integers = (
        boundary_touch_count,
        confidence_millionths,
        visible_area_pixels,
        source_area_pixels,
        visible_area_millionths,
        midpoint_numerator,
        midpoint_denominator,
        point_index,
        point_count,
    )
    if any(type(item) is not int for item in integers) or type(observation_id) is not str:
        raise ValueError("invalid evidence score")
    return EvidenceScore(
        *integers,
        _copy_time(pts),
        observation_id,
    )


def _copy_intent(value: EvidenceIntent) -> EvidenceIntent:
    if type(value) is not EvidenceIntent:
        raise ValueError("invalid evidence intent")
    fields = (
        value.rank,
        value.tracklet_id,
        value.observation_id,
        value.source_id,
        value.frame_id,
        value.stream_index,
        value.pts,
        value.geometry,
        value.score,
        value.selector,
        value.kind,
        value.retention,
        value.deletion_owner,
    )
    (
        rank,
        tracklet_id,
        observation_id,
        source_id,
        frame_id,
        stream_index,
        pts,
        geometry,
        score,
        selector,
        kind,
        retention,
        deletion_owner,
    ) = fields
    if (
        type(rank) is not int
        or type(stream_index) is not int
        or any(
            type(item) is not str
            for item in (
                tracklet_id,
                observation_id,
                source_id,
                frame_id,
                kind,
                retention,
                deletion_owner,
            )
        )
    ):
        raise ValueError("invalid evidence intent")
    return EvidenceIntent(
        rank,
        tracklet_id,
        observation_id,
        source_id,
        frame_id,
        stream_index,
        _copy_time(pts),
        _copy_geometry(geometry),
        _copy_score(score),
        _copy_producer(selector),
        kind,
        retention,
        deletion_owner,
    )


class BestFrameEvidenceSelector:
    """Select bounded deterministic evidence and crop it only on explicit request."""

    def __init__(self, *, limits: EvidenceSelectionLimits | None = None) -> None:
        supplied = EvidenceSelectionLimits() if limits is None else limits
        if type(supplied) is not EvidenceSelectionLimits:
            raise ValueError("limits must use EvidenceSelectionLimits")
        supplied.__post_init__()
        self._limits = EvidenceSelectionLimits(supplied.max_selected_observations)
        self._producer = _producer(self._limits.max_selected_observations)
        self._descriptor = CapabilityDescriptor(
            PortKind.EVIDENCE_SELECTOR,
            "visualworld.best-frame-evidence-selector",
            "1",
            deterministic=True,
            offline=True,
            max_batch_items=MAX_PORT_BATCH_ITEMS,
            max_payload_bytes=MAX_EVIDENCE_RGB24_FRAME_BYTES,
        )
        self._calls: list[PortCall] = []

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    @property
    def producer(self) -> Producer:
        return _copy_producer(self._producer)

    @property
    def calls(self) -> tuple[PortCall, ...]:
        return tuple(self._calls)

    def _validated_inputs(
        self,
        tracklet: Tracklet,
        observations: tuple[Observation, ...],
        operation: str,
    ) -> tuple[Tracklet, dict[str, Observation]]:
        if type(tracklet) is not Tracklet:
            raise _error(PortErrorCode.INVALID_REQUEST, operation)
        if type(observations) is not tuple or len(observations) > MAX_PORT_BATCH_ITEMS:
            raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
        if not all(type(observation) is Observation for observation in observations):
            raise _error(PortErrorCode.INVALID_REQUEST, operation)
        invalid = False
        owned_tracklet: Tracklet | None = None
        owned_observations: tuple[Observation, ...] = ()
        try:
            owned_tracklet = _copy_tracklet(tracklet)
            owned_observations = tuple(
                _copy_observation(observation) for observation in observations
            )
        except (TypeError, ValueError):
            invalid = True
        if invalid or owned_tracklet is None:
            raise _error(PortErrorCode.INVALID_REQUEST, operation)
        by_id = {observation.observation_id: observation for observation in owned_observations}
        if len(by_id) != len(owned_observations) or set(by_id) != {
            point.observation_id for point in owned_tracklet.points
        }:
            raise _error(PortErrorCode.CONFLICT, operation)
        for point in owned_tracklet.points:
            observation = by_id[point.observation_id]
            if (
                observation.source_id != owned_tracklet.source_id
                or observation.stream_index != owned_tracklet.stream_index
                or observation.category != owned_tracklet.category
                or observation.source_id != point.source_id
                or observation.stream_index != point.stream_index
                or observation.category != point.category
                or observation.frame_id != point.frame_id
                or observation.pts != point.pts
                or observation.geometry != point.geometry
            ):
                raise _error(PortErrorCode.CONFLICT, operation)
        return owned_tracklet, by_id

    def _plan(
        self,
        tracklet: Tracklet,
        observations: tuple[Observation, ...],
        operation: str,
    ) -> EvidencePlanResult:
        owned_tracklet, by_id = self._validated_inputs(tracklet, observations, operation)
        candidates = tuple(
            (
                _score(
                    by_id[point.observation_id],
                    point_index,
                    len(owned_tracklet.points),
                    owned_tracklet.start_pts,
                    owned_tracklet.end_pts,
                ),
                by_id[point.observation_id],
            )
            for point_index, point in enumerate(owned_tracklet.points)
        )
        selected = sorted(candidates, key=lambda item: item[0].rank_key())[
            : self._limits.max_selected_observations
        ]
        intents = tuple(
            EvidenceIntent(
                rank,
                owned_tracklet.tracklet_id,
                observation.observation_id,
                observation.source_id,
                observation.frame_id,
                observation.stream_index,
                _copy_time(observation.pts),
                _copy_geometry(observation.geometry),
                score,
                self.producer,
            )
            for rank, (score, observation) in enumerate(selected, start=1)
        )
        return EvidencePlanResult(PerceptionResultState.COMPLETE, intents)

    def select(
        self,
        tracklet: Tracklet,
        observations: tuple[Observation, ...],
    ) -> EvidenceSelectionResult:
        """Implement the stable metadata-only ``EvidenceSelector`` port."""

        failed = False
        result: EvidenceSelectionResult | None = None
        try:
            plan = self._plan(tracklet, observations, "select")
            result = EvidenceSelectionResult(plan.state, plan.observation_ids, plan.reason)
        except PortError:
            raise
        except BaseException:
            failed = True
        if failed or result is None:
            raise _error(PortErrorCode.INVALID_REQUEST, "select")
        self._calls.append(PortCall(PortKind.EVIDENCE_SELECTOR, "select", len(observations)))
        return result

    def plan(
        self,
        tracklet: Tracklet,
        observations: tuple[Observation, ...],
    ) -> EvidencePlanResult:
        """Return the selected source-coordinate intents and frozen score details."""

        failed = False
        result: EvidencePlanResult | None = None
        try:
            result = self._plan(tracklet, observations, "plan")
        except PortError:
            raise
        except BaseException:
            failed = True
        if failed or result is None:
            raise _error(PortErrorCode.INVALID_REQUEST, "plan")
        self._calls.append(PortCall(PortKind.EVIDENCE_SELECTOR, "plan", len(observations)))
        return result

    def materialize(
        self,
        intent: EvidenceIntent,
        tracklet: Tracklet,
        observations: tuple[Observation, ...],
        frame_rgb24: bytes | None,
        *,
        need: EvidenceNeed,
        detail_resolution: DetailResolution,
    ) -> EvidenceCropResult:
        """Validate selection context, then crop caller-supplied source RGB24."""

        failed = False
        result: EvidenceCropResult | None = None
        try:
            result = self._materialize(
                intent,
                tracklet,
                observations,
                frame_rgb24,
                need=need,
                detail_resolution=detail_resolution,
            )
        except PortError:
            raise
        except BaseException:
            failed = True
        if failed or result is None:
            raise _error(PortErrorCode.INVALID_REQUEST, "materialize")
        self._calls.append(PortCall(PortKind.EVIDENCE_SELECTOR, "materialize", 1))
        return result

    def _materialize(
        self,
        intent: EvidenceIntent,
        tracklet: Tracklet,
        observations: tuple[Observation, ...],
        frame_rgb24: bytes | None,
        *,
        need: EvidenceNeed,
        detail_resolution: DetailResolution,
    ) -> EvidenceCropResult:
        """Implement the sanitized public materialization operation."""

        operation = "materialize"
        if type(need) is not EvidenceNeed or type(detail_resolution) is not DetailResolution:
            raise _error(PortErrorCode.INVALID_REQUEST, operation)
        invalid_intent = False
        owned_intent: EvidenceIntent | None = None
        try:
            owned_intent = _copy_intent(intent)
        except (TypeError, ValueError):
            invalid_intent = True
        if invalid_intent or owned_intent is None:
            raise _error(PortErrorCode.INVALID_REQUEST, operation)
        expected = self._plan(tracklet, observations, operation)
        if owned_intent not in expected.intents:
            raise _error(PortErrorCode.CONFLICT, operation)
        if frame_rgb24 is None:
            result = EvidenceCropResult(
                PerceptionResultState.UNKNOWN,
                reason="source_pixels_unavailable",
            )
        elif detail_resolution is DetailResolution.UNRESOLVABLE:
            result = EvidenceCropResult(
                PerceptionResultState.UNKNOWN,
                reason="detail_unresolvable",
            )
        elif (
            need is EvidenceNeed.DOWNSTREAM_DETAIL and detail_resolution is DetailResolution.UNKNOWN
        ):
            result = EvidenceCropResult(
                PerceptionResultState.UNKNOWN,
                reason="detail_resolution_unknown",
            )
        else:
            if type(frame_rgb24) is not bytes:
                raise _error(PortErrorCode.INVALID_REQUEST, operation)
            expected_frame_bytes = (
                owned_intent.geometry.source_width * owned_intent.geometry.source_height * 3
            )
            if expected_frame_bytes > MAX_EVIDENCE_RGB24_FRAME_BYTES:
                raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
            invalid_crop = False
            crop: Rgb24Crop | None = None
            reference: EvidenceRef | None = None
            try:
                crop = extract_rgb24_crop(
                    frame_rgb24,
                    owned_intent.geometry.source_width,
                    owned_intent.geometry.source_height,
                    owned_intent.geometry,
                )
                reference = EvidenceRef.create(
                    owned_intent.frame_id,
                    crop.artifact(),
                    owned_intent.geometry,
                    owned_intent.kind,
                    owned_intent.retention,
                )
            except (CropError, TypeError, ValueError):
                invalid_crop = True
            if invalid_crop or crop is None or reference is None:
                raise _error(PortErrorCode.INVALID_REQUEST, operation)
            result = EvidenceCropResult(
                PerceptionResultState.COMPLETE,
                MaterializedEvidence(owned_intent, need, detail_resolution, reference, crop),
            )
        return result


__all__ = [
    "DEFAULT_EVIDENCE_SELECTOR_CONFIGURATION_SHA256",
    "DEFAULT_MAX_SELECTED_OBSERVATIONS",
    "MAX_EVIDENCE_RGB24_FRAME_BYTES",
    "MAX_SELECTED_OBSERVATIONS",
    "BestFrameEvidenceSelector",
    "DetailResolution",
    "EvidenceCropResult",
    "EvidenceIntent",
    "EvidenceNeed",
    "EvidencePlanResult",
    "EvidenceScore",
    "EvidenceSelectionLimits",
    "MaterializedEvidence",
    "dumps_evidence_intent",
    "load_evidence_intent",
    "loads_evidence_intent",
]
