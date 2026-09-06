# SPDX-License-Identifier: Apache-2.0
"""Disposable PyAV media-worker probe for issue #4.

This is an experiment, not an application adapter.  The trusted parent opens a
regular file and passes it as descriptor 3.  The worker writes a bounded binary
record stream to stdout and a bounded summary to stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import stat
import struct
import sys
from fractions import Fraction
from typing import Any

import av

MAGIC = b"VWFRAME1"
AVFMT_FLAG_GENPTS = 0x0001
AVFMT_FLAG_NOFILLIN = 0x0010
ALLOWED_FORMATS = frozenset({"h264", "mov"})
ALLOWED_CODECS = frozenset({"h264"})


class LimitExceeded(RuntimeError):
    """A deterministic experiment limit was exceeded."""


def _fraction(value: Fraction | None) -> dict[str, int] | None:
    if value is None:
        return None
    return {"numerator": value.numerator, "denominator": value.denominator}


def _timestamp(value: int | None, time_base: Fraction | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"value": value, "time_base": _fraction(time_base)}


def _enum_name(value: object) -> str:
    return str(value).rsplit(".", maxsplit=1)[-1]


def _side_data(frame: av.VideoFrame, max_bytes: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    total = 0
    for item in frame.side_data:
        raw = bytes(item)
        total += len(raw)
        if total > max_bytes:
            raise LimitExceeded("side_data_bytes")
        kind = _enum_name(item.type)
        record: dict[str, Any] = {
            "type": kind,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if kind == "DISPLAYMATRIX":
            if len(raw) != 36:
                raise LimitExceeded("invalid_display_matrix")
            record["matrix_i32_native"] = list(struct.unpack("=9i", raw))
        result.append(record)
    return result


def _packed_rgb24(
    frame: av.VideoFrame, reformatter: av.video.reformatter.VideoReformatter
) -> bytes:
    converted = reformatter.reformat(frame, format="rgb24")
    plane = converted.planes[0]
    source = bytes(plane)
    row_bytes = converted.width * 3
    if plane.line_size < row_bytes:
        raise RuntimeError("invalid_rgb_stride")
    return b"".join(
        source[offset : offset + row_bytes]
        for offset in range(0, plane.line_size * converted.height, plane.line_size)
    )


def _write_all(payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        try:
            written = os.write(1, remaining)
        except BlockingIOError:
            select.select([], [1], [], 1.0)
            continue
        if written == 0:
            raise BrokenPipeError("zero_byte_write")
        remaining = remaining[written:]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-source-bytes", type=int, default=67_108_864)
    parser.add_argument("--max-width", type=int, default=4096)
    parser.add_argument("--max-height", type=int, default=4096)
    parser.add_argument("--max-pixels", type=int, default=8_388_608)
    parser.add_argument("--max-frames", type=int, default=300)
    parser.add_argument("--max-decoded-bytes", type=int, default=402_653_184)
    parser.add_argument("--max-side-data-bytes", type=int, default=65_536)
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    for descriptor in (1, 2, 3):
        os.set_blocking(descriptor, True)
    source_stat = os.fstat(3)
    if not stat.S_ISREG(source_stat.st_mode):
        raise LimitExceeded("source_not_regular")
    if source_stat.st_size > args.max_source_bytes:
        raise LimitExceeded("source_bytes")

    source = os.fdopen(3, "rb", closefd=False)
    container = av.open(
        source,
        mode="r",
        options={
            "fflags": "-genpts+nofillin",
            "probesize": "8388608",
            "analyzeduration": "5000000",
            "max_probe_packets": "2500",
            "max_streams": "32",
        },
    )
    flags = int(container.flags)
    if flags & AVFMT_FLAG_GENPTS or not flags & AVFMT_FLAG_NOFILLIN:
        raise RuntimeError("unsafe_timestamp_flags")

    format_names = set((container.format.name or "").split(","))
    if not format_names.intersection(ALLOWED_FORMATS):
        raise LimitExceeded("format_not_allowed")
    if not container.streams.video:
        raise LimitExceeded("no_video_stream")
    stream = container.streams.video[0]
    codec_name = stream.codec_context.name
    if codec_name not in ALLOWED_CODECS:
        raise LimitExceeded("codec_not_allowed")
    stream.thread_type = "NONE"
    stream.thread_count = 1

    stream_sar_guess = _fraction(stream.sample_aspect_ratio)
    reformatter = av.video.reformatter.VideoReformatter()
    _write_all(MAGIC)
    frames = 0
    decoded_bytes = 0
    for frame in container.decode(stream):
        if frames >= args.max_frames:
            raise LimitExceeded("frame_count")
        if frame.width > args.max_width or frame.height > args.max_height:
            raise LimitExceeded("dimensions")
        if frame.width * frame.height > args.max_pixels:
            raise LimitExceeded("pixels")

        pixels = _packed_rgb24(frame, reformatter)
        decoded_bytes += len(pixels)
        if decoded_bytes > args.max_decoded_bytes:
            raise LimitExceeded("decoded_bytes")

        metadata = {
            "frame_index": frames,
            "pts": _timestamp(frame.pts, frame.time_base),
            "dts": _timestamp(frame.dts, frame.time_base),
            "duration": _timestamp(frame.duration, frame.time_base),
            "width": frame.width,
            "height": frame.height,
            "source_pixel_format": frame.format.name,
            "output_pixel_format": "rgb24",
            "pixel_sha256": hashlib.sha256(pixels).hexdigest(),
            "rotation_degrees_derived": frame.rotation,
            "color_range": _enum_name(frame.color_range),
            "color_space": _enum_name(frame.colorspace),
            "color_primaries": _enum_name(frame.color_primaries),
            "color_transfer": _enum_name(frame.color_trc),
            "frame_sar_observed": None,
            "stream_sar_guess": stream_sar_guess,
            "side_data": _side_data(frame, args.max_side_data_bytes),
        }
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > 65_536:
            raise LimitExceeded("metadata_record_bytes")
        _write_all(struct.pack(">II", len(encoded), len(pixels)))
        _write_all(encoded)
        _write_all(pixels)
        frames += 1

    container.close()
    return {
        "status": "ok",
        "frames": frames,
        "decoded_bytes": decoded_bytes,
        "container_format": container.format.name,
        "codec": codec_name,
        "timestamp_generation": "disabled",
        "frame_sar": "binding_unavailable",
    }


def main() -> int:
    try:
        summary = run(_parser().parse_args())
    except LimitExceeded as error:
        summary = {"status": "limit_exceeded", "limit": str(error)}
        code = 20
    except Exception as error:
        summary = {"status": "decode_error", "error_type": type(error).__name__}
        code = 21
    else:
        code = 0
    sys.stderr.write(json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
