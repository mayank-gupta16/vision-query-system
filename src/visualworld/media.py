# SPDX-License-Identifier: Apache-2.0
"""Bounded Linux-only local media adapter and hostile-input supervisor."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import platform
import re
import selectors
import signal
import stat
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from visualworld.ingestion import (
    Fingerprint,
    FrameRef,
    MediaTime,
    Source,
    SourceStream,
    TimeBase,
)
from visualworld.ports import (
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    PortCall,
    PortError,
    PortErrorCode,
    PortKind,
)

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}\Z")
_MAX_STREAM_INDEX = 2**31 - 1
_MAX_U64 = 2**64 - 1
_OPENAT2 = 437
_MEMFD_CREATE = 319
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08
_MFD_ALLOW_SEALING = 0x0002
_MFD_CLOEXEC = 0x0001
_F_ADD_SEALS = 1033
_F_SEAL_SEAL = 0x0001
_F_SEAL_SHRINK = 0x0002
_F_SEAL_GROW = 0x0004
_F_SEAL_WRITE = 0x0008
_SYSTEMD_RUN = Path("/usr/bin/systemd-run")
_SYSTEMCTL = Path("/usr/bin/systemctl")
_BWRAP = Path("/usr/bin/bwrap")
_CGROUP_ROOT = Path("/sys/fs/cgroup")


@dataclass(frozen=True, slots=True)
class MediaLimits:
    max_source_bytes: int = 64 * 1024 * 1024
    max_duration_seconds: int = 60 * 60
    max_width: int = 4096
    max_height: int = 4096
    max_pixels: int = 8_388_608
    max_frames: int = 300
    max_decoded_bytes: int = 384 * 1024 * 1024
    max_worker_output_bytes: int = 1024 * 1024
    wall_timeout_ms: int = 20_000
    cpu_budget_ms: int = 10_000
    memory_bytes: int = 256 * 1024 * 1024
    task_count: int = 64

    def __post_init__(self) -> None:
        positive = (
            self.max_source_bytes,
            self.max_duration_seconds,
            self.max_width,
            self.max_height,
            self.max_pixels,
            self.max_frames,
            self.max_decoded_bytes,
            self.max_worker_output_bytes,
            self.wall_timeout_ms,
            self.cpu_budget_ms,
            self.memory_bytes,
            self.task_count,
        )
        if not all(type(value) is int and value > 0 for value in positive):
            raise ValueError("media limits must be positive integers")
        if self.max_source_bytes > 2**30 or self.max_worker_output_bytes > 16 * 1024 * 1024:
            raise ValueError("media byte limit exceeds the v1 bound")
        if self.max_frames > 100_000 or self.task_count > 1024:
            raise ValueError("media count limit exceeds the v1 bound")


@dataclass(frozen=True, slots=True)
class MediaRuntime:
    root: Path
    worker: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _absolute(self.root))
        object.__setattr__(self, "worker", _absolute(self.worker))


@dataclass(frozen=True, slots=True)
class ProbeDetails:
    format_name: str
    codec: str
    duration: MediaTime | None
    pixel_hashes: tuple[str, ...]
    pyav_version: str
    libavformat_version: str
    libavcodec_version: str


@dataclass(frozen=True, slots=True)
class MediaMetrics:
    wall_ms: int
    cpu_ms: int
    memory_peak_bytes: int
    source_bytes: int
    frame_count: int


@dataclass(frozen=True, slots=True)
class _Decoded:
    source: Source
    frames: tuple[FrameRef, ...]
    details: ProbeDetails
    metrics: MediaMetrics


@dataclass(frozen=True, slots=True)
class _WorkerRun:
    stdout: bytes
    stderr: bytes
    returncode: int
    wall_ms: int
    cpu_ms: int
    memory_peak_bytes: int


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


def _error(code: PortErrorCode, operation: str) -> PortError:
    return PortError(code, PortKind.VIDEO_SOURCE, operation)


def _absolute(path: Path) -> Path:
    if not isinstance(path, Path):
        raise ValueError("runtime paths must be pathlib.Path values")
    return Path(os.path.abspath(os.fspath(path)))


def _relative_source(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error(PortErrorCode.INVALID_REQUEST, "open")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise _error(PortErrorCode.INVALID_REQUEST, "open") from None
    if len(encoded) > 4096:
        raise _error(PortErrorCode.INVALID_REQUEST, "open")
    if any(ord(character) < 32 for character in value) or "\\" in value:
        raise _error(PortErrorCode.INVALID_REQUEST, "open")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _error(PortErrorCode.INVALID_REQUEST, "open")
    return value


def _trusted_regular(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_mode & 0o022 == 0
    )


def _trusted_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_mode & 0o022 == 0
        and metadata.st_mode & stat.S_IXOTH != 0
    )


def _source_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _capability_check(runtime: MediaRuntime) -> None:
    if platform.system() != "Linux" or platform.machine() != "x86_64" or os.geteuid() != 0:
        raise _error(PortErrorCode.ISOLATION_UNAVAILABLE, "probe")
    if not all(_trusted_regular(path) for path in (_SYSTEMD_RUN, _SYSTEMCTL, _BWRAP)):
        raise _error(PortErrorCode.ISOLATION_UNAVAILABLE, "probe")
    try:
        relative_worker = runtime.worker.relative_to(runtime.root)
    except ValueError:
        raise _error(PortErrorCode.ISOLATION_UNAVAILABLE, "probe") from None
    trusted_directories = {
        runtime.root,
        *runtime.root.parents,
        *(
            runtime.root / Path(*relative_worker.parts[:index])
            for index in range(1, len(relative_worker.parts))
        ),
    }
    if not all(_trusted_directory(path) for path in trusted_directories) or not _trusted_regular(
        runtime.worker
    ):
        raise _error(PortErrorCode.ISOLATION_UNAVAILABLE, "probe")
    required = (
        runtime.root / "python/bin/python3.13",
        runtime.root / "python/BUILD",
        runtime.root / "ffmpeg/lib",
        runtime.root / "venv/lib/python3.13/site-packages",
    )
    if not all(path.exists() and not path.is_symlink() for path in required):
        raise _error(PortErrorCode.ISOLATION_UNAVAILABLE, "probe")
    if not (_CGROUP_ROOT / "cgroup.controllers").is_file():
        raise _error(PortErrorCode.ISOLATION_UNAVAILABLE, "probe")


def _open_source(root: Path, relative: str, maximum: int) -> tuple[int, os.stat_result]:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise _error(PortErrorCode.ISOLATION_UNAVAILABLE, "open")
    root = _absolute(root)
    if not _source_directory(root):
        raise _error(PortErrorCode.INVALID_REQUEST, "open")
    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    root_fd: int | None = None
    with suppress(OSError):
        root_fd = os.open(root, root_flags)
    if root_fd is None:
        raise _error(PortErrorCode.INVALID_REQUEST, "open")
    try:
        how = _OpenHow(
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            0,
            _RESOLVE_BENEATH | _RESOLVE_NO_MAGICLINKS | _RESOLVE_NO_SYMLINKS,
        )
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.syscall(
            ctypes.c_long(_OPENAT2),
            ctypes.c_int(root_fd),
            ctypes.c_char_p(os.fsencode(relative)),
            ctypes.byref(how),
            ctypes.sizeof(how),
        )
        if result < 0:
            code = ctypes.get_errno()
            if code in {errno.ENOSYS, errno.EPERM}:
                raise _error(PortErrorCode.ISOLATION_UNAVAILABLE, "open")
            if code == errno.ENOENT:
                raise _error(PortErrorCode.NOT_FOUND, "open")
            raise _error(PortErrorCode.INVALID_REQUEST, "open")
        source_fd = int(result)
    finally:
        os.close(root_fd)
    metadata = os.fstat(source_fd)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(source_fd)
        raise _error(PortErrorCode.INVALID_REQUEST, "open")
    if metadata.st_size > maximum:
        os.close(source_fd)
        raise _error(PortErrorCode.LIMIT_EXCEEDED, "open")
    return source_fd, metadata


def _sealed_snapshot(source_fd: int, maximum: int) -> tuple[int, str, int]:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(
        ctypes.c_long(_MEMFD_CREATE),
        ctypes.c_char_p(b"visualworld-source"),
        ctypes.c_uint(_MFD_CLOEXEC | _MFD_ALLOW_SEALING),
    )
    if result < 0:
        raise _error(PortErrorCode.ISOLATION_UNAVAILABLE, "snapshot") from None
    snapshot = int(result)
    digest = hashlib.sha256()
    total = 0
    os_failure = False
    try:
        while True:
            chunk = os.read(source_fd, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise _error(PortErrorCode.LIMIT_EXCEEDED, "snapshot")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(snapshot, view)
                if written <= 0:
                    raise _error(PortErrorCode.DECODE_FAILED, "snapshot")
                view = view[written:]
        fcntl.fcntl(
            snapshot,
            _F_ADD_SEALS,
            _F_SEAL_SEAL | _F_SEAL_SHRINK | _F_SEAL_GROW | _F_SEAL_WRITE,
        )
        os.lseek(snapshot, 0, os.SEEK_SET)
    except OSError:
        os_failure = True
    except BaseException:
        os.close(snapshot)
        raise
    if os_failure:
        os.close(snapshot)
        raise _error(PortErrorCode.DECODE_FAILED, "snapshot")
    return snapshot, digest.hexdigest(), total


def _namespace_argv(runtime: MediaRuntime, limits: MediaLimits) -> list[str]:
    relative_worker = runtime.worker.relative_to(runtime.root)
    worker_args = [
        "/runtime/python/bin/python3.13",
        os.fspath(PurePosixPath("/runtime", *relative_worker.parts)),
        "--max-duration-seconds",
        str(limits.max_duration_seconds),
        "--max-width",
        str(limits.max_width),
        "--max-height",
        str(limits.max_height),
        "--max-pixels",
        str(limits.max_pixels),
        "--max-frames",
        str(limits.max_frames),
        "--max-decoded-bytes",
        str(limits.max_decoded_bytes),
    ]
    return [
        os.fspath(_BWRAP),
        "--unshare-user",
        "--uid",
        "65534",
        "--gid",
        "65534",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "PATH",
        "/runtime/python/bin",
        "--setenv",
        "HOME",
        "/nonexistent",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "PYTHONPATH",
        "/runtime/venv/lib/python3.13/site-packages",
        "--setenv",
        "LD_LIBRARY_PATH",
        "/runtime/ffmpeg/lib",
        "--ro-bind",
        os.fspath(runtime.root),
        "/runtime",
        "--dir",
        "/lib",
        "--ro-bind",
        "/usr/lib/x86_64-linux-gnu",
        "/lib/x86_64-linux-gnu",
        "--ro-bind",
        "/usr/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        "33554432",
        "--tmpfs",
        "/tmp",
        "--remount-ro",
        "/",
        "--chdir",
        "/",
        *worker_args,
    ]


def _systemd_argv(
    runtime: MediaRuntime,
    limits: MediaLimits,
    snapshot_fd: int,
    unit: str,
) -> list[str]:
    properties = (
        "User=nobody",
        "Group=nogroup",
        "NoNewPrivileges=yes",
        f"MemoryMax={limits.memory_bytes}",
        "MemorySwapMax=0",
        f"TasksMax={limits.task_count}",
        "CPUQuota=200%",
        f"RuntimeMaxSec={max(1, (limits.wall_timeout_ms + 999) // 1000)}s",
        "LimitNOFILE=64",
        f"LimitNPROC={limits.task_count}",
        f"LimitFSIZE={limits.max_worker_output_bytes}",
        "UMask=0077",
        f"OpenFile=/proc/{os.getpid()}/fd/{snapshot_fd}:visualworld-source:read-only",
    )
    return [
        os.fspath(_SYSTEMD_RUN),
        "--quiet",
        "--pipe",
        "--wait",
        "--service-type=exec",
        f"--unit={unit}",
        *(f"--property={item}" for item in properties),
        *_namespace_argv(runtime, limits),
    ]


def _read_number(path: Path, field: str | None = None) -> int:
    try:
        text = path.read_text(encoding="ascii")
    except OSError:
        return 0
    if field is None:
        return int(text.strip()) if text.strip().isdigit() else 0
    for line in text.splitlines():
        name, separator, value = line.partition(" ")
        if separator and name == field and value.isdigit():
            return int(value)
    return 0


def _kill_unit(unit: str, cgroup: Path, process: subprocess.Popen[bytes]) -> None:
    kill_file = cgroup / "cgroup.kill"
    try:
        descriptor = os.open(kill_file, os.O_WRONLY | os.O_CLOEXEC)
    except OSError:
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [os.fspath(_SYSTEMCTL), "kill", "--kill-whom=all", "--signal=SIGKILL", unit],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
    else:
        try:
            os.write(descriptor, b"1")
        finally:
            os.close(descriptor)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def _drain_worker(
    process: subprocess.Popen[bytes],
    unit: str,
    limits: MediaLimits,
    cancelled: threading.Event | None,
) -> _WorkerRun:
    if process.stdout is None or process.stderr is None:
        raise _error(PortErrorCode.ISOLATION_UNAVAILABLE, "decode")
    selector = selectors.DefaultSelector()
    streams: dict[int, bytearray] = {
        process.stdout.fileno(): bytearray(),
        process.stderr.fileno(): bytearray(),
    }
    for descriptor in streams:
        os.set_blocking(descriptor, False)
        selector.register(descriptor, selectors.EVENT_READ)
    started = time.monotonic()
    deadline = started + limits.wall_timeout_ms / 1000
    cgroup = _CGROUP_ROOT / "system.slice" / f"{unit}.service"
    cpu_peak = 0
    memory_peak = 0
    failure: PortErrorCode | None = None
    killed = False
    while selector.get_map() or process.poll() is None:
        now = time.monotonic()
        cpu_peak = max(cpu_peak, _read_number(cgroup / "cpu.stat", "usage_usec"))
        memory_peak = max(
            memory_peak,
            _read_number(cgroup / "memory.peak"),
            _read_number(cgroup / "memory.current"),
        )
        if cancelled is not None and cancelled.is_set():
            failure = PortErrorCode.CANCELLED
        elif now >= deadline or cpu_peak > limits.cpu_budget_ms * 1000:
            failure = PortErrorCode.TIMEOUT
        if failure is not None and not killed:
            _kill_unit(f"{unit}.service", cgroup, process)
            killed = True
        events = selector.select(0.02)
        for key, _ in events:
            descriptor = cast(int, key.fileobj)
            try:
                chunk = os.read(descriptor, 65_536)
            except BlockingIOError:
                continue
            if not chunk:
                selector.unregister(descriptor)
                continue
            streams[descriptor].extend(chunk)
            if sum(len(buffer) for buffer in streams.values()) > limits.max_worker_output_bytes:
                failure = PortErrorCode.LIMIT_EXCEEDED
                if not killed:
                    _kill_unit(f"{unit}.service", cgroup, process)
                    killed = True
        if failure is not None and process.poll() is not None and not selector.get_map():
            break
    try:
        returncode = process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _kill_unit(f"{unit}.service", cgroup, process)
        returncode = process.wait(timeout=2)
    wall_ms = max(0, int((time.monotonic() - started) * 1000))
    stdout = bytes(streams[process.stdout.fileno()])
    stderr = bytes(streams[process.stderr.fileno()])
    selector.close()
    process.stdout.close()
    process.stderr.close()
    if failure is not None:
        raise _error(failure, "decode")
    return _WorkerRun(stdout, stderr, returncode, wall_ms, cpu_peak // 1000, memory_peak)


def _run_worker(
    runtime: MediaRuntime,
    limits: MediaLimits,
    snapshot_fd: int,
    cancelled: threading.Event | None,
) -> _WorkerRun:
    unit = f"visualworld-media-{os.getpid()}-{time.monotonic_ns()}"
    process = subprocess.Popen(
        _systemd_argv(runtime, limits, snapshot_fd, unit),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        return _drain_worker(process, unit, limits, cancelled)
    finally:
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [os.fspath(_SYSTEMCTL), "reset-failed", f"{unit}.service"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )


def _mapping(value: object, fields: frozenset[str]) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or not all(isinstance(key, str) for key in value)
    ):
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    return cast(dict[str, object], value)


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    return value


def _token(value: object) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    return value


def _decimal(value: object, maximum: int = _MAX_U64) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
        or len(value) > 20
        or int(value) > maximum
    ):
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    return value


def _cursor(value: object) -> int:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise _error(PortErrorCode.INVALID_REQUEST, "read_frames")
    if len(value) > 20 or int(value) > _MAX_U64:
        raise _error(PortErrorCode.LIMIT_EXCEEDED, "read_frames")
    return int(value)


def _signed_decimal(value: object) -> str:
    if not isinstance(value, str) or not value.isascii() or len(value) > 20:
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    unsigned = value[1:] if value.startswith("-") else value
    if (
        not unsigned.isdecimal()
        or (len(unsigned) > 1 and unsigned.startswith("0"))
        or value == "-0"
    ):
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    integer = int(value)
    if not -(2**63) <= integer <= 2**63 - 1:
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    return value


def _time_base(value: object) -> TimeBase:
    item = _mapping(value, frozenset({"numerator", "denominator"}))
    numerator = _decimal(item["numerator"], 2**63 - 1)
    denominator = _decimal(item["denominator"], 2**63 - 1)
    if numerator == "0" or denominator == "0":
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    return TimeBase(numerator, denominator)


def _media_time(value: object) -> MediaTime:
    item = _mapping(value, frozenset({"value", "time_base"}))
    return MediaTime(_signed_decimal(item["value"]), _time_base(item["time_base"]))


def _decode_output(
    run: _WorkerRun,
    digest: str,
    source_bytes: int,
    limits: MediaLimits,
) -> _Decoded:
    if run.returncode != 0:
        code = PortErrorCode.LIMIT_EXCEEDED if run.returncode == 20 else PortErrorCode.DECODE_FAILED
        if run.returncode == 22:
            code = PortErrorCode.ISOLATION_UNAVAILABLE
        raise _error(code, "decode")
    sentinel = object()
    raw: object = sentinel
    with suppress(UnicodeDecodeError, json.JSONDecodeError):
        raw = json.loads(run.stdout)
    if raw is sentinel:
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    top = _mapping(
        raw,
        frozenset({"schema_version", "status", "runtime", "isolation", "stream", "frames"}),
    )
    if top["schema_version"] != 1 or top["status"] != "ok":
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    runtime = _mapping(top["runtime"], frozenset({"pyav", "libavformat", "libavcodec"}))
    pyav_version = _token(runtime["pyav"])
    libavformat_version = _token(runtime["libavformat"])
    libavcodec_version = _token(runtime["libavcodec"])
    if pyav_version != "18.1.0":
        raise _error(PortErrorCode.ISOLATION_UNAVAILABLE, "decode")
    isolation = _mapping(
        top["isolation"],
        frozenset({"non_root", "no_new_privileges", "network_denied", "landlock_denied"}),
    )
    if any(value is not True for value in isolation.values()):
        raise _error(PortErrorCode.ISOLATION_UNAVAILABLE, "decode")
    stream = _mapping(
        top["stream"],
        frozenset(
            {
                "stream_index",
                "width",
                "height",
                "rotation_degrees",
                "time_base",
                "duration",
                "codec",
                "format_name",
            }
        ),
    )
    stream_index = _integer(stream["stream_index"], 0, _MAX_STREAM_INDEX)
    time_base = _time_base(stream["time_base"])
    duration = None if stream["duration"] is None else _media_time(stream["duration"])
    if duration is not None and duration.time_base != time_base:
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    source = Source.create(
        Fingerprint(digest, str(source_bytes)),
        (
            SourceStream(
                stream_index,
                _integer(stream["width"], 1, limits.max_width),
                _integer(stream["height"], 1, limits.max_height),
                _integer(stream["rotation_degrees"], -(2**31 - 1), 2**31 - 1),
                time_base,
            ),
        ),
    )
    values = top["frames"]
    if not isinstance(values, list) or len(values) > limits.max_frames:
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    frames: list[FrameRef] = []
    hashes: list[str] = []
    for value in values:
        item = _mapping(
            value,
            frozenset({"decode_index", "pts", "duration", "key_frame", "pixel_sha256"}),
        )
        pixel_hash = item["pixel_sha256"]
        if not isinstance(pixel_hash, str) or not _DIGEST_RE.fullmatch(pixel_hash):
            raise _error(PortErrorCode.DECODE_FAILED, "decode")
        key_frame = item["key_frame"]
        if type(key_frame) is not bool:
            raise _error(PortErrorCode.DECODE_FAILED, "decode")
        frame_duration = None if item["duration"] is None else _media_time(item["duration"])
        pts = _media_time(item["pts"])
        if pts.time_base != time_base or (
            frame_duration is not None and frame_duration.time_base != time_base
        ):
            raise _error(PortErrorCode.DECODE_FAILED, "decode")
        frames.append(
            FrameRef.create(
                source.source_id,
                stream_index,
                _decimal(item["decode_index"]),
                pts,
                frame_duration,
                key_frame,
            )
        )
        hashes.append(pixel_hash)
    if not frames:
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    positions = {(frame.stream_index, frame.decode_index) for frame in frames}
    if len(positions) != len(frames):
        raise _error(PortErrorCode.DECODE_FAILED, "decode")
    details = ProbeDetails(
        _token(stream["format_name"]),
        _token(stream["codec"]),
        duration,
        tuple(hashes),
        pyav_version,
        libavformat_version,
        libavcodec_version,
    )
    metrics = MediaMetrics(
        run.wall_ms, run.cpu_ms, run.memory_peak_bytes, source_bytes, len(frames)
    )
    return _Decoded(source, tuple(frames), details, metrics)


class LocalVideoSource:
    """One bounded, sealed local source decoded through the accepted Linux boundary."""

    def __init__(
        self,
        source_root: Path,
        relative_path: str,
        runtime: MediaRuntime,
        *,
        limits: MediaLimits | None = None,
        cancelled: threading.Event | None = None,
    ) -> None:
        selected_limits = MediaLimits() if limits is None else limits
        if not isinstance(selected_limits, MediaLimits) or not isinstance(runtime, MediaRuntime):
            raise ValueError("runtime and limits must use the v1 media records")
        self._source_root = _absolute(source_root)
        self._relative_path = _relative_source(relative_path)
        self._runtime = runtime
        self._limits = selected_limits
        self._cancelled = cancelled
        self._decoded: _Decoded | None = None
        self._calls: list[PortCall] = []
        self._descriptor = CapabilityDescriptor(
            PortKind.VIDEO_SOURCE,
            "visualworld.local-media",
            "1",
            deterministic=True,
            offline=True,
            max_batch_items=MAX_PORT_BATCH_ITEMS,
            max_payload_bytes=selected_limits.max_worker_output_bytes,
        )

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    @property
    def calls(self) -> tuple[PortCall, ...]:
        return tuple(self._calls)

    @property
    def details(self) -> ProbeDetails:
        return self._ensure_decoded().details

    @property
    def metrics(self) -> MediaMetrics:
        return self._ensure_decoded().metrics

    def _ensure_decoded(self) -> _Decoded:
        if self._decoded is not None:
            return self._decoded
        _capability_check(self._runtime)
        source_fd, _ = _open_source(
            self._source_root,
            self._relative_path,
            self._limits.max_source_bytes,
        )
        try:
            snapshot_fd, digest, source_bytes = _sealed_snapshot(
                source_fd,
                self._limits.max_source_bytes,
            )
        finally:
            os.close(source_fd)
        run: _WorkerRun | None = None
        try:
            run = _run_worker(self._runtime, self._limits, snapshot_fd, self._cancelled)
        except PortError:
            raise
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            os.close(snapshot_fd)
        if run is None:
            raise _error(PortErrorCode.ISOLATION_UNAVAILABLE, "decode")
        self._decoded = _decode_output(run, digest, source_bytes, self._limits)
        return self._decoded

    def probe(self) -> Source:
        result = self._ensure_decoded().source
        self._calls.append(PortCall(PortKind.VIDEO_SOURCE, "probe", 1))
        return result

    def read_frames(
        self,
        *,
        stream_index: int,
        after_decode_index: str | None = None,
        limit: int = MAX_PORT_BATCH_ITEMS,
    ) -> tuple[FrameRef, ...]:
        if type(limit) is not int or not 1 <= limit <= MAX_PORT_BATCH_ITEMS:
            raise _error(PortErrorCode.LIMIT_EXCEEDED, "read_frames")
        if type(stream_index) is not int or not 0 <= stream_index <= _MAX_STREAM_INDEX:
            raise _error(PortErrorCode.INVALID_REQUEST, "read_frames")
        after = -1
        if after_decode_index is not None:
            after = _cursor(after_decode_index)
        decoded = self._ensure_decoded()
        if stream_index not in {stream.stream_index for stream in decoded.source.streams}:
            raise _error(PortErrorCode.NOT_FOUND, "read_frames")
        result = tuple(
            frame
            for frame in decoded.frames
            if frame.stream_index == stream_index and int(frame.decode_index) > after
        )[:limit]
        self._calls.append(PortCall(PortKind.VIDEO_SOURCE, "read_frames", len(result)))
        return result


__all__ = [
    "LocalVideoSource",
    "MediaLimits",
    "MediaMetrics",
    "MediaRuntime",
    "ProbeDetails",
]
