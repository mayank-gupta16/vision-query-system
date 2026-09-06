# SPDX-License-Identifier: Apache-2.0
"""Unit and contract tests for the bounded local media adapter."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import threading
from contextlib import suppress
from errno import ELOOP, ENOENT, ENOSYS
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

import visualworld.media as media
from visualworld.ingestion import MediaTime
from visualworld.ports import PortError, PortErrorCode, VideoSource


def worker_payload() -> dict[str, object]:
    time_base = {"numerator": "1", "denominator": "1000"}
    return {
        "schema_version": 1,
        "status": "ok",
        "runtime": {
            "pyav": "18.1.0",
            "libavformat": "63.1.101",
            "libavcodec": "63.1.101",
        },
        "isolation": {
            "non_root": True,
            "no_new_privileges": True,
            "network_denied": True,
            "landlock_denied": True,
        },
        "stream": {
            "stream_index": 0,
            "width": 16,
            "height": 12,
            "rotation_degrees": 90,
            "time_base": time_base,
            "duration": {"value": "800", "time_base": time_base},
            "codec": "rawvideo",
            "format_name": "mov",
        },
        "frames": [
            {
                "decode_index": "0",
                "pts": {"value": "-1", "time_base": time_base},
                "duration": {"value": "100", "time_base": time_base},
                "key_frame": True,
                "pixel_sha256": "1" * 64,
            },
            {
                "decode_index": "1",
                "pts": {"value": "100", "time_base": time_base},
                "duration": None,
                "key_frame": False,
                "pixel_sha256": "2" * 64,
            },
        ],
    }


def worker_run(payload: object | None = None, *, returncode: int = 0) -> media._WorkerRun:
    selected = worker_payload() if payload is None else payload
    return media._WorkerRun(
        json.dumps(selected, sort_keys=True).encode(),
        b'{"schema_version":1,"status":"ok"}\n',
        returncode,
        12,
        4,
        1024,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"max_source_bytes": 0},
        {"max_source_bytes": 2**30 + 1},
        {"max_worker_output_bytes": 16 * 1024 * 1024 + 1},
        {"max_frames": 100_001},
        {"task_count": 1025},
        {"wall_timeout_ms": True},
    ],
)
def test_media_limits_are_bounded(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "max_source_bytes": 64,
        "max_duration_seconds": 60,
        "max_width": 16,
        "max_height": 12,
        "max_pixels": 192,
        "max_frames": 4,
        "max_decoded_bytes": 2304,
        "max_worker_output_bytes": 4096,
        "wall_timeout_ms": 1000,
        "cpu_budget_ms": 1000,
        "memory_bytes": 64 * 1024 * 1024,
        "task_count": 8,
    }
    values.update(changes)
    with pytest.raises(ValueError):
        media.MediaLimits(**cast(Any, values))


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/absolute.mov",
        "../escape.mov",
        "nested/../escape.mov",
        "bad\\path",
        "bad\npath",
        "\ud800",
    ],
)
def test_source_name_rejects_escape_and_control_syntax(value: str) -> None:
    with pytest.raises(PortError) as raised:
        media._relative_source(value)
    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    if value:
        assert value not in str(raised.value)


def test_source_name_accepts_a_bounded_relative_path() -> None:
    assert media._relative_source("camera/day-1.mov") == "camera/day-1.mov"


def test_runtime_paths_are_normalized_without_resolving_symlinks(tmp_path: Path) -> None:
    runtime = media.MediaRuntime(tmp_path / "runtime/../runtime", tmp_path / "worker.py")
    assert runtime.root == tmp_path / "runtime"
    assert runtime.worker == tmp_path / "worker.py"

    with pytest.raises(ValueError):
        media.MediaRuntime(cast(Path, "runtime"), tmp_path / "worker.py")


def test_unsupported_platform_fails_closed_before_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cast(Any, media).platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cast(Any, media).platform, "machine", lambda: "arm64")

    with pytest.raises(PortError) as raised:
        media._capability_check(
            media.MediaRuntime(Path("/runtime"), Path("/runtime/worker/media_worker.py"))
        )
    assert raised.value.code is PortErrorCode.ISOLATION_UNAVAILABLE


def test_trusted_path_checks_require_root_owned_nonwritable_nodes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    assert media._trusted_regular(missing) is False
    assert media._trusted_directory(missing) is False
    assert media._source_directory(missing) is False

    metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)
    monkeypatch.setattr(Path, "lstat", lambda _path: metadata)
    assert media._trusted_regular(tmp_path) is True
    assert media._trusted_directory(tmp_path) is False
    metadata.st_mode = stat.S_IFDIR | 0o755
    assert media._trusted_directory(tmp_path) is True
    assert media._source_directory(tmp_path) is True
    metadata.st_uid = 501
    assert media._trusted_directory(tmp_path) is False
    metadata.st_uid = 0
    metadata.st_mode = stat.S_IFDIR | 0o744
    assert media._trusted_directory(tmp_path) is False
    metadata.st_mode = stat.S_IFREG | 0o777
    assert media._trusted_regular(tmp_path) is False


def test_capability_probe_checks_runtime_tools_and_cgroup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    for relative in (
        "python/bin/python3.13",
        "python/BUILD",
        "ffmpeg/lib/keep",
        "venv/lib/python3.13/site-packages/keep",
    ):
        path = runtime_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("trusted", encoding="utf-8")
    worker = runtime_root / "worker/media_worker.py"
    worker.parent.mkdir()
    worker.write_text("trusted", encoding="utf-8")
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.controllers").write_text("cpu memory", encoding="ascii")
    runtime = media.MediaRuntime(runtime_root, worker)
    monkeypatch.setattr(cast(Any, media).platform, "system", lambda: "Linux")
    monkeypatch.setattr(cast(Any, media).platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(cast(Any, media).os, "geteuid", lambda: 0)
    monkeypatch.setattr(media, "_CGROUP_ROOT", cgroup)
    monkeypatch.setattr(media, "_trusted_regular", lambda _path: True)
    monkeypatch.setattr(media, "_trusted_directory", lambda _path: True)
    monkeypatch.setattr(media, "_runtime_manifest_valid", lambda _runtime: True)

    media._capability_check(runtime)

    with pytest.raises(PortError, match="isolation_unavailable"):
        media._capability_check(media.MediaRuntime(runtime_root, tmp_path / "outside.py"))

    monkeypatch.setattr(media, "_trusted_regular", lambda _path: False)
    with pytest.raises(PortError, match="isolation_unavailable"):
        media._capability_check(runtime)
    monkeypatch.setattr(media, "_trusted_regular", lambda _path: True)
    monkeypatch.setattr(media, "_runtime_manifest_valid", lambda _runtime: False)
    with pytest.raises(PortError, match="isolation_unavailable"):
        media._capability_check(runtime)
    monkeypatch.setattr(media, "_runtime_manifest_valid", lambda _runtime: True)
    (cgroup / "cgroup.controllers").unlink()
    with pytest.raises(PortError, match="isolation_unavailable"):
        media._capability_check(runtime)


def test_runtime_manifest_checksum_and_declared_worker_are_exact() -> None:
    repository = Path(__file__).resolve().parents[1]
    manifest_path = repository / "workers/visualworld-runtime.json"
    worker_path = repository / "workers/media_worker.py"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == (
        media._APPROVED_RUNTIME_MANIFEST_SHA256
    )
    assert manifest["tree_sha256"] == media._APPROVED_RUNTIME_TREE_SHA256
    assert manifest["worker"] == os.fspath(media._APPROVED_RUNTIME_WORKER)
    assert manifest["worker_sha256"] == hashlib.sha256(worker_path.read_bytes()).hexdigest()
    assert manifest["network_enabled"] is False


def test_runtime_tree_digest_binds_contents_and_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    package = runtime / "package"
    package.mkdir(parents=True)
    runtime.chmod(0o755)
    package.chmod(0o755)
    payload = package / "payload"
    payload.write_bytes(b"approved")
    payload.chmod(0o644)
    manifest = runtime / media._RUNTIME_MANIFEST_NAME
    manifest.write_text("ignored by tree digest", encoding="utf-8")
    manifest.chmod(0o644)
    actual_lstat = Path.lstat

    def root_owned(path: Path) -> SimpleNamespace:
        metadata = actual_lstat(path)
        return SimpleNamespace(st_mode=metadata.st_mode, st_uid=0, st_size=metadata.st_size)

    monkeypatch.setattr(Path, "lstat", root_owned)
    approved = media._runtime_tree_digest(runtime)
    assert approved is not None
    manifest.write_text("still excluded", encoding="utf-8")
    assert media._runtime_tree_digest(runtime) == approved
    payload.write_bytes(b"tampered")
    assert media._runtime_tree_digest(runtime) != approved
    payload.chmod(0o666)
    assert media._runtime_tree_digest(runtime) is None


def test_runtime_manifest_rejects_worker_or_tree_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = media.MediaRuntime(
        Path("/runtime"),
        Path("/runtime/worker/media_worker.py"),
    )
    monkeypatch.setattr(media, "_trusted_regular", lambda _path: True)
    monkeypatch.setattr(
        media,
        "_file_sha256",
        lambda _path: media._APPROVED_RUNTIME_MANIFEST_SHA256,
    )
    monkeypatch.setattr(
        media,
        "_runtime_tree_digest",
        lambda _root: media._APPROVED_RUNTIME_TREE_SHA256,
    )
    assert media._runtime_manifest_valid(runtime) is True

    monkeypatch.setattr(media, "_runtime_tree_digest", lambda _root: "0" * 64)
    assert media._runtime_manifest_valid(runtime) is False
    assert (
        media._runtime_manifest_valid(
            media.MediaRuntime(Path("/runtime"), Path("/runtime/worker/other.py"))
        )
        is False
    )


def test_namespace_and_systemd_arguments_are_fixed_and_source_opaque(tmp_path: Path) -> None:
    runtime = media.MediaRuntime(
        Path("/opt/visualworld-runtime"),
        Path("/opt/visualworld-runtime/worker/media_worker.py"),
    )
    limits = media.MediaLimits()

    namespace = media._namespace_argv(runtime, limits)
    systemd = media._systemd_argv(runtime, limits, 19, "visualworld-media-test")

    assert namespace[0] == "/usr/bin/bwrap"
    assert "--unshare-net" in namespace
    assert "--disable-userns" in namespace
    assert "--cap-drop" in namespace
    assert "/opt/visualworld-runtime" in namespace
    assert "/runtime/worker/media_worker.py" in namespace
    assert "/opt/visualworld-runtime/worker/media_worker.py" not in namespace
    assert not any("http://" in item or "https://" in item or "rtsp://" in item for item in systemd)
    assert not any(os.fspath(tmp_path) in item for item in systemd)
    assert any("OpenFile=/proc/" in item and "/fd/19:" in item for item in systemd)
    assert "--property=KillMode=control-group" in systemd
    assert systemd[0] == "/usr/bin/systemd-run"


class _OpenAt2Libc:
    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    def syscall(self, *arguments: object) -> int:
        self.calls.append(arguments)
        return self.result


def test_open_source_uses_openat2_and_enforces_regular_size(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mov"
    source.write_bytes(b"fixture")
    source_fd = os.open(source, os.O_RDONLY)
    libc = _OpenAt2Libc(source_fd)
    monkeypatch.setattr(cast(Any, media).platform, "system", lambda: "Linux")
    monkeypatch.setattr(cast(Any, media).platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(cast(Any, media).ctypes, "CDLL", lambda *_args, **_kwargs: libc)

    opened, metadata = media._open_source(tmp_path, "source.mov", 7)
    assert opened == source_fd
    assert metadata.st_size == 7
    assert libc.calls and cast(Any, libc.calls[0][0]).value == media._OPENAT2
    os.close(opened)

    oversized_fd = os.open(source, os.O_RDONLY)
    libc.result = oversized_fd
    with pytest.raises(PortError) as raised:
        media._open_source(tmp_path, "source.mov", 6)
    assert raised.value.code is PortErrorCode.LIMIT_EXCEEDED


@pytest.mark.parametrize(
    ("error_number", "expected"),
    [
        (ENOENT, PortErrorCode.NOT_FOUND),
        (ENOSYS, PortErrorCode.ISOLATION_UNAVAILABLE),
        (ELOOP, PortErrorCode.INVALID_REQUEST),
    ],
)
def test_open_source_maps_kernel_failures_without_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_number: int,
    expected: PortErrorCode,
) -> None:
    libc = _OpenAt2Libc(-1)
    monkeypatch.setattr(cast(Any, media).platform, "system", lambda: "Linux")
    monkeypatch.setattr(cast(Any, media).platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(cast(Any, media).ctypes, "CDLL", lambda *_args, **_kwargs: libc)
    monkeypatch.setattr(cast(Any, media).ctypes, "get_errno", lambda: error_number)

    with pytest.raises(PortError) as raised:
        media._open_source(tmp_path, "secret.mov", 10)
    assert raised.value.code is expected
    assert "secret.mov" not in str(raised.value)
    assert raised.value.__context__ is None


def test_open_source_rejects_unsupported_or_non_directory_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cast(Any, media).platform, "system", lambda: "Darwin")
    with pytest.raises(PortError, match="isolation_unavailable"):
        media._open_source(tmp_path, "source.mov", 10)
    monkeypatch.setattr(cast(Any, media).platform, "system", lambda: "Linux")
    monkeypatch.setattr(cast(Any, media).platform, "machine", lambda: "x86_64")
    with pytest.raises(PortError, match="invalid_request"):
        media._open_source(tmp_path / "missing", "source.mov", 10)


def test_open_source_rejects_nonregular_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_fd = os.open(tmp_path, os.O_RDONLY)
    libc = _OpenAt2Libc(source_fd)
    monkeypatch.setattr(cast(Any, media).platform, "system", lambda: "Linux")
    monkeypatch.setattr(cast(Any, media).platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(cast(Any, media).ctypes, "CDLL", lambda *_args, **_kwargs: libc)
    with pytest.raises(PortError, match="invalid_request"):
        media._open_source(tmp_path, "directory", 10)


def test_sealed_snapshot_hashes_and_bounds_without_path_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source"
    snapshot_path = tmp_path / "snapshot"
    source_path.write_bytes(b"sealed bytes")
    snapshot_path.write_bytes(b"")
    source_fd = os.open(source_path, os.O_RDONLY)
    snapshot_fd = os.open(snapshot_path, os.O_RDWR)
    libc = _OpenAt2Libc(snapshot_fd)
    monkeypatch.setattr(cast(Any, media).ctypes, "CDLL", lambda *_args, **_kwargs: libc)
    seals: list[tuple[int, int, int]] = []
    monkeypatch.setattr(cast(Any, media).fcntl, "fcntl", lambda *args: seals.append(args))

    result_fd, digest, size = media._sealed_snapshot(source_fd, 64)
    assert result_fd == snapshot_fd
    assert digest == hashlib.sha256(b"sealed bytes").hexdigest()
    assert size == len(b"sealed bytes")
    assert os.read(result_fd, 64) == b"sealed bytes"
    assert seals == [
        (
            snapshot_fd,
            media._F_ADD_SEALS,
            media._F_SEAL_SEAL | media._F_SEAL_SHRINK | media._F_SEAL_GROW | media._F_SEAL_WRITE,
        )
    ]
    os.close(source_fd)
    os.close(result_fd)


def test_sealed_snapshot_maps_unavailable_and_copy_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"too large")
    source_fd = os.open(source, os.O_RDONLY)
    libc = _OpenAt2Libc(-1)
    monkeypatch.setattr(cast(Any, media).ctypes, "CDLL", lambda *_args, **_kwargs: libc)
    with pytest.raises(PortError, match="isolation_unavailable"):
        media._sealed_snapshot(source_fd, 64)

    snapshot = tmp_path / "snapshot"
    snapshot.write_bytes(b"")
    libc.result = os.open(snapshot, os.O_RDWR)
    with pytest.raises(PortError, match="limit_exceeded"):
        media._sealed_snapshot(source_fd, 1)
    os.close(source_fd)


def test_worker_output_builds_exact_domain_records() -> None:
    decoded = media._decode_output(
        worker_run(),
        "a" * 64,
        2892,
        media.MediaLimits(),
    )

    assert decoded.source.fingerprint.digest == "a" * 64
    assert decoded.source.streams[0].rotation_degrees == 90
    assert [frame.decode_index for frame in decoded.frames] == ["0", "1"]
    assert decoded.frames[0].pts.value == "-1"
    assert decoded.details.codec == "rawvideo"
    assert decoded.details.duration == MediaTime("800", decoded.source.streams[0].time_base)
    assert decoded.details.pixel_hashes == ("1" * 64, "2" * 64)
    assert decoded.details.pyav_version == "18.1.0"
    assert decoded.details.libavformat_version == "63.1.101"
    assert decoded.metrics == media.MediaMetrics(12, 4, 1024, 2892, 2)


@pytest.mark.parametrize("returncode", [1, 20, 21, 22])
def test_worker_exit_codes_are_structured(returncode: int) -> None:
    with pytest.raises(PortError) as raised:
        media._decode_output(worker_run(returncode=returncode), "a" * 64, 1, media.MediaLimits())
    expected = {
        1: PortErrorCode.DECODE_FAILED,
        20: PortErrorCode.LIMIT_EXCEEDED,
        21: PortErrorCode.DECODE_FAILED,
        22: PortErrorCode.ISOLATION_UNAVAILABLE,
    }
    assert raised.value.code is expected[returncode]


def invalid_payloads() -> list[object]:
    values: list[object] = []
    for mutator in (
        lambda item: item.update(schema_version=2),
        lambda item: item.update(schema_version=True),
        lambda item: cast(dict[str, object], item["runtime"]).update(pyav="19.0.0"),
        lambda item: cast(dict[str, object], item["isolation"]).update(network_denied=False),
        lambda item: cast(dict[str, object], item["stream"]).update(width=100_000),
        lambda item: cast(dict[str, object], item["stream"]).update(codec="bad value"),
        lambda item: cast(
            dict[str, object], cast(dict[str, object], item["stream"])["time_base"]
        ).update(numerator=str(2**32)),
        lambda item: cast(list[dict[str, object]], item["frames"])[0].update(pixel_sha256="secret"),
        lambda item: cast(list[dict[str, object]], item["frames"])[1].update(decode_index="0"),
        lambda item: item.update(frames=[]),
    ):
        selected = cast(dict[str, object], json.loads(json.dumps(worker_payload())))
        mutator(selected)
        values.append(selected)
    values.extend(
        (
            b"not-json",
            b'{"value":' + b"1" * 5_000 + b"}",
            b"[" * 2_000 + b"0" + b"]" * 2_000,
            ["not", "mapping"],
        )
    )
    return values


@pytest.mark.parametrize("payload", invalid_payloads())
def test_worker_output_is_strict_bounded_and_redacted(payload: object) -> None:
    run = (
        media._WorkerRun(payload, b"", 0, 1, 1, 1)
        if isinstance(payload, bytes)
        else worker_run(payload)
    )
    with pytest.raises(PortError) as raised:
        media._decode_output(run, "a" * 64, 1, media.MediaLimits())
    assert raised.value.code in {
        PortErrorCode.DECODE_FAILED,
        PortErrorCode.ISOLATION_UNAVAILABLE,
    }
    assert "secret" not in str(raised.value)


def test_local_video_source_caches_one_decode_and_pages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mov"
    source.write_bytes(b"fixture")
    worker = tmp_path / "worker.py"
    worker.write_text("# worker", encoding="utf-8")
    runs: list[int] = []

    monkeypatch.setattr(media, "_capability_check", lambda _runtime: None)
    monkeypatch.setattr(
        media,
        "_open_source",
        lambda _root, _relative, _maximum: (os.open(source, os.O_RDONLY), source.stat()),
    )

    def snapshot(source_fd: int, _maximum: int) -> tuple[int, str, int]:
        content = os.read(source_fd, 64)
        return os.dup(source_fd), hashlib.sha256(content).hexdigest(), len(content)

    def run(
        _runtime: media.MediaRuntime,
        _limits: media.MediaLimits,
        snapshot_fd: int,
        _cancelled: threading.Event | None,
    ) -> media._WorkerRun:
        os.fstat(snapshot_fd)
        runs.append(snapshot_fd)
        return worker_run()

    monkeypatch.setattr(media, "_sealed_snapshot", snapshot)
    monkeypatch.setattr(media, "_run_worker", run)
    adapter = media.LocalVideoSource(
        tmp_path,
        "source.mov",
        media.MediaRuntime(tmp_path / "runtime", worker),
    )

    assert isinstance(adapter, VideoSource)
    source_record = adapter.probe()
    assert adapter.probe() == source_record
    assert adapter.read_frames(stream_index=0, limit=1)[0].decode_index == "0"
    assert (
        adapter.read_frames(stream_index=0, after_decode_index="0", limit=2)[0].decode_index == "1"
    )
    assert adapter.details.codec == "rawvideo"
    assert adapter.metrics.frame_count == 2
    assert len(runs) == 1
    assert [call.operation for call in adapter.calls] == [
        "probe",
        "probe",
        "read_frames",
        "read_frames",
    ]

    with pytest.raises(PortError, match="limit_exceeded"):
        adapter.read_frames(stream_index=0, limit=0)
    with pytest.raises(PortError, match="invalid_request"):
        adapter.read_frames(stream_index=-1)
    with pytest.raises(PortError, match="not_found"):
        adapter.read_frames(stream_index=1)
    with pytest.raises(PortError, match="invalid_request"):
        adapter.read_frames(stream_index=0, after_decode_index="01")
    with pytest.raises(PortError, match="limit_exceeded"):
        adapter.read_frames(stream_index=0, after_decode_index=str(2**64))


def _child(code: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _kill_process(_unit: str, _cgroup: Path, process: subprocess.Popen[bytes]) -> bool:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    return True


def test_drain_worker_reads_both_pipes_with_bounds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(media, "_CGROUP_ROOT", tmp_path)
    process = _child("import sys; sys.stdout.buffer.write(b'ok'); sys.stderr.buffer.write(b'fine')")
    result = media._drain_worker(process, "test", media.MediaLimits(), None)
    assert result.stdout == b"ok"
    assert result.stderr == b"fine"
    assert result.returncode == 0


@pytest.mark.parametrize("failure", ["output", "timeout", "cancel"])
def test_drain_worker_kills_on_limit_timeout_or_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    monkeypatch.setattr(media, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(media, "_kill_unit", _kill_process)
    monkeypatch.setattr(media, "_wait_unit_stopped", lambda *_args: True)
    cancelled = threading.Event()
    if failure == "output":
        process = _child(
            "import sys,time; sys.stdout.buffer.write(b'x'*4096); sys.stdout.flush(); time.sleep(1)"
        )
        limits = media.MediaLimits(max_worker_output_bytes=16)
        expected = PortErrorCode.LIMIT_EXCEEDED
    else:
        process = _child("import time; time.sleep(1)")
        limits = media.MediaLimits(wall_timeout_ms=10)
        expected = PortErrorCode.TIMEOUT
        if failure == "cancel":
            cancelled.set()
            limits = media.MediaLimits()
            expected = PortErrorCode.CANCELLED

    with pytest.raises(PortError) as raised:
        media._drain_worker(process, "test", limits, cancelled)
    assert raised.value.code is expected
    assert process.poll() is not None


def test_read_number_accepts_only_named_decimal_values(tmp_path: Path) -> None:
    value = tmp_path / "value"
    value.write_text("123\n", encoding="ascii")
    fields = tmp_path / "fields"
    fields.write_text("usage_usec 456\ninvalid nope\n", encoding="ascii")

    assert media._read_number(value) == 123
    assert media._read_number(fields, "usage_usec") == 456
    assert media._read_number(fields, "missing") == 0
    assert media._read_number(tmp_path / "missing") == 0


class _ProcessWithoutPipes:
    stdout = None
    stderr = None


def test_drain_requires_bounded_output_pipes() -> None:
    with pytest.raises(PortError, match="isolation_unavailable"):
        media._drain_worker(
            cast(subprocess.Popen[bytes], _ProcessWithoutPipes()),
            "test",
            media.MediaLimits(),
            None,
        )


def test_kill_unit_prefers_cgroup_kill_and_has_fixed_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    writes: list[tuple[int, bytes]] = []
    closes: list[int] = []
    signals: list[tuple[int, int]] = []
    process = cast(subprocess.Popen[bytes], SimpleNamespace(pid=123))

    def write(fd: int, value: bytes) -> int:
        writes.append((fd, value))
        return 1

    monkeypatch.setattr(cast(Any, media).os, "open", lambda *_args: 7)
    monkeypatch.setattr(cast(Any, media).os, "write", write)
    monkeypatch.setattr(cast(Any, media).os, "close", lambda fd: closes.append(fd))
    monkeypatch.setattr(
        cast(Any, media).os,
        "killpg",
        lambda pid, selected_signal: signals.append((pid, selected_signal)),
    )

    assert media._kill_unit("unit.service", tmp_path, process) is True
    assert writes == [(7, b"1")]
    assert closes == [7]
    assert signals == [(123, signal.SIGKILL)]

    commands: list[list[str]] = []

    def missing(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cast(Any, media).os, "open", missing)
    monkeypatch.setattr(cast(Any, media).subprocess, "run", run)
    assert media._kill_unit("unit.service", tmp_path, process) is True
    assert commands == [
        ["/usr/bin/systemctl", "kill", "--kill-whom=all", "--signal=SIGKILL", "unit.service"]
    ]

    commands.clear()
    monkeypatch.setattr(cast(Any, media).os, "open", lambda *_args: 8)
    monkeypatch.setattr(cast(Any, media).os, "write", missing)
    assert media._kill_unit("unit.service", tmp_path, process) is True
    assert commands == [
        ["/usr/bin/systemctl", "kill", "--kill-whom=all", "--signal=SIGKILL", "unit.service"]
    ]


def test_cgroup_and_unit_stop_checks_are_strict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cgroup = tmp_path / "unit.service"
    cgroup.mkdir()
    assert media._cgroup_processes(cgroup) == ()
    (cgroup / "cgroup.procs").write_text("12\n34\n", encoding="ascii")
    assert media._cgroup_processes(cgroup) == (12, 34)
    (cgroup / "cgroup.procs").write_text("12 invalid\n", encoding="ascii")
    assert media._cgroup_processes(cgroup) is None

    monkeypatch.setattr(media, "_cgroup_processes", lambda _path: ())
    monkeypatch.setattr(media, "_unit_inactive", lambda _unit: True)
    assert media._wait_unit_stopped("unit.service", cgroup, timeout_seconds=0) is True
    monkeypatch.setattr(media, "_unit_inactive", lambda _unit: False)
    assert media._wait_unit_stopped("unit.service", cgroup, timeout_seconds=0) is False


@pytest.mark.parametrize(
    ("returncode", "state", "expected"),
    [(0, b"inactive\n", True), (4, b"inactive\n", True), (0, b"active\n", False)],
)
def test_unit_inactive_accepts_only_stopped_states(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    state: bytes,
    expected: bool,
) -> None:
    monkeypatch.setattr(
        cast(Any, media).subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode, state),
    )
    assert media._unit_inactive("unit.service") is expected


def test_ensure_unit_stopped_kills_when_cgroup_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = cast(
        subprocess.Popen[bytes],
        SimpleNamespace(pid=123, poll=lambda: 0, wait=lambda **_kwargs: 0),
    )
    killed: list[str] = []

    def kill(unit: str, _cgroup: Path, _process: subprocess.Popen[bytes]) -> bool:
        killed.append(unit)
        return True

    monkeypatch.setattr(media, "_cgroup_processes", lambda _path: None)
    monkeypatch.setattr(media, "_kill_unit", kill)
    monkeypatch.setattr(media, "_wait_unit_stopped", lambda *_args: True)

    assert media._ensure_unit_stopped("unit.service", tmp_path, process) is True
    assert killed == ["unit.service"]


def test_run_worker_uses_argument_array_and_cleans_transient_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = cast(subprocess.Popen[bytes], SimpleNamespace())
    popen_calls: list[tuple[list[str], dict[str, object]]] = []
    cleanup: list[list[str]] = []

    def popen(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        popen_calls.append((command, kwargs))
        return process

    expected = worker_run()
    monkeypatch.setattr(cast(Any, media).subprocess, "Popen", popen)
    monkeypatch.setattr(media, "_drain_worker", lambda *_args: expected)
    monkeypatch.setattr(media, "_ensure_unit_stopped", lambda *_args: True)
    monkeypatch.setattr(
        cast(Any, media).subprocess,
        "run",
        lambda command, **_kwargs: cleanup.append(command),
    )
    result = media._run_worker(
        media.MediaRuntime(Path("/runtime"), Path("/runtime/worker/media_worker.py")),
        media.MediaLimits(),
        3,
        None,
    )

    assert result is expected
    assert popen_calls[0][0][0] == "/usr/bin/systemd-run"
    assert popen_calls[0][1]["start_new_session"] is True
    assert cleanup and cleanup[0][:2] == ["/usr/bin/systemctl", "reset-failed"]


def test_run_worker_rejects_unverified_unit_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    process = cast(subprocess.Popen[bytes], SimpleNamespace())
    monkeypatch.setattr(cast(Any, media).subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(media, "_drain_worker", lambda *_args: worker_run())
    monkeypatch.setattr(media, "_ensure_unit_stopped", lambda *_args: False)
    monkeypatch.setattr(
        cast(Any, media).subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )

    with pytest.raises(PortError, match="isolation_unavailable"):
        media._run_worker(
            media.MediaRuntime(Path("/runtime"), Path("/runtime/worker/media_worker.py")),
            media.MediaLimits(),
            3,
            None,
        )


def test_runner_os_failures_are_mapped_without_backend_chaining(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mov"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(media, "_capability_check", lambda _runtime: None)
    monkeypatch.setattr(
        media,
        "_open_source",
        lambda _root, _relative, _maximum: (os.open(source, os.O_RDONLY), source.stat()),
    )
    monkeypatch.setattr(
        media,
        "_sealed_snapshot",
        lambda source_fd, _maximum: (
            os.dup(source_fd),
            hashlib.sha256(b"fixture").hexdigest(),
            7,
        ),
    )

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("secret backend detail")

    monkeypatch.setattr(media, "_run_worker", fail)
    adapter = media.LocalVideoSource(
        tmp_path,
        "source.mov",
        media.MediaRuntime(tmp_path / "runtime", tmp_path / "worker.py"),
    )
    with pytest.raises(PortError) as raised:
        adapter.probe()
    assert raised.value.code is PortErrorCode.ISOLATION_UNAVAILABLE
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "secret" not in str(raised.value)
