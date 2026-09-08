# SPDX-License-Identifier: Apache-2.0
"""Deterministic best-frame planning and explicit original-pixel materialization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from visualworld.geometry import CropError, Rgb24Crop, extract_rgb24_crop
from visualworld.ingestion import EvidenceRef, Geometry, MediaTime, Producer
from visualworld.perception import MAX_CONFIDENCE_MILLIONTHS, Observation, Tracklet
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


def _error(code: PortErrorCode, operation: str) -> PortError:
    return PortError(code, PortKind.EVIDENCE_SELECTOR, operation)


def _copy_time(value: MediaTime) -> MediaTime:
    if type(value) is not MediaTime:
        raise ValueError("invalid time")
    return MediaTime.from_mapping(value.to_mapping(), "pts")


def _copy_geometry(value: Geometry) -> Geometry:
    if type(value) is not Geometry:
        raise ValueError("invalid geometry")
    return Geometry.from_mapping(value.to_mapping())


def _copy_producer(value: Producer) -> Producer:
    if type(value) is not Producer:
        raise ValueError("invalid producer")
    return Producer.from_mapping(value.to_mapping())


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
        if not 1 <= self.visible_area_pixels <= self.source_area_pixels:
            raise ValueError("visible area is invalid")
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
        _copy_time(self.pts)
        if type(self.observation_id) is not str or not _OBSERVATION_ID_RE.fullmatch(
            self.observation_id
        ):
            raise ValueError("observation identifier is invalid")

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
        _copy_time(self.pts)
        _copy_geometry(self.geometry)
        if type(self.score) is not EvidenceScore:
            raise ValueError("evidence score is invalid")
        self.score.__post_init__()
        _copy_producer(self.selector)
        if self.score.observation_id != self.observation_id or self.score.pts != self.pts:
            raise ValueError("evidence score scope is inconsistent")
        x_min, y_min, x_max, y_max = self.geometry.box_xyxy
        visible_area = (x_max - x_min) * (y_max - y_min)
        source_area = self.geometry.source_width * self.geometry.source_height
        boundary_touches = sum(
            (
                x_min == 0,
                y_min == 0,
                x_max == self.geometry.source_width,
                y_max == self.geometry.source_height,
            )
        )
        if (
            self.score.boundary_touch_count != boundary_touches
            or self.score.visible_area_pixels != visible_area
            or self.score.source_area_pixels != source_area
        ):
            raise ValueError("evidence score geometry is inconsistent")
        if (
            self.kind != _KIND
            or self.retention != _RETENTION
            or self.deletion_owner != _DELETION_OWNER
        ):
            raise ValueError("evidence lifecycle policy is invalid")

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
        self.intent.__post_init__()
        if type(self.need) is not EvidenceNeed:
            raise ValueError("evidence need is invalid")
        if type(self.detail_resolution) is not DetailResolution:
            raise ValueError("detail resolution is invalid")
        if type(self.reference) is not EvidenceRef:
            raise ValueError("evidence reference is invalid")
        EvidenceRef.__post_init__(self.reference)
        if type(self.crop) is not Rgb24Crop:
            raise ValueError("evidence crop is invalid")
        self.crop.__post_init__()
        if (
            self.reference.frame_id != self.intent.frame_id
            or self.reference.geometry != self.intent.geometry
            or self.reference.kind != self.intent.kind
            or self.reference.retention != self.intent.retention
            or self.reference.artifact != self.crop.artifact()
        ):
            raise ValueError("materialized evidence is inconsistent")

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


def _copy_intent(value: EvidenceIntent) -> EvidenceIntent:
    if type(value) is not EvidenceIntent:
        raise ValueError("invalid evidence intent")
    value.__post_init__()
    score = value.score
    return EvidenceIntent(
        value.rank,
        value.tracklet_id,
        value.observation_id,
        value.source_id,
        value.frame_id,
        value.stream_index,
        _copy_time(value.pts),
        _copy_geometry(value.geometry),
        EvidenceScore(
            score.boundary_touch_count,
            score.confidence_millionths,
            score.visible_area_pixels,
            score.source_area_pixels,
            score.visible_area_millionths,
            score.midpoint_distance_seconds_x2_numerator,
            score.midpoint_distance_seconds_x2_denominator,
            score.point_index,
            score.point_count,
            _copy_time(score.pts),
            score.observation_id,
        ),
        _copy_producer(value.selector),
        value.kind,
        value.retention,
        value.deletion_owner,
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
            Tracklet.__post_init__(tracklet)
            for observation in observations:
                Observation.__post_init__(observation)
            owned_tracklet = Tracklet.from_mapping(tracklet.to_mapping())
            owned_observations = tuple(
                Observation.from_mapping(observation.to_mapping()) for observation in observations
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

        plan = self._plan(tracklet, observations, "select")
        result = EvidenceSelectionResult(plan.state, plan.observation_ids, plan.reason)
        self._calls.append(PortCall(PortKind.EVIDENCE_SELECTOR, "select", len(observations)))
        return result

    def plan(
        self,
        tracklet: Tracklet,
        observations: tuple[Observation, ...],
    ) -> EvidencePlanResult:
        """Return the selected source-coordinate intents and frozen score details."""

        result = self._plan(tracklet, observations, "plan")
        self._calls.append(PortCall(PortKind.EVIDENCE_SELECTOR, "plan", len(observations)))
        return result

    def materialize(
        self,
        intent: EvidenceIntent,
        frame_rgb24: bytes | None,
        *,
        need: EvidenceNeed,
        detail_resolution: DetailResolution,
    ) -> EvidenceCropResult:
        """Crop exact source RGB24 only after a caller declares an evidence need."""

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
        if owned_intent.selector != self._producer:
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
        self._calls.append(PortCall(PortKind.EVIDENCE_SELECTOR, operation, 1))
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
]
