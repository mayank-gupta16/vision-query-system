# SPDX-License-Identifier: Apache-2.0
"""Linux-only original-frame decoder staged as an immutable media-runtime overlay.

The worker is intentionally standalone.  It must not import application modules from
the mutable checkout.  A launcher supplies one canonical metadata request on stdin,
a sealed source memfd as descriptor 3, and an exactly pre-sized output memfd as
descriptor 4.  Successful stdout and all stderr records are pixel-free JSON.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import importlib
import json
import os
import platform
import re
import socket
import stat
import sys
from contextlib import suppress
from fractions import Fraction
from pathlib import Path
from typing import Any, NoReturn, cast

_EXPECTED_PYAV = "18.1.0"
_EXPECTED_LIBAVFORMAT = "63.1.101"
_EXPECTED_LIBAVCODEC = "63.1.101"
_MEDIA_RUNTIME_ID = "visualworld-pyav-18.1.0-ffmpeg-9.0.1-v2"
_MEDIA_RUNTIME_MANIFEST_SHA256 = "58cf6f64280888ecc01c647044c38b9b56f197389b6bfc7ead7fbbe93a52ba32"
_MEDIA_RUNTIME_TREE_SHA256 = "7015262cd5dfdfd976d6ee092f0f93541597ea35e0331c3abb9ce6d4c54daeaf"

_REQUEST_SCHEMA = "visualworld.original_frame_request"
_RESULT_SCHEMA = "visualworld.original_frame_result"
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_OUTPUT_BYTES = 128 * 1024 * 1024
_MAX_FRAME_BYTES = 32 * 1024 * 1024
_MAX_DECODED_BYTES = 384 * 1024 * 1024
_MAX_FRAMES = 300
_MAX_REQUESTED_FRAMES = 64
_MAX_DURATION_SECONDS = 60 * 60
_MAX_WIDTH = 4096
_MAX_HEIGHT = 4096
_MAX_PIXELS = 8_388_608
_MAX_I31 = 2**31 - 1
_MAX_I64 = 2**63 - 1
_MIN_I64 = -(2**63)
_MAX_U32 = 2**32 - 1
_MAX_U64 = 2**64 - 1

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_UNSIGNED_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_SIGNED_RE = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")
_TYPED_ID_RE = re.compile(r"(?:src|frm)_[0-9a-f]{64}\Z")

_F_ADD_SEALS = 1033
_F_GET_SEALS = 1034
_F_SEAL_SEAL = 0x0001
_F_SEAL_SHRINK = 0x0002
_F_SEAL_GROW = 0x0004
_F_SEAL_WRITE = 0x0008
_REQUIRED_SEALS = _F_SEAL_SEAL | _F_SEAL_SHRINK | _F_SEAL_GROW | _F_SEAL_WRITE
_INITIAL_OUTPUT_SEALS = _F_SEAL_SHRINK | _F_SEAL_GROW
_FINAL_OUTPUT_SEALS = _F_SEAL_SEAL | _F_SEAL_WRITE

_ALLOWED_FORMATS = frozenset({"h264", "mov"})
_ALLOWED_CODECS = frozenset({"h264", "rawvideo"})
_FORMAT_FLAG_GENPTS = 0x0001
_FORMAT_FLAG_NOFILLIN = 0x0010

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
_LANDLOCK_READ_PATHS = ("/runtime", "/overlay", "/lib", "/lib64")


class _InvalidRequest(RuntimeError):
    pass


class _LimitExceeded(RuntimeError):
    pass


class _DecodeFailed(RuntimeError):
    pass


class _IsolationUnavailable(RuntimeError):
    pass


class _OutputFailed(RuntimeError):
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


def _fail() -> NoReturn:
    raise _InvalidRequest("invalid_request")


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError, RecursionError):
        _fail()


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _reject_number(_: str) -> NoReturn:
    _fail()


def _parse_integer(value: str) -> int:
    if len(value) > 10 or not re.fullmatch(r"(?:0|-?[1-9][0-9]*)", value):
        _fail()
    return int(value)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail()
    return cast(dict[str, object], value)


def _exact(value: object, fields: set[str]) -> dict[str, object]:
    item = _mapping(value)
    if set(item) != fields:
        _fail()
    return item


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail()
    return value


def _decimal(value: object, minimum: int, maximum: int, *, signed: bool) -> str:
    if not isinstance(value, str):
        _fail()
    pattern = _SIGNED_RE if signed else _UNSIGNED_RE
    if not pattern.fullmatch(value):
        _fail()
    number = int(value)
    if not minimum <= number <= maximum:
        _fail()
    return value


def _sha256(value: object) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        _fail()
    return value


def _typed_id(value: object, prefix: str) -> str:
    if (
        not isinstance(value, str)
        or not _TYPED_ID_RE.fullmatch(value)
        or not value.startswith(prefix + "_")
    ):
        _fail()
    return value


def _identifier(prefix: str, projection: dict[str, object]) -> str:
    return f"{prefix}_{hashlib.sha256(_canonical_json(projection)).hexdigest()}"


def _time_base(value: object) -> dict[str, object]:
    item = _exact(value, {"denominator", "numerator"})
    _decimal(item["numerator"], 1, _MAX_U32, signed=False)
    _decimal(item["denominator"], 1, _MAX_U32, signed=False)
    return item


def _producer(value: object) -> None:
    item = _exact(value, {"configuration_sha256", "name", "version"})
    _sha256(item["configuration_sha256"])
    for key in ("name", "version"):
        token = item[key]
        if not isinstance(token, str) or not token.isascii() or not 1 <= len(token) <= 128:
            _fail()


def _media_time(value: object) -> dict[str, object]:
    item = _mapping(value)
    basis = item.get("basis")
    fields = {"basis", "time_base", "value"}
    if basis == "estimated":
        fields.add("estimate")
    elif basis != "measured":
        _fail()
    if set(item) != fields:
        _fail()
    _time_base(item["time_base"])
    _decimal(item["value"], _MIN_I64, _MAX_I64, signed=True)
    if basis == "estimated":
        estimate = _exact(item["estimate"], {"method", "producer"})
        if estimate["method"] != "previous_pts_plus_duration":
            _fail()
        _producer(estimate["producer"])
    return item


def _source(
    value: object,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    item = _exact(
        value,
        {
            "access",
            "fingerprint",
            "identity_version",
            "origin",
            "schema",
            "schema_version",
            "source_id",
            "streams",
        },
    )
    if (
        item["schema"] != "visualworld.source"
        or type(item["schema_version"]) is not int
        or item["schema_version"] != 1
        or type(item["identity_version"]) is not int
        or item["identity_version"] != 1
    ):
        _fail()
    access = _exact(item["access"], {"classification", "retention"})
    origin = _exact(item["origin"], {"kind", "locator_stored"})
    if access != {"classification": "private", "retention": "source_controlled"}:
        _fail()
    if origin != {"kind": "local_file", "locator_stored": False}:
        _fail()
    fingerprint = _exact(item["fingerprint"], {"algorithm", "bytes", "digest"})
    if fingerprint["algorithm"] != "sha256":
        _fail()
    _sha256(fingerprint["digest"])
    source_bytes = int(_decimal(fingerprint["bytes"], 1, _MAX_SOURCE_BYTES, signed=False))
    source_id = _typed_id(item["source_id"], "src")
    projection = {"fingerprint": fingerprint, "identity_version": 1}
    if source_id != _identifier("src", projection):
        _fail()
    streams = item["streams"]
    if not isinstance(streams, list) or not 1 <= len(streams) <= 32:
        _fail()
    parsed: list[dict[str, object]] = []
    indexes: set[int] = set()
    for value_stream in streams:
        stream = _exact(
            value_stream,
            {
                "height",
                "media_type",
                "rotation_degrees",
                "stream_index",
                "time_base",
                "width",
            },
        )
        index = _bounded_int(stream["stream_index"], 0, _MAX_I31)
        width = _bounded_int(stream["width"], 1, _MAX_WIDTH)
        height = _bounded_int(stream["height"], 1, _MAX_HEIGHT)
        rotation = _bounded_int(stream["rotation_degrees"], 0, 270)
        if (
            index in indexes
            or stream["media_type"] != "video"
            or rotation not in {0, 90, 180, 270}
            or width * height > _MAX_PIXELS
            or width * height * 3 > _MAX_FRAME_BYTES
        ):
            _fail()
        _time_base(stream["time_base"])
        indexes.add(index)
        parsed.append(stream)
    # Bytes are returned separately so descriptor validation does not trust a reparsed string.
    item["_validated_source_bytes"] = source_bytes
    return item, {str(stream["stream_index"]): stream for stream in parsed}


def _frame_ref(
    value: object,
    source_id: str,
    streams: dict[str, dict[str, object]],
) -> dict[str, object]:
    item = _mapping(value)
    required = {
        "decode_index",
        "frame_id",
        "identity_version",
        "pts",
        "schema",
        "schema_version",
        "source_id",
        "stream_index",
    }
    optional = {"duration", "key_frame"}
    if not required.issubset(item) or not set(item).issubset(required | optional):
        _fail()
    if (
        item["schema"] != "visualworld.frame_ref"
        or type(item["schema_version"]) is not int
        or item["schema_version"] != 1
        or type(item["identity_version"]) is not int
        or item["identity_version"] != 1
        or item["source_id"] != source_id
    ):
        _fail()
    stream_index = _bounded_int(item["stream_index"], 0, _MAX_I31)
    if str(stream_index) not in streams:
        _fail()
    decode_index = _decimal(item["decode_index"], 0, _MAX_FRAMES - 1, signed=False)
    pts = _media_time(item["pts"])
    if "duration" in item:
        duration = _media_time(item["duration"])
        if int(cast(str, duration["value"])) < 0:
            _fail()
    if "key_frame" in item and type(item["key_frame"]) is not bool:
        _fail()
    frame_id = _typed_id(item["frame_id"], "frm")
    projection = {
        "decode_index": decode_index,
        "identity_version": 1,
        "pts": pts,
        "source_id": source_id,
        "stream_index": stream_index,
    }
    if frame_id != _identifier("frm", projection):
        _fail()
    return item


def _parse_request(data: bytes) -> dict[str, object]:
    if not data or len(data) > _MAX_REQUEST_BYTES or data.startswith(b"\xef\xbb\xbf"):
        _fail()
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_pairs,
            parse_int=_parse_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except _InvalidRequest:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        _fail()
    request = _exact(
        value,
        {"frames", "output_bytes", "schema", "schema_version", "source"},
    )
    if _canonical_json(request) != data:
        _fail()
    if (
        request["schema"] != _REQUEST_SCHEMA
        or type(request["schema_version"]) is not int
        or request["schema_version"] != 1
    ):
        _fail()
    source, streams = _source(request["source"])
    source_bytes = cast(int, source.pop("_validated_source_bytes"))
    frames = request["frames"]
    if not isinstance(frames, list) or not 1 <= len(frames) <= _MAX_REQUESTED_FRAMES:
        _fail()
    parsed_frames = [_frame_ref(frame, cast(str, source["source_id"]), streams) for frame in frames]
    stream_indexes = {cast(int, frame["stream_index"]) for frame in parsed_frames}
    decode_indexes = [int(cast(str, frame["decode_index"])) for frame in parsed_frames]
    frame_ids = [cast(str, frame["frame_id"]) for frame in parsed_frames]
    if (
        len(stream_indexes) != 1
        or decode_indexes != sorted(decode_indexes)
        or len(set(decode_indexes)) != len(decode_indexes)
        or len(set(frame_ids)) != len(frame_ids)
    ):
        _fail()
    stream = streams[str(stream_indexes.pop())]
    for frame in parsed_frames:
        if cast(dict[str, object], frame["pts"])["time_base"] != stream["time_base"]:
            _fail()
        if (
            "duration" in frame
            and cast(dict[str, object], frame["duration"])["time_base"] != stream["time_base"]
        ):
            _fail()
    frame_bytes = cast(int, stream["width"]) * cast(int, stream["height"]) * 3
    expected_output = frame_bytes * len(parsed_frames)
    output_bytes = int(_decimal(request["output_bytes"], 1, _MAX_OUTPUT_BYTES, signed=False))
    if output_bytes != expected_output:
        _fail()
    return {
        "frames": parsed_frames,
        "output_bytes": output_bytes,
        "source": source,
        "source_bytes": source_bytes,
        "stream": stream,
    }


def _stmt(code: int, value: int) -> _SockFilter:
    return _SockFilter(code, 0, 0, value)


def _jump(code: int, value: int, true_offset: int, false_offset: int) -> _SockFilter:
    return _SockFilter(code, true_offset, false_offset, value)


def _no_new_privileges(libc: Any) -> None:
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise _IsolationUnavailable("isolation_unavailable")


def _apply_landlock(libc: Any) -> None:
    abi = libc.syscall(
        _LANDLOCK_CREATE_RULESET,
        0,
        0,
        _LANDLOCK_CREATE_RULESET_VERSION,
    )
    if abi < 1:
        raise _IsolationUnavailable("isolation_unavailable")
    attributes = _LandlockRulesetAttr(_LANDLOCK_ACCESS_FS_V1)
    descriptor = libc.syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.byref(attributes),
        ctypes.sizeof(attributes),
        0,
    )
    if descriptor < 0:
        raise _IsolationUnavailable("isolation_unavailable")
    allowed_descriptors: list[int] = []
    try:
        for path in _LANDLOCK_READ_PATHS:
            try:
                allowed = os.open(path, getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC)
            except OSError:
                raise _IsolationUnavailable("isolation_unavailable") from None
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
                raise _IsolationUnavailable("isolation_unavailable")
        if libc.syscall(_LANDLOCK_RESTRICT_SELF, descriptor, 0) != 0:
            raise _IsolationUnavailable("isolation_unavailable")
    finally:
        for allowed in allowed_descriptors:
            os.close(allowed)
        os.close(descriptor)


def _apply_seccomp(libc: Any) -> None:
    # Matches the accepted media worker: no network, process creation, ptrace,
    # namespace/mount mutation, BPF, userfaultfd, or io_uring escape surface.
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
        raise _IsolationUnavailable("isolation_unavailable")


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


def _descriptor_seals(descriptor: int) -> int:
    try:
        return int(fcntl.fcntl(descriptor, _F_GET_SEALS))
    except OSError:
        raise _InvalidRequest("invalid_request") from None


def _validate_descriptors(source_bytes: int, output_bytes: int) -> None:
    try:
        source = os.fstat(3)
        output = os.fstat(4)
        output_flags = fcntl.fcntl(4, fcntl.F_GETFL)
    except OSError:
        raise _InvalidRequest("invalid_request") from None
    if (
        not stat.S_ISREG(source.st_mode)
        or not stat.S_ISREG(output.st_mode)
        or source.st_size != source_bytes
        or output.st_size != output_bytes
        or (source.st_ino == output.st_ino and source.st_dev == output.st_dev)
        or output_flags & os.O_ACCMODE == os.O_RDONLY
        or _descriptor_seals(3) & _REQUIRED_SEALS != _REQUIRED_SEALS
        or _descriptor_seals(4) != _INITIAL_OUTPUT_SEALS
    ):
        _fail()


def _source_sha256(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    try:
        while offset < size:
            chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
            if not chunk:
                raise _DecodeFailed("decode_failed")
            digest.update(chunk)
            offset += len(chunk)
    except OSError:
        raise _DecodeFailed("decode_failed") from None
    return digest.hexdigest()


def _fraction(value: Fraction | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {"denominator": str(value.denominator), "numerator": str(value.numerator)}


def _decoded_time(value: int | None, time_base: Fraction | None) -> dict[str, object] | None:
    if value is None or time_base is None:
        return None
    return {
        "basis": "measured",
        "time_base": _fraction(time_base),
        "value": str(value),
    }


def _time_equal(actual: dict[str, object] | None, expected: object) -> bool:
    return actual is not None and actual == expected


def _rotation(value: object) -> int:
    if type(value) not in {int, float}:
        raise _DecodeFailed("decode_failed")
    numeric = cast(int | float, value)
    if int(numeric) != numeric:
        raise _DecodeFailed("decode_failed")
    rotation = int(numeric) % 360
    if rotation not in {0, 90, 180, 270}:
        raise _DecodeFailed("decode_failed")
    return rotation


def _packed_rgb24(frame: Any, reformatter: Any) -> bytes:
    converted = reformatter.reformat(frame, format="rgb24")
    plane = converted.planes[0]
    raw = bytes(plane)
    row_bytes = int(converted.width) * 3
    line_size = int(plane.line_size)
    height = int(converted.height)
    if line_size < row_bytes or len(raw) < line_size * height:
        raise _DecodeFailed("decode_failed")
    return b"".join(
        raw[offset : offset + row_bytes] for offset in range(0, line_size * height, line_size)
    )


def _library_version(av: Any, name: str) -> str:
    try:
        values = av.library_versions[name]
        return ".".join(str(int(value)) for value in values)
    except (KeyError, TypeError, ValueError):
        raise _IsolationUnavailable("isolation_unavailable") from None


def _decode_requested(
    av: Any,
    request: dict[str, object],
    source: Any,
) -> tuple[list[bytes], list[dict[str, object]], dict[str, object]]:
    frames = cast(list[dict[str, object]], request["frames"])
    stream_record = cast(dict[str, object], request["stream"])
    requested = {int(cast(str, frame["decode_index"])): frame for frame in frames}
    selected_pixels: list[bytes] = []
    selected_metadata: list[dict[str, object]] = []
    decoded_bytes = 0
    minimum_time: Fraction | None = None
    maximum_time: Fraction | None = None
    try:
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
    except BaseException:
        raise _DecodeFailed("decode_failed") from None
    try:
        flags = int(container.flags)
        format_names = set((container.format.name or "").split(","))
        if flags & _FORMAT_FLAG_GENPTS or not flags & _FORMAT_FLAG_NOFILLIN:
            raise _IsolationUnavailable("isolation_unavailable")
        if not format_names.intersection(_ALLOWED_FORMATS) or len(container.streams.video) != 1:
            raise _DecodeFailed("decode_failed")
        stream = container.streams.video[0]
        codec = stream.codec_context.name
        if codec not in _ALLOWED_CODECS:
            raise _DecodeFailed("decode_failed")
        stream.thread_type = "NONE"
        stream.thread_count = 1
        width = int(stream.codec_context.width)
        height = int(stream.codec_context.height)
        if (
            int(stream.index) != stream_record["stream_index"]
            or width != stream_record["width"]
            or height != stream_record["height"]
            or _fraction(stream.time_base) != stream_record["time_base"]
        ):
            raise _DecodeFailed("decode_failed")
        if stream.duration is not None:
            duration = Fraction(stream.duration) * stream.time_base
            if duration < 0 or duration > _MAX_DURATION_SECONDS:
                raise _LimitExceeded("limit_exceeded")
        reformatter = av.video.reformatter.VideoReformatter()
        for index, frame in enumerate(container.decode(stream)):
            if index >= _MAX_FRAMES:
                raise _LimitExceeded("limit_exceeded")
            if frame.pts is None or frame.time_base is None:
                raise _DecodeFailed("decode_failed")
            if int(frame.width) != width or int(frame.height) != height:
                raise _DecodeFailed("decode_failed")
            expected_bytes = width * height * 3
            decoded_bytes += expected_bytes
            if decoded_bytes > _MAX_DECODED_BYTES:
                raise _LimitExceeded("limit_exceeded")
            position = Fraction(frame.pts) * frame.time_base
            end = position
            if frame.duration is not None:
                frame_duration = Fraction(frame.duration) * frame.time_base
                if frame_duration < 0:
                    raise _DecodeFailed("decode_failed")
                end += frame_duration
            minimum_time = position if minimum_time is None else min(minimum_time, position)
            maximum_time = end if maximum_time is None else max(maximum_time, end)
            if maximum_time - minimum_time > _MAX_DURATION_SECONDS:
                raise _LimitExceeded("limit_exceeded")
            if _rotation(frame.rotation) != stream_record["rotation_degrees"]:
                raise _DecodeFailed("decode_failed")
            expected = requested.get(index)
            if expected is None:
                continue
            pts = _decoded_time(frame.pts, frame.time_base)
            duration = _decoded_time(frame.duration, frame.time_base)
            if not _time_equal(pts, expected["pts"]):
                raise _DecodeFailed("decode_failed")
            if "duration" in expected and not _time_equal(duration, expected["duration"]):
                raise _DecodeFailed("decode_failed")
            if "key_frame" in expected and bool(frame.key_frame) is not expected["key_frame"]:
                raise _DecodeFailed("decode_failed")
            pixels = _packed_rgb24(frame, reformatter)
            if len(pixels) != expected_bytes:
                raise _DecodeFailed("decode_failed")
            selected_pixels.append(pixels)
            selected_metadata.append(
                {
                    "artifact": {
                        "bytes": str(len(pixels)),
                        "media_type": "application/vnd.visualworld.rgb24",
                        "sha256": hashlib.sha256(pixels).hexdigest(),
                    },
                    "byte_offset": str((len(selected_pixels) - 1) * expected_bytes),
                    "frame_ref": expected,
                }
            )
            if index == max(requested):
                break
        if [item["frame_ref"] for item in selected_metadata] != frames:
            raise _DecodeFailed("decode_failed")
        stream_metadata = {
            "codec": codec,
            "format_name": sorted(format_names.intersection(_ALLOWED_FORMATS))[0],
            "height": height,
            "rotation_degrees": stream_record["rotation_degrees"],
            "stream_index": int(stream.index),
            "time_base": _fraction(stream.time_base),
            "width": width,
        }
        return selected_pixels, selected_metadata, stream_metadata
    except (_InvalidRequest, _LimitExceeded, _DecodeFailed, _IsolationUnavailable):
        raise
    except BaseException:
        raise _DecodeFailed("decode_failed") from None
    finally:
        with suppress(BaseException):
            container.close()


def _publish_output(descriptor: int, chunks: list[bytes], expected_bytes: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    try:
        for chunk in chunks:
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.pwrite(descriptor, view, offset)
                if written <= 0:
                    raise _OutputFailed("output_failed")
                offset += written
                view = view[written:]
        if offset != expected_bytes or os.fstat(descriptor).st_size != expected_bytes:
            raise _OutputFailed("output_failed")
        os.fsync(descriptor)
        fcntl.fcntl(descriptor, _F_ADD_SEALS, _FINAL_OUTPUT_SEALS)
        if _descriptor_seals(descriptor) != _REQUIRED_SEALS:
            raise _OutputFailed("output_failed")
    except _OutputFailed:
        raise
    except BaseException:
        raise _OutputFailed("output_failed") from None
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        raise _IsolationUnavailable("isolation_unavailable") from None
    if not data or len(data) > 1024 * 1024:
        raise _IsolationUnavailable("isolation_unavailable")
    return hashlib.sha256(data).hexdigest()


def _run(data: bytes) -> dict[str, object]:
    if platform.system() != "Linux" or platform.machine() != "x86_64" or os.geteuid() == 0:
        raise _IsolationUnavailable("isolation_unavailable")
    request = _parse_request(data)
    _validate_descriptors(cast(int, request["source_bytes"]), cast(int, request["output_bytes"]))
    source = cast(dict[str, object], request["source"])
    fingerprint = cast(dict[str, object], source["fingerprint"])
    source_digest = _source_sha256(3, cast(int, request["source_bytes"]))
    if source_digest != fingerprint["digest"]:
        raise _DecodeFailed("decode_failed")
    try:
        av: Any = importlib.import_module("av")
    except ImportError:
        raise _IsolationUnavailable("isolation_unavailable") from None
    if (
        av.__version__ != _EXPECTED_PYAV
        or _library_version(av, "libavformat") != _EXPECTED_LIBAVFORMAT
        or _library_version(av, "libavcodec") != _EXPECTED_LIBAVCODEC
    ):
        raise _IsolationUnavailable("isolation_unavailable")
    libc: Any = ctypes.CDLL(None, use_errno=True)
    _no_new_privileges(libc)
    _apply_landlock(libc)
    _apply_seccomp(libc)
    isolation = {
        "landlock_denied": _landlock_denied(),
        "network_denied": _network_denied(),
        "no_new_privileges": True,
        "non_root": True,
    }
    if not all(isolation.values()):
        raise _IsolationUnavailable("isolation_unavailable")
    worker_sha256 = _file_sha256(Path(__file__))
    try:
        os.lseek(3, 0, os.SEEK_SET)
    except OSError:
        raise _DecodeFailed("decode_failed") from None
    source_stream = os.fdopen(3, "rb", closefd=False)
    pixels, frames, stream = _decode_requested(av, request, source_stream)
    output_digest = _publish_output(4, pixels, cast(int, request["output_bytes"]))
    return {
        "frames": frames,
        "isolation": isolation,
        "output": {
            "bytes": str(request["output_bytes"]),
            "layout": "request_order_contiguous_packed_rgb24_encoded_source",
            "sealed": True,
            "sha256": output_digest,
        },
        "runtime": {
            "libavcodec": _EXPECTED_LIBAVCODEC,
            "libavformat": _EXPECTED_LIBAVFORMAT,
            "media_runtime_id": _MEDIA_RUNTIME_ID,
            "media_runtime_manifest_sha256": _MEDIA_RUNTIME_MANIFEST_SHA256,
            "media_runtime_tree_sha256": _MEDIA_RUNTIME_TREE_SHA256,
            "pyav": _EXPECTED_PYAV,
            "worker_sha256": worker_sha256,
        },
        "schema": _RESULT_SCHEMA,
        "schema_version": 1,
        "source": {
            "bytes": str(request["source_bytes"]),
            "sha256": source_digest,
            "source_id": source["source_id"],
        },
        "status": "ok",
        "stream": stream,
    }


def _summary(status: str) -> bytes:
    return _canonical_json({"schema_version": 1, "status": status}) + b"\n"


def main() -> int:
    result: dict[str, object] | None = None
    try:
        if len(sys.argv) != 1:
            raise _InvalidRequest("invalid_request")
        data = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
        result = _run(data)
        sys.stdout.buffer.write(_canonical_json(result) + b"\n")
        sys.stdout.buffer.flush()
    except _LimitExceeded:
        code, status = 20, "limit_exceeded"
    except _DecodeFailed:
        code, status = 21, "decode_failed"
    except _IsolationUnavailable:
        code, status = 22, "isolation_unavailable"
    except _InvalidRequest:
        code, status = 23, "invalid_request"
    except _OutputFailed:
        code, status = 24, "output_failed"
    except BaseException:
        code, status = 21, "decode_failed"
    else:
        code, status = 0, "ok"
    try:
        sys.stderr.buffer.write(_summary(status))
        sys.stderr.buffer.flush()
    except BaseException:
        return code
    return code


if __name__ == "__main__":
    raise SystemExit(main())
