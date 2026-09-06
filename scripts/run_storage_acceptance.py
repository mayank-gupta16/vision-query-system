#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run issue #14 local EvidenceStore acceptance on the CPU-LITE host."""

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

from visualworld.ingestion import Artifact
from visualworld.storage import (
    ArtifactState,
    CommitDisposition,
    DeleteDisposition,
    InventoryKind,
    LocalEvidenceStore,
)

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
    for directory, _, names in os.walk(root):
        for name in names:
            metadata = (Path(directory) / name).stat(follow_symlinks=False)
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


def _throughput(payload_bytes: int, wall_ns: int) -> int:
    return payload_bytes * 1_000_000_000 // max(1, wall_ns)


def _run(work_root: Path, payload_bytes: int) -> dict[str, object]:
    cpu_count = os.cpu_count() or 0
    memory_bytes = _memory_bytes()
    profile_ok = (
        platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and cpu_count >= 4
        and memory_bytes >= 16_000_000_000
    )
    pattern = bytes(range(256))
    content = (pattern * ((payload_bytes + 255) // 256))[:payload_bytes]

    with tempfile.TemporaryDirectory(prefix="visualworld-storage-", dir=work_root) as temporary:
        root = Path(temporary) / "store"
        store = LocalEvidenceStore(root, max_payload_bytes=payload_bytes)

        digest_result, hash_wall_ns, hash_cpu_ns = _measure(
            lambda: hashlib.sha256(content).hexdigest()
        )
        if not isinstance(digest_result, str):
            raise ValueError("hash result unavailable")
        artifact = Artifact(digest_result, str(payload_bytes))
        _, unique_wall_ns, unique_cpu_ns = _measure(lambda: store.put(artifact, content))
        unique_files, unique_logical, unique_allocated = _disk_usage(root)
        _, read_wall_ns, read_cpu_ns = _measure(lambda: store.get(artifact.sha256))
        _, dedupe_wall_ns, dedupe_cpu_ns = _measure(lambda: store.put(artifact, content))
        dedupe_files, dedupe_logical, dedupe_allocated = _disk_usage(root)

        second_content = bytes(reversed(content))
        second = Artifact(hashlib.sha256(second_content).hexdigest(), str(payload_bytes))
        staged_result, stage_wall_ns, stage_cpu_ns = _measure(
            lambda: store.stage("run_" + "1" * 64, second, second_content)
        )
        if not hasattr(staged_result, "artifact"):
            raise ValueError("stage result unavailable")
        commit_result, commit_wall_ns, commit_cpu_ns = _measure(
            lambda: store.commit_stage(staged_result)
        )
        audit_result, audit_wall_ns, audit_cpu_ns = _measure(
            lambda: (
                store.inspect((artifact, second)),
                store.inventory(),
            )
        )
        delete_result, delete_wall_ns, delete_cpu_ns = _measure(
            lambda: store.delete_artifact(second)
        )
        after_delete_files, after_delete_logical, after_delete_allocated = _disk_usage(root)

        checks = {
            "hash_matches": digest_result == hashlib.sha256(content).hexdigest(),
            "read_matches": store.get(artifact.sha256) == content,
            "dedupe_disk_stable": (dedupe_files, dedupe_logical, dedupe_allocated)
            == (unique_files, unique_logical, unique_allocated),
            "stage_commit_promoted": getattr(commit_result, "disposition", None)
            is CommitDisposition.PROMOTED,
            "audit_valid": all(check.state is ArtifactState.VALID for check in audit_result[0]),
            "audit_two_artifacts": sum(
                entry.kind is InventoryKind.ARTIFACT for entry in audit_result[1].entries
            )
            == 2,
            "delete_completed": delete_result is DeleteDisposition.DELETED,
            "delete_disk_restored": (
                after_delete_files,
                after_delete_logical,
                after_delete_allocated,
            )
            == (dedupe_files, dedupe_logical, dedupe_allocated),
        }
        peak_rss = _peak_rss_bytes()
        resource_checks = {
            "timing_measured": all(
                value > 0
                for value in (
                    hash_wall_ns,
                    unique_wall_ns,
                    read_wall_ns,
                    dedupe_wall_ns,
                    stage_wall_ns,
                    commit_wall_ns,
                    audit_wall_ns,
                    delete_wall_ns,
                )
            ),
            "rss_measured": peak_rss > 0,
            "bounded": peak_rss <= 2 * 1024 * 1024 * 1024,
        }
        passed = profile_ok and all(checks.values()) and all(resource_checks.values())
        return {
            "schema": "visualworld.storage-acceptance-receipt",
            "schema_version": 1,
            "status": "pass" if passed else "fail",
            "implementation": {
                "python_version": platform.python_version(),
                "storage_module_sha256": _sha256(ROOT / "src/visualworld/storage.py"),
                "harness_sha256": _sha256(Path(__file__)),
                "executable_implementation": sys.implementation.name,
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
                "payload_bytes": payload_bytes,
                "deterministic_synthetic_bytes": True,
                "artifact_count": 2,
            },
            "checks": checks,
            "resources": {
                **resource_checks,
                "process_peak_rss_bytes": peak_rss,
                "peak_rss_limit_bytes": 2 * 1024 * 1024 * 1024,
            },
            "measurements": {
                "hash": {
                    "wall_ns": hash_wall_ns,
                    "cpu_ns": hash_cpu_ns,
                    "bytes_per_second": _throughput(payload_bytes, hash_wall_ns),
                },
                "unique_put": {
                    "wall_ns": unique_wall_ns,
                    "cpu_ns": unique_cpu_ns,
                    "bytes_per_second": _throughput(payload_bytes, unique_wall_ns),
                    "store_file_count": unique_files,
                    "store_logical_bytes": unique_logical,
                    "store_allocated_bytes": unique_allocated,
                },
                "read_verify": {
                    "wall_ns": read_wall_ns,
                    "cpu_ns": read_cpu_ns,
                    "bytes_per_second": _throughput(payload_bytes, read_wall_ns),
                },
                "deduplicated_put": {
                    "wall_ns": dedupe_wall_ns,
                    "cpu_ns": dedupe_cpu_ns,
                    "bytes_per_second": _throughput(payload_bytes, dedupe_wall_ns),
                    "store_file_count": dedupe_files,
                    "store_logical_bytes": dedupe_logical,
                    "store_allocated_bytes": dedupe_allocated,
                },
                "stage": {
                    "wall_ns": stage_wall_ns,
                    "cpu_ns": stage_cpu_ns,
                    "bytes_per_second": _throughput(payload_bytes, stage_wall_ns),
                },
                "commit_rename": {"wall_ns": commit_wall_ns, "cpu_ns": commit_cpu_ns},
                "audit": {"wall_ns": audit_wall_ns, "cpu_ns": audit_cpu_ns},
                "delete": {
                    "wall_ns": delete_wall_ns,
                    "cpu_ns": delete_cpu_ns,
                    "remaining_file_count": after_delete_files,
                    "remaining_logical_bytes": after_delete_logical,
                    "remaining_allocated_bytes": after_delete_allocated,
                },
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--payload-bytes", type=int, default=16 * 1024 * 1024)
    arguments = parser.parse_args()
    if not 1 <= arguments.payload_bytes <= 256 * 1024 * 1024:
        parser.error("--payload-bytes must be between 1 and 268435456")
    result = _run(arguments.work_root.resolve(strict=True), arguments.payload_bytes)
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": result["status"]}, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
