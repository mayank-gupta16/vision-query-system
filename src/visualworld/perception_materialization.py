# SPDX-License-Identifier: Apache-2.0
"""Original-frame discontinuity scoring and bounded evidence materialization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from visualworld.evidence import (
    MAX_EVIDENCE_RGB24_FRAME_BYTES,
    DetailResolution,
    EvidenceCropResult,
    EvidenceIntent,
    EvidenceNeed,
    MaterializedEvidence,
)
from visualworld.frame_access import (
    MAX_ORIGINAL_FRAME_TOTAL_BYTES,
    OriginalFrame,
    OriginalFrameReader,
    OriginalFrameReadResult,
)
from visualworld.ingestion import FrameRef, Producer, Source
from visualworld.perception import FrameDiscontinuity, Observation, Tracklet
from visualworld.ports import (
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    PerceptionResultState,
    PortError,
    PortErrorCode,
    PortKind,
)
from visualworld.tracking import rgb24_discontinuity_basis_points

if TYPE_CHECKING:
    from visualworld.coordinator import FrameDiscontinuityResult

MATERIALIZATION_PROTOCOL_VERSION = 1
MAX_TOTAL_EVIDENCE_BYTES = 256 * 1024 * 1024
MAX_MATERIALIZED_EVIDENCE_ITEMS = 4_096
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _error(code: PortErrorCode, operation: str) -> PortError:
    return PortError(code, PortKind.EVIDENCE_SELECTOR, operation)


@dataclass(frozen=True, slots=True)
class EvidenceMaterializationConfig:
    """Explicit policy and byte limits for an evidence-bearing perception run.

    ``max_total_original_frame_bytes`` bounds each reader call. Discontinuity
    scoring reads at most one predecessor/current pair, while crop
    materialization reads exactly one selected frame at a time.
    ``max_total_evidence_bytes`` bounds the sum of all crop bytes before any
    artifact is staged.
    """

    need: EvidenceNeed = EvidenceNeed.INSPECTION
    detail_resolution: DetailResolution = DetailResolution.UNKNOWN
    max_total_original_frame_bytes: int = MAX_ORIGINAL_FRAME_TOTAL_BYTES
    max_total_evidence_bytes: int = MAX_EVIDENCE_RGB24_FRAME_BYTES
    protocol_version: int = MATERIALIZATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.need) is not EvidenceNeed
            or type(self.detail_resolution) is not DetailResolution
            or type(self.max_total_original_frame_bytes) is not int
            or not 3 <= self.max_total_original_frame_bytes <= MAX_ORIGINAL_FRAME_TOTAL_BYTES
            or type(self.max_total_evidence_bytes) is not int
            or not 0 <= self.max_total_evidence_bytes <= MAX_TOTAL_EVIDENCE_BYTES
            or type(self.protocol_version) is not int
            or self.protocol_version != MATERIALIZATION_PROTOCOL_VERSION
        ):
            raise ValueError("invalid evidence materialization configuration")

    def to_mapping(self) -> dict[str, object]:
        self.__post_init__()
        return {
            "detail_resolution": self.detail_resolution.value,
            "max_total_evidence_bytes": self.max_total_evidence_bytes,
            "max_total_original_frame_bytes": self.max_total_original_frame_bytes,
            "need": self.need.value,
            "protocol_version": self.protocol_version,
        }


def materialization_producer(config: EvidenceMaterializationConfig) -> Producer:
    if type(config) is not EvidenceMaterializationConfig:
        raise ValueError("invalid evidence materialization configuration")
    encoded = json.dumps(config.to_mapping(), sort_keys=True, separators=(",", ":")).encode()
    return Producer(
        "visualworld.perception-evidence-materialization",
        "1",
        hashlib.sha256(encoded).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class EvidenceMaterializationResult:
    """All-or-nothing transient crop batch produced before publication."""

    state: PerceptionResultState
    materialized: tuple[MaterializedEvidence, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.state) is not PerceptionResultState
            or type(self.materialized) is not tuple
            or len(self.materialized) > MAX_MATERIALIZED_EVIDENCE_ITEMS
            or not all(type(item) is MaterializedEvidence for item in self.materialized)
        ):
            raise ValueError("invalid evidence materialization result")
        for item in self.materialized:
            MaterializedEvidence.__post_init__(item)
        if self.state is PerceptionResultState.COMPLETE:
            if self.reason is not None:
                raise ValueError("complete materialization has a reason")
        elif (
            self.materialized
            or type(self.reason) is not str
            or not _REASON_RE.fullmatch(self.reason)
        ):
            raise ValueError("incomplete materialization is invalid")

    @classmethod
    def complete(
        cls, materialized: tuple[MaterializedEvidence, ...]
    ) -> EvidenceMaterializationResult:
        return cls(PerceptionResultState.COMPLETE, materialized)


class MaterializingEvidencePlanner(Protocol):
    def materialize(
        self,
        intent: EvidenceIntent,
        tracklet: Tracklet,
        observations: tuple[Observation, ...],
        frame_rgb24: bytes | None,
        *,
        need: EvidenceNeed,
        detail_resolution: DetailResolution,
    ) -> EvidenceCropResult: ...


class OriginalFrameDiscontinuityProvider:
    """Adapt exact reader pixels to the existing pixel-free scorer contract."""

    def __init__(
        self,
        reader: OriginalFrameReader,
        reader_descriptor: CapabilityDescriptor,
        config: EvidenceMaterializationConfig,
    ) -> None:
        if (
            type(reader_descriptor) is not CapabilityDescriptor
            or reader_descriptor.port is not PortKind.ORIGINAL_FRAME_READER
            or type(config) is not EvidenceMaterializationConfig
        ):
            raise ValueError("invalid original-frame discontinuity adapter")
        config.__post_init__()
        self._reader = reader
        self._reader_descriptor = reader_descriptor
        self._config = config
        encoded = json.dumps(
            {
                "algorithm": "rgb24_every_fourth_pixel_mean_absolute_delta",
                "algorithm_version": 1,
                "materialization": config.to_mapping(),
                "reader_implementation": reader_descriptor.implementation,
                "reader_implementation_version": reader_descriptor.implementation_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self._producer = Producer(
            "visualworld.original-frame-discontinuity",
            "1",
            hashlib.sha256(encoded).hexdigest(),
        )
        self._descriptor = CapabilityDescriptor(
            PortKind.FRAME_DISCONTINUITY,
            "visualworld.original-frame-discontinuity",
            "1",
            deterministic=True,
            offline=True,
            max_batch_items=MAX_PORT_BATCH_ITEMS,
        )

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    @property
    def producer(self) -> Producer:
        return self._producer

    def _read(self, source: Source, requested: tuple[FrameRef, ...]) -> OriginalFrameReadResult:
        if len(requested) > self._reader_descriptor.max_batch_items:
            raise PortError(
                PortErrorCode.LIMIT_EXCEEDED,
                PortKind.ORIGINAL_FRAME_READER,
                "read",
            )
        result = self._reader.read(
            source,
            requested,
            max_total_bytes=self._config.max_total_original_frame_bytes,
        )
        if type(result) is not OriginalFrameReadResult:
            raise PortError(
                PortErrorCode.INVALID_REQUEST,
                PortKind.ORIGINAL_FRAME_READER,
                "read",
            )
        try:
            OriginalFrameReadResult.__post_init__(result)
        except (TypeError, ValueError):
            raise PortError(
                PortErrorCode.INVALID_REQUEST,
                PortKind.ORIGINAL_FRAME_READER,
                "read",
            ) from None
        if result.state is PerceptionResultState.COMPLETE and (
            len(result.frames) != len(requested)
            or any(
                original.source != source or original.frame != frame
                for original, frame in zip(result.frames, requested, strict=True)
            )
        ):
            raise PortError(
                PortErrorCode.CONFLICT,
                PortKind.ORIGINAL_FRAME_READER,
                "read",
            )
        return result

    def score(
        self,
        source: Source,
        previous_selected: FrameRef | None,
        frames: tuple[FrameRef, ...],
    ) -> FrameDiscontinuityResult:
        # Local import avoids coupling the generic reader module to coordinator.
        from visualworld.coordinator import FrameDiscontinuityResult

        if not frames:
            return FrameDiscontinuityResult.complete(())
        scored: list[FrameDiscontinuity] = []
        previous = previous_selected
        for current in frames:
            requested = (current,) if previous is None else (previous, current)
            result = self._read(source, requested)
            if result.state is not PerceptionResultState.COMPLETE:
                return FrameDiscontinuityResult(result.state, reason=result.reason)
            current_original = result.frames[-1]
            previous_pixels = result.frames[0].pixels if previous is not None else None
            score = rgb24_discontinuity_basis_points(
                previous_pixels,
                current_original.pixels,
                width=current_original.width,
                height=current_original.height,
            )
            scored.append(FrameDiscontinuity.from_frame(current, score))
            previous = current
            del current_original, previous_pixels, result
        return FrameDiscontinuityResult.complete(tuple(scored))


class OriginalFrameMaterializer:
    """Materialize every authorized intent, retaining only bounded crops."""

    def __init__(
        self,
        reader: OriginalFrameReader,
        reader_descriptor: CapabilityDescriptor,
        selector: object,
        config: EvidenceMaterializationConfig,
    ) -> None:
        if (
            type(reader_descriptor) is not CapabilityDescriptor
            or reader_descriptor.port is not PortKind.ORIGINAL_FRAME_READER
            or type(config) is not EvidenceMaterializationConfig
            or not callable(getattr(selector, "materialize", None))
        ):
            raise ValueError("invalid original-frame materializer")
        config.__post_init__()
        self._reader = reader
        self._reader_descriptor = reader_descriptor
        self._selector = cast(MaterializingEvidencePlanner, selector)
        self._config = config

    def _read_one(self, source: Source, frame: FrameRef) -> OriginalFrameReadResult:
        result = self._reader.read(
            source,
            (frame,),
            max_total_bytes=self._config.max_total_original_frame_bytes,
        )
        if type(result) is not OriginalFrameReadResult:
            raise PortError(
                PortErrorCode.INVALID_REQUEST,
                PortKind.ORIGINAL_FRAME_READER,
                "read",
            )
        try:
            OriginalFrameReadResult.__post_init__(result)
        except (TypeError, ValueError):
            raise PortError(
                PortErrorCode.INVALID_REQUEST,
                PortKind.ORIGINAL_FRAME_READER,
                "read",
            ) from None
        if result.state is PerceptionResultState.COMPLETE and (
            len(result.frames) != 1
            or result.frames[0].source != source
            or result.frames[0].frame != frame
        ):
            raise PortError(
                PortErrorCode.CONFLICT,
                PortKind.ORIGINAL_FRAME_READER,
                "read",
            )
        return result

    def materialize(
        self,
        source: Source,
        frames: tuple[FrameRef, ...],
        observations: tuple[Observation, ...],
        tracklets: tuple[Tracklet, ...],
        intents: tuple[EvidenceIntent, ...],
    ) -> EvidenceMaterializationResult:
        if intents and self._config.detail_resolution is DetailResolution.UNRESOLVABLE:
            return EvidenceMaterializationResult(
                PerceptionResultState.UNKNOWN, reason="detail_unresolvable"
            )
        if (
            intents
            and self._config.need is EvidenceNeed.DOWNSTREAM_DETAIL
            and self._config.detail_resolution is DetailResolution.UNKNOWN
        ):
            return EvidenceMaterializationResult(
                PerceptionResultState.UNKNOWN, reason="detail_resolution_unknown"
            )
        by_frame = {frame.frame_id: frame for frame in frames}
        by_observation = {item.observation_id: item for item in observations}
        by_tracklet = {item.tracklet_id: item for item in tracklets}
        if (
            len(by_frame) != len(frames)
            or len(by_observation) != len(observations)
            or len(by_tracklet) != len(tracklets)
        ):
            raise _error(PortErrorCode.CONFLICT, "materialize_run")
        selected: list[MaterializedEvidence] = []
        total_crop_bytes = 0
        evidence_ids: set[str] = set()
        for intent in intents:
            frame = by_frame.get(intent.frame_id)
            tracklet = by_tracklet.get(intent.tracklet_id)
            if frame is None or tracklet is None:
                raise _error(PortErrorCode.CONFLICT, "materialize_run")
            selected_observations = tuple(
                by_observation[point.observation_id]
                for point in tracklet.points
                if point.observation_id in by_observation
            )
            if len(selected_observations) != len(tracklet.points):
                raise _error(PortErrorCode.CONFLICT, "materialize_run")
            read = self._read_one(source, frame)
            if read.state is not PerceptionResultState.COMPLETE:
                return EvidenceMaterializationResult(read.state, reason=read.reason)
            original: OriginalFrame = read.frames[0]
            result = self._selector.materialize(
                intent,
                tracklet,
                selected_observations,
                original.pixels,
                need=self._config.need,
                detail_resolution=self._config.detail_resolution,
            )
            if type(result) is not EvidenceCropResult:
                raise _error(PortErrorCode.INVALID_REQUEST, "materialize_run")
            try:
                EvidenceCropResult.__post_init__(result)
            except (TypeError, ValueError):
                raise _error(PortErrorCode.INVALID_REQUEST, "materialize_run") from None
            if result.state is not PerceptionResultState.COMPLETE:
                return EvidenceMaterializationResult(result.state, reason=result.reason)
            item = result.materialized
            if (
                item is None
                or item.intent != intent
                or item.need is not self._config.need
                or item.detail_resolution is not self._config.detail_resolution
            ):
                raise _error(PortErrorCode.CONFLICT, "materialize_run")
            del original, read
            total_crop_bytes += len(item.crop.pixels)
            if total_crop_bytes > self._config.max_total_evidence_bytes:
                raise _error(PortErrorCode.LIMIT_EXCEEDED, "materialize_run")
            # Schema v2 currently makes evidence_id unique per run. Until that
            # schema is relaxed, reject an ambiguous shared-reference batch.
            if item.reference.evidence_id in evidence_ids:
                raise _error(PortErrorCode.CONFLICT, "materialize_run")
            evidence_ids.add(item.reference.evidence_id)
            selected.append(item)
        return EvidenceMaterializationResult.complete(tuple(selected))


__all__ = [
    "MATERIALIZATION_PROTOCOL_VERSION",
    "MAX_MATERIALIZED_EVIDENCE_ITEMS",
    "MAX_TOTAL_EVIDENCE_BYTES",
    "EvidenceMaterializationConfig",
    "EvidenceMaterializationResult",
    "OriginalFrameDiscontinuityProvider",
    "OriginalFrameMaterializer",
    "materialization_producer",
]
