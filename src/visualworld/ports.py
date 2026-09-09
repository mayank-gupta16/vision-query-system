# SPDX-License-Identifier: Apache-2.0
"""Version-1 application ports and deterministic in-memory fakes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from visualworld.ingestion import (
    Artifact,
    EvidenceRef,
    Fingerprint,
    FrameRef,
    MediaTime,
    Producer,
    Record,
    RunManifest,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
    dumps_record,
)
from visualworld.perception import FrameDiscontinuity, Observation, Tracklet

MAX_PORT_BATCH_ITEMS = 64
MAX_PERCEPTION_OBSERVATIONS = MAX_PORT_BATCH_ITEMS * MAX_PORT_BATCH_ITEMS
MAX_FAKE_ARTIFACT_BYTES = 1024 * 1024
_MAX_STREAM_INDEX = 2**31 - 1

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}\Z")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_RECORD_ID_RE = re.compile(r"(?:src|frm|evi|run)_[0-9a-f]{64}\Z")
_UNSIGNED_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")


class PortKind(StrEnum):
    VIDEO_SOURCE = "video_source"
    FRAME_SAMPLER = "frame_sampler"
    FRAME_DISCONTINUITY = "frame_discontinuity"
    DETECTOR = "detector"
    TRACKER = "tracker"
    EVIDENCE_SELECTOR = "evidence_selector"
    EVIDENCE_STORE = "evidence_store"
    WORLD_STORE = "world_store"


class Effect(StrEnum):
    """Ambient effects that application orchestration never grants directly."""

    SHELL = "shell"
    NETWORK = "network"
    ARBITRARY_FILESYSTEM = "arbitrary_filesystem"
    RAW_SQL = "raw_sql"


class PortErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    LIMIT_EXCEEDED = "limit_exceeded"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    CAPABILITY_DENIED = "capability_denied"
    UNSUPPORTED = "unsupported"
    ISOLATION_UNAVAILABLE = "isolation_unavailable"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    DECODE_FAILED = "decode_failed"
    CORRUPT = "corrupt"
    STORAGE_FAILED = "storage_failed"


class PerceptionResultState(StrEnum):
    COMPLETE = "complete"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


def _validate_result(
    state: PerceptionResultState,
    reason: str | None,
    output_count: int,
) -> None:
    if type(state) is not PerceptionResultState:
        raise ValueError("state must use PerceptionResultState")
    if state is PerceptionResultState.COMPLETE:
        if reason is not None:
            raise ValueError("complete result cannot have a reason")
        return
    if output_count:
        raise ValueError("incomplete result cannot contain outputs")
    if type(reason) is not str or not _REASON_RE.fullmatch(reason):
        raise ValueError("incomplete result requires a bounded stable reason code")


@dataclass(frozen=True, slots=True)
class DetectionResult:
    state: PerceptionResultState
    observations: tuple[Observation, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.observations) is not tuple
            or len(self.observations) > MAX_PERCEPTION_OBSERVATIONS
        ):
            raise ValueError("observations exceed the port bound")
        if not all(type(item) is Observation for item in self.observations):
            raise ValueError("observations must contain Observation records")
        for item in self.observations:
            Observation.__post_init__(item)
        if len({item.observation_id for item in self.observations}) != len(self.observations):
            raise ValueError("observation identifiers must be unique")
        _validate_result(self.state, self.reason, len(self.observations))


@dataclass(frozen=True, slots=True)
class TrackingResult:
    state: PerceptionResultState
    tracklets: tuple[Tracklet, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.tracklets) is not tuple or len(self.tracklets) > MAX_PORT_BATCH_ITEMS:
            raise ValueError("tracklets exceed the port bound")
        if not all(type(item) is Tracklet for item in self.tracklets):
            raise ValueError("tracklets must contain Tracklet records")
        for item in self.tracklets:
            Tracklet.__post_init__(item)
        if len({item.tracklet_id for item in self.tracklets}) != len(self.tracklets):
            raise ValueError("tracklet identifiers must be unique")
        _validate_result(self.state, self.reason, len(self.tracklets))


@dataclass(frozen=True, slots=True)
class EvidenceSelectionResult:
    state: PerceptionResultState
    observation_ids: tuple[str, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.observation_ids) is not tuple
            or len(self.observation_ids) > MAX_PORT_BATCH_ITEMS
        ):
            raise ValueError("selected observations exceed the port bound")
        if not all(
            type(item) is str and re.fullmatch(r"obs_[0-9a-f]{64}", item)
            for item in self.observation_ids
        ):
            raise ValueError("selected observation identifiers are invalid")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("selected observation identifiers must be unique")
        _validate_result(self.state, self.reason, len(self.observation_ids))


class PortError(RuntimeError):
    """Structured port failure without untrusted content in its message."""

    def __init__(
        self,
        code: PortErrorCode,
        port: PortKind,
        operation: str,
        *,
        retryable: bool = False,
    ) -> None:
        if not isinstance(code, PortErrorCode) or not isinstance(port, PortKind):
            raise ValueError("code and port must use the v1 enums")
        if not isinstance(operation, str) or not _TOKEN_RE.fullmatch(operation):
            raise ValueError("operation must be a bounded ASCII token")
        if type(retryable) is not bool:
            raise ValueError("retryable must be a boolean")
        self.code = code
        self.port = port
        self.operation = operation
        self.retryable = retryable
        super().__init__(f"{code.value} at {port.value}.{operation}")


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    port: PortKind
    implementation: str
    implementation_version: str
    deterministic: bool
    offline: bool
    max_batch_items: int = MAX_PORT_BATCH_ITEMS
    max_payload_bytes: int | None = None
    contract_version: int = 1
    allowed_effects: tuple[Effect, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.port, PortKind):
            raise ValueError("port must be a PortKind")
        for value in (self.implementation, self.implementation_version):
            if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
                raise ValueError("implementation identity must be a bounded ASCII token")
        if type(self.deterministic) is not bool or type(self.offline) is not bool:
            raise ValueError("deterministic and offline must be booleans")
        if type(self.contract_version) is not int or self.contract_version != 1:
            raise ValueError("only port contract version 1 is supported")
        if (
            type(self.max_batch_items) is not int
            or not 1 <= self.max_batch_items <= MAX_PORT_BATCH_ITEMS
        ):
            raise ValueError("max_batch_items is outside the v1 bound")
        if self.max_payload_bytes is not None and (
            type(self.max_payload_bytes) is not int or not 0 <= self.max_payload_bytes <= 2**63 - 1
        ):
            raise ValueError("max_payload_bytes is outside the v1 bound")
        if not isinstance(self.allowed_effects, tuple) or not all(
            isinstance(effect, Effect) for effect in self.allowed_effects
        ):
            raise ValueError("allowed_effects must be an immutable Effect tuple")
        if self.allowed_effects:
            raise ValueError("v1 orchestration never grants ambient effects")

    def require(self, effect: Effect) -> None:
        """Fail closed when orchestration requests an undeclared ambient effect."""
        if not isinstance(effect, Effect) or effect not in self.allowed_effects:
            raise PortError(
                PortErrorCode.CAPABILITY_DENIED,
                self.port,
                "require_effect",
            )


@dataclass(frozen=True, slots=True)
class PortCall:
    port: PortKind
    operation: str
    item_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.port, PortKind):
            raise ValueError("port must be a PortKind")
        if not isinstance(self.operation, str) or not _TOKEN_RE.fullmatch(self.operation):
            raise ValueError("operation must be a bounded ASCII token")
        if type(self.item_count) is not int or not 0 <= self.item_count <= MAX_PORT_BATCH_ITEMS:
            raise ValueError("item_count is outside the v1 bound")


@runtime_checkable
class VideoSource(Protocol):
    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    def probe(self) -> Source: ...

    def read_frames(
        self,
        *,
        stream_index: int,
        after_decode_index: str | None = None,
        limit: int = MAX_PORT_BATCH_ITEMS,
    ) -> tuple[FrameRef, ...]: ...


@runtime_checkable
class FrameSampler(Protocol):
    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    def sample(
        self,
        source: Source,
        candidates: tuple[FrameRef, ...],
        sampling: Sampling,
    ) -> tuple[FrameRef, ...]: ...


@runtime_checkable
class Detector(Protocol):
    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    def detect(
        self,
        source: Source,
        frames: tuple[FrameRef, ...],
    ) -> DetectionResult: ...


@runtime_checkable
class Tracker(Protocol):
    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    def track(
        self,
        source: Source,
        frames: tuple[FrameRef, ...],
        observations: tuple[Observation, ...],
        *,
        discontinuities: tuple[FrameDiscontinuity, ...] | None = None,
    ) -> TrackingResult: ...


@runtime_checkable
class EvidenceSelector(Protocol):
    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    def select(
        self,
        tracklet: Tracklet,
        observations: tuple[Observation, ...],
    ) -> EvidenceSelectionResult: ...


@runtime_checkable
class EvidenceStore(Protocol):
    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    def put(self, artifact: Artifact, content: bytes) -> Artifact: ...

    def get(self, digest: str) -> bytes: ...


@runtime_checkable
class WorldStore(Protocol):
    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    def commit(self, records: tuple[Record, ...]) -> None: ...

    def get(self, record_id: str) -> Record: ...

    def list_frames(
        self,
        source_id: str,
        *,
        stream_index: int,
        after_decode_index: str | None = None,
        limit: int,
    ) -> tuple[FrameRef, ...]: ...

    def list_evidence(self, frame_id: str, *, limit: int) -> tuple[EvidenceRef, ...]: ...


class _InstrumentedFake:
    def __init__(self, descriptor: CapabilityDescriptor) -> None:
        self._descriptor = descriptor
        self._calls: list[PortCall] = []

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    @property
    def calls(self) -> tuple[PortCall, ...]:
        return tuple(self._calls)

    def _record_call(self, operation: str, item_count: int) -> None:
        self._calls.append(PortCall(self.descriptor.port, operation, item_count))


def _fake_descriptor(
    port: PortKind,
    *,
    max_payload_bytes: int | None = None,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        port=port,
        implementation=f"visualworld.fake.{port.value}",
        implementation_version="1",
        deterministic=True,
        offline=True,
        max_payload_bytes=max_payload_bytes,
    )


def _port_error(code: PortErrorCode, port: PortKind, operation: str) -> PortError:
    return PortError(code, port, operation)


def _bounded_limit(limit: int, port: PortKind, operation: str) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_PORT_BATCH_ITEMS:
        raise _port_error(PortErrorCode.LIMIT_EXCEEDED, port, operation)
    return limit


def _unsigned_decimal(value: str, port: PortKind, operation: str) -> int:
    if not isinstance(value, str) or not _UNSIGNED_DECIMAL_RE.fullmatch(value):
        raise _port_error(PortErrorCode.INVALID_REQUEST, port, operation)
    if len(value) > 20:
        raise _port_error(PortErrorCode.LIMIT_EXCEEDED, port, operation)
    integer = int(value)
    if integer > 2**64 - 1:
        raise _port_error(PortErrorCode.LIMIT_EXCEEDED, port, operation)
    return integer


def _stream_index(value: int, port: PortKind, operation: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_STREAM_INDEX:
        raise _port_error(PortErrorCode.INVALID_REQUEST, port, operation)
    return value


def _digest(value: str, port: PortKind, operation: str) -> str:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        raise _port_error(PortErrorCode.INVALID_REQUEST, port, operation)
    return value


def _record_id(value: str, port: PortKind, operation: str) -> str:
    if not isinstance(value, str) or not _RECORD_ID_RE.fullmatch(value):
        raise _port_error(PortErrorCode.INVALID_REQUEST, port, operation)
    return value


def _identifier(record: Record) -> str:
    if isinstance(record, Source):
        return record.source_id
    if isinstance(record, FrameRef):
        return record.frame_id
    if isinstance(record, EvidenceRef):
        return record.evidence_id
    return record.run_id


class FakeVideoSource(_InstrumentedFake):
    def __init__(self, source: Source, frames: tuple[FrameRef, ...]) -> None:
        if not isinstance(source, Source):
            raise _port_error(PortErrorCode.INVALID_REQUEST, PortKind.VIDEO_SOURCE, "init")
        if not isinstance(frames, tuple) or len(frames) > MAX_PORT_BATCH_ITEMS:
            raise _port_error(PortErrorCode.LIMIT_EXCEEDED, PortKind.VIDEO_SOURCE, "init")
        if not all(isinstance(frame, FrameRef) for frame in frames):
            raise _port_error(PortErrorCode.INVALID_REQUEST, PortKind.VIDEO_SOURCE, "init")
        if any(frame.source_id != source.source_id for frame in frames):
            raise _port_error(PortErrorCode.INVALID_REQUEST, PortKind.VIDEO_SOURCE, "init")
        stream_indexes = {stream.stream_index for stream in source.streams}
        if any(frame.stream_index not in stream_indexes for frame in frames):
            raise _port_error(PortErrorCode.INVALID_REQUEST, PortKind.VIDEO_SOURCE, "init")
        if len({frame.frame_id for frame in frames}) != len(frames):
            raise _port_error(PortErrorCode.CONFLICT, PortKind.VIDEO_SOURCE, "init")
        positions = {(frame.stream_index, frame.decode_index) for frame in frames}
        if len(positions) != len(frames):
            raise _port_error(PortErrorCode.CONFLICT, PortKind.VIDEO_SOURCE, "init")
        super().__init__(_fake_descriptor(PortKind.VIDEO_SOURCE))
        self._source = source
        self._frames = tuple(sorted(frames, key=lambda frame: int(frame.decode_index)))

    def probe(self) -> Source:
        self._record_call("probe", 1)
        return self._source

    def read_frames(
        self,
        *,
        stream_index: int,
        after_decode_index: str | None = None,
        limit: int = MAX_PORT_BATCH_ITEMS,
    ) -> tuple[FrameRef, ...]:
        bounded_limit = _bounded_limit(limit, PortKind.VIDEO_SOURCE, "read_frames")
        selected_stream = _stream_index(
            stream_index,
            PortKind.VIDEO_SOURCE,
            "read_frames",
        )
        if selected_stream not in {stream.stream_index for stream in self._source.streams}:
            raise _port_error(PortErrorCode.NOT_FOUND, PortKind.VIDEO_SOURCE, "read_frames")
        after = -1
        if after_decode_index is not None:
            after = _unsigned_decimal(
                after_decode_index,
                PortKind.VIDEO_SOURCE,
                "read_frames",
            )
        result = tuple(
            frame
            for frame in self._frames
            if frame.stream_index == selected_stream and int(frame.decode_index) > after
        )[:bounded_limit]
        self._record_call("read_frames", len(result))
        return result


class FakeFrameSampler(_InstrumentedFake):
    def __init__(self, selected_frame_ids: tuple[str, ...]) -> None:
        if (
            not isinstance(selected_frame_ids, tuple)
            or len(selected_frame_ids) > MAX_PORT_BATCH_ITEMS
        ):
            raise _port_error(PortErrorCode.LIMIT_EXCEEDED, PortKind.FRAME_SAMPLER, "init")
        if (
            not all(isinstance(frame_id, str) for frame_id in selected_frame_ids)
            or len(set(selected_frame_ids)) != len(selected_frame_ids)
            or not all(
                re.fullmatch(r"frm_[0-9a-f]{64}", frame_id) for frame_id in selected_frame_ids
            )
        ):
            raise _port_error(PortErrorCode.INVALID_REQUEST, PortKind.FRAME_SAMPLER, "init")
        super().__init__(_fake_descriptor(PortKind.FRAME_SAMPLER))
        self._selected_frame_ids = selected_frame_ids

    def sample(
        self,
        source: Source,
        candidates: tuple[FrameRef, ...],
        sampling: Sampling,
    ) -> tuple[FrameRef, ...]:
        if not isinstance(source, Source) or not isinstance(sampling, Sampling):
            raise _port_error(
                PortErrorCode.INVALID_REQUEST,
                PortKind.FRAME_SAMPLER,
                "sample",
            )
        if not isinstance(candidates, tuple) or len(candidates) > MAX_PORT_BATCH_ITEMS:
            raise _port_error(PortErrorCode.LIMIT_EXCEEDED, PortKind.FRAME_SAMPLER, "sample")
        if not all(
            isinstance(frame, FrameRef) and frame.source_id == source.source_id
            for frame in candidates
        ):
            raise _port_error(
                PortErrorCode.INVALID_REQUEST,
                PortKind.FRAME_SAMPLER,
                "sample",
            )
        by_id = {frame.frame_id: frame for frame in candidates}
        if len(by_id) != len(candidates) or not set(self._selected_frame_ids).issubset(by_id):
            raise _port_error(PortErrorCode.CONFLICT, PortKind.FRAME_SAMPLER, "sample")
        result = tuple(by_id[frame_id] for frame_id in self._selected_frame_ids)
        self._record_call("sample", len(result))
        return result


def _plain_port_time(value: object) -> bool:
    if (
        type(value) is not MediaTime
        or type(value.value) is not str
        or type(value.basis) is not str
        or type(value.time_base) is not TimeBase
        or type(value.time_base.numerator) is not str
        or type(value.time_base.denominator) is not str
    ):
        return False
    if value.estimate_method is not None and type(value.estimate_method) is not str:
        return False
    producer = value.estimate_producer
    return producer is None or (
        type(producer) is Producer
        and type(producer.name) is str
        and type(producer.version) is str
        and type(producer.configuration_sha256) is str
    )


def _plain_perception_source(value: object) -> bool:
    if (
        type(value) is not Source
        or type(value.source_id) is not str
        or type(value.fingerprint) is not Fingerprint
        or type(value.fingerprint.digest) is not str
        or type(value.fingerprint.bytes) is not str
        or type(value.fingerprint.algorithm) is not str
        or type(value.streams) is not tuple
        or not all(
            type(stream) is SourceStream
            and type(stream.stream_index) is int
            and type(stream.width) is int
            and type(stream.height) is int
            and type(stream.rotation_degrees) is int
            and type(stream.media_type) is str
            and type(stream.time_base) is TimeBase
            and type(stream.time_base.numerator) is str
            and type(stream.time_base.denominator) is str
            for stream in value.streams
        )
    ):
        return False
    try:
        Fingerprint.__post_init__(value.fingerprint)
        for stream in value.streams:
            TimeBase.__post_init__(stream.time_base)
            SourceStream.__post_init__(stream)
        Source.__post_init__(value)
    except (TypeError, ValueError):
        return False
    return True


def _perception_frames(
    source: Source,
    frames: tuple[FrameRef, ...],
    port: PortKind,
    operation: str,
) -> dict[str, FrameRef]:
    if not _plain_perception_source(source):
        raise _port_error(PortErrorCode.INVALID_REQUEST, port, operation)
    if type(frames) is not tuple or len(frames) > MAX_PORT_BATCH_ITEMS:
        raise _port_error(PortErrorCode.LIMIT_EXCEEDED, port, operation)
    if not all(
        type(frame) is FrameRef
        and type(frame.frame_id) is str
        and type(frame.source_id) is str
        and type(frame.stream_index) is int
        and type(frame.decode_index) is str
        and _plain_port_time(frame.pts)
        and (frame.duration is None or _plain_port_time(frame.duration))
        for frame in frames
    ):
        raise _port_error(PortErrorCode.INVALID_REQUEST, port, operation)
    by_id = {frame.frame_id: frame for frame in frames}
    streams = {stream.stream_index: stream for stream in source.streams}
    positions = {(frame.stream_index, frame.decode_index) for frame in frames}
    if (
        len(streams) != len(source.streams)
        or len(by_id) != len(frames)
        or len(positions) != len(frames)
        or any(frame.source_id != source.source_id for frame in frames)
        or any(frame.stream_index not in streams for frame in frames)
        or any(frame.pts.time_base != streams[frame.stream_index].time_base for frame in frames)
        or any(
            frame.duration is not None
            and frame.duration.time_base != streams[frame.stream_index].time_base
            for frame in frames
        )
    ):
        raise _port_error(PortErrorCode.CONFLICT, port, operation)
    return by_id


def _perception_observations(
    source: Source,
    frames: dict[str, FrameRef],
    observations: tuple[Observation, ...],
    port: PortKind,
    operation: str,
) -> dict[str, Observation]:
    if type(observations) is not tuple or len(observations) > MAX_PERCEPTION_OBSERVATIONS:
        raise _port_error(PortErrorCode.LIMIT_EXCEEDED, port, operation)
    if not all(type(observation) is Observation for observation in observations):
        raise _port_error(PortErrorCode.INVALID_REQUEST, port, operation)
    for observation in observations:
        try:
            Observation.__post_init__(observation)
        except (TypeError, ValueError):
            raise _port_error(PortErrorCode.INVALID_REQUEST, port, operation) from None
    by_id = {observation.observation_id: observation for observation in observations}
    if len(by_id) != len(observations):
        raise _port_error(PortErrorCode.CONFLICT, port, operation)
    streams = {stream.stream_index: stream for stream in source.streams}
    for observation in observations:
        frame = frames.get(observation.frame_id)
        stream = streams.get(observation.stream_index)
        if (
            observation.source_id != source.source_id
            or frame is None
            or stream is None
            or observation.stream_index != frame.stream_index
            or observation.pts != frame.pts
            or observation.geometry.source_width != stream.width
            or observation.geometry.source_height != stream.height
        ):
            raise _port_error(PortErrorCode.CONFLICT, port, operation)
    return by_id


def _perception_discontinuities(
    frames: tuple[FrameRef, ...],
    discontinuities: tuple[FrameDiscontinuity, ...] | None,
    port: PortKind,
    operation: str,
) -> None:
    if discontinuities is None:
        return
    if type(discontinuities) is not tuple or len(discontinuities) != len(frames):
        raise _port_error(PortErrorCode.INVALID_REQUEST, port, operation)
    if not all(type(item) is FrameDiscontinuity for item in discontinuities):
        raise _port_error(PortErrorCode.INVALID_REQUEST, port, operation)
    try:
        for item in discontinuities:
            FrameDiscontinuity.__post_init__(item)
    except (TypeError, ValueError):
        raise _port_error(PortErrorCode.INVALID_REQUEST, port, operation) from None
    if any(
        item.source_id != frame.source_id
        or item.frame_id != frame.frame_id
        or item.stream_index != frame.stream_index
        or item.pts != frame.pts
        for item, frame in zip(discontinuities, frames, strict=True)
    ):
        raise _port_error(PortErrorCode.CONFLICT, port, operation)


class FakeDetector(_InstrumentedFake):
    def __init__(self, result: DetectionResult) -> None:
        if type(result) is not DetectionResult:
            raise _port_error(PortErrorCode.INVALID_REQUEST, PortKind.DETECTOR, "init")
        super().__init__(_fake_descriptor(PortKind.DETECTOR))
        self._result = result

    def detect(
        self,
        source: Source,
        frames: tuple[FrameRef, ...],
    ) -> DetectionResult:
        by_frame = _perception_frames(source, frames, PortKind.DETECTOR, "detect")
        _perception_observations(
            source,
            by_frame,
            self._result.observations,
            PortKind.DETECTOR,
            "detect",
        )
        self._record_call("detect", len(frames))
        return self._result


class FakeTracker(_InstrumentedFake):
    def __init__(self, result: TrackingResult) -> None:
        if type(result) is not TrackingResult:
            raise _port_error(PortErrorCode.INVALID_REQUEST, PortKind.TRACKER, "init")
        super().__init__(_fake_descriptor(PortKind.TRACKER))
        self._result = result

    def track(
        self,
        source: Source,
        frames: tuple[FrameRef, ...],
        observations: tuple[Observation, ...],
        *,
        discontinuities: tuple[FrameDiscontinuity, ...] | None = None,
    ) -> TrackingResult:
        by_frame = _perception_frames(source, frames, PortKind.TRACKER, "track")
        _perception_discontinuities(
            frames,
            discontinuities,
            PortKind.TRACKER,
            "track",
        )
        by_observation = _perception_observations(
            source,
            by_frame,
            observations,
            PortKind.TRACKER,
            "track",
        )
        tracked_observation_ids: list[str] = []
        for tracklet in self._result.tracklets:
            if tracklet.source_id != source.source_id:
                raise _port_error(PortErrorCode.CONFLICT, PortKind.TRACKER, "track")
            for point in tracklet.points:
                observation = by_observation.get(point.observation_id)
                if (
                    observation is None
                    or tracklet.stream_index != observation.stream_index
                    or tracklet.category != observation.category
                    or point.source_id != observation.source_id
                    or point.stream_index != observation.stream_index
                    or point.category != observation.category
                    or point.frame_id != observation.frame_id
                    or point.pts != observation.pts
                    or point.geometry != observation.geometry
                ):
                    raise _port_error(PortErrorCode.CONFLICT, PortKind.TRACKER, "track")
                tracked_observation_ids.append(point.observation_id)
        if self._result.state is PerceptionResultState.COMPLETE and (
            len(set(tracked_observation_ids)) != len(tracked_observation_ids)
            or set(tracked_observation_ids) != set(by_observation)
        ):
            raise _port_error(PortErrorCode.CONFLICT, PortKind.TRACKER, "track")
        self._record_call("track", len(frames))
        return self._result


class FakeEvidenceSelector(_InstrumentedFake):
    def __init__(self, result: EvidenceSelectionResult) -> None:
        if type(result) is not EvidenceSelectionResult:
            raise _port_error(
                PortErrorCode.INVALID_REQUEST,
                PortKind.EVIDENCE_SELECTOR,
                "init",
            )
        super().__init__(_fake_descriptor(PortKind.EVIDENCE_SELECTOR))
        self._result = result

    def select(
        self,
        tracklet: Tracklet,
        observations: tuple[Observation, ...],
    ) -> EvidenceSelectionResult:
        if type(tracklet) is not Tracklet:
            raise _port_error(
                PortErrorCode.INVALID_REQUEST,
                PortKind.EVIDENCE_SELECTOR,
                "select",
            )
        try:
            Tracklet.__post_init__(tracklet)
        except (TypeError, ValueError):
            raise _port_error(
                PortErrorCode.INVALID_REQUEST,
                PortKind.EVIDENCE_SELECTOR,
                "select",
            ) from None
        if type(observations) is not tuple or len(observations) > MAX_PORT_BATCH_ITEMS:
            raise _port_error(
                PortErrorCode.LIMIT_EXCEEDED,
                PortKind.EVIDENCE_SELECTOR,
                "select",
            )
        if not all(type(observation) is Observation for observation in observations):
            raise _port_error(
                PortErrorCode.INVALID_REQUEST,
                PortKind.EVIDENCE_SELECTOR,
                "select",
            )
        try:
            for observation in observations:
                Observation.__post_init__(observation)
        except (TypeError, ValueError):
            raise _port_error(
                PortErrorCode.INVALID_REQUEST,
                PortKind.EVIDENCE_SELECTOR,
                "select",
            ) from None
        by_id = {observation.observation_id: observation for observation in observations}
        if len(by_id) != len(observations):
            raise _port_error(
                PortErrorCode.CONFLICT,
                PortKind.EVIDENCE_SELECTOR,
                "select",
            )
        point_ids = {point.observation_id for point in tracklet.points}
        if set(by_id) != point_ids or not set(self._result.observation_ids).issubset(by_id):
            raise _port_error(
                PortErrorCode.CONFLICT,
                PortKind.EVIDENCE_SELECTOR,
                "select",
            )
        for observation in observations:
            point = next(
                item
                for item in tracklet.points
                if item.observation_id == observation.observation_id
            )
            if (
                observation.source_id != tracklet.source_id
                or observation.stream_index != tracklet.stream_index
                or observation.category != tracklet.category
                or point.source_id != observation.source_id
                or point.stream_index != observation.stream_index
                or point.category != observation.category
                or point.frame_id != observation.frame_id
                or point.pts != observation.pts
                or point.geometry != observation.geometry
            ):
                raise _port_error(
                    PortErrorCode.CONFLICT,
                    PortKind.EVIDENCE_SELECTOR,
                    "select",
                )
        self._record_call("select", len(observations))
        return self._result


class FakeEvidenceStore(_InstrumentedFake):
    def __init__(self, *, max_payload_bytes: int = MAX_FAKE_ARTIFACT_BYTES) -> None:
        if type(max_payload_bytes) is not int or max_payload_bytes < 0:
            raise ValueError("max_payload_bytes must be non-negative")
        super().__init__(
            _fake_descriptor(
                PortKind.EVIDENCE_STORE,
                max_payload_bytes=max_payload_bytes,
            )
        )
        self._content: dict[str, bytes] = {}

    def put(self, artifact: Artifact, content: bytes) -> Artifact:
        if (
            type(artifact) is not Artifact
            or type(artifact.sha256) is not str
            or type(artifact.bytes) is not str
            or type(artifact.media_type) is not str
            or type(content) is not bytes
        ):
            raise _port_error(
                PortErrorCode.INVALID_REQUEST,
                PortKind.EVIDENCE_STORE,
                "put",
            )
        maximum = self.descriptor.max_payload_bytes
        if maximum is None or len(content) > maximum:
            raise _port_error(PortErrorCode.LIMIT_EXCEEDED, PortKind.EVIDENCE_STORE, "put")
        if (
            int(artifact.bytes) != len(content)
            or hashlib.sha256(content).hexdigest() != artifact.sha256
        ):
            raise _port_error(PortErrorCode.CONFLICT, PortKind.EVIDENCE_STORE, "put")
        existing = self._content.get(artifact.sha256)
        if existing is not None and existing != content:
            raise _port_error(PortErrorCode.CONFLICT, PortKind.EVIDENCE_STORE, "put")
        self._content[artifact.sha256] = content
        self._record_call("put", 1)
        return artifact

    def get(self, digest: str) -> bytes:
        validated = _digest(digest, PortKind.EVIDENCE_STORE, "get")
        if validated not in self._content:
            raise _port_error(PortErrorCode.NOT_FOUND, PortKind.EVIDENCE_STORE, "get")
        content = self._content[validated]
        self._record_call("get", 1)
        return content


class FakeWorldStore(_InstrumentedFake):
    def __init__(self) -> None:
        super().__init__(_fake_descriptor(PortKind.WORLD_STORE))
        self._records: dict[str, Record] = {}

    def commit(self, records: tuple[Record, ...]) -> None:
        if not isinstance(records, tuple) or len(records) > MAX_PORT_BATCH_ITEMS:
            raise _port_error(PortErrorCode.LIMIT_EXCEEDED, PortKind.WORLD_STORE, "commit")
        if not all(
            isinstance(record, (Source, FrameRef, EvidenceRef, RunManifest)) for record in records
        ):
            raise _port_error(
                PortErrorCode.INVALID_REQUEST,
                PortKind.WORLD_STORE,
                "commit",
            )
        pending = dict(self._records)
        for record in records:
            dumps_record(record)
            pending[_identifier(record)] = record
        source_ids = {record.source_id for record in pending.values() if isinstance(record, Source)}
        frame_ids = {record.frame_id for record in pending.values() if isinstance(record, FrameRef)}
        sources = {
            record.source_id: record for record in pending.values() if isinstance(record, Source)
        }
        if any(
            isinstance(record, (FrameRef, RunManifest)) and record.source_id not in source_ids
            for record in records
        ) or any(
            isinstance(record, EvidenceRef) and record.frame_id not in frame_ids
            for record in records
        ):
            raise _port_error(PortErrorCode.CONFLICT, PortKind.WORLD_STORE, "commit")
        frames = [record for record in pending.values() if isinstance(record, FrameRef)]
        if any(
            frame.stream_index
            not in {stream.stream_index for stream in sources[frame.source_id].streams}
            for frame in frames
        ):
            raise _port_error(PortErrorCode.CONFLICT, PortKind.WORLD_STORE, "commit")
        positions = {(frame.source_id, frame.stream_index, frame.decode_index) for frame in frames}
        if len(positions) != len(frames):
            raise _port_error(PortErrorCode.CONFLICT, PortKind.WORLD_STORE, "commit")
        self._records = pending
        self._record_call("commit", len(records))

    def get(self, record_id: str) -> Record:
        validated = _record_id(record_id, PortKind.WORLD_STORE, "get")
        if validated not in self._records:
            raise _port_error(PortErrorCode.NOT_FOUND, PortKind.WORLD_STORE, "get")
        record = self._records[validated]
        self._record_call("get", 1)
        return record

    def list_frames(
        self,
        source_id: str,
        *,
        stream_index: int,
        after_decode_index: str | None = None,
        limit: int,
    ) -> tuple[FrameRef, ...]:
        if not isinstance(source_id, str) or not re.fullmatch(r"src_[0-9a-f]{64}", source_id):
            raise _port_error(
                PortErrorCode.INVALID_REQUEST,
                PortKind.WORLD_STORE,
                "list_frames",
            )
        selected_stream = _stream_index(
            stream_index,
            PortKind.WORLD_STORE,
            "list_frames",
        )
        source = self._records.get(source_id)
        if not isinstance(source, Source) or selected_stream not in {
            stream.stream_index for stream in source.streams
        }:
            raise _port_error(PortErrorCode.NOT_FOUND, PortKind.WORLD_STORE, "list_frames")
        bounded_limit = _bounded_limit(limit, PortKind.WORLD_STORE, "list_frames")
        after = -1
        if after_decode_index is not None:
            after = _unsigned_decimal(
                after_decode_index,
                PortKind.WORLD_STORE,
                "list_frames",
            )
        result = tuple(
            sorted(
                (
                    record
                    for record in self._records.values()
                    if isinstance(record, FrameRef)
                    and record.source_id == source_id
                    and record.stream_index == selected_stream
                    and int(record.decode_index) > after
                ),
                key=lambda frame: int(frame.decode_index),
            )[:bounded_limit]
        )
        self._record_call("list_frames", len(result))
        return result

    def list_evidence(self, frame_id: str, *, limit: int) -> tuple[EvidenceRef, ...]:
        if not isinstance(frame_id, str) or not re.fullmatch(r"frm_[0-9a-f]{64}", frame_id):
            raise _port_error(
                PortErrorCode.INVALID_REQUEST,
                PortKind.WORLD_STORE,
                "list_evidence",
            )
        bounded_limit = _bounded_limit(limit, PortKind.WORLD_STORE, "list_evidence")
        result = tuple(
            sorted(
                (
                    record
                    for record in self._records.values()
                    if isinstance(record, EvidenceRef) and record.frame_id == frame_id
                ),
                key=lambda evidence: evidence.evidence_id,
            )[:bounded_limit]
        )
        self._record_call("list_evidence", len(result))
        return result


__all__ = [
    "MAX_FAKE_ARTIFACT_BYTES",
    "MAX_PERCEPTION_OBSERVATIONS",
    "MAX_PORT_BATCH_ITEMS",
    "CapabilityDescriptor",
    "DetectionResult",
    "Detector",
    "Effect",
    "EvidenceSelectionResult",
    "EvidenceSelector",
    "EvidenceStore",
    "FakeDetector",
    "FakeEvidenceSelector",
    "FakeEvidenceStore",
    "FakeFrameSampler",
    "FakeTracker",
    "FakeVideoSource",
    "FakeWorldStore",
    "FrameSampler",
    "PerceptionResultState",
    "PortCall",
    "PortError",
    "PortErrorCode",
    "PortKind",
    "Tracker",
    "TrackingResult",
    "VideoSource",
    "WorldStore",
]
