#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run issue #11 acceptance and CPU-LITE checks on the supported Linux host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import cast

import generate_synthetic_fixtures as fixtures

import visualworld.media as media
from visualworld.media import LocalVideoSource, MediaLimits, MediaRuntime
from visualworld.ports import PortError, PortErrorCode


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _memory_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def _expect_error(operation: Callable[[], object], expected: PortErrorCode) -> bool:
    try:
        operation()
    except PortError as error:
        return error.code is expected and error.__cause__ is None
    return False


def _fixture_records(manifest: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], manifest["fixtures"])


def _process_start_time(pid: int) -> str | None:
    try:
        fields = (
            (Path("/proc") / str(pid) / "stat")
            .read_text(encoding="ascii")
            .rpartition(") ")[2]
            .split()
        )
    except OSError:
        return None
    return fields[19] if len(fields) > 19 and fields[19].isdigit() else None


def _descendant_cleanup(runtime_root: Path) -> tuple[bool, int]:
    unit = f"visualworld-media-descendant-{os.getpid()}-{time.monotonic_ns()}"
    service = f"{unit}.service"
    cgroup = media._CGROUP_ROOT / "system.slice" / service
    cancelled = threading.Event()
    monitor_done = threading.Event()
    observed: dict[int, str] = {}
    command = [
        os.fspath(media._SYSTEMD_RUN),
        "--quiet",
        "--pipe",
        "--wait",
        "--service-type=exec",
        f"--unit={unit}",
        "--property=User=nobody",
        "--property=Group=nogroup",
        "--property=NoNewPrivileges=yes",
        "--property=KillMode=control-group",
        "--property=MemoryMax=67108864",
        "--property=MemorySwapMax=0",
        "--property=TasksMax=8",
        "--property=RuntimeMaxSec=10s",
        os.fspath(runtime_root / "python/bin/python3.13"),
        "-c",
        "import os,time; os.fork(); time.sleep(30)",
    ]
    process: subprocess.Popen[bytes] | None = None

    def observe_descendants() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not monitor_done.is_set():
            pids = media._cgroup_processes(cgroup)
            identities = (
                {pid: start for pid in pids if (start := _process_start_time(pid)) is not None}
                if pids is not None
                else {}
            )
            if len(identities) >= 2:
                observed.update(identities)
                cancelled.set()
                return
            time.sleep(0.01)
        cancelled.set()

    structured_cancel = False
    stopped = False
    monitor = threading.Thread(target=observe_descendants, daemon=True)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        monitor.start()
        try:
            media._drain_worker(process, unit, media.MediaLimits(wall_timeout_ms=8_000), cancelled)
        except PortError as error:
            structured_cancel = error.code is PortErrorCode.CANCELLED and error.__cause__ is None
        stopped = media._wait_unit_stopped(service, cgroup)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        monitor_done.set()
        if monitor.ident is not None:
            monitor.join(timeout=1)
        if process is not None and process.poll() is None:
            media._kill_unit(service, cgroup, process)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
        stopped = media._wait_unit_stopped(service, cgroup)
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [os.fspath(media._SYSTEMCTL), "reset-failed", service],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
    identities_gone = all(_process_start_time(pid) != start for pid, start in observed.items())
    return structured_cancel and stopped and len(observed) >= 2 and identities_gone, len(observed)


def _run(runtime_root: Path, worker: Path, work_root: Path) -> dict[str, object]:
    manifest = fixtures.load_manifest()
    cpu_count = os.cpu_count() or 0
    memory_bytes = _memory_bytes()
    profile_ok = (
        platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and cpu_count >= 4
        and memory_bytes >= 16_000_000_000
    )
    with tempfile.TemporaryDirectory(prefix="visualworld-media-", dir=work_root) as temporary:
        source_root = Path(temporary) / "sources"
        fixtures.generate(source_root)
        runtime = MediaRuntime(runtime_root, worker)
        successes: dict[str, object] = {}
        all_checks = [profile_ok]
        for expected in _fixture_records(manifest):
            filename = cast(str, expected["filename"])
            adapter = LocalVideoSource(source_root, filename, runtime)
            source = adapter.probe()
            frames = adapter.read_frames(stream_index=0)
            expected_frames = cast(list[dict[str, object]], expected["frames"])
            checks = {
                "fingerprint": source.fingerprint.digest == expected["sha256"]
                and source.fingerprint.bytes == str(expected["byte_count"]),
                "dimensions": source.streams[0].width
                == cast(dict[str, int], expected["encoded_dimensions"])["width"]
                and source.streams[0].height
                == cast(dict[str, int], expected["encoded_dimensions"])["height"],
                "rotation": source.streams[0].rotation_degrees == expected["rotation_degrees"],
                "time_base": source.streams[0].time_base.to_mapping() == expected["time_base"],
                "stream_duration": adapter.details.duration is not None
                and adapter.details.duration.time_base.to_mapping() == expected["time_base"]
                and adapter.details.duration.value
                == str(sum(int(cast(str, frame["duration"])) for frame in expected_frames)),
                "pts": [frame.pts.value for frame in frames]
                == [cast(str, frame["pts"]) for frame in expected_frames],
                "durations": [
                    None if frame.duration is None else frame.duration.value for frame in frames
                ]
                == [cast(str, frame["duration"]) for frame in expected_frames],
                "pixels": list(adapter.details.pixel_hashes)
                == [cast(str, frame["rgb24_sha256"]) for frame in expected_frames],
                "codec": adapter.details.codec == expected["codec"],
                "frame_count": len(frames) == expected["frame_count"],
            }
            all_checks.extend(checks.values())
            successes[filename] = {
                "checks": checks,
                "metrics": {
                    "wall_ms": adapter.metrics.wall_ms,
                    "cpu_ms": adapter.metrics.cpu_ms,
                    "memory_peak_bytes": adapter.metrics.memory_peak_bytes,
                    "source_bytes": adapter.metrics.source_bytes,
                    "frame_count": adapter.metrics.frame_count,
                },
                "runtime": {
                    "pyav": adapter.details.pyav_version,
                    "libavformat": adapter.details.libavformat_version,
                    "libavcodec": adapter.details.libavcodec_version,
                },
            }

        (source_root / "corrupt.mov").write_bytes(b"not a video")
        (source_root / "oversize.mov").write_bytes(b"x" * 1025)
        (source_root / "linked.mov").symlink_to(source_root / "cfr.mov")
        cancelled = threading.Event()
        cancelled.set()
        failures = {
            "corrupt": _expect_error(
                lambda: LocalVideoSource(source_root, "corrupt.mov", runtime).probe(),
                PortErrorCode.DECODE_FAILED,
            ),
            "oversize": _expect_error(
                lambda: LocalVideoSource(
                    source_root,
                    "oversize.mov",
                    runtime,
                    limits=MediaLimits(max_source_bytes=1024),
                ).probe(),
                PortErrorCode.LIMIT_EXCEEDED,
            ),
            "timeout": _expect_error(
                lambda: LocalVideoSource(
                    source_root,
                    "cfr.mov",
                    runtime,
                    limits=MediaLimits(wall_timeout_ms=1),
                ).probe(),
                PortErrorCode.TIMEOUT,
            ),
            "cancellation": _expect_error(
                lambda: LocalVideoSource(
                    source_root,
                    "cfr.mov",
                    runtime,
                    cancelled=cancelled,
                ).probe(),
                PortErrorCode.CANCELLED,
            ),
            "traversal": _expect_error(
                lambda: LocalVideoSource(source_root, "../cfr.mov", runtime),
                PortErrorCode.INVALID_REQUEST,
            ),
            "symlink": _expect_error(
                lambda: LocalVideoSource(source_root, "linked.mov", runtime).probe(),
                PortErrorCode.INVALID_REQUEST,
            ),
        }
        all_checks.extend(failures.values())
        descendant_cleanup, descendant_count = _descendant_cleanup(runtime_root)
        all_checks.append(descendant_cleanup)
        return {
            "schema": "visualworld.media-acceptance-receipt",
            "schema_version": 1,
            "status": "pass" if all(all_checks) else "fail",
            "profile": {
                "name": "CPU-LITE",
                "os": platform.platform(),
                "machine": platform.machine(),
                "cpu_count": cpu_count,
                "memory_bytes": memory_bytes,
                "required_vcpu": 4,
                "meets_requirements": profile_ok,
                "required_memory_bytes": 16_000_000_000,
                "gpu_required": False,
            },
            "fixture_manifest_sha256": _sha256(fixtures.MANIFEST_PATH),
            "worker_sha256": _sha256(worker),
            "successful_fixtures": successes,
            "failure_and_security_cases": failures,
            "isolation": {
                "linux_x86_64_only": True,
                "non_root_worker_required": True,
                "no_new_privileges_required": True,
                "network_denial_required": True,
                "landlock_denial_required": True,
                "seccomp_filter_required": True,
                "whole_cgroup_kill_on_limit": descendant_cleanup,
                "descendant_processes_observed": descendant_count,
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = _run(
        arguments.runtime.resolve(strict=True),
        arguments.worker.resolve(strict=True),
        arguments.work_root.resolve(strict=True),
    )
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": os.fspath(arguments.output), "status": result["status"]}))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
