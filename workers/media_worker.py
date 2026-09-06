# SPDX-License-Identifier: Apache-2.0
"""External PyAV worker staged only in the isolated media runtime."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import importlib
import json
import os
import socket
import stat
import sys
from fractions import Fraction
from typing import Any

_EXPECTED_PYAV = "18.1.0"
_ALLOWED_FORMATS = frozenset({"h264", "mov"})
_ALLOWED_CODECS = frozenset({"h264", "rawvideo"})
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_AUDIT_ARCH_X86_64 = 0xC000003E
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_BPF_LD_W_ABS = 0x20
_BPF_JMP_JEQ_K = 0x15
_BPF_RET_K = 0x06
_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_ACCESS_FS_V1 = (1 << 13) - 1
_LANDLOCK_RUNTIME_ACCESS = (1 << 0) | (1 << 2) | (1 << 3)
_FORMAT_FLAG_GENPTS = 0x0001
_FORMAT_FLAG_NOFILLIN = 0x0010


class _LimitExceeded(RuntimeError):
    pass


class _IsolationUnavailable(RuntimeError):
    pass


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _stmt(code: int, value: int) -> _SockFilter:
    return _SockFilter(code, 0, 0, value)


def _jump(code: int, value: int, true_offset: int, false_offset: int) -> _SockFilter:
    return _SockFilter(code, true_offset, false_offset, value)


def _no_new_privileges(libc: Any) -> None:
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise _IsolationUnavailable("no_new_privileges")


def _apply_landlock(libc: Any) -> None:
    abi = libc.syscall(
        _LANDLOCK_CREATE_RULESET,
        0,
        0,
        _LANDLOCK_CREATE_RULESET_VERSION,
    )
    if abi < 1:
        raise _IsolationUnavailable("landlock_abi")
    attributes = _LandlockRulesetAttr(_LANDLOCK_ACCESS_FS_V1)
    descriptor = libc.syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.byref(attributes),
        ctypes.sizeof(attributes),
        0,
    )
    if descriptor < 0:
        raise _IsolationUnavailable("landlock_ruleset")
    allowed_descriptors: list[int] = []
    try:
        for path in ("/runtime", "/lib", "/lib64"):
            try:
                allowed = os.open(path, getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC)
            except OSError:
                raise _IsolationUnavailable("landlock_path") from None
            allowed_descriptors.append(allowed)
            rule = _LandlockPathBeneathAttr(_LANDLOCK_RUNTIME_ACCESS, allowed)
            if (
                libc.syscall(
                    _LANDLOCK_ADD_RULE,
                    descriptor,
                    _LANDLOCK_RULE_PATH_BENEATH,
                    ctypes.byref(rule),
                    0,
                )
                != 0
            ):
                raise _IsolationUnavailable("landlock_rule")
        if libc.syscall(_LANDLOCK_RESTRICT_SELF, descriptor, 0) != 0:
            raise _IsolationUnavailable("landlock_restrict")
    finally:
        for allowed in allowed_descriptors:
            os.close(allowed)
        os.close(descriptor)


def _apply_seccomp(libc: Any) -> None:
    denied = (
        41,
        42,
        43,
        44,
        45,
        46,
        47,
        48,
        49,
        50,
        51,
        52,
        53,
        54,
        55,
        56,
        57,
        58,
        59,
        101,
        165,
        166,
        169,
        170,
        171,
        175,
        176,
        246,
        272,
        298,
        300,
        303,
        304,
        308,
        313,
        321,
        322,
        323,
        425,
        426,
        427,
        428,
        429,
        430,
        435,
    )
    instructions = [
        _stmt(_BPF_LD_W_ABS, 4),
        _jump(_BPF_JMP_JEQ_K, _AUDIT_ARCH_X86_64, 1, 0),
        _stmt(_BPF_RET_K, _SECCOMP_RET_KILL_PROCESS),
        _stmt(_BPF_LD_W_ABS, 0),
    ]
    for syscall_number in denied:
        instructions.extend(
            (
                _jump(_BPF_JMP_JEQ_K, syscall_number, 0, 1),
                _stmt(_BPF_RET_K, _SECCOMP_RET_ERRNO | errno.EPERM),
            )
        )
    instructions.append(_stmt(_BPF_RET_K, _SECCOMP_RET_ALLOW))
    array = (_SockFilter * len(instructions))(*instructions)
    program = _SockFprog(len(instructions), array)
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(program)) != 0:
        raise _IsolationUnavailable("seccomp")


def _network_denied() -> bool:
    try:
        attempt = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as error:
        return error.errno == errno.EPERM
    attempt.close()
    return False


def _landlock_denied() -> bool:
    try:
        descriptor = os.open("/proc/self/status", os.O_RDONLY)
    except OSError as error:
        return error.errno in {errno.EACCES, errno.EPERM}
    os.close(descriptor)
    return False


def _fraction(value: Fraction | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def _media_time(value: int | None, time_base: Fraction | None) -> dict[str, object] | None:
    if value is None or time_base is None:
        return None
    return {"value": str(value), "time_base": _fraction(time_base)}


def _rotation(value: object) -> int:
    if not isinstance(value, (int, float)) or int(value) != value:
        raise _LimitExceeded("rotation")
    return int(value)


def _packed_rgb24(frame: Any, reformatter: Any) -> bytes:
    converted = reformatter.reformat(frame, format="rgb24")
    plane = converted.planes[0]
    raw = bytes(plane)
    row_bytes = converted.width * 3
    if plane.line_size < row_bytes:
        raise _LimitExceeded("rgb_stride")
    return b"".join(
        raw[offset : offset + row_bytes]
        for offset in range(0, plane.line_size * converted.height, plane.line_size)
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--max-duration-seconds", type=int, required=True)
    parser.add_argument("--max-width", type=int, required=True)
    parser.add_argument("--max-height", type=int, required=True)
    parser.add_argument("--max-pixels", type=int, required=True)
    parser.add_argument("--max-frames", type=int, required=True)
    parser.add_argument("--max-decoded-bytes", type=int, required=True)
    return parser


def _run(arguments: argparse.Namespace) -> dict[str, object]:
    if os.geteuid() == 0:
        raise _IsolationUnavailable("root_worker")
    source_stat = os.fstat(3)
    if not stat.S_ISREG(source_stat.st_mode):
        raise _LimitExceeded("source_not_regular")
    av: Any = importlib.import_module("av")
    if av.__version__ != _EXPECTED_PYAV:
        raise _IsolationUnavailable("pyav_version")
    libc: Any = ctypes.CDLL(None, use_errno=True)
    _no_new_privileges(libc)
    _apply_landlock(libc)
    _apply_seccomp(libc)
    isolation = {
        "non_root": os.geteuid() != 0,
        "no_new_privileges": True,
        "network_denied": _network_denied(),
        "landlock_denied": _landlock_denied(),
    }
    if not all(isolation.values()):
        raise _IsolationUnavailable("isolation_probe")
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
    format_flags = int(container.flags)
    if format_flags & _FORMAT_FLAG_GENPTS or not format_flags & _FORMAT_FLAG_NOFILLIN:
        raise _IsolationUnavailable("format_flags")
    format_names = set((container.format.name or "").split(","))
    if not format_names.intersection(_ALLOWED_FORMATS):
        raise _LimitExceeded("format")
    if len(container.streams.video) != 1:
        raise _LimitExceeded("video_stream_count")
    stream = container.streams.video[0]
    codec = stream.codec_context.name
    if codec not in _ALLOWED_CODECS:
        raise _LimitExceeded("codec")
    stream.thread_type = "NONE"
    stream.thread_count = 1
    if stream.time_base is None or stream.time_base <= 0:
        raise _LimitExceeded("time_base")
    if stream.duration is not None:
        stream_duration = Fraction(stream.duration) * stream.time_base
        if stream_duration < 0 or stream_duration > Fraction(arguments.max_duration_seconds):
            raise _LimitExceeded("duration")
    stream_width = int(stream.codec_context.width)
    stream_height = int(stream.codec_context.height)
    if (
        not 1 <= stream_width <= arguments.max_width
        or not 1 <= stream_height <= arguments.max_height
    ):
        raise _LimitExceeded("dimensions")
    if stream_width * stream_height > arguments.max_pixels:
        raise _LimitExceeded("pixels")
    reformatter = av.video.reformatter.VideoReformatter()
    frames: list[dict[str, object]] = []
    decoded_bytes = 0
    rotations: set[int] = set()
    minimum_time: Fraction | None = None
    maximum_time: Fraction | None = None
    for index, frame in enumerate(container.decode(stream)):
        if index >= arguments.max_frames:
            raise _LimitExceeded("frames")
        if frame.pts is None or frame.time_base is None:
            raise _LimitExceeded("missing_pts")
        frame_width = int(frame.width)
        frame_height = int(frame.height)
        if (
            not 1 <= frame_width <= arguments.max_width
            or not 1 <= frame_height <= arguments.max_height
            or frame_width * frame_height > arguments.max_pixels
        ):
            raise _LimitExceeded("frame_dimensions")
        expected_bytes = frame_width * frame_height * 3
        if decoded_bytes + expected_bytes > arguments.max_decoded_bytes:
            raise _LimitExceeded("decoded_bytes")
        position = Fraction(frame.pts) * frame.time_base
        end = position
        if frame.duration is not None:
            duration = Fraction(frame.duration) * frame.time_base
            if duration < 0:
                raise _LimitExceeded("frame_duration")
            end += duration
        minimum_time = position if minimum_time is None else min(minimum_time, position)
        maximum_time = end if maximum_time is None else max(maximum_time, end)
        if maximum_time - minimum_time > Fraction(arguments.max_duration_seconds):
            raise _LimitExceeded("duration")
        pixels = _packed_rgb24(frame, reformatter)
        if len(pixels) != expected_bytes:
            raise _LimitExceeded("rgb_size")
        decoded_bytes += expected_bytes
        rotations.add(_rotation(frame.rotation))
        frames.append(
            {
                "decode_index": str(index),
                "pts": _media_time(frame.pts, frame.time_base),
                "duration": _media_time(frame.duration, frame.time_base),
                "key_frame": bool(frame.key_frame),
                "pixel_sha256": hashlib.sha256(pixels).hexdigest(),
            }
        )
    if not frames or len(rotations) != 1:
        raise _LimitExceeded("frames")
    result = {
        "schema_version": 1,
        "status": "ok",
        "runtime": {
            "pyav": av.__version__,
            "libavformat": ".".join(str(value) for value in av.library_versions["libavformat"]),
            "libavcodec": ".".join(str(value) for value in av.library_versions["libavcodec"]),
        },
        "isolation": isolation,
        "stream": {
            "stream_index": int(stream.index),
            "width": int(stream.codec_context.width),
            "height": int(stream.codec_context.height),
            "rotation_degrees": rotations.pop(),
            "time_base": _fraction(stream.time_base),
            "duration": _media_time(stream.duration, stream.time_base),
            "codec": codec,
            "format_name": sorted(format_names.intersection(_ALLOWED_FORMATS))[0],
        },
        "frames": frames,
    }
    container.close()
    return result


def main() -> int:
    try:
        result = _run(_parser().parse_args())
    except _LimitExceeded:
        code = 20
        summary = {"schema_version": 1, "status": "limit_exceeded"}
    except _IsolationUnavailable:
        code = 22
        summary = {"schema_version": 1, "status": "isolation_unavailable"}
    except BaseException as error:
        code = 21
        summary = {
            "schema_version": 1,
            "status": "decode_failed",
            "error_type": type(error).__name__,
        }
    else:
        code = 0
        sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
        summary = {"schema_version": 1, "status": "ok"}
    sys.stderr.write(json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
