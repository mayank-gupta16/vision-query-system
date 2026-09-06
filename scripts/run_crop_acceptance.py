#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run issue #13 geometry/crop acceptance and CPU-LITE copy baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import generate_synthetic_fixtures as fixtures

from visualworld.geometry import (
    CropError,
    DetectorTransform,
    Rgb24Crop,
    extract_rgb24_crop,
    write_rgb24_crop,
)

CROP_MANIFEST_PATH = fixtures.ROOT / "fixtures" / "synthetic-v1" / "crops.json"


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


def _display_box(
    source_box: tuple[int, int, int, int],
    width: int,
    height: int,
    rotation: int,
) -> tuple[int, int, int, int]:
    x_min, y_min, x_max, y_max = source_box
    if rotation == 0:
        return source_box
    if rotation == 90:
        return (height - y_max, x_min, height - y_min, x_max)
    if rotation == 180:
        return (width - x_max, height - y_max, width - x_min, height - y_min)
    if rotation == 270:
        return (y_min, width - x_max, y_max, width - x_min)
    raise ValueError("fixture rotation is not a supported quarter turn")


def _half_scale_box(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x_min, y_min, x_max, y_max = box
    return (x_min // 2, y_min // 2, (x_max + 1) // 2, (y_max + 1) // 2)


def _expect_crop_error(operation: Callable[[], object], code: str) -> bool:
    try:
        operation()
    except CropError as error:
        return (
            error.code == code
            and error.__cause__ is None
            and error.__suppress_context__
            and str(error) == f"{code} at crop"
        )
    return False


def _crop_manifest() -> dict[str, object]:
    try:
        raw: object = json.loads(CROP_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("unable to read crop golden manifest") from error
    if not isinstance(raw, dict) or set(raw) != {
        "crop_format",
        "crops",
        "schema",
        "schema_version",
        "source_fixture_manifest_sha256",
    }:
        raise ValueError("invalid crop golden manifest")
    result = cast(dict[str, object], raw)
    crops = result["crops"]
    expected_ids = {spec.fixture_id for spec in fixtures.SPECS}
    if (
        result["schema"] != "visualworld.synthetic-crop-goldens"
        or result["schema_version"] != 1
        or result["crop_format"] != "packed_rgb24_encoded_source"
        or result["source_fixture_manifest_sha256"] != _sha256(fixtures.MANIFEST_PATH)
        or not isinstance(crops, dict)
        or set(crops) != expected_ids
    ):
        raise ValueError("invalid crop golden manifest")
    for records in crops.values():
        if not isinstance(records, list) or len(records) != 4:
            raise ValueError("invalid crop golden manifest")
        for record in records:
            if not isinstance(record, dict) or set(record) != {"box_xyxy", "rgb24_sha256"}:
                raise ValueError("invalid crop golden manifest")
            box = record["box_xyxy"]
            digest = record["rgb24_sha256"]
            if (
                not isinstance(box, list)
                or len(box) != 4
                or any(type(value) is not int for value in box)
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("invalid crop golden manifest")
    return result


def _fixture_checks() -> tuple[dict[str, object], list[bool], Rgb24Crop]:
    manifest = fixtures.load_manifest()
    crop_manifest = _crop_manifest()
    expected_fixtures = {
        cast(str, item["fixture_id"]): item
        for item in cast(list[dict[str, object]], manifest["fixtures"])
    }
    crop_records = cast(dict[str, list[dict[str, object]]], crop_manifest["crops"])
    results: dict[str, object] = {}
    checks: list[bool] = []
    last_crop: Rgb24Crop | None = None
    for spec in fixtures.SPECS:
        expected = expected_fixtures[spec.fixture_id]
        expected_frames = cast(list[dict[str, object]], expected["frames"])
        _, frames = fixtures._movie(spec)
        rotation = spec.rotation_degrees % 360
        display_width = fixtures.HEIGHT if rotation in {90, 270} else fixtures.WIDTH
        display_height = fixtures.WIDTH if rotation in {90, 270} else fixtures.HEIGHT
        exact_transform = DetectorTransform(
            fixtures.WIDTH,
            fixtures.HEIGHT,
            display_width,
            display_height,
            rotation_degrees=rotation,
        )
        half_transform = DetectorTransform(
            fixtures.WIDTH,
            fixtures.HEIGHT,
            display_width // 2,
            display_height // 2,
            rotation_degrees=rotation,
        )
        hashes: list[str] = []
        max_error = 0
        exact = True
        for frame, expected_frame, expected_crop in zip(
            frames,
            expected_frames,
            crop_records[spec.fixture_id],
            strict=True,
        ):
            region = cast(dict[str, object], expected_frame["moving_region"])
            source_box = (
                cast(int, region["x"]),
                cast(int, region["y"]),
                cast(int, region["x"]) + cast(int, region["width"]),
                cast(int, region["y"]) + cast(int, region["height"]),
            )
            display_box = _display_box(
                source_box,
                fixtures.WIDTH,
                fixtures.HEIGHT,
                rotation,
            )
            geometry = exact_transform.map_box(display_box)
            crop = extract_rgb24_crop(frame, fixtures.WIDTH, fixtures.HEIGHT, geometry)
            hashes.append(crop.sha256)
            exact = (
                exact
                and geometry.box_xyxy == source_box
                and list(source_box) == expected_crop["box_xyxy"]
                and crop.sha256 == expected_crop["rgb24_sha256"]
            )
            half_geometry = half_transform.map_box(_half_scale_box(display_box))
            max_error = max(
                max_error,
                *(
                    abs(actual - wanted)
                    for actual, wanted in zip(
                        half_geometry.box_xyxy,
                        source_box,
                        strict=True,
                    )
                ),
            )
            last_crop = crop
        result = {
            "exact_source_boxes_and_pixel_hashes": exact,
            "crop_sha256": hashes,
            "half_scale_max_boundary_error_pixels": max_error,
            "within_one_source_pixel": max_error <= 1,
            "rotation_degrees": rotation,
        }
        checks.extend((exact, max_error <= 1))
        results[spec.fixture_id] = result
    if last_crop is None:
        raise ValueError("crop fixtures are incomplete")
    return results, checks, last_crop


def _destination_checks(work_root: Path, crop: Rgb24Crop) -> dict[str, bool]:
    with tempfile.TemporaryDirectory(prefix="visualworld-crop-", dir=work_root) as temporary:
        temporary_path = Path(temporary)
        root = temporary_path / "artifacts"
        destination_parent = root / "v1" / "sha256"
        destination_parent.mkdir(parents=True)
        destination = "v1/sha256/crop.rgb24"
        artifact = write_rgb24_crop(
            crop,
            artifact_root=root,
            relative_destination=destination,
        )
        stored = destination_parent / "crop.rgb24"
        outside = temporary_path / "outside.rgb24"
        escape = root / "escape"
        escape.symlink_to(temporary_path, target_is_directory=True)
        return {
            "exact_bytes": stored.read_bytes() == crop.pixels,
            "artifact_digest": artifact.sha256 == crop.sha256,
            "traversal_rejected": _expect_crop_error(
                lambda: write_rgb24_crop(
                    crop,
                    artifact_root=root,
                    relative_destination="../outside.rgb24",
                ),
                "invalid_destination",
            ),
            "symlink_parent_rejected": _expect_crop_error(
                lambda: write_rgb24_crop(
                    crop,
                    artifact_root=root,
                    relative_destination="escape/outside.rgb24",
                ),
                "destination_unavailable",
            ),
            "outside_absent": not outside.exists(),
        }


def _copy_benchmark() -> dict[str, object]:
    width, height = 1920, 1080
    detector_width, detector_height = 960, 540
    sampled_frames = 60 * 5
    frame_bytes = width * height * 3
    pattern = bytes(range(256))
    frame = (pattern * ((frame_bytes + len(pattern) - 1) // len(pattern)))[:frame_bytes]
    transform = DetectorTransform(width, height, detector_width, detector_height)
    detector_box = (160, 90, 800, 450)
    expected_source_box = (320, 180, 1600, 900)
    warmup = extract_rgb24_crop(frame, width, height, transform.map_box(detector_box))
    aggregate = hashlib.sha256()
    started_wall = time.perf_counter_ns()
    started_cpu = time.process_time_ns()
    exact_geometry = True
    copied_bytes = 0
    for _ in range(sampled_frames):
        geometry = transform.map_box(detector_box)
        crop = extract_rgb24_crop(frame, width, height, geometry)
        exact_geometry = exact_geometry and geometry.box_xyxy == expected_source_box
        aggregate.update(bytes.fromhex(crop.sha256))
        copied_bytes += len(crop.pixels)
    cpu_ns = max(0, time.process_time_ns() - started_cpu)
    wall_ns = max(1, time.perf_counter_ns() - started_wall)
    peak_rss = _peak_rss_bytes()
    return {
        "source_dimensions": {"width": width, "height": height},
        "detector_dimensions": {"width": detector_width, "height": detector_height},
        "detector_box_xyxy": list(detector_box),
        "source_box_xyxy": list(expected_source_box),
        "video_seconds": 60,
        "sampling_fps": 5,
        "sampled_frames": sampled_frames,
        "crop_dimensions": {"width": warmup.width, "height": warmup.height},
        "copied_bytes": copied_bytes,
        "workload": "exact_map_copy_and_sha256",
        "aggregate_sha256": aggregate.hexdigest(),
        "wall_ns": wall_ns,
        "cpu_ns": cpu_ns,
        "frames_per_second": sampled_frames * 1_000_000_000 // wall_ns,
        "bytes_per_second": copied_bytes * 1_000_000_000 // wall_ns,
        "process_peak_rss_bytes": peak_rss,
        "peak_rss_limit_bytes": 2 * 1024 * 1024 * 1024,
        "exact_geometry": exact_geometry,
        "bounded": peak_rss <= 2 * 1024 * 1024 * 1024,
    }


def _run(work_root: Path) -> dict[str, object]:
    cpu_count = os.cpu_count() or 0
    memory_bytes = _memory_bytes()
    profile_ok = (
        platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and cpu_count >= 4
        and memory_bytes >= 16_000_000_000
    )
    fixture_results, checks, last_crop = _fixture_checks()
    destination = _destination_checks(work_root, last_crop)
    benchmark = _copy_benchmark()
    checks.extend(destination.values())
    checks.extend((cast(bool, benchmark["exact_geometry"]), cast(bool, benchmark["bounded"])))
    checks.append(profile_ok)
    return {
        "schema": "visualworld.crop-acceptance-receipt",
        "schema_version": 1,
        "status": "pass" if all(checks) else "fail",
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
        "crop_manifest_sha256": _sha256(CROP_MANIFEST_PATH),
        "policy": {
            "coordinates": "half_open_integer",
            "rounding": "minimum_floor_maximum_ceil",
            "clamping": "encoded_source_bounds",
            "rotation": "clockwise_display_quarter_turn_to_encoded_source",
            "crop_orientation": "encoded_source",
        },
        "fixtures": fixture_results,
        "destination_security": destination,
        "benchmark": benchmark,
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
    print(json.dumps({"output": os.fspath(arguments.output), "status": result["status"]}))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
