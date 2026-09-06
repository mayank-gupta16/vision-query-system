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
    FrameRef,
    Record,
    RunManifest,
    Sampling,
    Source,
    dumps_record,
)

MAX_PORT_BATCH_ITEMS = 64
MAX_FAKE_ARTIFACT_BYTES = 1024 * 1024

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_RECORD_ID_RE = re.compile(r"(?:src|frm|evi|run)_[0-9a-f]{64}\Z")
_UNSIGNED_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")


class PortKind(StrEnum):
    VIDEO_SOURCE = "video_source"
    FRAME_SAMPLER = "frame_sampler"
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
    try:
        integer = int(value)
    except ValueError as error:
        raise _port_error(PortErrorCode.LIMIT_EXCEEDED, port, operation) from error
    if integer > 2**64 - 1:
        raise _port_error(PortErrorCode.LIMIT_EXCEEDED, port, operation)
    return integer


def _digest(value: str, port: PortKind, operation: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
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
        super().__init__(_fake_descriptor(PortKind.VIDEO_SOURCE))
        self._source = source
        self._frames = tuple(sorted(frames, key=lambda frame: int(frame.decode_index)))

    def probe(self) -> Source:
        self._record_call("probe", 1)
        return self._source

    def read_frames(
        self,
        *,
        after_decode_index: str | None = None,
        limit: int = MAX_PORT_BATCH_ITEMS,
    ) -> tuple[FrameRef, ...]:
        bounded_limit = _bounded_limit(limit, PortKind.VIDEO_SOURCE, "read_frames")
        after = -1
        if after_decode_index is not None:
            after = _unsigned_decimal(
                after_decode_index,
                PortKind.VIDEO_SOURCE,
                "read_frames",
            )
        result = tuple(frame for frame in self._frames if int(frame.decode_index) > after)[
            :bounded_limit
        ]
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
        if not isinstance(artifact, Artifact) or not isinstance(content, bytes):
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
        try:
            content = self._content[validated]
        except KeyError as error:
            raise _port_error(PortErrorCode.NOT_FOUND, PortKind.EVIDENCE_STORE, "get") from error
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
        if any(
            isinstance(record, (FrameRef, RunManifest)) and record.source_id not in source_ids
            for record in records
        ) or any(
            isinstance(record, EvidenceRef) and record.frame_id not in frame_ids
            for record in records
        ):
            raise _port_error(PortErrorCode.CONFLICT, PortKind.WORLD_STORE, "commit")
        self._records = pending
        self._record_call("commit", len(records))

    def get(self, record_id: str) -> Record:
        validated = _record_id(record_id, PortKind.WORLD_STORE, "get")
        try:
            record = self._records[validated]
        except KeyError as error:
            raise _port_error(PortErrorCode.NOT_FOUND, PortKind.WORLD_STORE, "get") from error
        self._record_call("get", 1)
        return record

    def list_frames(
        self,
        source_id: str,
        *,
        after_decode_index: str | None = None,
        limit: int,
    ) -> tuple[FrameRef, ...]:
        if not isinstance(source_id, str) or not re.fullmatch(r"src_[0-9a-f]{64}", source_id):
            raise _port_error(
                PortErrorCode.INVALID_REQUEST,
                PortKind.WORLD_STORE,
                "list_frames",
            )
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
    "MAX_PORT_BATCH_ITEMS",
    "CapabilityDescriptor",
    "Effect",
    "EvidenceStore",
    "FakeEvidenceStore",
    "FakeFrameSampler",
    "FakeVideoSource",
    "FakeWorldStore",
    "FrameSampler",
    "PortCall",
    "PortError",
    "PortErrorCode",
    "PortKind",
    "VideoSource",
    "WorldStore",
]
