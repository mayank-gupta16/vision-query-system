# SPDX-License-Identifier: Apache-2.0
"""Contract tests for bounded transient original-frame access."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, cast

import pytest

from visualworld.frame_access import (
    MAX_ORIGINAL_FRAME_TOTAL_BYTES,
    FakeOriginalFrameReader,
    OriginalFrame,
    OriginalFrameReader,
    OriginalFrameReadResult,
)
from visualworld.ingestion import Fingerprint, FrameRef, MediaTime, Source, SourceStream, TimeBase
from visualworld.ports import PerceptionResultState, PortCall, PortError, PortErrorCode, PortKind


def records() -> tuple[Source, tuple[FrameRef, ...], tuple[bytes, ...]]:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint("ab" * 32, "96"),
        (
            SourceStream(0, 4, 2, 0, time_base),
            SourceStream(1, 2, 2, 0, time_base),
        ),
    )
    frames = (
        FrameRef.create(source.source_id, 0, "0", MediaTime("0", time_base)),
        FrameRef.create(source.source_id, 0, "1", MediaTime("40", time_base)),
        FrameRef.create(source.source_id, 1, "0", MediaTime("0", time_base)),
    )
    pixels = (bytes(range(24)), bytes(reversed(range(24))), bytes(range(12)))
    return source, frames, pixels


def originals() -> tuple[Source, tuple[FrameRef, ...], tuple[OriginalFrame, ...]]:
    source, frames, pixels = records()
    return (
        source,
        frames,
        tuple(
            OriginalFrame(source, frame, content)
            for frame, content in zip(frames, pixels, strict=True)
        ),
    )


def test_original_frame_owns_binding_validates_rgb24_and_hides_pixels() -> None:
    source, frames, pixels = records()
    original = OriginalFrame(source, frames[0], pixels[0])

    assert original.source == source
    assert original.source is not source
    assert original.source.fingerprint is not source.fingerprint
    assert original.source.streams[0] is not source.streams[0]
    assert original.source.streams[0].time_base is not source.streams[0].time_base
    assert original.frame == frames[0]
    assert original.frame is not frames[0]
    assert original.frame.pts is not frames[0].pts
    assert original.frame.pts.time_base is not frames[0].pts.time_base
    assert (original.width, original.height) == (4, 2)
    assert original.sha256 == hashlib.sha256(pixels[0]).hexdigest()
    assert repr(pixels[0]) not in repr(original)
    assert "pixels=" not in repr(original)
    assert not hasattr(original, "to_mapping")
    with pytest.raises(TypeError):
        json.dumps(original)


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (lambda source, frame, pixels: OriginalFrame(source, frame, pixels[:-1]), "conflict"),
        (
            lambda source, frame, pixels: OriginalFrame(
                source,
                FrameRef.create(
                    source.source_id,
                    9,
                    frame.decode_index,
                    frame.pts,
                ),
                pixels,
            ),
            "conflict",
        ),
        (
            lambda source, frame, pixels: OriginalFrame(
                Source.create(
                    Fingerprint("cd" * 32, "1"),
                    source.streams,
                ),
                frame,
                pixels,
            ),
            "conflict",
        ),
    ],
)
def test_original_frame_rejects_invalid_binding_or_length(
    build: Callable[[Source, FrameRef, bytes], object], code: str
) -> None:
    source, frames, pixels = records()

    with pytest.raises(PortError) as raised:
        build(source, frames[0], pixels[0])

    assert raised.value.code.value == code
    assert repr(pixels[0]) not in str(raised.value)


def test_read_result_is_all_or_nothing_and_owns_frames() -> None:
    _, _, available = originals()
    result = OriginalFrameReadResult.complete(available[:2])

    assert result.frames == available[:2]
    assert result.frames[0] is not available[0]
    assert result.frames[0].source is not available[0].source
    assert result.frames[0].frame is not available[0].frame

    for state in (PerceptionResultState.UNKNOWN, PerceptionResultState.UNSUPPORTED):
        unavailable = OriginalFrameReadResult(state, reason="frames_unavailable")
        assert unavailable.frames == ()
        with pytest.raises(ValueError, match="incomplete"):
            OriginalFrameReadResult(state, available[:1], "frames_unavailable")

    with pytest.raises(ValueError, match="reason"):
        OriginalFrameReadResult(PerceptionResultState.COMPLETE, reason="unexpected")
    with pytest.raises(ValueError, match="incomplete"):
        OriginalFrameReadResult(PerceptionResultState.UNKNOWN)
    with pytest.raises(ValueError, match="duplicate"):
        OriginalFrameReadResult.complete((available[0], available[0]))
    with pytest.raises(ValueError, match="mixes"):
        OriginalFrameReadResult.complete((available[0], available[2]))


def test_fake_reader_satisfies_protocol_preserves_order_and_is_instrumented() -> None:
    source, frames, available = originals()
    reader = FakeOriginalFrameReader(source, available)

    assert isinstance(reader, OriginalFrameReader)
    result = reader.read(source, (frames[1], frames[0]), max_total_bytes=48)

    assert result.state is PerceptionResultState.COMPLETE
    assert tuple(item.frame for item in result.frames) == (frames[1], frames[0])
    assert tuple(item.pixels for item in result.frames) == (
        available[1].pixels,
        available[0].pixels,
    )
    assert reader.calls == (PortCall(PortKind.ORIGINAL_FRAME_READER, "read", 2),)
    assert reader.descriptor.port is PortKind.ORIGINAL_FRAME_READER
    assert reader.descriptor.max_payload_bytes == MAX_ORIGINAL_FRAME_TOTAL_BYTES
    assert reader.descriptor.allowed_effects == ()


def test_fake_reader_producer_binds_pixel_hashes_without_exposing_pixels() -> None:
    source, _, available = originals()
    changed = OriginalFrame(source, available[0].frame, bytes([7]) * len(available[0].pixels))
    first = FakeOriginalFrameReader(source, available[:1])
    second = FakeOriginalFrameReader(source, (changed,))

    assert first.producer != second.producer
    assert first.producer.name == "visualworld.fake.original-frame-reader"
    assert repr(available[0].pixels) not in repr(first.producer)


def test_fake_reader_rejects_non_exact_duplicate_foreign_and_mixed_requests() -> None:
    source, frames, available = originals()
    reader = FakeOriginalFrameReader(source, available)
    foreign_source = Source.create(
        Fingerprint("ef" * 32, "12"),
        source.streams,
    )
    foreign_frame = FrameRef.create(
        foreign_source.source_id,
        0,
        "0",
        frames[0].pts,
    )
    altered = FrameRef(
        frames[0].frame_id,
        frames[0].source_id,
        frames[0].stream_index,
        frames[0].decode_index,
        frames[0].pts,
        key_frame=True,
    )

    invalid_calls: tuple[Callable[[], object], ...] = (
        lambda: reader.read(source, (frames[0], frames[0])),
        lambda: reader.read(source, (frames[0], frames[2])),
        lambda: reader.read(foreign_source, (foreign_frame,)),
        lambda: reader.read(source, (foreign_frame,)),
        lambda: reader.read(source, (altered,)),
    )
    for invalid in invalid_calls:
        with pytest.raises(PortError) as raised:
            invalid()
        assert raised.value.code is PortErrorCode.CONFLICT
    assert reader.calls == ()


@pytest.mark.parametrize("maximum", [0, 23, MAX_ORIGINAL_FRAME_TOTAL_BYTES + 1])
def test_fake_reader_enforces_page_and_byte_bounds_before_returning_pixels(maximum: int) -> None:
    source, frames, available = originals()
    reader = FakeOriginalFrameReader(source, available)

    with pytest.raises(PortError) as raised:
        reader.read(source, (frames[0],), max_total_bytes=maximum)

    assert raised.value.code is PortErrorCode.LIMIT_EXCEEDED
    assert reader.calls == ()


def test_fake_reader_rejects_invalid_page_shapes_without_instrumenting() -> None:
    source, frames, available = originals()
    reader = FakeOriginalFrameReader(source, available)

    invalid_pages = (
        (),
        cast(tuple[FrameRef, ...], list(frames[:1])),
        cast(tuple[FrameRef, ...], (object(),)),
        frames[:1] * 65,
    )
    for page in invalid_pages:
        with pytest.raises(PortError):
            reader.read(source, page)
    assert reader.calls == ()


@pytest.mark.parametrize(
    "state", [PerceptionResultState.UNKNOWN, PerceptionResultState.UNSUPPORTED]
)
def test_fake_reader_reports_unavailable_state_without_partial_pixels(
    state: PerceptionResultState,
) -> None:
    source, frames, _ = originals()
    reader = FakeOriginalFrameReader(source, (), state=state, reason="frames_unavailable")

    result = reader.read(source, frames[:1])

    assert result == OriginalFrameReadResult(state, reason="frames_unavailable")
    assert result.frames == ()
    assert reader.calls == (PortCall(PortKind.ORIGINAL_FRAME_READER, "read", 1),)


def test_pixel_values_do_not_escape_result_repr_or_errors() -> None:
    source, frames, available = originals()
    secret = available[0].pixels
    reader = FakeOriginalFrameReader(source, available)

    result = reader.read(source, frames[:1])
    rendered = repr(result)
    assert repr(secret) not in rendered
    assert "pixels=" not in rendered

    with pytest.raises(PortError) as raised:
        reader.read(source, frames[:1], max_total_bytes=len(secret) - 1)
    assert repr(secret) not in str(raised.value)
    assert repr(secret) not in repr(raised.value)


@pytest.mark.parametrize(
    "invalid",
    [
        lambda result: OriginalFrameReadResult(cast(Any, "complete"), result.frames),
        lambda result: OriginalFrameReadResult(PerceptionResultState.UNKNOWN, reason="Not Stable"),
        lambda result: FakeOriginalFrameReader(
            result.frames[0].source,
            result.frames,
            state=PerceptionResultState.UNKNOWN,
            reason="frames_unavailable",
        ),
    ],
)
def test_result_and_fake_configuration_reject_invalid_values(
    invalid: Callable[[OriginalFrameReadResult], object],
) -> None:
    _, _, available = originals()
    result = OriginalFrameReadResult.complete(available[:1])

    with pytest.raises((PortError, ValueError)):
        invalid(result)


def test_frame_and_result_reject_unowned_shapes_and_oversized_geometry() -> None:
    source, frames, pixels = records()
    with pytest.raises(PortError) as bad_source:
        OriginalFrame(cast(Any, object()), frames[0], pixels[0])
    assert bad_source.value.code is PortErrorCode.INVALID_REQUEST
    with pytest.raises(PortError) as bad_frame:
        OriginalFrame(source, cast(Any, object()), pixels[0])
    assert bad_frame.value.code is PortErrorCode.INVALID_REQUEST
    with pytest.raises(ValueError, match="invalid original-frame result"):
        OriginalFrameReadResult(PerceptionResultState.COMPLETE, cast(Any, []))

    wide_source = Source.create(
        source.fingerprint,
        (SourceStream(0, 7000, 7000, 0, source.streams[0].time_base),),
    )
    wide_frame = FrameRef.create(
        wide_source.source_id, 0, "0", MediaTime("0", source.streams[0].time_base)
    )
    with pytest.raises(PortError) as too_large:
        OriginalFrame(wide_source, wide_frame, b"")
    assert too_large.value.code is PortErrorCode.LIMIT_EXCEEDED


def test_fake_reader_constructor_and_frame_bounds_fail_closed() -> None:
    source, frames, available = originals()
    foreign = Source.create(Fingerprint("cd" * 32, "1"), source.streams)
    foreign_original = OriginalFrame(
        foreign, FrameRef.create(foreign.source_id, 0, "0", frames[0].pts), available[0].pixels
    )
    invalid_builds: tuple[Callable[[], object], ...] = (
        lambda: FakeOriginalFrameReader(source, available, max_payload_bytes=0),
        lambda: FakeOriginalFrameReader(
            source, (), state=cast(Any, "complete"), reason="frames_unavailable"
        ),
        lambda: FakeOriginalFrameReader(source, cast(Any, list(available))),
        lambda: FakeOriginalFrameReader(source, cast(Any, (object(),))),
        lambda: FakeOriginalFrameReader(source, (foreign_original,)),
        lambda: FakeOriginalFrameReader(source, (available[0], available[0])),
    )
    for build in invalid_builds:
        with pytest.raises(PortError):
            build()

    wide_source = Source.create(
        source.fingerprint,
        (SourceStream(0, 7000, 7000, 0, source.streams[0].time_base),),
    )
    wide_frame = FrameRef.create(
        wide_source.source_id, 0, "0", MediaTime("0", source.streams[0].time_base)
    )
    wide_reader = FakeOriginalFrameReader(wide_source, ())
    with pytest.raises(PortError) as too_large:
        wide_reader.read(wide_source, (wide_frame,))
    assert too_large.value.code is PortErrorCode.LIMIT_EXCEEDED
