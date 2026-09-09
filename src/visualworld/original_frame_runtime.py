# SPDX-License-Identifier: Apache-2.0
"""Linux-only hostile-media adapter for bounded transient original RGB24 frames."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn, cast

from visualworld import media as _media
from visualworld.frame_access import (
    MAX_ORIGINAL_FRAME_BYTES,
    MAX_ORIGINAL_FRAME_TOTAL_BYTES,
    OriginalFrame,
    OriginalFrameReadResult,
)
from visualworld.ingestion import Artifact, FrameRef, Producer, Source, TimeBase
from visualworld.ports import (
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    PerceptionResultState,
    PortCall,
    PortError,
    PortErrorCode,
    PortKind,
)

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}\Z")
_MAX_U32 = 2**32 - 1
_MAX_U64 = 2**64 - 1
_MFD_CLOEXEC = 0x0001
_MFD_ALLOW_SEALING = 0x0002
_F_ADD_SEALS = 1033
_F_GET_SEALS = 1034
_F_SEAL_SEAL = 0x0001
_F_SEAL_SHRINK = 0x0002
_F_SEAL_GROW = 0x0004
_F_SEAL_WRITE = 0x0008
_INITIAL_OUTPUT_SEALS = _F_SEAL_SHRINK | _F_SEAL_GROW
_FINAL_OUTPUT_SEALS = _INITIAL_OUTPUT_SEALS | _F_SEAL_WRITE | _F_SEAL_SEAL
_OVERLAY_MANIFEST_NAME = "original-frame-runtime-v1.json"
_OVERLAY_RECEIPT_NAME = "visualworld-original-frame-overlay.json"
_OVERLAY_WORKER = Path("worker/original_frame_worker.py")
_MAX_REQUEST_BYTES = 256 * 1024
_APPROVED_OVERLAY_MANIFEST_SHA256 = (
    "45c97bfdc7cf58308e9c629acb6b6d6e163141e6aac7a93fdee79f6fdad74bc6"
)
_APPROVED_OVERLAY_WORKER_SHA256 = "197b8ccc04c0fbcb545abce7b4b7c059ef5268e2c429451945ccd3b54c27a007"
_EXPECTED_SUCCESS_STDERR = b'{"schema_version":1,"status":"ok"}\n'


def _fail(code: PortErrorCode, operation: str = "read") -> NoReturn:
    raise PortError(code, PortKind.ORIGINAL_FRAME_READER, operation)


@dataclass(frozen=True, slots=True)
class OriginalFrameLimits:
    """Pre-allocation and worker bounds for one original-frame page."""

    max_source_bytes: int = 64 * 1024 * 1024
    max_duration_seconds: int = 60 * 60
    max_width: int = 4096
    max_height: int = 4096
    max_pixels: int = 8_388_608
    max_frames: int = MAX_PORT_BATCH_ITEMS
    max_frame_bytes: int = 32 * 1024 * 1024
    max_total_bytes: int = 128 * 1024 * 1024
    max_decoded_frames: int = 300
    max_decoded_bytes: int = 384 * 1024 * 1024
    max_stdout_bytes: int = 1024 * 1024
    max_stderr_bytes: int = 4096
    wall_timeout_ms: int = 20_000
    cpu_budget_ms: int = 10_000
    memory_bytes: int = 768 * 1024 * 1024
    task_count: int = 64

    def __post_init__(self) -> None:
        values = (
            self.max_source_bytes,
            self.max_duration_seconds,
            self.max_width,
            self.max_height,
            self.max_pixels,
            self.max_frames,
            self.max_frame_bytes,
            self.max_total_bytes,
            self.max_decoded_frames,
            self.max_decoded_bytes,
            self.max_stdout_bytes,
            self.max_stderr_bytes,
            self.wall_timeout_ms,
            self.cpu_budget_ms,
            self.memory_bytes,
            self.task_count,
        )
        if not all(type(value) is int and value > 0 for value in values):
            raise ValueError("original-frame limits must be positive integers")
        frozen_worker_limits = (
            self.max_source_bytes,
            self.max_duration_seconds,
            self.max_width,
            self.max_height,
            self.max_pixels,
            self.max_frames,
            self.max_frame_bytes,
            self.max_total_bytes,
            self.max_decoded_frames,
            self.max_decoded_bytes,
        )
        if frozen_worker_limits != (
            64 * 1024 * 1024,
            60 * 60,
            4096,
            4096,
            8_388_608,
            MAX_PORT_BATCH_ITEMS,
            32 * 1024 * 1024,
            128 * 1024 * 1024,
            300,
            384 * 1024 * 1024,
        ):
            raise ValueError("original-frame worker limits must match the frozen manifest")
        if (
            self.max_source_bytes > 2**30
            or self.max_frames > MAX_PORT_BATCH_ITEMS
            or self.max_frame_bytes > MAX_ORIGINAL_FRAME_BYTES
            or self.max_total_bytes > MAX_ORIGINAL_FRAME_TOTAL_BYTES
            or self.max_stdout_bytes > 16 * 1024 * 1024
            or self.max_stderr_bytes > 1024 * 1024
            or self.task_count > 1024
        ):
            raise ValueError("original-frame limit exceeds the v1 contract")


@dataclass(frozen=True, slots=True)
class OriginalFrameRuntime:
    """Accepted media closure plus a separately frozen first-party worker overlay."""

    media: _media.MediaRuntime
    overlay_root: Path
    worker: Path

    def __post_init__(self) -> None:
        if type(self.media) is not _media.MediaRuntime:
            raise ValueError("media runtime must use MediaRuntime")
        if not isinstance(self.overlay_root, Path) or not isinstance(self.worker, Path):
            raise ValueError("overlay paths must use pathlib.Path")
        object.__setattr__(self, "overlay_root", Path(os.path.abspath(self.overlay_root)))
        object.__setattr__(self, "worker", Path(os.path.abspath(self.worker)))


@dataclass(frozen=True, slots=True)
class _WorkerRun:
    stdout: bytes
    stderr: bytes
    returncode: int


@dataclass(frozen=True, slots=True)
class _OutputFrame:
    frame: FrameRef
    offset: int
    size: int
    sha256: str


def _copy_runtime(value: OriginalFrameRuntime) -> OriginalFrameRuntime:
    if type(value) is not OriginalFrameRuntime:
        raise ValueError("invalid original-frame runtime")
    media = _media.MediaRuntime(Path(value.media.root), Path(value.media.worker))
    return OriginalFrameRuntime(media, Path(value.overlay_root), Path(value.worker))


def _copy_limits(
    value: OriginalFrameLimits, *, wall_timeout_ms: int | None = None
) -> OriginalFrameLimits:
    if type(value) is not OriginalFrameLimits:
        raise ValueError("invalid original-frame limits")
    return OriginalFrameLimits(
        value.max_source_bytes,
        value.max_duration_seconds,
        value.max_width,
        value.max_height,
        value.max_pixels,
        value.max_frames,
        value.max_frame_bytes,
        value.max_total_bytes,
        value.max_decoded_frames,
        value.max_decoded_bytes,
        value.max_stdout_bytes,
        value.max_stderr_bytes,
        value.wall_timeout_ms if wall_timeout_ms is None else wall_timeout_ms,
        value.cpu_budget_ms,
        value.memory_bytes,
        value.task_count,
    )


def _checkpoint(cancelled: threading.Event | None, deadline_ns: int) -> int:
    """Fail closed when cancellation or the complete-call deadline is reached."""

    if cancelled is not None and cancelled.is_set():
        _fail(PortErrorCode.CANCELLED)
    now = time.monotonic_ns()
    if now >= deadline_ns:
        _fail(PortErrorCode.TIMEOUT)
    return now


def _remaining_worker_limits(
    limits: OriginalFrameLimits,
    cancelled: threading.Event | None,
    deadline_ns: int,
) -> OriginalFrameLimits:
    """Copy limits with only the whole-call wall budget that remains."""

    now = _checkpoint(cancelled, deadline_ns)
    remaining_ms = (deadline_ns - now) // 1_000_000
    if remaining_ms < 1:
        _fail(PortErrorCode.TIMEOUT)
    return _copy_limits(
        limits,
        wall_timeout_ms=min(limits.wall_timeout_ms, remaining_ms),
    )


def _owned_source(value: object) -> Source:
    if type(value) is not Source:
        _fail(PortErrorCode.INVALID_REQUEST)
    try:
        return Source.from_mapping(Source.to_mapping(value))
    except (TypeError, ValueError):
        _fail(PortErrorCode.INVALID_REQUEST)
    raise AssertionError("unreachable")


def _owned_frames(
    value: object, source: Source, limits: OriginalFrameLimits
) -> tuple[FrameRef, ...]:
    if type(value) is not tuple or len(value) > min(limits.max_frames, MAX_PORT_BATCH_ITEMS):
        _fail(PortErrorCode.LIMIT_EXCEEDED)
    if not all(type(frame) is FrameRef for frame in value):
        _fail(PortErrorCode.INVALID_REQUEST)
    try:
        frames = tuple(FrameRef.from_mapping(FrameRef.to_mapping(frame)) for frame in value)
    except (AttributeError, TypeError, ValueError):
        _fail(PortErrorCode.INVALID_REQUEST)
    ids = {frame.frame_id for frame in frames}
    positions = {(frame.stream_index, frame.decode_index) for frame in frames}
    streams = {stream.stream_index: stream for stream in source.streams}
    if len(ids) != len(frames) or len(positions) != len(frames):
        _fail(PortErrorCode.CONFLICT)
    if any(
        frame.source_id != source.source_id
        or frame.stream_index not in streams
        or frame.pts.time_base != streams[frame.stream_index].time_base
        or (
            frame.duration is not None
            and frame.duration.time_base != streams[frame.stream_index].time_base
        )
        for frame in frames
    ):
        _fail(PortErrorCode.CONFLICT)
    return frames


def _supported_platform() -> bool:
    libc, version = platform.libc_ver()
    try:
        parts = tuple(int(part) for part in version.split("."))
    except ValueError:
        return False
    return (
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and libc == "glibc"
        and parts >= (2, 28)
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
        and stat.S_IMODE(metadata.st_mode) & 0o022 == 0
        and stat.S_IMODE(metadata.st_mode) & stat.S_IXOTH != 0
    )


def _trusted_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) & 0o022 == 0
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_overlay(runtime: OriginalFrameRuntime) -> None:
    if not _APPROVED_OVERLAY_MANIFEST_SHA256 or not _APPROVED_OVERLAY_WORKER_SHA256:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    manifest_path = runtime.overlay_root / _OVERLAY_MANIFEST_NAME
    receipt_path = runtime.overlay_root / _OVERLAY_RECEIPT_NAME
    if runtime.worker != runtime.overlay_root / _OVERLAY_WORKER:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    try:
        relative = runtime.worker.relative_to(runtime.overlay_root)
    except ValueError:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    directories = {
        runtime.overlay_root,
        *runtime.overlay_root.parents,
        *(
            runtime.overlay_root / Path(*relative.parts[:index])
            for index in range(1, len(relative.parts))
        ),
    }
    if not all(_trusted_directory(path) for path in directories):
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    try:
        root_entries = {path.name for path in runtime.overlay_root.iterdir()}
        worker_entries = {path.name for path in runtime.worker.parent.iterdir()}
    except OSError:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    if root_entries != {
        _OVERLAY_MANIFEST_NAME,
        _OVERLAY_RECEIPT_NAME,
        _OVERLAY_WORKER.parts[0],
    } or worker_entries != {_OVERLAY_WORKER.name}:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    if (
        not _trusted_file(manifest_path)
        or not _trusted_file(receipt_path)
        or not _trusted_file(runtime.worker)
    ):
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw, object_pairs_hook=_no_duplicate_object)
        receipt_raw = receipt_path.read_bytes()
        receipt = json.loads(receipt_raw, object_pairs_hook=_no_duplicate_object)
    except (OSError, PortError, UnicodeError, ValueError, RecursionError):
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    application_worker = manifest.get("application_worker") if type(manifest) is dict else None
    media_runtime = manifest.get("media_runtime") if type(manifest) is dict else None
    try:
        worker_sha256 = _file_sha256(runtime.worker)
    except OSError:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    expected_receipt = {
        "complete": True,
        "manifest_sha256": _APPROVED_OVERLAY_MANIFEST_SHA256,
        "media_runtime_manifest_sha256": _media._APPROVED_RUNTIME_MANIFEST_SHA256,
        "media_runtime_tree_sha256": _media._APPROVED_RUNTIME_TREE_SHA256,
        "runtime_id": "visualworld-original-frame-overlay-v1",
        "schema": "visualworld.original-frame-overlay-receipt",
        "schema_version": 1,
        "worker_sha256": _APPROVED_OVERLAY_WORKER_SHA256,
    }
    expected_receipt_raw = (
        json.dumps(expected_receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    if (
        hashlib.sha256(raw).hexdigest() != _APPROVED_OVERLAY_MANIFEST_SHA256
        or type(manifest) is not dict
        or manifest.get("schema") != "visualworld.original-frame-runtime"
        or manifest.get("schema_version") != 1
        or type(application_worker) is not dict
        or application_worker.get("install_path") != _OVERLAY_WORKER.as_posix()
        or application_worker.get("sha256") != _APPROVED_OVERLAY_WORKER_SHA256
        or worker_sha256 != _APPROVED_OVERLAY_WORKER_SHA256
        or type(media_runtime) is not dict
        or media_runtime.get("runtime_id") != "visualworld-pyav-18.1.0-ffmpeg-9.0.1-v2"
        or media_runtime.get("manifest_sha256") != _media._APPROVED_RUNTIME_MANIFEST_SHA256
        or media_runtime.get("tree_sha256") != _media._APPROVED_RUNTIME_TREE_SHA256
        or receipt != expected_receipt
        or receipt_raw != expected_receipt_raw
    ):
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _memfd(name: bytes) -> int:
    creator = cast(Callable[[str, int], int] | None, getattr(os, "memfd_create", None))
    if creator is None:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    try:
        return creator(name.decode("ascii"), _MFD_CLOEXEC | _MFD_ALLOW_SEALING)
    except (OSError, UnicodeError):
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    raise AssertionError("unreachable")


def _output_memfd(size: int) -> int:
    descriptor = _memfd(b"visualworld-original-frame-output")
    try:
        os.ftruncate(descriptor, size)
        fcntl.fcntl(descriptor, _F_ADD_SEALS, _INITIAL_OUTPUT_SEALS)
        return descriptor
    except (OSError, PortError):
        os.close(descriptor)
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    raise AssertionError("unreachable")


def _request_bytes(
    source: Source,
    frames: tuple[FrameRef, ...],
    total_bytes: int,
) -> bytes:
    request = {
        "frames": [frame.to_mapping() for frame in frames],
        "output_bytes": str(total_bytes),
        "schema": "visualworld.original_frame_request",
        "schema_version": 1,
        "source": source.to_mapping(),
    }
    return json.dumps(
        request,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _namespace_argv(runtime: OriginalFrameRuntime, limits: OriginalFrameLimits) -> list[str]:
    relative = runtime.worker.relative_to(runtime.overlay_root)
    worker = PurePosixPath("/overlay", *relative.parts)
    return [
        os.fspath(_media._BWRAP),
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
        os.fspath(runtime.media.root),
        "/runtime",
        "--ro-bind",
        os.fspath(runtime.overlay_root),
        "/overlay",
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
        "/runtime/python/bin/python3.13",
        worker.as_posix(),
    ]


def _systemd_argv(
    runtime: OriginalFrameRuntime,
    limits: OriginalFrameLimits,
    source_fd: int,
    output_fd: int,
    unit: str,
) -> list[str]:
    properties = (
        "User=nobody",
        "Group=nogroup",
        "NoNewPrivileges=yes",
        "KillMode=control-group",
        f"MemoryMax={limits.memory_bytes}",
        "MemorySwapMax=0",
        f"TasksMax={limits.task_count}",
        "CPUQuota=200%",
        f"RuntimeMaxSec={max(1, (limits.wall_timeout_ms + 999) // 1000)}s",
        "LimitNOFILE=64",
        f"LimitNPROC={limits.task_count}",
        f"LimitFSIZE={limits.max_total_bytes}",
        "UMask=0077",
        f"OpenFile=/proc/{os.getpid()}/fd/{source_fd}:visualworld-source:read-only",
        f"OpenFile=/proc/{os.getpid()}/fd/{output_fd}:visualworld-output:read-write",
    )
    return [
        os.fspath(_media._SYSTEMD_RUN),
        "--quiet",
        "--pipe",
        "--wait",
        "--service-type=exec",
        f"--unit={unit}",
        *(f"--property={item}" for item in properties),
        *_namespace_argv(runtime, limits),
    ]


def _run_worker(
    runtime: OriginalFrameRuntime,
    limits: OriginalFrameLimits,
    source_fd: int,
    output_fd: int,
    request: bytes,
    cancelled: threading.Event | None,
) -> _WorkerRun:
    unit = f"visualworld-original-frame-{os.getpid()}-{time.monotonic_ns()}"
    process: subprocess.Popen[bytes] | None = None
    with suppress(OSError, subprocess.SubprocessError):
        process = subprocess.Popen(
            _systemd_argv(runtime, limits, source_fd, output_fd, unit),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    if process is None:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    service = f"{unit}.service"
    cgroup = _media._CGROUP_ROOT / "system.slice" / service
    input_stream = process.stdin
    send_failed = threading.Event()
    sender: threading.Thread | None = None
    sender_started = False

    def _send_request() -> None:
        if input_stream is None:
            send_failed.set()
            return
        try:
            input_stream.write(request)
            input_stream.flush()
        except (BrokenPipeError, OSError):
            send_failed.set()
        finally:
            with suppress(OSError):
                input_stream.close()

    try:
        if input_stream is None:
            _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
        sender = threading.Thread(
            target=_send_request,
            name="original-frame-request",
            daemon=True,
        )
        try:
            sender.start()
        except (OSError, RuntimeError):
            _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
        sender_started = True
        media_limits = _media.MediaLimits(
            max_source_bytes=limits.max_source_bytes,
            max_duration_seconds=limits.max_duration_seconds,
            max_width=limits.max_width,
            max_height=limits.max_height,
            max_pixels=limits.max_pixels,
            max_frames=limits.max_decoded_frames,
            max_decoded_bytes=limits.max_decoded_bytes,
            max_worker_output_bytes=limits.max_stdout_bytes + limits.max_stderr_bytes,
            wall_timeout_ms=limits.wall_timeout_ms,
            cpu_budget_ms=limits.cpu_budget_ms,
            memory_bytes=limits.memory_bytes,
            task_count=limits.task_count,
        )
        try:
            result = _media._drain_worker(process, unit, media_limits, cancelled)
        except PortError as error:
            _fail(error.code)
        sender.join(timeout=1)
        if sender.is_alive() or send_failed.is_set():
            _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
        return _WorkerRun(result.stdout, result.stderr, result.returncode)
    finally:
        if input_stream is not None:
            with suppress(OSError):
                input_stream.close()
        if sender is not None and sender_started:
            sender.join(timeout=1)
        cleanup_ok = _media._ensure_unit_stopped(service, cgroup, process)
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [os.fspath(_media._SYSTEMCTL), "reset-failed", service],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        if not cleanup_ok:
            _fail(PortErrorCode.ISOLATION_UNAVAILABLE)


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(PortErrorCode.DECODE_FAILED)
        result[key] = value
    return result


def _mapping(value: object, fields: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        _fail(PortErrorCode.DECODE_FAILED)
    return cast(dict[str, object], value)


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(PortErrorCode.DECODE_FAILED)
    return value


def _decimal(value: object, maximum: int = _MAX_U64) -> str:
    if (
        type(value) is not str
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
        or len(value) > 20
        or int(value) > maximum
    ):
        _fail(PortErrorCode.DECODE_FAILED)
    return value


def _time_base(value: object) -> TimeBase:
    item = _mapping(value, frozenset({"denominator", "numerator"}))
    try:
        return TimeBase(
            _decimal(item["numerator"], _MAX_U32), _decimal(item["denominator"], _MAX_U32)
        )
    except ValueError:
        _fail(PortErrorCode.DECODE_FAILED)
    raise AssertionError("unreachable")


def _decode_output(
    run: _WorkerRun,
    source: Source,
    frames: tuple[FrameRef, ...],
    total_bytes: int,
    limits: OriginalFrameLimits,
) -> tuple[tuple[_OutputFrame, ...], str]:
    if len(run.stdout) > limits.max_stdout_bytes or len(run.stderr) > limits.max_stderr_bytes:
        _fail(PortErrorCode.LIMIT_EXCEEDED)
    if run.returncode != 0:
        if run.returncode == 20:
            _fail(PortErrorCode.LIMIT_EXCEEDED)
        if run.returncode == 22:
            _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
        _fail(PortErrorCode.DECODE_FAILED)
    if run.stderr != _EXPECTED_SUCCESS_STDERR:
        _fail(PortErrorCode.DECODE_FAILED)
    if not run.stdout:
        _fail(PortErrorCode.DECODE_FAILED)
    try:
        raw = json.loads(run.stdout, object_pairs_hook=_no_duplicate_object)
        canonical = (
            json.dumps(raw, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    except PortError:
        raise
    except (RecursionError, TypeError, UnicodeError, ValueError):
        _fail(PortErrorCode.DECODE_FAILED)
    if canonical != run.stdout:
        _fail(PortErrorCode.DECODE_FAILED)
    top = _mapping(
        raw,
        frozenset(
            {
                "frames",
                "isolation",
                "output",
                "runtime",
                "schema",
                "schema_version",
                "source",
                "status",
                "stream",
            }
        ),
    )
    if (
        top["schema"] != "visualworld.original_frame_result"
        or type(top["schema_version"]) is not int
        or top["schema_version"] != 1
        or top["status"] != "ok"
    ):
        _fail(PortErrorCode.DECODE_FAILED)
    output_source = _mapping(top["source"], frozenset({"bytes", "sha256", "source_id"}))
    if (
        output_source["sha256"] != source.fingerprint.digest
        or _decimal(output_source["bytes"], limits.max_source_bytes) != source.fingerprint.bytes
        or output_source["source_id"] != source.source_id
    ):
        _fail(PortErrorCode.CONFLICT)
    runtime = _mapping(
        top["runtime"],
        frozenset(
            {
                "libavcodec",
                "libavformat",
                "media_runtime_id",
                "media_runtime_manifest_sha256",
                "media_runtime_tree_sha256",
                "pyav",
                "worker_sha256",
            }
        ),
    )
    if runtime != {
        "libavcodec": "63.1.101",
        "libavformat": "63.1.101",
        "media_runtime_id": "visualworld-pyav-18.1.0-ffmpeg-9.0.1-v2",
        "media_runtime_manifest_sha256": _media._APPROVED_RUNTIME_MANIFEST_SHA256,
        "media_runtime_tree_sha256": _media._APPROVED_RUNTIME_TREE_SHA256,
        "pyav": "18.1.0",
        "worker_sha256": _APPROVED_OVERLAY_WORKER_SHA256,
    }:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    isolation = _mapping(
        top["isolation"],
        frozenset({"landlock_denied", "network_denied", "no_new_privileges", "non_root"}),
    )
    if any(value is not True for value in isolation.values()):
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    selected_streams = {frame.stream_index for frame in frames}
    if len(selected_streams) != 1:
        _fail(PortErrorCode.CONFLICT)
    selected_stream = next(
        stream for stream in source.streams if stream.stream_index in selected_streams
    )
    stream = _mapping(
        top["stream"],
        frozenset(
            {
                "codec",
                "format_name",
                "height",
                "rotation_degrees",
                "stream_index",
                "time_base",
                "width",
            }
        ),
    )
    if (
        _integer(stream["stream_index"], 0, 2**31 - 1) != selected_stream.stream_index
        or _integer(stream["width"], 1, limits.max_width) != selected_stream.width
        or _integer(stream["height"], 1, limits.max_height) != selected_stream.height
        or _integer(stream["rotation_degrees"], -(2**31 - 1), 2**31 - 1)
        != selected_stream.rotation_degrees
        or _time_base(stream["time_base"]) != selected_stream.time_base
        or type(stream["codec"]) is not str
        or not _TOKEN_RE.fullmatch(stream["codec"])
        or type(stream["format_name"]) is not str
        or not _TOKEN_RE.fullmatch(stream["format_name"])
    ):
        _fail(PortErrorCode.CONFLICT)
    output_record = _mapping(top["output"], frozenset({"bytes", "layout", "sealed", "sha256"}))
    output_digest = output_record["sha256"]
    if (
        _decimal(output_record["bytes"], limits.max_total_bytes) != str(total_bytes)
        or output_record["layout"] != "request_order_contiguous_packed_rgb24_encoded_source"
        or output_record["sealed"] is not True
        or type(output_digest) is not str
        or not _DIGEST_RE.fullmatch(output_digest)
    ):
        _fail(PortErrorCode.CONFLICT)
    values = top["frames"]
    if type(values) is not list or len(values) != len(frames):
        _fail(PortErrorCode.DECODE_FAILED)
    output: list[_OutputFrame] = []
    expected_offset = 0
    for raw_frame, expected in zip(values, frames, strict=True):
        item = _mapping(raw_frame, frozenset({"artifact", "byte_offset", "frame_ref"}))
        try:
            returned_frame = FrameRef.from_mapping(item["frame_ref"])
            artifact = Artifact.from_mapping(item["artifact"])
        except (TypeError, ValueError):
            _fail(PortErrorCode.DECODE_FAILED)
        offset = int(_decimal(item["byte_offset"], total_bytes))
        size = int(artifact.bytes)
        if (
            returned_frame != expected
            or offset != expected_offset
            or size != selected_stream.width * selected_stream.height * 3
            or size > limits.max_frame_bytes
        ):
            _fail(PortErrorCode.CONFLICT)
        output.append(_OutputFrame(expected, offset, size, artifact.sha256))
        expected_offset += size
    if expected_offset != total_bytes:
        _fail(PortErrorCode.CONFLICT)
    return tuple(output), output_digest


class IsolatedOriginalFrameReader:
    """Read exact authorized local frames through the accepted Linux isolation boundary."""

    def __init__(
        self,
        source_root: Path,
        relative_path: str,
        runtime: OriginalFrameRuntime,
        *,
        limits: OriginalFrameLimits | None = None,
        cancelled: threading.Event | None = None,
    ) -> None:
        if not isinstance(source_root, Path) or type(relative_path) is not str:
            raise ValueError("source location must use bounded path values")
        selected_limits = OriginalFrameLimits() if limits is None else limits
        try:
            self._runtime = _copy_runtime(runtime)
            self._limits = _copy_limits(selected_limits)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("invalid original-frame runtime or limits") from None
        if cancelled is not None and type(cancelled) is not threading.Event:
            raise ValueError("cancelled must be a threading.Event")
        self._source_root = Path(os.path.abspath(source_root))
        self._relative_path = relative_path
        self._cancelled = cancelled
        self._calls: list[PortCall] = []
        configuration = {
            "ipc": "sealed-memfd-v1",
            "limits": {name: getattr(self._limits, name) for name in self._limits.__slots__},
            "media_manifest_sha256": _media._APPROVED_RUNTIME_MANIFEST_SHA256,
            "media_tree_sha256": _media._APPROVED_RUNTIME_TREE_SHA256,
            "overlay_manifest_sha256": _APPROVED_OVERLAY_MANIFEST_SHA256,
            "schema": "visualworld.original-frame-reader-configuration",
            "schema_version": 1,
        }
        config_sha = hashlib.sha256(
            json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self._producer = Producer("visualworld.isolated-original-frame-reader", "1", config_sha)
        self._descriptor = CapabilityDescriptor(
            PortKind.ORIGINAL_FRAME_READER,
            self._producer.name,
            self._producer.version,
            deterministic=True,
            offline=True,
            max_batch_items=self._limits.max_frames,
            max_payload_bytes=self._limits.max_total_bytes,
        )

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    @property
    def producer(self) -> Producer:
        return self._producer

    @property
    def supported(self) -> bool:
        return _supported_platform()

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
        if self._cancelled is not None and self._cancelled.is_set():
            _fail(PortErrorCode.CANCELLED)
        deadline_ns = time.monotonic_ns() + self._limits.wall_timeout_ms * 1_000_000
        _checkpoint(self._cancelled, deadline_ns)
        owned_source = _owned_source(source)
        _checkpoint(self._cancelled, deadline_ns)
        owned_frames = _owned_frames(frames, owned_source, self._limits)
        _checkpoint(self._cancelled, deadline_ns)
        if (
            type(max_total_bytes) is not int
            or not 1 <= max_total_bytes <= MAX_ORIGINAL_FRAME_TOTAL_BYTES
        ):
            _fail(PortErrorCode.LIMIT_EXCEEDED)
        if not owned_frames:
            _checkpoint(self._cancelled, deadline_ns)
            result = OriginalFrameReadResult.complete(())
            _checkpoint(self._cancelled, deadline_ns)
            self._calls.append(PortCall(PortKind.ORIGINAL_FRAME_READER, "read", 0))
            return result
        stream_indexes = {frame.stream_index for frame in owned_frames}
        if len(stream_indexes) != 1:
            _checkpoint(self._cancelled, deadline_ns)
            result = OriginalFrameReadResult(
                PerceptionResultState.UNSUPPORTED, reason="multi_stream_batch_unsupported"
            )
            _checkpoint(self._cancelled, deadline_ns)
            self._calls.append(PortCall(PortKind.ORIGINAL_FRAME_READER, "read", len(owned_frames)))
            return result
        _checkpoint(self._cancelled, deadline_ns)
        if not self.supported:
            result = OriginalFrameReadResult(
                PerceptionResultState.UNSUPPORTED, reason="platform_unsupported"
            )
            _checkpoint(self._cancelled, deadline_ns)
            self._calls.append(PortCall(PortKind.ORIGINAL_FRAME_READER, "read", len(owned_frames)))
            return result
        stream_index = next(iter(stream_indexes))
        stream = next(item for item in owned_source.streams if item.stream_index == stream_index)
        frame_bytes = stream.width * stream.height * 3
        total_bytes = frame_bytes * len(owned_frames)
        if (
            stream.width > self._limits.max_width
            or stream.height > self._limits.max_height
            or stream.width * stream.height > self._limits.max_pixels
            or frame_bytes > self._limits.max_frame_bytes
            or total_bytes > min(max_total_bytes, self._limits.max_total_bytes)
            or max(int(frame.decode_index) for frame in owned_frames)
            >= self._limits.max_decoded_frames
        ):
            _fail(PortErrorCode.LIMIT_EXCEEDED)
        _checkpoint(self._cancelled, deadline_ns)
        try:
            _media._capability_check(self._runtime.media)
        except PortError as error:
            _fail(error.code)
        _checkpoint(self._cancelled, deadline_ns)
        _verify_overlay(self._runtime)
        _checkpoint(self._cancelled, deadline_ns)
        source_fd = -1
        snapshot_fd = -1
        output_fd = -1
        try:
            try:
                _checkpoint(self._cancelled, deadline_ns)
                source_fd, before = _media._open_source(
                    self._source_root, self._relative_path, self._limits.max_source_bytes
                )
                _checkpoint(self._cancelled, deadline_ns)
                snapshot_fd, digest, source_bytes = _media._sealed_snapshot(
                    source_fd, self._limits.max_source_bytes
                )
                _checkpoint(self._cancelled, deadline_ns)
                after = os.fstat(source_fd)
                _checkpoint(self._cancelled, deadline_ns)
            except PortError as error:
                _fail(error.code)
            except OSError:
                _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
            if _metadata_identity(before) != _metadata_identity(after):
                _fail(PortErrorCode.CONFLICT)
            if (
                digest != owned_source.fingerprint.digest
                or str(source_bytes) != owned_source.fingerprint.bytes
            ):
                _fail(PortErrorCode.CONFLICT)
            worker_frames = tuple(sorted(owned_frames, key=lambda frame: int(frame.decode_index)))
            request = _request_bytes(owned_source, worker_frames, total_bytes)
            if len(request) > _MAX_REQUEST_BYTES:
                _fail(PortErrorCode.LIMIT_EXCEEDED)
            _checkpoint(self._cancelled, deadline_ns)
            output_fd = _output_memfd(total_bytes)
            _checkpoint(self._cancelled, deadline_ns)
            worker_limits = _remaining_worker_limits(
                self._limits,
                self._cancelled,
                deadline_ns,
            )
            run = _run_worker(
                self._runtime,
                worker_limits,
                snapshot_fd,
                output_fd,
                request,
                self._cancelled,
            )
            _checkpoint(self._cancelled, deadline_ns)
            output, output_digest = _decode_output(
                run, owned_source, worker_frames, total_bytes, self._limits
            )
            _checkpoint(self._cancelled, deadline_ns)
            try:
                output_stat = os.fstat(output_fd)
                seals = fcntl.fcntl(output_fd, _F_GET_SEALS)
            except OSError:
                _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
            if output_stat.st_size != total_bytes or seals != _FINAL_OUTPUT_SEALS:
                _fail(PortErrorCode.DECODE_FAILED)
            originals_by_id: dict[str, OriginalFrame] = {}
            aggregate = hashlib.sha256()
            for item in output:
                _checkpoint(self._cancelled, deadline_ns)
                try:
                    pixels = os.pread(output_fd, item.size, item.offset)
                except OSError:
                    _fail(PortErrorCode.DECODE_FAILED)
                _checkpoint(self._cancelled, deadline_ns)
                if len(pixels) != item.size or hashlib.sha256(pixels).hexdigest() != item.sha256:
                    _fail(PortErrorCode.DECODE_FAILED)
                aggregate.update(pixels)
                originals_by_id[item.frame.frame_id] = OriginalFrame(
                    owned_source, item.frame, pixels
                )
                _checkpoint(self._cancelled, deadline_ns)
            if aggregate.hexdigest() != output_digest or len(originals_by_id) != len(owned_frames):
                _fail(PortErrorCode.DECODE_FAILED)
            result = OriginalFrameReadResult.complete(
                tuple(originals_by_id[frame.frame_id] for frame in owned_frames)
            )
            _checkpoint(self._cancelled, deadline_ns)
        finally:
            for descriptor in (output_fd, snapshot_fd, source_fd):
                if descriptor >= 0:
                    with suppress(OSError):
                        os.close(descriptor)
        _checkpoint(self._cancelled, deadline_ns)
        self._calls.append(PortCall(PortKind.ORIGINAL_FRAME_READER, "read", len(owned_frames)))
        return result


__all__ = [
    "IsolatedOriginalFrameReader",
    "OriginalFrameLimits",
    "OriginalFrameRuntime",
]
