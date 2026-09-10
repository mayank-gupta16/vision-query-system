# SPDX-License-Identifier: Apache-2.0
"""Mac-safe parent contracts for the Linux original-frame worker boundary."""

from __future__ import annotations

import fcntl as fcntl_module
import hashlib
import json
import os
import platform as platform_module
import stat
import subprocess as subprocess_module
import threading
import time as time_module
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from visualworld import media
from visualworld import original_frame_runtime as runtime_module
from visualworld.frame_access import OriginalFrameReader
from visualworld.ingestion import (
    Artifact,
    Fingerprint,
    FrameRef,
    MediaTime,
    Source,
    SourceStream,
    TimeBase,
)
from visualworld.original_frame_runtime import (
    IsolatedOriginalFrameReader,
    OriginalFrameLimits,
    OriginalFrameRuntime,
)
from visualworld.ports import PerceptionResultState, PortError, PortErrorCode, PortKind


def _values(
    content: bytes = b"source-bytes",
) -> tuple[Source, tuple[FrameRef, ...], tuple[bytes, ...]]:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(hashlib.sha256(content).hexdigest(), str(len(content))),
        (SourceStream(0, 2, 2, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(
            source.source_id,
            0,
            str(index),
            MediaTime(str(index), time_base),
            MediaTime("1", time_base),
            index == 0,
        )
        for index in range(2)
    )
    pixels = (bytes(range(12)), bytes(reversed(range(12))))
    return source, frames, pixels


def _configured_runtime(tmp_path: Path) -> OriginalFrameRuntime:
    media_root = tmp_path / "media"
    overlay_root = tmp_path / "overlay"
    return OriginalFrameRuntime(
        media.MediaRuntime(media_root, media_root / "worker/media_worker.py"),
        overlay_root,
        overlay_root / "worker/original_frame_worker.py",
    )


def _worker_run(
    source: Source,
    frames: tuple[FrameRef, ...],
    pixels: tuple[bytes, ...],
    *,
    mutate: Any | None = None,
) -> runtime_module._WorkerRun:
    offset = 0
    frame_values: list[dict[str, object]] = []
    for frame, content in zip(frames, pixels, strict=True):
        artifact = Artifact(hashlib.sha256(content).hexdigest(), str(len(content)))
        frame_values.append(
            {
                "artifact": artifact.to_mapping(),
                "byte_offset": str(offset),
                "frame_ref": frame.to_mapping(),
            }
        )
        offset += len(content)
    payload: dict[str, object] = {
        "frames": frame_values,
        "isolation": {
            "landlock_denied": True,
            "network_denied": True,
            "no_new_privileges": True,
            "non_root": True,
        },
        "output": {
            "bytes": str(offset),
            "layout": "request_order_contiguous_packed_rgb24_encoded_source",
            "sealed": True,
            "sha256": hashlib.sha256(b"".join(pixels)).hexdigest(),
        },
        "runtime": {
            "libavcodec": "63.1.101",
            "libavformat": "63.1.101",
            "media_runtime_id": "visualworld-pyav-18.1.0-ffmpeg-9.0.1-v2",
            "media_runtime_manifest_sha256": media._APPROVED_RUNTIME_MANIFEST_SHA256,
            "media_runtime_tree_sha256": media._APPROVED_RUNTIME_TREE_SHA256,
            "pyav": "18.1.0",
            "worker_sha256": runtime_module._APPROVED_OVERLAY_WORKER_SHA256,
        },
        "schema": "visualworld.original_frame_result",
        "schema_version": 1,
        "source": {
            "bytes": source.fingerprint.bytes,
            "sha256": source.fingerprint.digest,
            "source_id": source.source_id,
        },
        "status": "ok",
        "stream": {
            "codec": "rawvideo",
            "format_name": "mov",
            "height": 2,
            "rotation_degrees": 0,
            "stream_index": 0,
            "time_base": source.streams[0].time_base.to_mapping(),
            "width": 2,
        },
    }
    if mutate is not None:
        mutate(payload)
    stdout = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return runtime_module._WorkerRun(
        stdout,
        b'{"schema_version":1,"status":"ok"}\n',
        0,
    )


def test_runtime_manifest_and_worker_hashes_are_pinned() -> None:
    root = Path(__file__).resolve().parents[1]
    worker = root / "workers/original_frame_worker.py"
    manifest = root / "workers/original-frame-runtime-v1.json"

    assert hashlib.sha256(worker.read_bytes()).hexdigest() == (
        runtime_module._APPROVED_OVERLAY_WORKER_SHA256
    )
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == (
        runtime_module._APPROVED_OVERLAY_MANIFEST_SHA256
    )


def test_overlay_verification_requires_exact_installed_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[1]
    configured = _configured_runtime(tmp_path)
    configured.worker.parent.mkdir(parents=True)
    configured.worker.write_bytes((root / "workers/original_frame_worker.py").read_bytes())
    (configured.overlay_root / "original-frame-runtime-v1.json").write_bytes(
        (root / "workers/original-frame-runtime-v1.json").read_bytes()
    )
    receipt = {
        "complete": True,
        "manifest_sha256": runtime_module._APPROVED_OVERLAY_MANIFEST_SHA256,
        "media_runtime_manifest_sha256": media._APPROVED_RUNTIME_MANIFEST_SHA256,
        "media_runtime_tree_sha256": media._APPROVED_RUNTIME_TREE_SHA256,
        "runtime_id": "visualworld-original-frame-overlay-v1",
        "schema": "visualworld.original-frame-overlay-receipt",
        "schema_version": 1,
        "worker_sha256": runtime_module._APPROVED_OVERLAY_WORKER_SHA256,
    }
    (configured.overlay_root / "visualworld-original-frame-overlay.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    directory_checks: list[tuple[Path, bool]] = []

    def trust_directory(path: Path, *, frozen: bool = False) -> bool:
        directory_checks.append((path, frozen))
        return True

    monkeypatch.setattr(
        runtime_module,
        "_trusted_directory",
        trust_directory,
    )
    monkeypatch.setattr(runtime_module, "_trusted_file", lambda _path: True)

    runtime_module._verify_overlay(configured)
    assert (configured.overlay_root, True) in directory_checks
    assert (configured.worker.parent, True) in directory_checks
    assert all((parent, False) in directory_checks for parent in configured.overlay_root.parents)
    (configured.worker.parent / "av.py").write_text("raise RuntimeError\n", encoding="utf-8")
    with pytest.raises(PortError) as raised:
        runtime_module._verify_overlay(configured)
    assert raised.value.code is PortErrorCode.ISOLATION_UNAVAILABLE


def test_parent_request_is_canonical_full_record_pixel_free_and_sorted() -> None:
    source, frames, pixels = _values()
    request = runtime_module._request_bytes(source, tuple(reversed(frames)), 24)
    parsed = json.loads(request)

    assert (
        request
        == json.dumps(
            parsed,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    assert parsed["source"] == source.to_mapping()
    assert parsed["frames"] == [frames[1].to_mapping(), frames[0].to_mapping()]
    assert parsed["output_bytes"] == "24"
    assert not request.endswith(b"\n")
    assert all(content not in request for content in pixels)


def test_worker_output_parser_accepts_only_exact_pixel_free_receipt() -> None:
    source, frames, pixels = _values()

    output, digest = runtime_module._decode_output(
        _worker_run(source, frames, pixels),
        source,
        frames,
        24,
        OriginalFrameLimits(),
    )

    assert tuple(item.frame for item in output) == frames
    assert tuple(item.offset for item in output) == (0, 12)
    assert tuple(item.size for item in output) == (12, 12)
    assert digest == hashlib.sha256(b"".join(pixels)).hexdigest()
    rendered = repr((output, digest))
    assert all(repr(content) not in rendered for content in pixels)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: cast(dict[str, object], value["source"]).update(sha256="0" * 64),
        lambda value: cast(dict[str, object], value["output"]).update(bytes="23"),
        lambda value: cast(dict[str, object], value["runtime"]).update(pyav="18.2.0"),
        lambda value: cast(list[object], value["frames"]).reverse(),
        lambda value: cast(dict[str, object], value["stream"]).update(width=3),
        lambda value: value.update(status="failed"),
        lambda value: cast(dict[str, object], value["isolation"]).update(non_root=False),
        lambda value: value.update(frames=[]),
        lambda value: cast(dict[str, object], cast(list[object], value["frames"])[0]).update(
            frame_ref={}
        ),
        lambda value: cast(dict[str, object], cast(list[object], value["frames"])[0]).update(
            byte_offset="01"
        ),
        lambda value: cast(
            dict[str, object], cast(dict[str, object], value["stream"])["time_base"]
        ).update(denominator="0"),
        lambda value: cast(dict[str, object], value["stream"]).update(width=True),
        lambda value: value.update(schema_version=True),
        lambda value: value.update(schema_version=1.0),
        lambda value: value.update(secret="forbidden"),
    ],
)
def test_worker_output_mismatch_or_unknown_fields_fail_closed(mutate: Any) -> None:
    source, frames, pixels = _values()

    with pytest.raises(PortError) as raised:
        runtime_module._decode_output(
            _worker_run(source, frames, pixels, mutate=mutate),
            source,
            frames,
            24,
            OriginalFrameLimits(),
        )

    assert raised.value.code in {
        PortErrorCode.CONFLICT,
        PortErrorCode.DECODE_FAILED,
        PortErrorCode.ISOLATION_UNAVAILABLE,
    }
    assert raised.value.port is PortKind.ORIGINAL_FRAME_READER
    assert all(repr(content) not in str(raised.value) for content in pixels)


def test_systemd_command_passes_only_two_fixed_descriptors_and_read_only_roots(
    tmp_path: Path,
) -> None:
    configured = _configured_runtime(tmp_path)
    command = runtime_module._systemd_argv(
        configured, OriginalFrameLimits(), 40, 41, "visualworld-original-frame-test"
    )
    text = "\n".join(command)

    assert f"OpenFile=/proc/{os.getpid()}/fd/40:visualworld-source:read-only" in text
    assert f"OpenFile=/proc/{os.getpid()}/fd/41:visualworld-output:read-write" in text
    assert "visualworld-request" not in text
    assert "--unshare-net" in command
    assert command.count("--ro-bind") == 4
    assert "/overlay/worker/original_frame_worker.py" in command


def test_unsupported_host_returns_no_pixels_without_invoking_native_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, frames, _ = _values()
    reader = IsolatedOriginalFrameReader(tmp_path, "source.mov", _configured_runtime(tmp_path))
    monkeypatch.setattr(runtime_module, "_supported_platform", lambda: False)
    monkeypatch.setattr(
        media,
        "_capability_check",
        lambda _runtime: pytest.fail("native capability check must not run"),
    )

    result = reader.read(source, frames[:1])

    assert isinstance(reader, OriginalFrameReader)
    assert result.state is PerceptionResultState.UNSUPPORTED
    assert result.frames == ()
    assert result.reason == "platform_unsupported"


def test_pre_cancelled_reader_stops_before_clock_and_native_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, frames, pixels = _values()
    cancelled = threading.Event()
    cancelled.set()
    reader = IsolatedOriginalFrameReader(
        tmp_path,
        "private-source.mov",
        _configured_runtime(tmp_path),
        cancelled=cancelled,
    )
    monkeypatch.setattr(
        time_module,
        "monotonic_ns",
        lambda: pytest.fail("pre-cancelled read must not start its deadline"),
    )
    monkeypatch.setattr(
        media,
        "_capability_check",
        lambda _runtime: pytest.fail("pre-cancelled read must not check capabilities"),
    )
    monkeypatch.setattr(
        runtime_module,
        "_verify_overlay",
        lambda _runtime: pytest.fail("pre-cancelled read must not verify the overlay"),
    )
    monkeypatch.setattr(
        media,
        "_open_source",
        lambda *_args: pytest.fail("pre-cancelled read must not open the source"),
    )
    monkeypatch.setattr(
        runtime_module,
        "_output_memfd",
        lambda _size: pytest.fail("pre-cancelled read must not allocate output"),
    )
    monkeypatch.setattr(
        runtime_module,
        "_run_worker",
        lambda *_args: pytest.fail("pre-cancelled read must not launch the worker"),
    )

    with pytest.raises(PortError) as raised:
        reader.read(source, frames[:1])

    assert raised.value.code is PortErrorCode.CANCELLED
    assert str(raised.value) == "cancelled at original_frame_reader.read"
    assert "private-source.mov" not in str(raised.value)
    assert all(repr(content) not in str(raised.value) for content in pixels)
    assert reader.calls == ()


def test_preflight_deadline_overrun_stops_before_overlay_and_source_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, frames, pixels = _values()
    clock = [0]
    reader = IsolatedOriginalFrameReader(
        tmp_path,
        "private-source.mov",
        _configured_runtime(tmp_path),
        limits=OriginalFrameLimits(wall_timeout_ms=10),
    )
    monkeypatch.setattr(time_module, "monotonic_ns", lambda: clock[0])
    monkeypatch.setattr(runtime_module, "_supported_platform", lambda: True)

    def consume_budget(_runtime: object) -> None:
        clock[0] = 10_000_000

    monkeypatch.setattr(media, "_capability_check", consume_budget)
    monkeypatch.setattr(
        runtime_module,
        "_verify_overlay",
        lambda _runtime: pytest.fail("expired preflight must not verify the overlay"),
    )
    monkeypatch.setattr(
        media,
        "_open_source",
        lambda *_args: pytest.fail("expired preflight must not open the source"),
    )

    with pytest.raises(PortError) as raised:
        reader.read(source, frames[:1])

    assert raised.value.code is PortErrorCode.TIMEOUT
    assert str(raised.value) == "timeout at original_frame_reader.read"
    assert "private-source.mov" not in str(raised.value)
    assert all(repr(content) not in str(raised.value) for content in pixels)
    assert reader.calls == ()


def test_reader_integration_reorders_transient_pixels_and_checks_final_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"source-bytes"
    source, frames, pixels = _values(content)
    source_path = tmp_path / "source.mov"
    source_path.write_bytes(content)
    output_path = tmp_path / "output.memfd"
    output_path.write_bytes(b"\0" * 24)
    reader = IsolatedOriginalFrameReader(
        tmp_path,
        "source.mov",
        _configured_runtime(tmp_path),
        limits=OriginalFrameLimits(wall_timeout_ms=20),
    )
    clock = [0]
    worker_wall_budgets: list[int] = []

    monkeypatch.setattr(time_module, "monotonic_ns", lambda: clock[0])
    monkeypatch.setattr(runtime_module, "_supported_platform", lambda: True)
    monkeypatch.setattr(media, "_capability_check", lambda _runtime: None)
    monkeypatch.setattr(runtime_module, "_verify_overlay", lambda _runtime: None)

    def open_source(_root: Path, _relative: str, _maximum: int) -> tuple[int, os.stat_result]:
        descriptor = os.open(source_path, os.O_RDONLY)
        return descriptor, os.fstat(descriptor)

    def snapshot(source_fd: int, _maximum: int) -> tuple[int, str, int]:
        return os.dup(source_fd), hashlib.sha256(content).hexdigest(), len(content)

    monkeypatch.setattr(media, "_open_source", open_source)
    monkeypatch.setattr(media, "_sealed_snapshot", snapshot)

    def output_memfd(_size: int) -> int:
        clock[0] = 5_000_000
        return os.open(output_path, os.O_RDWR)

    monkeypatch.setattr(runtime_module, "_output_memfd", output_memfd)
    real_fcntl = cast(Any, fcntl_module.fcntl)

    def fcntl_result(descriptor: int, command: int, *args: object) -> int:
        if command == runtime_module._F_GET_SEALS:
            return runtime_module._FINAL_OUTPUT_SEALS
        return cast(int, real_fcntl(descriptor, command, *args))

    monkeypatch.setattr(fcntl_module, "fcntl", fcntl_result)

    def run_worker(
        _runtime: OriginalFrameRuntime,
        _limits: OriginalFrameLimits,
        _source_fd: int,
        output_fd: int,
        request: bytes,
        _cancelled: object,
    ) -> runtime_module._WorkerRun:
        worker_wall_budgets.append(_limits.wall_timeout_ms)
        requested = tuple(
            FrameRef.from_mapping(item)
            for item in cast(list[object], json.loads(request)["frames"])
        )
        selected = tuple(pixels[frames.index(frame)] for frame in requested)
        os.pwrite(output_fd, b"".join(selected), 0)
        return _worker_run(source, requested, selected)

    monkeypatch.setattr(runtime_module, "_run_worker", run_worker)

    result = reader.read(source, tuple(reversed(frames)))

    assert tuple(item.frame for item in result.frames) == tuple(reversed(frames))
    assert tuple(item.pixels for item in result.frames) == tuple(reversed(pixels))
    assert all(repr(item.pixels) not in repr(result) for item in result.frames)
    assert worker_wall_budgets == [15]
    assert reader.calls[-1].item_count == 2


@pytest.mark.parametrize(
    ("mode", "code"),
    [("deadline", PortErrorCode.TIMEOUT), ("cancellation", PortErrorCode.CANCELLED)],
)
def test_post_read_checks_enforce_deadline_and_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    code: PortErrorCode,
) -> None:
    content = b"source-bytes"
    source, frames, pixels = _values(content)
    source_path = tmp_path / "source.mov"
    source_path.write_bytes(content)
    output_path = tmp_path / "output.memfd"
    output_path.write_bytes(b"\0" * 12)
    clock = [0]
    cancelled = threading.Event()
    reader = IsolatedOriginalFrameReader(
        tmp_path,
        "private-source.mov",
        _configured_runtime(tmp_path),
        limits=OriginalFrameLimits(wall_timeout_ms=10),
        cancelled=cancelled,
    )
    monkeypatch.setattr(time_module, "monotonic_ns", lambda: clock[0])
    monkeypatch.setattr(runtime_module, "_supported_platform", lambda: True)
    monkeypatch.setattr(media, "_capability_check", lambda _runtime: None)
    monkeypatch.setattr(runtime_module, "_verify_overlay", lambda _runtime: None)

    def open_source(_root: Path, _relative: str, _maximum: int) -> tuple[int, os.stat_result]:
        descriptor = os.open(source_path, os.O_RDONLY)
        return descriptor, os.fstat(descriptor)

    def snapshot(source_fd: int, _maximum: int) -> tuple[int, str, int]:
        return os.dup(source_fd), hashlib.sha256(content).hexdigest(), len(content)

    monkeypatch.setattr(media, "_open_source", open_source)
    monkeypatch.setattr(media, "_sealed_snapshot", snapshot)
    monkeypatch.setattr(
        runtime_module,
        "_output_memfd",
        lambda _size: os.open(output_path, os.O_RDWR),
    )
    real_fcntl = cast(Any, fcntl_module.fcntl)

    def fcntl_result(descriptor: int, command: int, *args: object) -> int:
        if command == runtime_module._F_GET_SEALS:
            return runtime_module._FINAL_OUTPUT_SEALS
        return cast(int, real_fcntl(descriptor, command, *args))

    monkeypatch.setattr(fcntl_module, "fcntl", fcntl_result)

    def run_worker(
        _runtime: OriginalFrameRuntime,
        _limits: OriginalFrameLimits,
        _source_fd: int,
        output_fd: int,
        _request: bytes,
        _cancelled: object,
    ) -> runtime_module._WorkerRun:
        os.pwrite(output_fd, pixels[0], 0)
        return _worker_run(source, frames[:1], pixels[:1])

    monkeypatch.setattr(runtime_module, "_run_worker", run_worker)
    if mode == "deadline":
        real_decode = runtime_module._decode_output

        def consume_postprocess_budget(*args: Any, **kwargs: Any) -> object:
            decoded = real_decode(*args, **kwargs)
            clock[0] = 10_000_000
            return decoded

        monkeypatch.setattr(runtime_module, "_decode_output", consume_postprocess_budget)
    else:
        real_pread = os.pread

        def cancel_after_read(descriptor: int, size: int, offset: int) -> bytes:
            value = real_pread(descriptor, size, offset)
            cancelled.set()
            return value

        monkeypatch.setattr(os, "pread", cancel_after_read)

    with pytest.raises(PortError) as raised:
        reader.read(source, frames[:1])

    assert raised.value.code is code
    assert str(raised.value) == f"{code.value} at original_frame_reader.read"
    assert "private-source.mov" not in str(raised.value)
    assert all(repr(value) not in str(raised.value) for value in pixels)
    assert reader.calls == ()


def test_limits_and_result_metadata_are_stable() -> None:
    limits = OriginalFrameLimits()
    assert limits.max_frame_bytes == 32 * 1024 * 1024
    assert limits.max_total_bytes == 128 * 1024 * 1024
    with pytest.raises(ValueError):
        replace(limits, max_total_bytes=512 * 1024 * 1024)


def test_runtime_value_and_helper_validation_fail_closed(tmp_path: Path) -> None:
    configured = _configured_runtime(tmp_path)
    source, frames, _ = _values()

    with pytest.raises(ValueError, match="positive"):
        replace(OriginalFrameLimits(), wall_timeout_ms=0)
    with pytest.raises(ValueError, match="exceeds"):
        replace(OriginalFrameLimits(), max_stdout_bytes=17 * 1024 * 1024)
    with pytest.raises(ValueError, match="MediaRuntime"):
        OriginalFrameRuntime(cast(Any, object()), tmp_path, tmp_path / "worker.py")
    with pytest.raises(ValueError, match="pathlib"):
        OriginalFrameRuntime(configured.media, cast(Any, "overlay"), tmp_path / "worker.py")
    with pytest.raises(ValueError, match="runtime"):
        runtime_module._copy_runtime(cast(Any, object()))
    with pytest.raises(ValueError, match="limits"):
        runtime_module._copy_limits(cast(Any, object()))
    with pytest.raises(PortError) as bad_source:
        runtime_module._owned_source(object())
    assert bad_source.value.code is PortErrorCode.INVALID_REQUEST
    with pytest.raises(PortError) as bad_page:
        runtime_module._owned_frames(cast(Any, list(frames)), source, OriginalFrameLimits())
    assert bad_page.value.code is PortErrorCode.LIMIT_EXCEEDED
    with pytest.raises(PortError) as bad_frame:
        runtime_module._owned_frames(cast(Any, (object(),)), source, OriginalFrameLimits())
    assert bad_frame.value.code is PortErrorCode.INVALID_REQUEST
    with pytest.raises(PortError) as duplicate:
        runtime_module._owned_frames((frames[0], frames[0]), source, OriginalFrameLimits())
    assert duplicate.value.code is PortErrorCode.CONFLICT


def test_platform_and_trust_helpers_reject_missing_or_invalid_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform_module, "libc_ver", lambda: ("glibc", "not-a-version"))
    assert runtime_module._supported_platform() is False
    assert runtime_module._trusted_directory(tmp_path / "missing") is False
    assert runtime_module._trusted_file(tmp_path / "missing") is False


def test_trusted_directory_rejects_owner_writable_frozen_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
    monkeypatch.setattr(Path, "lstat", lambda _path: metadata)

    assert runtime_module._trusted_directory(tmp_path) is True
    assert runtime_module._trusted_directory(tmp_path, frozen=True) is False
    metadata.st_mode = stat.S_IFDIR | 0o555
    assert runtime_module._trusted_directory(tmp_path, frozen=True) is True


def test_overlay_verification_rejects_owner_writable_root_before_reading_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = _configured_runtime(tmp_path)

    def directory_metadata(path: Path) -> SimpleNamespace:
        mode = 0o755 if path == configured.overlay_root else 0o555
        return SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=0)

    monkeypatch.setattr(Path, "lstat", directory_metadata)
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda _path: pytest.fail("writable overlay contents must not be read"),
    )

    with pytest.raises(PortError) as raised:
        runtime_module._verify_overlay(configured)

    assert raised.value.code is PortErrorCode.ISOLATION_UNAVAILABLE


def test_memfd_helpers_fail_closed_and_create_exact_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "memfd_create", raising=False)
    with pytest.raises(PortError) as unavailable:
        runtime_module._memfd(b"test")
    assert unavailable.value.code is PortErrorCode.ISOLATION_UNAVAILABLE

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(runtime_module, "_memfd", lambda _name: 55)
    monkeypatch.setattr(os, "ftruncate", lambda *arguments: calls.append(arguments))
    monkeypatch.setattr(fcntl_module, "fcntl", lambda *arguments: calls.append(arguments))
    output = runtime_module._output_memfd(12)
    assert output == 55
    assert calls == [
        (55, 12),
        (55, runtime_module._F_ADD_SEALS, runtime_module._INITIAL_OUTPUT_SEALS),
    ]


def test_worker_supervisor_sends_request_waits_for_cleanup_and_redacts_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = _configured_runtime(tmp_path)

    class Input:
        def __init__(self) -> None:
            self.data = bytearray()
            self.closed = False

        def write(self, value: bytes) -> int:
            self.data.extend(value)
            return len(value)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class Process:
        def __init__(self) -> None:
            self.stdin = Input()
            self.stdout = object()
            self.stderr = object()

    process = Process()
    commands: list[object] = []
    monkeypatch.setattr(runtime_module, "_systemd_argv", lambda *arguments: ["worker"])
    monkeypatch.setattr(subprocess_module, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        media,
        "_drain_worker",
        lambda *arguments: media._WorkerRun(b"stdout", b"stderr", 0, 1, 2, 3),
    )
    monkeypatch.setattr(media, "_ensure_unit_stopped", lambda *arguments: True)
    monkeypatch.setattr(
        subprocess_module,
        "run",
        lambda command, **kwargs: commands.append(command),
    )

    result = runtime_module._run_worker(
        configured, OriginalFrameLimits(), 40, 41, b"private-request", None
    )

    assert result == runtime_module._WorkerRun(b"stdout", b"stderr", 0)
    assert bytes(process.stdin.data) == b"private-request"
    assert process.stdin.closed is True
    assert commands and "private-request" not in repr(commands)


@pytest.mark.parametrize(
    ("run", "code"),
    [
        (runtime_module._WorkerRun(b"x" * (1024 * 1024 + 1), b"", 0), PortErrorCode.LIMIT_EXCEEDED),
        (runtime_module._WorkerRun(b"", b"", 20), PortErrorCode.LIMIT_EXCEEDED),
        (runtime_module._WorkerRun(b"", b"", 22), PortErrorCode.ISOLATION_UNAVAILABLE),
        (runtime_module._WorkerRun(b"", b"", 21), PortErrorCode.DECODE_FAILED),
        (runtime_module._WorkerRun(b"{}\n", b"unexpected\n", 0), PortErrorCode.DECODE_FAILED),
        (
            runtime_module._WorkerRun(b"", runtime_module._EXPECTED_SUCCESS_STDERR, 0),
            PortErrorCode.DECODE_FAILED,
        ),
        (
            runtime_module._WorkerRun(b"not-json\n", runtime_module._EXPECTED_SUCCESS_STDERR, 0),
            PortErrorCode.DECODE_FAILED,
        ),
        (
            runtime_module._WorkerRun(b"{ }\n", runtime_module._EXPECTED_SUCCESS_STDERR, 0),
            PortErrorCode.DECODE_FAILED,
        ),
    ],
)
def test_worker_status_and_malformed_output_fail_closed(
    run: runtime_module._WorkerRun, code: PortErrorCode
) -> None:
    source, frames, _ = _values()
    with pytest.raises(PortError) as raised:
        runtime_module._decode_output(run, source, frames, 24, OriginalFrameLimits())
    assert raised.value.code is code


def test_reader_handles_empty_mixed_stream_and_preallocation_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, frames, _ = _values()
    reader = IsolatedOriginalFrameReader(tmp_path, "source.mov", _configured_runtime(tmp_path))
    assert reader.read(source, ()).frames == ()
    with pytest.raises(PortError) as invalid_limit:
        reader.read(source, frames[:1], max_total_bytes=0)
    assert invalid_limit.value.code is PortErrorCode.LIMIT_EXCEEDED

    time_base = source.streams[0].time_base
    mixed_source = Source.create(
        source.fingerprint,
        (
            source.streams[0],
            SourceStream(1, 2, 2, 0, time_base),
        ),
    )
    mixed = (
        FrameRef.create(mixed_source.source_id, 0, "0", MediaTime("0", time_base)),
        FrameRef.create(mixed_source.source_id, 1, "0", MediaTime("0", time_base)),
    )
    assert reader.read(mixed_source, mixed).reason == "multi_stream_batch_unsupported"

    wide_source = Source.create(
        source.fingerprint,
        (SourceStream(0, 4097, 1, 0, time_base),),
    )
    wide_frame = FrameRef.create(wide_source.source_id, 0, "0", MediaTime("0", time_base))
    monkeypatch.setattr(runtime_module, "_supported_platform", lambda: True)
    with pytest.raises(PortError) as too_wide:
        reader.read(wide_source, (wide_frame,))
    assert too_wide.value.code is PortErrorCode.LIMIT_EXCEEDED


def test_output_parser_rejects_duplicate_keys_mixed_streams_and_trailing_bytes() -> None:
    source, frames, pixels = _values()
    duplicate = runtime_module._WorkerRun(
        b'{"schema":1,"schema":1}\n', runtime_module._EXPECTED_SUCCESS_STDERR, 0
    )
    with pytest.raises(PortError) as duplicate_key:
        runtime_module._decode_output(duplicate, source, frames, 24, OriginalFrameLimits())
    assert duplicate_key.value.code is PortErrorCode.DECODE_FAILED

    mixed_source = Source.create(
        source.fingerprint,
        (
            source.streams[0],
            SourceStream(1, 2, 2, 0, source.streams[0].time_base),
        ),
    )
    mixed_frames = (
        FrameRef.create(mixed_source.source_id, 0, "0", frames[0].pts),
        FrameRef.create(mixed_source.source_id, 1, "0", frames[1].pts),
    )
    with pytest.raises(PortError) as mixed:
        runtime_module._decode_output(
            _worker_run(mixed_source, mixed_frames, pixels),
            mixed_source,
            mixed_frames,
            24,
            OriginalFrameLimits(),
        )
    assert mixed.value.code is PortErrorCode.CONFLICT

    run = _worker_run(
        source,
        frames,
        pixels,
        mutate=lambda value: cast(dict[str, object], value["output"]).update(bytes="25"),
    )
    with pytest.raises(PortError) as trailing:
        runtime_module._decode_output(run, source, frames, 25, OriginalFrameLimits())
    assert trailing.value.code is PortErrorCode.CONFLICT


def test_reader_constructor_metadata_and_capability_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = _configured_runtime(tmp_path)
    with pytest.raises(ValueError, match="source location"):
        IsolatedOriginalFrameReader(cast(Any, "root"), "source.mov", configured)
    with pytest.raises(ValueError, match="runtime or limits"):
        IsolatedOriginalFrameReader(tmp_path, "source.mov", cast(Any, object()))
    with pytest.raises(ValueError, match="cancelled"):
        IsolatedOriginalFrameReader(
            tmp_path, "source.mov", configured, cancelled=cast(Any, object())
        )

    reader = IsolatedOriginalFrameReader(tmp_path, "source.mov", configured)
    assert reader.descriptor.port is PortKind.ORIGINAL_FRAME_READER
    assert reader.producer.name == "visualworld.isolated-original-frame-reader"
    assert reader.calls == ()
    monkeypatch.setattr(runtime_module, "_supported_platform", lambda: True)
    source, frames, _ = _values()

    def unavailable(_runtime: object) -> None:
        raise PortError(PortErrorCode.ISOLATION_UNAVAILABLE, PortKind.VIDEO_SOURCE, "probe")

    monkeypatch.setattr(media, "_capability_check", unavailable)
    with pytest.raises(PortError) as raised:
        reader.read(source, frames[:1])
    assert raised.value.code is PortErrorCode.ISOLATION_UNAVAILABLE


def test_worker_supervisor_rejects_missing_process_or_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = _configured_runtime(tmp_path)
    cleanup_calls: list[tuple[object, ...]] = []
    reset_calls: list[object] = []

    def cleanup(*arguments: object) -> bool:
        cleanup_calls.append(arguments)
        return True

    monkeypatch.setattr(runtime_module, "_systemd_argv", lambda *arguments: ["worker"])
    monkeypatch.setattr(media, "_ensure_unit_stopped", cleanup)
    monkeypatch.setattr(
        subprocess_module,
        "run",
        lambda command, **_kwargs: reset_calls.append(command),
    )
    monkeypatch.setattr(subprocess_module, "Popen", lambda *args, **kwargs: None)
    with pytest.raises(PortError) as unavailable:
        runtime_module._run_worker(configured, OriginalFrameLimits(), 40, 41, b"request", None)
    assert unavailable.value.code is PortErrorCode.ISOLATION_UNAVAILABLE
    assert cleanup_calls == []
    assert reset_calls == []

    class Process:
        stdin = None

    monkeypatch.setattr(subprocess_module, "Popen", lambda *args, **kwargs: Process())
    with pytest.raises(PortError) as no_stdin:
        runtime_module._run_worker(configured, OriginalFrameLimits(), 40, 41, b"request", None)
    assert no_stdin.value.code is PortErrorCode.ISOLATION_UNAVAILABLE
    assert len(cleanup_calls) == 1
    assert len(reset_calls) == 1
    assert "reset-failed" in cast(list[str], reset_calls[0])


def test_worker_supervisor_cleans_whole_cgroup_when_sender_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = _configured_runtime(tmp_path)
    cleanup_calls: list[tuple[object, ...]] = []
    reset_calls: list[object] = []

    class Input:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Process:
        stdin = Input()

    process = Process()

    def cleanup(*arguments: object) -> bool:
        cleanup_calls.append(arguments)
        return True

    monkeypatch.setattr(runtime_module, "_systemd_argv", lambda *arguments: ["worker"])
    monkeypatch.setattr(subprocess_module, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _thread: (_ for _ in ()).throw(RuntimeError("thread unavailable")),
    )
    monkeypatch.setattr(media, "_ensure_unit_stopped", cleanup)
    monkeypatch.setattr(
        subprocess_module,
        "run",
        lambda command, **_kwargs: reset_calls.append(command),
    )
    monkeypatch.setattr(
        media,
        "_drain_worker",
        lambda *_arguments: pytest.fail("worker drain must not run"),
    )

    with pytest.raises(PortError) as raised:
        runtime_module._run_worker(
            configured,
            OriginalFrameLimits(),
            40,
            41,
            b"private-request",
            None,
        )

    assert raised.value.code is PortErrorCode.ISOLATION_UNAVAILABLE
    assert str(raised.value) == "isolation_unavailable at original_frame_reader.read"
    assert process.stdin.closed is True
    assert len(cleanup_calls) == 1
    assert len(reset_calls) == 1
    assert "reset-failed" in cast(list[str], reset_calls[0])
    assert "private-request" not in repr((cleanup_calls, reset_calls, raised.value))


@pytest.mark.parametrize(
    ("mode", "cleanup_ok", "expected"),
    [
        ("port", True, PortErrorCode.CANCELLED),
        ("port", False, PortErrorCode.ISOLATION_UNAVAILABLE),
        ("exception", True, RuntimeError),
        ("exception", False, PortErrorCode.ISOLATION_UNAVAILABLE),
        ("success", False, PortErrorCode.ISOLATION_UNAVAILABLE),
        ("send_failure", True, PortErrorCode.ISOLATION_UNAVAILABLE),
    ],
)
def test_worker_supervisor_failure_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    cleanup_ok: bool,
    expected: object,
) -> None:
    configured = _configured_runtime(tmp_path)

    class Input:
        def write(self, _value: bytes) -> int:
            if mode == "send_failure":
                raise BrokenPipeError
            return 1

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

    class Process:
        stdin = Input()
        stdout = object()
        stderr = object()

    def drain(*_arguments: object) -> media._WorkerRun:
        if mode == "port":
            raise PortError(PortErrorCode.CANCELLED, PortKind.VIDEO_SOURCE, "probe")
        if mode == "exception":
            raise RuntimeError("boom")
        return media._WorkerRun(b"stdout", b"stderr", 0, 1, 2, 3)

    monkeypatch.setattr(runtime_module, "_systemd_argv", lambda *arguments: ["worker"])
    monkeypatch.setattr(subprocess_module, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(media, "_drain_worker", drain)
    monkeypatch.setattr(media, "_ensure_unit_stopped", lambda *arguments: cleanup_ok)
    monkeypatch.setattr(subprocess_module, "run", lambda *args, **kwargs: None)

    error_type = expected if isinstance(expected, type) else PortError
    with pytest.raises(error_type) as raised:
        runtime_module._run_worker(configured, OriginalFrameLimits(), 40, 41, b"request", None)
    if isinstance(expected, PortErrorCode):
        assert cast(PortError, raised.value).code is expected
