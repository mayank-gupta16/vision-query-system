# SPDX-License-Identifier: Apache-2.0
"""Composite PyAV/OpenVINO worker staged only in the private runtime."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import importlib
import json
import math
import os
import socket
import stat
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

_EXPECTED_PYAV = "18.1.0"
_EXPECTED_OPENVINO = "2026.3.1-22476-759c5a6ab8c-releases/2026/3"
_EXPECTED_NUMPY = "2.5.3"
_EXPECTED_TELEMETRY = "2025.2.0"
_RUNTIME_ID = "visualworld-perception-openvino-2026.3.1-vehicle-0201-v1"
_MODEL_XML = Path("/perception-runtime/model/vehicle-detection-0201.xml")
_MODEL_BIN = Path("/perception-runtime/model/vehicle-detection-0201.bin")
_MODEL_XML_SHA256 = "ae39ec7c4cc5c1ab5ef3db71c8fa307500f07a87f17d95bdb0d1c84762751d1a"
_MODEL_BIN_SHA256 = "612df843314c179460e754316d67e6eedd0e778f96ad1129fc0c34c8e935cb0b"
_INPUT_WIDTH = 384
_INPUT_HEIGHT = 384
_CONFIDENCE_FLOOR_MILLIONTHS = 950_000
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


class _WorkerFailed(RuntimeError):
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
        for path, access in (
            ("/perception-runtime", _LANDLOCK_RUNTIME_ACCESS),
            ("/media-runtime", _LANDLOCK_RUNTIME_ACCESS),
            ("/lib", _LANDLOCK_RUNTIME_ACCESS),
            ("/lib64", _LANDLOCK_RUNTIME_ACCESS),
            ("/proc", _LANDLOCK_RUNTIME_ACCESS),
            ("/sys/devices/system/cpu", _LANDLOCK_RUNTIME_ACCESS),
            ("/sys/devices/system/node", _LANDLOCK_RUNTIME_ACCESS),
            ("/sys/kernel/mm", _LANDLOCK_RUNTIME_ACCESS),
            ("/etc/ld.so.cache", 1 << 2),
        ):
            try:
                allowed = os.open(path, getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC)
            except OSError:
                raise _IsolationUnavailable("landlock_path") from None
            allowed_descriptors.append(allowed)
            rule = _LandlockPathBeneathAttr(access, allowed)
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
        descriptor = os.open("/dev/null", os.O_RDONLY)
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


def _positive(value: int, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise _LimitExceeded("argument")
    return value


def _decode_indices(value: str, maximum: int) -> tuple[int, ...]:
    values = value.split(",")
    if not values or len(values) > 64:
        raise _LimitExceeded("decode_indices")
    selected: list[int] = []
    for item in values:
        if (
            not item.isascii()
            or not item.isdecimal()
            or (len(item) > 1 and item.startswith("0"))
            or len(item) > 20
        ):
            raise _LimitExceeded("decode_indices")
        index = int(item)
        if index >= maximum or (selected and index <= selected[-1]):
            raise _LimitExceeded("decode_indices")
        selected.append(index)
    return tuple(selected)


def _rotation(value: object) -> int:
    numeric: int | float
    if type(value) is int or type(value) is float:
        numeric = value
    else:
        raise _LimitExceeded("rotation")
    if not math.isfinite(numeric) or int(numeric) != numeric:
        raise _LimitExceeded("rotation")
    rotation = int(numeric) % 360
    if rotation % 90:
        raise _LimitExceeded("rotation")
    return rotation


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


def _preprocessed_model(model: Any, ov: Any, preprocess: Any) -> Any:
    preprocessing = preprocess.PrePostProcessor(model)
    preprocessing.input().tensor().set_element_type(ov.Type.u8).set_layout(
        ov.Layout("NHWC")
    ).set_color_format(preprocess.ColorFormat.RGB).set_spatial_dynamic_shape()
    preprocessing.input().preprocess().convert_color(preprocess.ColorFormat.BGR).resize(
        preprocess.ResizeAlgorithm.RESIZE_LINEAR
    )
    preprocessing.input().model().set_layout(ov.Layout("NCHW"))
    return preprocessing.build()


def _inference_batch(
    pixels: bytes,
    width: int,
    height: int,
    rotation: int,
    np: Any,
) -> Any:
    array = np.frombuffer(pixels, dtype=np.uint8).reshape((height, width, 3))
    if rotation:
        array = np.rot90(array, k={90: 3, 180: 2, 270: 1}[rotation])
    array = np.ascontiguousarray(array)
    return array.reshape((1, *array.shape))


def _file_sha256(path: Path, maximum: int) -> str:
    try:
        metadata = path.stat()
    except OSError:
        raise _IsolationUnavailable("runtime_file") from None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
        raise _IsolationUnavailable("runtime_file")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        raise _IsolationUnavailable("runtime_file") from None
    return digest.hexdigest()


def _source_sha256(descriptor: int, maximum: int) -> tuple[str, int]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= maximum:
        raise _LimitExceeded("source")
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest(), metadata.st_size


def _detections(output: Any, maximum: int) -> list[dict[str, object]]:
    if tuple(output.shape)[-1:] != (7,) or output.size > 7 * 10_000:
        raise _WorkerFailed("model_output")
    selected: list[dict[str, object]] = []
    for row in output.reshape((-1, 7)):
        values = [float(value) for value in row]
        if any(not math.isfinite(value) for value in values):
            raise _WorkerFailed("model_output")
        if values[0] < 0:
            break
        label = int(values[1])
        if values[1] != label:
            raise _WorkerFailed("model_output")
        confidence = max(0, min(1_000_000, int(values[2] * 1_000_000)))
        if label != 0 or confidence < _CONFIDENCE_FLOOR_MILLIONTHS:
            continue
        coordinates = values[3:7]
        if any(value < -1 or value > 2 for value in coordinates):
            raise _WorkerFailed("model_output")
        left = max(0, min(_INPUT_WIDTH, math.floor(coordinates[0] * _INPUT_WIDTH)))
        top = max(0, min(_INPUT_HEIGHT, math.floor(coordinates[1] * _INPUT_HEIGHT)))
        right = max(0, min(_INPUT_WIDTH, math.ceil(coordinates[2] * _INPUT_WIDTH)))
        bottom = max(0, min(_INPUT_HEIGHT, math.ceil(coordinates[3] * _INPUT_HEIGHT)))
        if left >= right or top >= bottom:
            continue
        selected.append(
            {
                "box_xyxy": [left, top, right, bottom],
                "confidence_millionths": confidence,
            }
        )
        if len(selected) > maximum:
            raise _LimitExceeded("detections")
    selected.sort(
        key=lambda item: (
            -cast(int, item["confidence_millionths"]),
            cast(list[int], item["box_xyxy"]),
        )
    )
    if len(
        {
            (
                tuple(cast(list[int], item["box_xyxy"])),
                cast(int, item["confidence_millionths"]),
            )
            for item in selected
        }
    ) != len(selected):
        raise _WorkerFailed("duplicate_detection")
    return selected


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--decode-indices", required=True)
    parser.add_argument("--max-source-bytes", type=int, required=True)
    parser.add_argument("--max-duration-seconds", type=int, required=True)
    parser.add_argument("--max-width", type=int, required=True)
    parser.add_argument("--max-height", type=int, required=True)
    parser.add_argument("--max-pixels", type=int, required=True)
    parser.add_argument("--max-frames", type=int, required=True)
    parser.add_argument("--max-decoded-bytes", type=int, required=True)
    parser.add_argument("--max-detections-per-frame", type=int, required=True)
    return parser


def _run(arguments: argparse.Namespace) -> dict[str, object]:
    max_source_bytes = _positive(arguments.max_source_bytes, 2**30)
    max_duration_seconds = _positive(arguments.max_duration_seconds, 24 * 60 * 60)
    max_width = _positive(arguments.max_width, 2**31 - 1)
    max_height = _positive(arguments.max_height, 2**31 - 1)
    max_pixels = _positive(arguments.max_pixels, 2**34)
    max_frames = _positive(arguments.max_frames, 100_000)
    max_decoded_bytes = _positive(arguments.max_decoded_bytes, 2**40)
    max_detections = _positive(arguments.max_detections_per_frame, 64)
    requested = _decode_indices(arguments.decode_indices, max_frames)
    if os.geteuid() == 0:
        raise _IsolationUnavailable("root_worker")
    source_sha256, source_bytes = _source_sha256(3, max_source_bytes)

    try:
        av: Any = importlib.import_module("av")
        np: Any = importlib.import_module("numpy")
        ov: Any = importlib.import_module("openvino")
        telemetry: Any = importlib.import_module("openvino_telemetry")
        preprocess: Any = importlib.import_module("openvino.preprocess")
    except ImportError:
        raise _IsolationUnavailable("runtime_import") from None
    if (
        av.__version__ != _EXPECTED_PYAV
        or np.__version__ != _EXPECTED_NUMPY
        or ov.__version__ != _EXPECTED_OPENVINO
        or telemetry.__version__ != _EXPECTED_TELEMETRY
        or os.environ.get("OPENVINO_TELEMETRY_CONSENT") != "NO"
    ):
        raise _IsolationUnavailable("runtime_version")

    libc: Any = ctypes.CDLL(None, use_errno=True)
    _no_new_privileges(libc)
    _apply_landlock(libc)
    _apply_seccomp(libc)
    isolation = {
        "landlock_denied": _landlock_denied(),
        "network_denied": _network_denied(),
        "no_new_privileges": True,
        "non_root": os.geteuid() != 0,
    }
    if not all(isolation.values()):
        raise _IsolationUnavailable("isolation_probe")

    if (
        _file_sha256(_MODEL_XML, 1024 * 1024) != _MODEL_XML_SHA256
        or _file_sha256(_MODEL_BIN, 16 * 1024 * 1024) != _MODEL_BIN_SHA256
    ):
        raise _IsolationUnavailable("model_digest")
    worker_sha256 = _file_sha256(Path(__file__), 1024 * 1024)
    core = ov.Core()
    if core.available_devices != ["CPU"]:
        raise _IsolationUnavailable("device")
    model = core.read_model(_MODEL_XML)
    compiled = core.compile_model(
        _preprocessed_model(model, ov, preprocess),
        "CPU",
        {
            "INFERENCE_NUM_THREADS": 4,
            "NUM_STREAMS": 1,
            "PERFORMANCE_HINT": "LATENCY",
        },
    )
    output_port = compiled.output(0)

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
    try:
        format_flags = int(container.flags)
        if format_flags & _FORMAT_FLAG_GENPTS or not format_flags & _FORMAT_FLAG_NOFILLIN:
            raise _IsolationUnavailable("format_flags")
        format_names = set((container.format.name or "").split(","))
        if not format_names.intersection(_ALLOWED_FORMATS) or len(container.streams.video) != 1:
            raise _LimitExceeded("format")
        stream = container.streams.video[0]
        codec = stream.codec_context.name
        if codec not in _ALLOWED_CODECS:
            raise _LimitExceeded("codec")
        stream.thread_type = "NONE"
        stream.thread_count = 1
        if stream.time_base is None or stream.time_base <= 0:
            raise _LimitExceeded("time_base")
        if stream.duration is not None:
            duration = Fraction(stream.duration) * stream.time_base
            if duration < 0 or duration > Fraction(max_duration_seconds):
                raise _LimitExceeded("duration")
        width = int(stream.codec_context.width)
        height = int(stream.codec_context.height)
        if not 1 <= width <= max_width or not 1 <= height <= max_height:
            raise _LimitExceeded("dimensions")
        if width * height > max_pixels:
            raise _LimitExceeded("pixels")

        reformatter = av.video.reformatter.VideoReformatter()
        requested_set = set(requested)
        selected_frames: list[dict[str, object]] = []
        decoded_bytes = 0
        rotations: set[int] = set()
        minimum_time: Fraction | None = None
        maximum_time: Fraction | None = None
        for index, frame in enumerate(container.decode(stream)):
            if index >= max_frames:
                raise _LimitExceeded("frames")
            if frame.pts is None or frame.time_base is None:
                raise _LimitExceeded("missing_pts")
            frame_width = int(frame.width)
            frame_height = int(frame.height)
            if (frame_width, frame_height) != (width, height):
                raise _LimitExceeded("frame_dimensions")
            expected_bytes = frame_width * frame_height * 3
            decoded_bytes += expected_bytes
            if decoded_bytes > max_decoded_bytes:
                raise _LimitExceeded("decoded_bytes")
            position = Fraction(frame.pts) * frame.time_base
            end = position
            if frame.duration is not None:
                frame_duration = Fraction(frame.duration) * frame.time_base
                if frame_duration < 0:
                    raise _LimitExceeded("frame_duration")
                end += frame_duration
            minimum_time = position if minimum_time is None else min(minimum_time, position)
            maximum_time = end if maximum_time is None else max(maximum_time, end)
            if maximum_time - minimum_time > Fraction(max_duration_seconds):
                raise _LimitExceeded("duration")
            rotation = _rotation(frame.rotation)
            rotations.add(rotation)
            if len(rotations) > 1:
                raise _LimitExceeded("rotation")
            if index not in requested_set:
                continue
            pixels = _packed_rgb24(frame, reformatter)
            if len(pixels) != expected_bytes:
                raise _LimitExceeded("rgb_size")
            batch = _inference_batch(pixels, frame_width, frame_height, rotation, np)
            output = compiled([batch])[output_port]
            selected_frames.append(
                {
                    "decode_index": str(index),
                    "detections": _detections(output, max_detections),
                    "duration": _media_time(frame.duration, frame.time_base),
                    "key_frame": bool(frame.key_frame),
                    "pts": _media_time(frame.pts, frame.time_base),
                }
            )
            if index == requested[-1]:
                break
        if tuple(int(cast(str, item["decode_index"])) for item in selected_frames) != requested:
            raise _LimitExceeded("frames")
        if len(rotations) != 1:
            raise _LimitExceeded("rotation")
        return {
            "frames": selected_frames,
            "isolation": isolation,
            "runtime": {
                "model_bin_sha256": _MODEL_BIN_SHA256,
                "model_xml_sha256": _MODEL_XML_SHA256,
                "numpy": np.__version__,
                "openvino": ov.__version__,
                "pyav": av.__version__,
                "runtime_id": _RUNTIME_ID,
                "telemetry": telemetry.__version__,
                "worker_sha256": worker_sha256,
            },
            "schema_version": 1,
            "source": {"bytes": source_bytes, "sha256": source_sha256},
            "status": "ok",
            "stream": {
                "height": height,
                "rotation_degrees": rotations.pop(),
                "stream_index": int(stream.index),
                "time_base": _fraction(stream.time_base),
                "width": width,
            },
        }
    finally:
        container.close()


def main() -> int:
    try:
        result = _run(_parser().parse_args())
    except _LimitExceeded:
        code = 20
        summary = {"schema_version": 1, "status": "limit_exceeded"}
    except _IsolationUnavailable:
        code = 22
        summary = {"schema_version": 1, "status": "isolation_unavailable"}
    except BaseException:
        code = 21
        summary = {"schema_version": 1, "status": "worker_failed"}
    else:
        code = 0
        sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
        summary = {"schema_version": 1, "status": "ok"}
    sys.stderr.write(json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
