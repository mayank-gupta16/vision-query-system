# SPDX-License-Identifier: Apache-2.0
"""Bounded, vendor-neutral access to transient packed RGB24 source frames."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from visualworld.ingestion import FrameRef, Producer, Source
from visualworld.ports import (
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    PerceptionResultState,
    PortCall,
    PortError,
    PortErrorCode,
    PortKind,
)

MAX_ORIGINAL_FRAME_BYTES = 128 * 1024 * 1024
MAX_ORIGINAL_FRAME_TOTAL_BYTES = 512 * 1024 * 1024

_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _error(code: PortErrorCode, operation: str) -> PortError:
    return PortError(code, PortKind.ORIGINAL_FRAME_READER, operation)


def _copy_source(value: object, operation: str) -> Source:
    if type(value) is not Source:
        raise _error(PortErrorCode.INVALID_REQUEST, operation)
    try:
        return Source.from_mapping(value.to_mapping())
    except (TypeError, ValueError):
        raise _error(PortErrorCode.INVALID_REQUEST, operation) from None


def _copy_frame(value: object, operation: str) -> FrameRef:
    if type(value) is not FrameRef:
        raise _error(PortErrorCode.INVALID_REQUEST, operation)
    try:
        return FrameRef.from_mapping(value.to_mapping())
    except (TypeError, ValueError):
        raise _error(PortErrorCode.INVALID_REQUEST, operation) from None


def _stream_dimensions(source: Source, frame: FrameRef, operation: str) -> tuple[int, int]:
    if frame.source_id != source.source_id:
        raise _error(PortErrorCode.CONFLICT, operation)
    stream = next(
        (item for item in source.streams if item.stream_index == frame.stream_index),
        None,
    )
    if (
        stream is None
        or frame.pts.time_base != stream.time_base
        or (frame.duration is not None and frame.duration.time_base != stream.time_base)
    ):
        raise _error(PortErrorCode.CONFLICT, operation)
    return stream.width, stream.height


@dataclass(frozen=True, slots=True)
class OriginalFrame:
    """One exact source frame whose transient pixels never appear in its repr."""

    source: Source
    frame: FrameRef
    pixels: bytes = field(repr=False)
    width: int = field(init=False)
    height: int = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        source = _copy_source(self.source, "construct")
        frame = _copy_frame(self.frame, "construct")
        width, height = _stream_dimensions(source, frame, "construct")
        expected_bytes = width * height * 3
        if expected_bytes > MAX_ORIGINAL_FRAME_BYTES:
            raise _error(PortErrorCode.LIMIT_EXCEEDED, "construct")
        if type(self.pixels) is not bytes or len(self.pixels) != expected_bytes:
            raise _error(PortErrorCode.CONFLICT, "construct")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "frame", frame)
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "height", height)
        object.__setattr__(self, "sha256", hashlib.sha256(self.pixels).hexdigest())


@dataclass(frozen=True, slots=True)
class OriginalFrameReadResult:
    """An all-or-nothing response for one exact original-frame request."""

    state: PerceptionResultState
    frames: tuple[OriginalFrame, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not PerceptionResultState:
            raise ValueError("invalid original-frame result")
        if (
            type(self.frames) is not tuple
            or len(self.frames) > MAX_PORT_BATCH_ITEMS
            or not all(type(item) is OriginalFrame for item in self.frames)
        ):
            raise ValueError("invalid original-frame result")
        if self.state is PerceptionResultState.COMPLETE:
            if self.reason is not None:
                raise ValueError("complete original-frame result has a reason")
        elif self.frames or type(self.reason) is not str or not _REASON_RE.fullmatch(self.reason):
            raise ValueError("incomplete original-frame result is invalid")
        if len({item.frame.frame_id for item in self.frames}) != len(self.frames):
            raise ValueError("original-frame result contains duplicate frames")
        if self.frames and (
            len({item.source.source_id for item in self.frames}) != 1
            or len({item.frame.stream_index for item in self.frames}) != 1
        ):
            raise ValueError("original-frame result mixes sources or streams")
        owned = tuple(OriginalFrame(item.source, item.frame, item.pixels) for item in self.frames)
        object.__setattr__(self, "frames", owned)

    @classmethod
    def complete(cls, frames: tuple[OriginalFrame, ...]) -> OriginalFrameReadResult:
        return cls(PerceptionResultState.COMPLETE, frames)


@runtime_checkable
class OriginalFrameReader(Protocol):
    """Read only the exact authorized frames requested by deterministic orchestration."""

    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    @property
    def producer(self) -> Producer: ...

    def read(
        self,
        source: Source,
        frames: tuple[FrameRef, ...],
        *,
        max_total_bytes: int = MAX_ORIGINAL_FRAME_TOTAL_BYTES,
    ) -> OriginalFrameReadResult: ...


class FakeOriginalFrameReader:
    """Deterministic, bounded in-memory original-frame reader for contract tests."""

    def __init__(
        self,
        source: Source,
        frames: tuple[OriginalFrame, ...],
        *,
        state: PerceptionResultState = PerceptionResultState.COMPLETE,
        reason: str | None = None,
        max_payload_bytes: int = MAX_ORIGINAL_FRAME_TOTAL_BYTES,
    ) -> None:
        owned_source = _copy_source(source, "init")
        if type(max_payload_bytes) is not int or not 1 <= max_payload_bytes <= 2**63 - 1:
            raise _error(PortErrorCode.LIMIT_EXCEEDED, "init")
        try:
            configured_result = OriginalFrameReadResult(state, (), reason)
        except ValueError:
            raise _error(PortErrorCode.INVALID_REQUEST, "init") from None
        if type(frames) is not tuple or len(frames) > MAX_PORT_BATCH_ITEMS:
            raise _error(PortErrorCode.LIMIT_EXCEEDED, "init")
        if configured_result.state is not PerceptionResultState.COMPLETE and frames:
            raise _error(PortErrorCode.INVALID_REQUEST, "init")
        if not all(type(item) is OriginalFrame for item in frames):
            raise _error(PortErrorCode.INVALID_REQUEST, "init")
        if any(item.source != owned_source for item in frames):
            raise _error(PortErrorCode.CONFLICT, "init")
        if len({item.frame.frame_id for item in frames}) != len(frames):
            raise _error(PortErrorCode.CONFLICT, "init")
        owned_frames = tuple(
            OriginalFrame(owned_source, item.frame, item.pixels) for item in frames
        )
        self._source = owned_source
        self._frames = {item.frame.frame_id: item for item in owned_frames}
        self._state = configured_result.state
        self._reason = configured_result.reason
        self._descriptor = CapabilityDescriptor(
            PortKind.ORIGINAL_FRAME_READER,
            "visualworld.fake.original_frame_reader",
            "1",
            True,
            True,
            max_payload_bytes=max_payload_bytes,
        )
        configuration = json.dumps(
            {
                "frames": [
                    {"frame_id": item.frame.frame_id, "sha256": item.sha256}
                    for item in owned_frames
                ],
                "max_payload_bytes": max_payload_bytes,
                "reason": configured_result.reason,
                "state": configured_result.state.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self._producer = Producer(
            "visualworld.fake.original-frame-reader",
            "1",
            hashlib.sha256(configuration).hexdigest(),
        )
        self._calls: list[PortCall] = []

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    @property
    def producer(self) -> Producer:
        return self._producer

    @property
    def calls(self) -> tuple[PortCall, ...]:
        return tuple(self._calls)

    def read(
        self,
        source: Source,
        frames: tuple[FrameRef, ...],
        *,
        max_total_bytes: int = MAX_ORIGINAL_FRAME_TOTAL_BYTES,
    ) -> OriginalFrameReadResult:
        operation = "read"
        requested_source = _copy_source(source, operation)
        if requested_source != self._source:
            raise _error(PortErrorCode.CONFLICT, operation)
        if type(frames) is not tuple or not 1 <= len(frames) <= self.descriptor.max_batch_items:
            raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
        requested = tuple(_copy_frame(item, operation) for item in frames)
        frame_ids = {item.frame_id for item in requested}
        positions = {(item.stream_index, item.decode_index) for item in requested}
        if len(frame_ids) != len(requested) or len(positions) != len(requested):
            raise _error(PortErrorCode.CONFLICT, operation)
        if (
            any(item.source_id != requested_source.source_id for item in requested)
            or len({item.stream_index for item in requested}) != 1
        ):
            raise _error(PortErrorCode.CONFLICT, operation)
        expected_bytes = 0
        for item in requested:
            width, height = _stream_dimensions(requested_source, item, operation)
            frame_bytes = width * height * 3
            if frame_bytes > MAX_ORIGINAL_FRAME_BYTES:
                raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
            expected_bytes += frame_bytes
        maximum = self.descriptor.max_payload_bytes
        if (
            type(max_total_bytes) is not int
            or not 1 <= max_total_bytes <= MAX_ORIGINAL_FRAME_TOTAL_BYTES
            or maximum is None
            or expected_bytes > min(max_total_bytes, maximum)
        ):
            raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
        if self._state is not PerceptionResultState.COMPLETE:
            result = OriginalFrameReadResult(self._state, reason=self._reason)
        else:
            selected: list[OriginalFrame] = []
            for item in requested:
                stored = self._frames.get(item.frame_id)
                if stored is None or stored.frame != item:
                    raise _error(PortErrorCode.CONFLICT, operation)
                selected.append(stored)
            result = OriginalFrameReadResult.complete(tuple(selected))
        self._calls.append(PortCall(PortKind.ORIGINAL_FRAME_READER, operation, len(requested)))
        return result


__all__ = [
    "MAX_ORIGINAL_FRAME_BYTES",
    "MAX_ORIGINAL_FRAME_TOTAL_BYTES",
    "FakeOriginalFrameReader",
    "OriginalFrame",
    "OriginalFrameReadResult",
    "OriginalFrameReader",
]
