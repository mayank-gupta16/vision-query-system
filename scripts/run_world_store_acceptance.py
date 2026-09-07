#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run issue #15 local WorldStore acceptance on the CPU-LITE host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from visualworld.ingestion import (
    Artifact,
    EvidenceRef,
    Fingerprint,
    FrameRef,
    MediaTime,
    Rational,
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
)
from visualworld.storage import StageHandle
from visualworld.world_store import LocalWorldStore, WorldStoreStats

ROOT = Path(__file__).resolve().parents[1]


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
        reported = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return reported if platform.system() == "Darwin" else reported * 1024
    except (OSError, ValueError):
        pass
    return 0


def _disk_usage(root: Path) -> tuple[int, int, int]:
    files = 0
    logical = 0
    allocated = 0
    for entry in root.iterdir():
        metadata = entry.stat(follow_symlinks=False)
        if metadata.st_mode & 0o170000 != 0o100000:
            continue
        files += 1
        logical += metadata.st_size
        allocated += metadata.st_blocks * 512
    return files, logical, allocated


def _measure[T](operation: Callable[[], T]) -> tuple[T, int, int]:
    started_wall = time.perf_counter_ns()
    started_cpu = time.process_time_ns()
    result = operation()
    return (
        result,
        max(1, time.perf_counter_ns() - started_wall),
        max(1, time.process_time_ns() - started_cpu),
    )


def _batches[T](values: tuple[T, ...], size: int = 60) -> tuple[tuple[T, ...], ...]:
    return tuple(values[index : index + size] for index in range(0, len(values), size))


def _run(work_root: Path, record_count: int) -> dict[str, object]:
    cpu_count = os.cpu_count() or 0
    memory_bytes = _memory_bytes()
    profile_ok = (
        platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and cpu_count >= 4
        and memory_bytes >= 16_000_000_000
    )
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(hashlib.sha256(b"world-store-source").hexdigest(), "0"),
        (SourceStream(0, 1920, 1080, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(
            source.source_id,
            0,
            str(index),
            MediaTime(str(index * 200), time_base),
        )
        for index in range(record_count)
    )
    evidence = tuple(
        EvidenceRef.create(
            frame.frame_id,
            Artifact(
                hashlib.sha256(f"synthetic-artifact-{index}".encode("ascii")).hexdigest(),
                str(1920 * 1080 * 3),
            ),
            None,
        )
        for index, frame in enumerate(frames)
    )
    sampling = Sampling(Rational("5", "1"))
    preparing = RunManifest.create(source.source_id, (), sampling, "preparing")
    committed = RunManifest.create(
        source.source_id,
        (),
        sampling,
        "committed",
        RunOutputs(
            str(record_count),
            hashlib.sha256(b"world-store-sample-index").hexdigest(),
        ),
    )
    stages = tuple(
        StageHandle(preparing.run_id, f"{index:032x}.part", item.artifact)
        for index, item in enumerate(evidence)
    )

    with tempfile.TemporaryDirectory(prefix="visualworld-world-store-", dir=work_root) as temporary:
        root = Path(temporary) / "store"
        store_result, initialize_wall_ns, initialize_cpu_ns = _measure(
            lambda: LocalWorldStore(root, max_audit_records=2 + 2 * record_count)
        )
        if not isinstance(store_result, LocalWorldStore):
            raise ValueError("store initialization unavailable")
        store = store_result
        _, prepare_wall_ns, prepare_cpu_ns = _measure(lambda: store.commit((source, preparing)))

        def write_intents() -> None:
            for batch in _batches(stages):
                store.record_artifact_intents(preparing.run_id, batch)

        _, intents_wall_ns, intents_cpu_ns = _measure(write_intents)

        def write_records() -> None:
            for frame_batch in _batches(frames):
                store.commit_for_run(preparing.run_id, frame_batch)
            for evidence_batch in _batches(evidence):
                store.commit_for_run(preparing.run_id, evidence_batch)

        _, records_wall_ns, records_cpu_ns = _measure(write_records)
        _, finalize_wall_ns, finalize_cpu_ns = _measure(lambda: store.finalize_run(committed))
        _, retry_wall_ns, retry_cpu_ns = _measure(lambda: store.finalize_run(committed))

        def list_all_frames() -> tuple[FrameRef, ...]:
            result: list[FrameRef] = []
            after: str | None = None
            while True:
                page = store.list_frames(
                    source.source_id,
                    stream_index=0,
                    after_decode_index=after,
                    limit=64,
                )
                result.extend(page)
                if len(page) < 64:
                    return tuple(result)
                after = page[-1].decode_index

        listed, frame_query_wall_ns, frame_query_cpu_ns = _measure(list_all_frames)
        evidence_counts, evidence_query_wall_ns, evidence_query_cpu_ns = _measure(
            lambda: tuple(len(store.list_evidence(frame.frame_id, limit=1)) for frame in frames)
        )
        reopened_result, reopen_wall_ns, reopen_cpu_ns = _measure(
            lambda: LocalWorldStore(root, max_audit_records=2 + 2 * record_count)
        )
        if not isinstance(reopened_result, LocalWorldStore):
            raise ValueError("store reopen unavailable")
        statistics, verify_wall_ns, verify_cpu_ns = _measure(reopened_result.verify)
        files, logical_bytes, allocated_bytes = _disk_usage(root)
        peak_rss = _peak_rss_bytes()

        checks = {
            "frame_count_matches": len(listed) == record_count,
            "exact_pts_match": all(
                frame.pts.value == str(index * 200) for index, frame in enumerate(listed)
            ),
            "evidence_index_matches": evidence_counts == (1,) * record_count,
            "final_manifest_matches": reopened_result.get(committed.run_id) == committed,
            "intents_cleared": reopened_result.list_artifact_intents(committed.run_id) == (),
            "aggregate_counts_match": statistics
            == WorldStoreStats(
                record_count=2 + 2 * record_count,
                artifact_count=record_count,
                intent_count=0,
            ),
            "reopen_preserves_records": reopened_result.get(source.source_id) == source,
        }
        timing_values = (
            initialize_wall_ns,
            prepare_wall_ns,
            intents_wall_ns,
            records_wall_ns,
            finalize_wall_ns,
            retry_wall_ns,
            frame_query_wall_ns,
            evidence_query_wall_ns,
            reopen_wall_ns,
            verify_wall_ns,
        )
        resource_checks = {
            "timing_measured": all(value > 0 for value in timing_values),
            "rss_measured": peak_rss > 0,
            "rss_bounded": peak_rss <= 2 * 1024 * 1024 * 1024,
            "disk_bounded": logical_bytes <= 16 * 1024 * 1024,
        }
        passed = profile_ok and all(checks.values()) and all(resource_checks.values())
        return {
            "schema": "visualworld.world-store-acceptance-receipt",
            "schema_version": 1,
            "status": "pass" if passed else "fail",
            "implementation": {
                "python_version": platform.python_version(),
                "world_store_module_sha256": _sha256(ROOT / "src/visualworld/world_store.py"),
                "storage_module_sha256": _sha256(ROOT / "src/visualworld/storage.py"),
                "harness_sha256": _sha256(Path(__file__)),
                "executable_implementation": sys.implementation.name,
                "sqlite_version": sqlite_version(),
            },
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
            "workload": {
                "source_count": 1,
                "run_count": 1,
                "frame_count": record_count,
                "evidence_count": record_count,
                "target_fps": 5,
                "source_seconds": 60,
                "source_dimensions": [1920, 1080],
                "deterministic_synthetic_records": True,
            },
            "checks": checks,
            "resources": {
                **resource_checks,
                "process_peak_rss_bytes": peak_rss,
                "peak_rss_limit_bytes": 2 * 1024 * 1024 * 1024,
                "database_file_count": files,
                "database_logical_bytes": logical_bytes,
                "database_allocated_bytes": allocated_bytes,
                "database_logical_limit_bytes": 16 * 1024 * 1024,
            },
            "measurements": {
                "initialize_migration": {
                    "wall_ns": initialize_wall_ns,
                    "cpu_ns": initialize_cpu_ns,
                },
                "prepare_transaction": {
                    "wall_ns": prepare_wall_ns,
                    "cpu_ns": prepare_cpu_ns,
                },
                "intent_transactions": {
                    "wall_ns": intents_wall_ns,
                    "cpu_ns": intents_cpu_ns,
                    "transaction_count": len(_batches(stages)),
                },
                "record_transactions": {
                    "wall_ns": records_wall_ns,
                    "cpu_ns": records_cpu_ns,
                    "transaction_count": len(_batches(frames)) + len(_batches(evidence)),
                },
                "finalize_transaction": {
                    "wall_ns": finalize_wall_ns,
                    "cpu_ns": finalize_cpu_ns,
                },
                "idempotent_retry": {"wall_ns": retry_wall_ns, "cpu_ns": retry_cpu_ns},
                "frame_index_scan": {
                    "wall_ns": frame_query_wall_ns,
                    "cpu_ns": frame_query_cpu_ns,
                    "page_count": (record_count + 63) // 64,
                },
                "evidence_index_lookups": {
                    "wall_ns": evidence_query_wall_ns,
                    "cpu_ns": evidence_query_cpu_ns,
                    "lookup_count": record_count,
                },
                "reopen": {"wall_ns": reopen_wall_ns, "cpu_ns": reopen_cpu_ns},
                "verify": {"wall_ns": verify_wall_ns, "cpu_ns": verify_cpu_ns},
            },
        }


def sqlite_version() -> str:
    import sqlite3

    return sqlite3.sqlite_version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--record-count", type=int, default=300)
    arguments = parser.parse_args()
    if not 1 <= arguments.record_count <= 10_000:
        parser.error("--record-count must be between 1 and 10000")
    result = _run(arguments.work_root.resolve(strict=True), arguments.record_count)
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": result["status"]}, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
