#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run issue #16's evidence-safe coordinator acceptance on CPU-LITE."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import sys
import tempfile
from pathlib import Path

from visualworld.coordinator import (
    CoordinatorEvent,
    IngestionConfig,
    IngestionCoordinator,
    IngestionDisposition,
    ManualEvidenceInput,
)
from visualworld.ingestion import (
    Fingerprint,
    FrameRef,
    MediaTime,
    Rational,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
)
from visualworld.ports import FakeFrameSampler, FakeVideoSource, PortError
from visualworld.storage import LocalEvidenceStore
from visualworld.world_store import DeletionState, LocalWorldStore

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
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if platform.system() == "Darwin" else value * 1024
    except (OSError, ValueError):
        return 0


def _disk_bytes(root: Path) -> int:
    total = 0
    for directory, _, names in os.walk(root):
        for name in names:
            metadata = (Path(directory) / name).stat(follow_symlinks=False)
            if metadata.st_mode & 0o170000 == 0o100000:
                total += metadata.st_size
    return total


def _run(work_root: Path) -> dict[str, object]:
    cpu_count = os.cpu_count() or 0
    memory_bytes = _memory_bytes()
    profile_ok = (
        platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and cpu_count >= 4
        and memory_bytes >= 16_000_000_000
    )
    width, height = 64, 36
    pixels = bytes(index % 251 for index in range(width * height * 3))
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(hashlib.sha256(b"coordinator-acceptance").hexdigest(), "0"),
        (SourceStream(0, width, height, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(
            source.source_id,
            0,
            str(index),
            MediaTime(str(index * 200), time_base),
        )
        for index in range(3)
    )
    video = FakeVideoSource(source, frames)
    sampler = FakeFrameSampler((frames[1].frame_id,))
    config = IngestionConfig(
        Sampling(Rational("5", "1")),
        max_frame_bytes=len(pixels),
    )
    manual = (
        ManualEvidenceInput(
            frames[1].frame_id,
            pixels,
            (8, 4, 56, 32),
        ),
    )
    events: list[CoordinatorEvent] = []

    with tempfile.TemporaryDirectory(prefix="visualworld-coordinator-", dir=work_root) as temp:
        root = Path(temp) / "store"
        evidence = LocalEvidenceStore(root, max_payload_bytes=len(pixels))
        world = LocalWorldStore(root)
        coordinator = IngestionCoordinator(evidence, world, event_sink=events.append)
        first = coordinator.ingest(video, sampler, config, manual)
        retry = coordinator.ingest(video, sampler, config, manual)
        crop = evidence.get(first.evidence[0].artifact.sha256)
        record_visible = world.get(first.evidence[0].evidence_id) == first.evidence[0]
        receipt = coordinator.delete_source(
            source.source_id,
            deletion_id="del_" + hashlib.sha256(b"acceptance-delete").hexdigest(),
        )
        artifact_absent = False
        try:
            evidence.get(first.evidence[0].artifact.sha256)
        except PortError:
            artifact_absent = True
        disk_bytes = _disk_bytes(root)

    stage_measurements: dict[str, dict[str, int]] = {}
    for event in events:
        entry = stage_measurements.setdefault(
            event.stage.value,
            {"calls": 0, "items": 0, "wall_ns": 0},
        )
        entry["calls"] += 1
        entry["items"] += event.item_count
        entry["wall_ns"] += event.duration_ns
    expected_crop_bytes = (56 - 8) * (32 - 4) * 3
    expected_crop = b"".join(
        pixels[(row * width + 8) * 3 : (row * width + 56) * 3] for row in range(4, 32)
    )
    checks = {
        "deterministic_manifest": first.manifest == retry.manifest,
        "idempotent_retry": retry.disposition is IngestionDisposition.ALREADY_COMMITTED,
        "evidence_record_visible": record_visible,
        "original_pixel_crop_retrievable": crop == expected_crop,
        "all_adapters_offline": video.descriptor.offline and sampler.descriptor.offline,
        "all_adapters_deterministic": (
            video.descriptor.deterministic and sampler.descriptor.deterministic
        ),
        "deletion_complete": receipt.state is DeletionState.COMPLETE,
        "deleted_artifact_absent": artifact_absent,
    }
    peak_rss = _peak_rss_bytes()
    resource_checks = {
        "timing_measured": bool(stage_measurements)
        and all(item["wall_ns"] > 0 for item in stage_measurements.values()),
        "rss_measured": peak_rss > 0,
        "rss_bounded": peak_rss <= 2 * 1024 * 1024 * 1024,
        "disk_bounded": disk_bytes <= 16 * 1024 * 1024,
    }
    passed = profile_ok and all(checks.values()) and all(resource_checks.values())
    return {
        "schema": "visualworld.coordinator-acceptance-receipt",
        "schema_version": 1,
        "status": "pass" if passed else "fail",
        "implementation": {
            "python_version": platform.python_version(),
            "coordinator_module_sha256": _sha256(ROOT / "src/visualworld/coordinator.py"),
            "world_store_module_sha256": _sha256(ROOT / "src/visualworld/world_store.py"),
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
            "candidate_count": len(frames),
            "sample_count": 1,
            "source_dimensions": [width, height],
            "source_rgb24_bytes": len(pixels),
            "crop_rgb24_bytes": expected_crop_bytes,
            "deterministic_synthetic_input": True,
        },
        "checks": checks,
        "resources": {
            **resource_checks,
            "process_peak_rss_bytes": peak_rss,
            "peak_rss_limit_bytes": 2 * 1024 * 1024 * 1024,
            "store_logical_bytes_after_delete": disk_bytes,
            "store_logical_limit_bytes": 16 * 1024 * 1024,
        },
        "measurements": stage_measurements,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = _run(arguments.work_root.resolve(strict=True))
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": result["status"]}, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
