#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run issue #12 exact-PTS sampling acceptance on the CPU-LITE host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import generate_synthetic_fixtures as fixtures

from visualworld.ingestion import FrameRef, MediaTime, Rational, Sampling, Source
from visualworld.media import LocalVideoSource, MediaRuntime
from visualworld.ports import PortError, PortErrorCode
from visualworld.sampling import PtsFrameSampler, SamplingCursor, SamplingLimits


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


def _peak_rss_bytes() -> int:
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmHWM:"):
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


def _resumed(
    source: Source,
    candidates: tuple[FrameRef, ...],
    sampling: Sampling,
) -> tuple[FrameRef, ...]:
    selected: list[FrameRef] = []
    cursor: SamplingCursor | None = None
    sampler = PtsFrameSampler()
    split = max(1, len(candidates) // 2)
    pages = (candidates[:split], candidates[split - 1 :])
    for index, page_candidates in enumerate(pages):
        page = sampler.sample_page(
            source,
            page_candidates,
            sampling,
            cursor=cursor,
            end_of_stream=index == len(pages) - 1,
        )
        selected.extend(page.frames)
        cursor = page.cursor
    return tuple(selected)


def _run(runtime_root: Path, worker: Path, work_root: Path) -> dict[str, object]:
    manifest = fixtures.load_manifest()
    fixture_records = cast(list[dict[str, object]], manifest["fixtures"])
    cpu_count = os.cpu_count() or 0
    memory_bytes = _memory_bytes()
    profile_ok = (
        platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and cpu_count >= 4
        and memory_bytes >= 16_000_000_000
    )
    sampling = Sampling(Rational("5", "1"))
    with tempfile.TemporaryDirectory(prefix="visualworld-sampling-", dir=work_root) as temporary:
        source_root = Path(temporary) / "sources"
        fixtures.generate(source_root)
        runtime = MediaRuntime(runtime_root, worker)
        successes: dict[str, object] = {}
        all_checks = [profile_ok]
        first_source = None
        first_candidates: tuple[FrameRef, ...] = ()
        for expected in fixture_records:
            filename = cast(str, expected["filename"])
            adapter = LocalVideoSource(source_root, filename, runtime)
            source = adapter.probe()
            candidates = adapter.read_frames(stream_index=0)
            sampler = PtsFrameSampler()
            selected = sampler.sample(source, candidates, sampling)
            repeated = sampler.sample(source, candidates, sampling)
            resumed = _resumed(source, candidates, sampling)
            expected_pts = [
                cast(str, frame["pts"])
                for frame in cast(list[dict[str, object]], expected["frames"])
            ]
            checks = {
                "exact_pts": [frame.pts.value for frame in selected] == expected_pts,
                "exact_records": selected == candidates,
                "repeat_ids": [frame.frame_id for frame in selected]
                == [frame.frame_id for frame in repeated],
                "resume_ids": [frame.frame_id for frame in selected]
                == [frame.frame_id for frame in resumed],
                "bounded_output": len(selected) <= len(candidates) <= 64,
            }
            all_checks.extend(checks.values())
            successes[filename] = {"checks": checks, "selected_pts": expected_pts}
            if first_source is None:
                first_source = source
                first_candidates = candidates

        if first_source is None or len(first_candidates) < 2:
            raise ValueError("sampling fixtures are incomplete")
        cancellation = threading.Event()
        initial = PtsFrameSampler().sample_page(
            first_source,
            first_candidates[:2],
            sampling,
        )
        if initial.cursor is None:
            raise ValueError("sampling cursor is missing")
        cancellation.set()
        failure_checks = {
            "cancellation": _expect_error(
                lambda: PtsFrameSampler().sample_page(
                    first_source,
                    first_candidates[1:],
                    sampling,
                    cursor=initial.cursor,
                    end_of_stream=True,
                    cancelled=cancellation,
                ),
                PortErrorCode.CANCELLED,
            ),
            "page_cap": _expect_error(
                lambda: PtsFrameSampler(limits=SamplingLimits(max_page_candidates=3)).sample(
                    first_source, first_candidates, sampling
                ),
                PortErrorCode.LIMIT_EXCEEDED,
            ),
            "duration_cap": _expect_error(
                lambda: PtsFrameSampler(limits=SamplingLimits(max_duration_seconds=1)).sample(
                    first_source,
                    (
                        first_candidates[0],
                        FrameRef.create(
                            first_source.source_id,
                            0,
                            "100",
                            MediaTime("1001", first_source.streams[0].time_base),
                        ),
                    ),
                    sampling,
                ),
                PortErrorCode.LIMIT_EXCEEDED,
            ),
        }
        all_checks.extend(failure_checks.values())

        benchmark_candidates = tuple(
            FrameRef.create(
                first_source.source_id,
                0,
                str(index),
                MediaTime(str(index * 33), first_source.streams[0].time_base),
                MediaTime("33", first_source.streams[0].time_base) if index == 63 else None,
            )
            for index in range(64)
        )
        started_wall = time.perf_counter_ns()
        started_cpu = time.process_time_ns()
        benchmark_selected = PtsFrameSampler().sample(
            first_source,
            benchmark_candidates,
            sampling,
        )
        cpu_ns = max(0, time.process_time_ns() - started_cpu)
        wall_ns = max(1, time.perf_counter_ns() - started_wall)
        benchmark_ok = len(benchmark_selected) > 0 and len(benchmark_selected) <= 64
        all_checks.append(benchmark_ok)
        return {
            "schema": "visualworld.sampling-acceptance-receipt",
            "schema_version": 1,
            "status": "pass" if all(all_checks) else "fail",
            "profile": {
                "name": "CPU-LITE",
                "os": platform.platform(),
                "machine": platform.machine(),
                "cpu_count": cpu_count,
                "memory_bytes": memory_bytes,
                "required_vcpu": 4,
                "required_memory_bytes": 16_000_000_000,
                "meets_requirements": profile_ok,
                "gpu_required": False,
            },
            "fixture_manifest_sha256": _sha256(fixtures.MANIFEST_PATH),
            "sampling": sampling.to_mapping(),
            "successful_fixtures": successes,
            "failure_cases": failure_checks,
            "benchmark": {
                "candidate_count": len(benchmark_candidates),
                "selected_count": len(benchmark_selected),
                "wall_ns": wall_ns,
                "cpu_ns": cpu_ns,
                "candidates_per_second": len(benchmark_candidates) * 1_000_000_000 // wall_ns,
                "process_peak_rss_bytes": _peak_rss_bytes(),
                "bounded": benchmark_ok,
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
