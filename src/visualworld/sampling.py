# SPDX-License-Identifier: Apache-2.0
"""Deterministic exact-PTS frame sampling for bounded candidate pages."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol, runtime_checkable

from visualworld.ingestion import FrameRef, MediaTime, Sampling, Source, TimeBase
from visualworld.ports import (
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    FrameSampler,
    PortCall,
    PortError,
    PortErrorCode,
    PortKind,
)

_FRAME_ID_RE = re.compile(r"frm_[0-9a-f]{64}\Z")
_SOURCE_ID_RE = re.compile(r"src_[0-9a-f]{64}\Z")
_MAX_U64 = 2**64 - 1


@dataclass(frozen=True, slots=True)
class SamplingLimits:
    """Fail-closed bounds for resumable sampling."""

    max_page_candidates: int = MAX_PORT_BATCH_ITEMS
    max_total_candidates: int = 100_000
    max_duration_seconds: int = 60 * 60

    def __post_init__(self) -> None:
        if (
            type(self.max_page_candidates) is not int
            or not 1 <= self.max_page_candidates <= MAX_PORT_BATCH_ITEMS
            or type(self.max_total_candidates) is not int
            or not self.max_page_candidates <= self.max_total_candidates <= 100_000
            or type(self.max_duration_seconds) is not int
            or not 1 <= self.max_duration_seconds <= 24 * 60 * 60
        ):
            raise ValueError("sampling limits are outside the v1 bounds")


@dataclass(frozen=True, slots=True)
class SamplingCursor:
    """Immutable state required to resume at an exact candidate-page boundary."""

    source_id: str
    stream_index: int
    time_base: TimeBase
    sampling: Sampling
    origin: MediaTime
    previous: FrameRef
    left: FrameRef
    next_target_index: int
    seen_candidates: int
    last_emitted_frame_id: str | None
    finished: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not _SOURCE_ID_RE.fullmatch(self.source_id):
            raise ValueError("cursor source identity is invalid")
        if type(self.stream_index) is not int or not 0 <= self.stream_index <= 2**31 - 1:
            raise ValueError("cursor stream index is invalid")
        if (
            not isinstance(self.time_base, TimeBase)
            or not isinstance(self.sampling, Sampling)
            or not isinstance(self.origin, MediaTime)
            or not isinstance(self.previous, FrameRef)
            or not isinstance(self.left, FrameRef)
        ):
            raise ValueError("cursor records are invalid")
        if (
            self.origin.time_base != self.time_base
            or self.previous.source_id != self.source_id
            or self.left.source_id != self.source_id
            or self.previous.stream_index != self.stream_index
            or self.left.stream_index != self.stream_index
            or self.previous.pts.time_base != self.time_base
            or self.left.pts.time_base != self.time_base
            or _time(self.left.pts) != _time(self.previous.pts)
            or int(self.left.decode_index) > int(self.previous.decode_index)
        ):
            raise ValueError("cursor records conflict")
        if (
            type(self.next_target_index) is not int
            or not 1 <= self.next_target_index <= _MAX_U64
            or type(self.seen_candidates) is not int
            or not 1 <= self.seen_candidates <= _MAX_U64
        ):
            raise ValueError("cursor counters are invalid")
        if self.last_emitted_frame_id is not None and (
            not isinstance(self.last_emitted_frame_id, str)
            or not _FRAME_ID_RE.fullmatch(self.last_emitted_frame_id)
        ):
            raise ValueError("cursor frame identity is invalid")
        if type(self.finished) is not bool:
            raise ValueError("cursor completion state is invalid")


@dataclass(frozen=True, slots=True)
class SamplingPage:
    """Selected originals plus the immutable state for the next page."""

    frames: tuple[FrameRef, ...]
    cursor: SamplingCursor | None
    finished: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.frames, tuple)
            or len(self.frames) > MAX_PORT_BATCH_ITEMS
            or not all(isinstance(frame, FrameRef) for frame in self.frames)
            or len({frame.frame_id for frame in self.frames}) != len(self.frames)
        ):
            raise ValueError("sample page frames are invalid")
        if type(self.finished) is not bool:
            raise ValueError("sample page completion state is invalid")
        if self.cursor is None:
            if self.frames or not self.finished:
                raise ValueError("only an empty completed page may omit its cursor")
        elif not isinstance(self.cursor, SamplingCursor) or self.cursor.finished != self.finished:
            raise ValueError("sample page cursor is invalid")


@runtime_checkable
class ResumableFrameSampler(FrameSampler, Protocol):
    def sample_page(
        self,
        source: Source,
        candidates: tuple[FrameRef, ...],
        sampling: Sampling,
        *,
        cursor: SamplingCursor | None = None,
        end_of_stream: bool = False,
        cancelled: threading.Event | None = None,
    ) -> SamplingPage: ...


def _error(code: PortErrorCode, operation: str) -> PortError:
    return PortError(code, PortKind.FRAME_SAMPLER, operation)


def _time(value: MediaTime) -> Fraction:
    return Fraction(
        int(value.value) * int(value.time_base.numerator),
        int(value.time_base.denominator),
    )


def _floor(value: Fraction) -> int:
    return value.numerator // value.denominator


def _ceil(value: Fraction) -> int:
    return -((-value.numerator) // value.denominator)


class PtsFrameSampler:
    """Select nearest original frames on a first-PTS-anchored rational lattice."""

    def __init__(self, *, limits: SamplingLimits | None = None) -> None:
        selected_limits = SamplingLimits() if limits is None else limits
        if not isinstance(selected_limits, SamplingLimits):
            raise ValueError("limits must use the v1 sampling type")
        self._limits = selected_limits
        self._calls: list[PortCall] = []
        self._descriptor = CapabilityDescriptor(
            PortKind.FRAME_SAMPLER,
            "visualworld.pts-sampler",
            "1",
            deterministic=True,
            offline=True,
            max_batch_items=selected_limits.max_page_candidates,
        )

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    @property
    def calls(self) -> tuple[PortCall, ...]:
        return tuple(self._calls)

    @staticmethod
    def _check_cancelled(cancelled: threading.Event | None, operation: str) -> None:
        if cancelled is not None and cancelled.is_set():
            raise _error(PortErrorCode.CANCELLED, operation)

    def sample(
        self,
        source: Source,
        candidates: tuple[FrameRef, ...],
        sampling: Sampling,
    ) -> tuple[FrameRef, ...]:
        """Run one complete bounded call through the stable FrameSampler contract."""
        return self._sample_page(
            source,
            candidates,
            sampling,
            cursor=None,
            end_of_stream=True,
            cancelled=None,
            operation="sample",
        ).frames

    def sample_page(
        self,
        source: Source,
        candidates: tuple[FrameRef, ...],
        sampling: Sampling,
        *,
        cursor: SamplingCursor | None = None,
        end_of_stream: bool = False,
        cancelled: threading.Event | None = None,
    ) -> SamplingPage:
        """Sample one page; resumed pages repeat the cursor's prior frame at index zero."""
        return self._sample_page(
            source,
            candidates,
            sampling,
            cursor=cursor,
            end_of_stream=end_of_stream,
            cancelled=cancelled,
            operation="sample_page",
        )

    def _sample_page(
        self,
        source: Source,
        candidates: tuple[FrameRef, ...],
        sampling: Sampling,
        *,
        cursor: SamplingCursor | None,
        end_of_stream: bool,
        cancelled: threading.Event | None,
        operation: str,
    ) -> SamplingPage:
        self._check_cancelled(cancelled, operation)
        if (
            not isinstance(source, Source)
            or not isinstance(sampling, Sampling)
            or type(end_of_stream) is not bool
            or (cancelled is not None and not isinstance(cancelled, threading.Event))
            or (cursor is not None and not isinstance(cursor, SamplingCursor))
        ):
            raise _error(PortErrorCode.INVALID_REQUEST, operation)
        if not isinstance(candidates, tuple):
            raise _error(PortErrorCode.INVALID_REQUEST, operation)
        if len(candidates) > self._limits.max_page_candidates:
            raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
        if not candidates:
            if cursor is None and end_of_stream:
                result = SamplingPage((), None, True)
                self._calls.append(PortCall(PortKind.FRAME_SAMPLER, operation, 0))
                return result
            raise _error(PortErrorCode.INVALID_REQUEST, operation)

        streams = {stream.stream_index: stream for stream in source.streams}
        if cursor is None:
            first = candidates[0]
            if not isinstance(first, FrameRef) or first.source_id != source.source_id:
                raise _error(PortErrorCode.INVALID_REQUEST, operation)
            stream = streams.get(first.stream_index)
            if (
                stream is None
                or first.pts.time_base != stream.time_base
                or (first.duration is not None and first.duration.time_base != stream.time_base)
            ):
                raise _error(PortErrorCode.CONFLICT, operation)
            origin = first.pts
            consumed = first
            left = first
            next_target = 1
            seen = len(candidates)
            last_emitted: str | None = first.frame_id
            selected: list[FrameRef] = [first]
            new_candidates = candidates[1:]
        else:
            if (
                cursor.finished
                or cursor.source_id != source.source_id
                or cursor.sampling != sampling
                or streams.get(cursor.stream_index) is None
                or streams[cursor.stream_index].time_base != cursor.time_base
                or candidates[0] != cursor.previous
                or (
                    cursor.previous.duration is not None
                    and cursor.previous.duration.time_base != cursor.time_base
                )
            ):
                raise _error(PortErrorCode.CONFLICT, operation)
            origin = cursor.origin
            consumed = cursor.previous
            left = cursor.left
            next_target = cursor.next_target_index
            seen = cursor.seen_candidates + len(candidates) - 1
            last_emitted = cursor.last_emitted_frame_id
            selected = []
            new_candidates = candidates[1:]

        if seen > self._limits.max_total_candidates:
            raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
        stream_index = consumed.stream_index
        time_base = consumed.pts.time_base
        fps = sampling.target_fps.fraction()
        origin_time = _time(origin)
        left_time = _time(left.pts)
        consumed_time = _time(consumed.pts)
        if left_time < origin_time or (
            cursor is not None and next_target != _floor((left_time - origin_time) * fps) + 1
        ):
            raise _error(PortErrorCode.CONFLICT, operation)
        positions = {
            (left.stream_index, left.decode_index),
            (consumed.stream_index, consumed.decode_index),
        }
        identities = {left.frame_id, consumed.frame_id}
        maximum_span = Fraction(self._limits.max_duration_seconds, 1)
        for right in new_candidates:
            self._check_cancelled(cancelled, operation)
            if not isinstance(right, FrameRef) or right.source_id != source.source_id:
                raise _error(PortErrorCode.INVALID_REQUEST, operation)
            if right.stream_index != stream_index or right.pts.time_base != time_base:
                raise _error(PortErrorCode.CONFLICT, operation)
            if right.duration is not None and right.duration.time_base != time_base:
                raise _error(PortErrorCode.CONFLICT, operation)
            position = (right.stream_index, right.decode_index)
            if right.frame_id in identities or position in positions:
                raise _error(PortErrorCode.CONFLICT, operation)
            right_time = _time(right.pts)
            if int(right.decode_index) <= int(consumed.decode_index) or right_time < consumed_time:
                raise _error(PortErrorCode.CONFLICT, operation)
            if right_time - origin_time > maximum_span:
                raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
            identities.add(right.frame_id)
            positions.add(position)

            if right_time > left_time:
                due = _floor((right_time - origin_time) * fps)
                split = _floor((((left_time + right_time) / 2) - origin_time) * fps)
                if next_target <= min(split, due) and left.frame_id != last_emitted:
                    selected.append(left)
                    last_emitted = left.frame_id
                if max(next_target, split + 1) <= due and right.frame_id != last_emitted:
                    selected.append(right)
                    last_emitted = right.frame_id
                next_target = max(next_target, due + 1)
                left = right
                left_time = right_time
            consumed = right
            consumed_time = right_time

        if end_of_stream and consumed.duration is not None:
            duration = _time(consumed.duration)
            if duration > 0:
                horizon = consumed_time + duration
                if horizon - origin_time > maximum_span:
                    raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
                due = _ceil((horizon - origin_time) * fps) - 1
                if next_target <= due and left.frame_id != last_emitted:
                    selected.append(left)
                    last_emitted = left.frame_id
                next_target = max(next_target, due + 1)

        self._check_cancelled(cancelled, operation)
        if next_target > _MAX_U64:
            raise _error(PortErrorCode.LIMIT_EXCEEDED, operation)
        next_cursor = SamplingCursor(
            source.source_id,
            stream_index,
            time_base,
            sampling,
            origin,
            consumed,
            left,
            next_target,
            seen,
            last_emitted,
            end_of_stream,
        )
        result = SamplingPage(tuple(selected), next_cursor, end_of_stream)
        self._calls.append(PortCall(PortKind.FRAME_SAMPLER, operation, len(result.frames)))
        return result


__all__ = [
    "PtsFrameSampler",
    "ResumableFrameSampler",
    "SamplingCursor",
    "SamplingLimits",
    "SamplingPage",
]
