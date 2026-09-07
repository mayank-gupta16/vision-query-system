#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Measure original-resolution crop value on the locked v0.2 research clips."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import resource
import struct
import sys
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import date
from pathlib import Path, PurePosixPath
from typing import NoReturn, TextIO, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT / "src"))

import evaluate_v02_gates as gate_evaluator  # noqa: E402
import prepare_v02_crop_dataset as crop_prep  # noqa: E402
import prepare_v02_detection_dataset as detection_prep  # noqa: E402
import run_v02_detection_benchmark as detection  # noqa: E402

from visualworld.geometry import DetectorTransform, extract_rgb24_crop  # noqa: E402

FIXTURE_ROOT = ROOT / "fixtures" / "v02-crop-research"
ANNOTATIONS_PATH = FIXTURE_ROOT / "annotations.json"
CANDIDATES_PATH = FIXTURE_ROOT / "candidates.json"
DATASET_MANIFEST_PATH = FIXTURE_ROOT / "dataset-manifest.json"
SOURCE_MANIFEST_PATH = FIXTURE_ROOT / "source-manifest.json"
DETECTION_CANDIDATES_PATH = ROOT / "fixtures" / "v02-detection-research" / "candidates.json"
DETECTION_ANNOTATIONS_PATH = ROOT / "fixtures" / "v02-detection-research" / "annotations.json"
DETECTION_SOURCE_MANIFEST_PATH = (
    ROOT / "fixtures" / "v02-detection-research" / "source-manifest.json"
)
GEOMETRY_PATH = ROOT / "src" / "visualworld" / "geometry.py"
POLICY_PATH = ROOT / "fixtures" / "v02-evaluation" / "policy-v0.2-gates-2.json"
SOURCE_WIDTH = crop_prep.SOURCE_WIDTH
SOURCE_HEIGHT = crop_prep.SOURCE_HEIGHT
DETECTOR_WIDTH = crop_prep.DETECTOR_WIDTH
DETECTOR_HEIGHT = crop_prep.DETECTOR_HEIGHT
SOURCE_FRAME_BYTES = SOURCE_WIDTH * SOURCE_HEIGHT * 3
DETECTOR_FRAME_BYTES = DETECTOR_WIDTH * DETECTOR_HEIGHT * 3
MAX_SOURCE_CLIP_BYTES = 80 * 1024 * 1024
MAX_DETECTOR_CLIP_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
SEEDS = (1729, 3253, 5081, 7919, 104729)
STRATA = ("overall", "tiny", "small", "medium")
CONTROLS = ("source_resolvable", "source_absent")
PATHS = ("original", "detector")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")


class CropBenchmarkError(RuntimeError):
    """A stable crop benchmark failure that does not expose caller data."""


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise CropBenchmarkError("invalid_arguments")

    def _print_message(self, message: str, file: object | None = None) -> None:
        if message:
            _write_text(message, sys.stderr if file is None else cast(TextIO, file))


def _fail(code: str) -> NoReturn:
    raise CropBenchmarkError(code)


def _silence_stream(stream: TextIO) -> None:
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return
    try:
        null_descriptor = os.open(os.devnull, os.O_WRONLY | os.O_CLOEXEC)
    except OSError:
        return
    if null_descriptor == descriptor:
        return
    try:
        os.dup2(null_descriptor, descriptor)
    except OSError:
        pass
    finally:
        with suppress(OSError):
            os.close(null_descriptor)


def _write_text(value: str, stream: TextIO) -> bool:
    try:
        stream.write(value)
        stream.flush()
    except (AttributeError, OSError, UnicodeError, ValueError):
        _silence_stream(stream)
        return False
    return True


def _emit(value: object, stream: TextIO) -> bool:
    try:
        serialized = json.dumps(value, allow_nan=False, sort_keys=True) + "\n"
    except (TypeError, UnicodeError, ValueError):
        _silence_stream(stream)
        return False
    return _write_text(serialized, stream)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, UnicodeError, ValueError) as error:
        raise CropBenchmarkError("invalid_json") from error


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_json(path: Path, code: str) -> tuple[dict[str, object], str]:
    try:
        raw = detection._read_regular(path, MAX_JSON_BYTES, code)
        value = detection._decode_json(raw)
    except (detection.BenchmarkError, UnicodeError, ValueError) as error:
        raise CropBenchmarkError(code) from error
    if not isinstance(value, dict):
        _fail(code)
    return cast(dict[str, object], value), _sha256(raw)


def _mapping(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        _fail(code)
    return cast(dict[str, object], value)


def _integer(value: object, code: str, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(code)
    return value


def _text(value: object, code: str, maximum: int = 256) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or not value.isascii()
        or any(ord(character) < 0x20 for character in value)
    ):
        _fail(code)
    return value


def _digest(value: object, code: str) -> str:
    selected = _text(value, code, 64)
    if re.fullmatch(r"[0-9a-f]{64}", selected) is None:
        _fail(code)
    return selected


def _box(value: object, width: int, height: int, code: str) -> tuple[int, int, int, int]:
    try:
        return detection_prep._box(value, width, height)
    except detection_prep.PreparationError as error:
        raise CropBenchmarkError(code) from error


def _split_payload(
    annotations: dict[str, object], split: str
) -> tuple[list[dict[str, object]], list[dict[str, object]], bytes, bytes]:
    clips_value = annotations.get("clips")
    items_value = annotations.get("items")
    if not isinstance(clips_value, list) or not isinstance(items_value, list):
        _fail("invalid_annotations")
    clips = [
        cast(dict[str, object], clip)
        for clip in clips_value
        if isinstance(clip, dict) and clip.get("split") == split
    ]
    items = [
        cast(dict[str, object], item)
        for item in items_value
        if isinstance(item, dict) and item.get("split") == split
    ]
    if not clips or not items:
        _fail("invalid_annotations")
    return (
        clips,
        items,
        _canonical({"clips": clips, "items": items}),
        _canonical([item["item_id"] for item in items]),
    )


def _validate_annotations(
    annotations: dict[str, object],
    annotation_sha256: str,
    source_manifest_sha256: str,
    candidate_manifest_sha256: str,
    dataset_manifest: dict[str, object],
) -> None:
    if (
        set(annotations)
        != {
            "candidate_manifest_sha256",
            "clips",
            "items",
            "schema",
            "schema_version",
            "source_manifest_sha256",
        }
        or annotations.get("schema") != "visualworld.v02-crop-annotation-lock"
        or annotations.get("schema_version") != 1
        or annotations.get("source_manifest_sha256") != source_manifest_sha256
        or annotations.get("candidate_manifest_sha256") != candidate_manifest_sha256
        or not re.fullmatch(r"[0-9a-f]{64}", annotation_sha256)
    ):
        _fail("invalid_annotations")
    clip_fields = {
        "clip_id",
        "detector_byte_count",
        "detector_relative_path",
        "detector_sha256",
        "source_byte_count",
        "source_id",
        "source_relative_path",
        "source_sha256",
        "split",
    }
    item_fields = {
        "absent_detector_panel_box",
        "absent_source_panel_box",
        "clip_id",
        "detector_box",
        "detector_crop_sha256",
        "detector_frame_sha256",
        "frame_index",
        "item_id",
        "labels",
        "original_crop_bytes",
        "original_crop_sha256",
        "resolved_detector_panel_box",
        "resolved_source_panel_box",
        "source_box",
        "source_frame_sha256",
        "source_id",
        "split",
        "stratum",
    }
    split_sources: dict[str, set[str]] = {}
    split_manifest = _mapping(dataset_manifest["splits"], "invalid_dataset")
    all_clip_ids: set[str] = set()
    all_item_ids: set[str] = set()
    for split in ("calibration", "test"):
        clips, items, payload, ids = _split_payload(annotations, split)
        expected = _mapping(split_manifest[split], "invalid_dataset")
        if (
            len(items) != expected["item_count"]
            or _sha256(payload) != expected["annotation_sha256"]
            or _sha256(ids) != expected["item_ids_sha256"]
        ):
            _fail("dataset_lock_mismatch")
        clip_ids: set[str] = set()
        sources: set[str] = set()
        for clip in clips:
            if set(clip) != clip_fields:
                _fail("invalid_annotations")
            clip_id = _text(clip["clip_id"], "invalid_annotations", 96)
            source_id = _text(clip["source_id"], "invalid_annotations", 64)
            if (
                clip_id in all_clip_ids
                or source_id in sources
                or clip["split"] != split
                or clip_id != f"{split}-{source_id}"
            ):
                _fail("invalid_annotations")
            for key in ("source_relative_path", "detector_relative_path"):
                relative = PurePosixPath(_text(clip[key], "invalid_annotations", 256))
                if relative.is_absolute() or any(
                    part in {"", ".", ".."} for part in relative.parts
                ):
                    _fail("invalid_annotations")
            _integer(clip["source_byte_count"], "invalid_annotations", 1, MAX_SOURCE_CLIP_BYTES)
            _integer(clip["detector_byte_count"], "invalid_annotations", 1, MAX_DETECTOR_CLIP_BYTES)
            _digest(clip["source_sha256"], "invalid_annotations")
            _digest(clip["detector_sha256"], "invalid_annotations")
            clip_ids.add(clip_id)
            all_clip_ids.add(clip_id)
            sources.add(source_id)
        counts = {stratum: 0 for stratum in crop_prep.STRATA}
        per_clip: dict[str, set[int]] = {clip_id: set() for clip_id in clip_ids}
        for item in items:
            if set(item) != item_fields:
                _fail("invalid_annotations")
            item_id = _text(item["item_id"], "invalid_annotations", 128)
            clip_id = _text(item["clip_id"], "invalid_annotations", 96)
            source_id = _text(item["source_id"], "invalid_annotations", 64)
            stratum = _text(item["stratum"], "invalid_annotations", 16)
            frame_index = _integer(item["frame_index"], "invalid_annotations", 0, 2)
            if (
                item_id in all_item_ids
                or clip_id not in clip_ids
                or item_id != f"{clip_id}-{stratum}"
                or stratum not in crop_prep.STRATA
                or frame_index != crop_prep.STRATA.index(stratum)
                or frame_index in per_clip[clip_id]
                or item["split"] != split
                or not clip_id.endswith(source_id)
            ):
                _fail("invalid_annotations")
            detector_box = _box(
                item["detector_box"], DETECTOR_WIDTH, DETECTOR_HEIGHT, "invalid_annotations"
            )
            source_box = _box(
                item["source_box"], SOURCE_WIDTH, SOURCE_HEIGHT, "invalid_annotations"
            )
            if source_box != crop_prep._source_box(detector_box):
                _fail("invalid_annotations")
            for control in ("resolved", "absent"):
                detector_panel = _box(
                    item[f"{control}_detector_panel_box"],
                    DETECTOR_WIDTH,
                    DETECTOR_HEIGHT,
                    "invalid_annotations",
                )
                source_panel = _box(
                    item[f"{control}_source_panel_box"],
                    SOURCE_WIDTH,
                    SOURCE_HEIGHT,
                    "invalid_annotations",
                )
                if source_panel != crop_prep._source_box(detector_panel):
                    _fail("invalid_annotations")
            labels = _mapping(item["labels"], "invalid_annotations")
            if labels != crop_prep.assigned_labels(source_id, stratum):
                _fail("invalid_annotations")
            for key in (
                "detector_crop_sha256",
                "detector_frame_sha256",
                "original_crop_sha256",
                "source_frame_sha256",
            ):
                _digest(item[key], "invalid_annotations")
            expected_bytes = (source_box[2] - source_box[0]) * (source_box[3] - source_box[1]) * 3
            if item["original_crop_bytes"] != expected_bytes:
                _fail("invalid_annotations")
            counts[stratum] += 1
            per_clip[clip_id].add(frame_index)
            all_item_ids.add(item_id)
        if any(indices != {0, 1, 2} for indices in per_clip.values()):
            _fail("invalid_annotations")
        stratum_counts = _mapping(expected["stratum_item_counts"], "invalid_dataset")
        if stratum_counts != {"overall": len(items), **counts}:
            _fail("dataset_lock_mismatch")
        split_sources[split] = sources
    if split_sources["calibration"] & split_sources["test"]:
        _fail("dataset_lock_mismatch")


def _movie_frames(
    dataset_root: Path,
    relative_value: object,
    expected_size: object,
    expected_sha256: object,
    width: int,
    height: int,
    maximum: int,
) -> tuple[bytes, int]:
    relative = PurePosixPath(_text(relative_value, "invalid_clip", 256))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        _fail("invalid_clip")
    try:
        raw = detection._read_regular(
            dataset_root.joinpath(*relative.parts), maximum, "invalid_clip"
        )
    except detection.BenchmarkError as error:
        raise CropBenchmarkError("invalid_clip") from error
    byte_count = _integer(expected_size, "invalid_clip", 1, maximum)
    if len(raw) != byte_count or _sha256(raw) != _digest(expected_sha256, "invalid_clip"):
        _fail("clip_digest_mismatch")
    frame_bytes = width * height * 3
    if (
        len(raw) < 36
        or raw[4:8] != b"ftyp"
        or struct.unpack(">I", raw[:4])[0] != 20
        or raw[24:28] != b"mdat"
        or struct.unpack(">I", raw[20:24])[0] != 8 + frame_bytes * 3
    ):
        _fail("invalid_clip")
    start = 28
    moov = start + frame_bytes * 3
    if (
        raw[moov + 4 : moov + 8] != b"moov"
        or struct.unpack(">I", raw[moov : moov + 4])[0] != len(raw) - moov
    ):
        _fail("invalid_clip")
    return raw, start


def sample_cells(
    crop: bytes,
    width: int,
    height: int,
    panel_box: tuple[int, int, int, int],
    threshold: int = 127,
) -> tuple[int, ...]:
    """Read the frozen 24x8 panel at normalized cell centers."""

    if type(crop) is not bytes or len(crop) != width * height * 3 or type(threshold) is not int:
        _fail("invalid_crop")
    left, top, right, bottom = _box(list(panel_box), width, height, "invalid_crop")
    panel_width, panel_height = right - left, bottom - top
    bits: list[int] = []
    for row in range(crop_prep.GRID_HEIGHT):
        y = min(bottom - 1, top + (2 * row + 1) * panel_height // (2 * crop_prep.GRID_HEIGHT))
        for column in range(crop_prep.GRID_WIDTH):
            x = min(right - 1, left + (2 * column + 1) * panel_width // (2 * crop_prep.GRID_WIDTH))
            offset = (y * width + x) * 3
            luminance = sum(crop[offset : offset + 3]) // 3
            bits.append(1 if luminance <= threshold else 0)
    return tuple(bits)


def _task_bits(bits: tuple[int, ...], task_index: int) -> tuple[int, ...]:
    return tuple(
        bits[row * crop_prep.GRID_WIDTH + task_index * 8 + column]
        for row in range(8)
        for column in range(8)
    )


def classify(bits: tuple[int, ...], task: str) -> int:
    """Return the minimum-Hamming frozen label with a numeric tie break."""

    if len(bits) != 64 or task not in crop_prep.TASKS:
        _fail("invalid_pattern")
    return min(
        range(8),
        key=lambda label: (
            sum(
                first != second
                for first, second in zip(bits, crop_prep.pattern_bits(task, label), strict=True)
            ),
            label,
        ),
    )


def _local_box(
    outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    if not (
        outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]
    ):
        _fail("invalid_annotations")
    return (inner[0] - outer[0], inner[1] - outer[1], inner[2] - outer[0], inner[3] - outer[1])


def _score_panel(
    bits: tuple[int, ...], labels: Mapping[str, object]
) -> tuple[int, int, dict[str, bool]]:
    expected = crop_prep.combined_pattern(
        {task: _integer(labels[task], "invalid_annotations", 0, 7) for task in crop_prep.TASKS}
    )
    readable = sum(first == second for first, second in zip(bits, expected, strict=True))
    tasks: dict[str, bool] = {}
    for index, task in enumerate(crop_prep.TASKS):
        tasks[task] = classify(_task_bits(bits, index), task) == labels[task]
    return readable, sum(tasks.values()), tasks


def _direct_crop(pixels: bytes, width: int, height: int, box: tuple[int, int, int, int]) -> bytes:
    try:
        return detection_prep.crop_rgb24(pixels, width, height, box)[2]
    except detection_prep.PreparationError as error:
        raise CropBenchmarkError("invalid_crop") from error


def _basis_points(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        _fail("metric_failed")
    return numerator * 10_000 // denominator


def _median(values: list[int]) -> int:
    if not values:
        _fail("metric_failed")
    return sorted(values)[len(values) // 2]


def _metrics(
    items: list[dict[str, object]],
    item_results: list[dict[str, object]],
    original_wall_ns: list[int],
    original_bytes: list[int],
    peak_rss_bytes: int,
) -> tuple[dict[str, dict[str, int]], dict[str, object]]:
    indexed = {cast(str, result["item_id"]): result for result in item_results}
    metric_values: dict[str, dict[str, int]] = {
        "crop_wall_ms": {"overall": (_median(original_wall_ns) + 999_999) // 1_000_000},
        "evidence_bytes_per_crop": {"overall": _median(original_bytes)},
        "exact_crop_byte_match_basis_points": {},
        "failure_rate_basis_points": {"overall": 0},
        "mapping_error_milli_pixels": {},
        "peak_rss_bytes": {"overall": peak_rss_bytes},
        "readable_detail_proxy_gain_basis_points": {},
        "specialist_accuracy_gain_basis_points": {},
    }
    diagnostics: dict[str, object] = {"by_stratum": {}}
    for stratum in STRATA:
        selected_items = (
            items
            if stratum == "overall"
            else [item for item in items if item["stratum"] == stratum]
        )
        selected = [indexed[cast(str, item["item_id"])] for item in selected_items]
        if not selected:
            _fail("metric_failed")
        metric_values["exact_crop_byte_match_basis_points"][stratum] = _basis_points(
            sum(bool(result["exact_crop_byte_match"]) for result in selected), len(selected)
        )
        metric_values["mapping_error_milli_pixels"][stratum] = max(
            cast(int, result["mapping_error_milli_pixels"]) for result in selected
        )
        condition: dict[str, object] = {}
        combined_readable: dict[str, int] = {}
        combined_specialist: dict[str, int] = {}
        for path in PATHS:
            readable_count = sum(
                cast(
                    int,
                    cast(dict[str, object], cast(dict[str, object], result["scores"])[path])[
                        control + "_readable_cells"
                    ],
                )
                for result in selected
                for control in ("resolved", "absent")
            )
            specialist_count = sum(
                cast(
                    int,
                    cast(dict[str, object], cast(dict[str, object], result["scores"])[path])[
                        control + "_specialists_correct"
                    ],
                )
                for result in selected
                for control in ("resolved", "absent")
            )
            combined_readable[path] = _basis_points(readable_count, len(selected) * 2 * 192)
            combined_specialist[path] = _basis_points(specialist_count, len(selected) * 2 * 3)
        metric_values["readable_detail_proxy_gain_basis_points"][stratum] = (
            combined_readable["original"] - combined_readable["detector"]
        )
        metric_values["specialist_accuracy_gain_basis_points"][stratum] = (
            combined_specialist["original"] - combined_specialist["detector"]
        )
        for control in ("resolved", "absent"):
            control_values: dict[str, object] = {}
            for path in PATHS:
                readable_count = sum(
                    cast(
                        int,
                        cast(dict[str, object], cast(dict[str, object], result["scores"])[path])[
                            control + "_readable_cells"
                        ],
                    )
                    for result in selected
                )
                specialist_count = sum(
                    cast(
                        int,
                        cast(dict[str, object], cast(dict[str, object], result["scores"])[path])[
                            control + "_specialists_correct"
                        ],
                    )
                    for result in selected
                )
                control_values[path] = {
                    "readable_detail_basis_points": _basis_points(
                        readable_count, len(selected) * 192
                    ),
                    "specialist_accuracy_basis_points": _basis_points(
                        specialist_count, len(selected) * 3
                    ),
                }
            original = cast(dict[str, int], control_values["original"])
            detector = cast(dict[str, int], control_values["detector"])
            control_values["gain"] = {
                "readable_detail_basis_points": original["readable_detail_basis_points"]
                - detector["readable_detail_basis_points"],
                "specialist_accuracy_basis_points": original["specialist_accuracy_basis_points"]
                - detector["specialist_accuracy_basis_points"],
            }
            condition[control] = control_values
        cast(dict[str, object], diagnostics["by_stratum"])[stratum] = {
            "combined": {
                "detector_readable_detail_basis_points": combined_readable["detector"],
                "detector_specialist_accuracy_basis_points": combined_specialist["detector"],
                "original_readable_detail_basis_points": combined_readable["original"],
                "original_specialist_accuracy_basis_points": combined_specialist["original"],
            },
            "conditions": condition,
            "item_count": len(selected),
        }
    return metric_values, diagnostics


def _evaluate_split(
    dataset_root: Path,
    clips: list[dict[str, object]],
    items: list[dict[str, object]],
    seed: int,
) -> dict[str, object]:
    order = list(clips)
    random.Random(seed).shuffle(order)
    items_by_clip: dict[str, list[dict[str, object]]] = {}
    for item in items:
        items_by_clip.setdefault(cast(str, item["clip_id"]), []).append(item)
    item_results: list[dict[str, object]] = []
    original_wall_ns: list[int] = []
    detector_wall_ns: list[int] = []
    original_cpu_ns: list[int] = []
    detector_cpu_ns: list[int] = []
    original_bytes: list[int] = []
    detector_bytes: list[int] = []
    transform = DetectorTransform(SOURCE_WIDTH, SOURCE_HEIGHT, DETECTOR_WIDTH, DETECTOR_HEIGHT)
    for clip in order:
        source_movie, source_start = _movie_frames(
            dataset_root,
            clip["source_relative_path"],
            clip["source_byte_count"],
            clip["source_sha256"],
            SOURCE_WIDTH,
            SOURCE_HEIGHT,
            MAX_SOURCE_CLIP_BYTES,
        )
        detector_movie, detector_start = _movie_frames(
            dataset_root,
            clip["detector_relative_path"],
            clip["detector_byte_count"],
            clip["detector_sha256"],
            DETECTOR_WIDTH,
            DETECTOR_HEIGHT,
            MAX_DETECTOR_CLIP_BYTES,
        )
        for item in sorted(
            items_by_clip[cast(str, clip["clip_id"])],
            key=lambda value: cast(int, value["frame_index"]),
        ):
            index = cast(int, item["frame_index"])
            source_frame = source_movie[
                source_start + index * SOURCE_FRAME_BYTES : source_start
                + (index + 1) * SOURCE_FRAME_BYTES
            ]
            detector_frame = detector_movie[
                detector_start + index * DETECTOR_FRAME_BYTES : detector_start
                + (index + 1) * DETECTOR_FRAME_BYTES
            ]
            if (
                _sha256(source_frame) != item["source_frame_sha256"]
                or _sha256(detector_frame) != item["detector_frame_sha256"]
            ):
                _fail("frame_digest_mismatch")
            detector_box = _box(
                item["detector_box"], DETECTOR_WIDTH, DETECTOR_HEIGHT, "invalid_annotations"
            )
            expected_source_box = _box(
                item["source_box"], SOURCE_WIDTH, SOURCE_HEIGHT, "invalid_annotations"
            )
            geometry = transform.map_box(detector_box)
            mapping_error = max(
                abs(actual - expected)
                for actual, expected in zip(geometry.box_xyxy, expected_source_box, strict=True)
            )
            wall_start = time.perf_counter_ns()
            cpu_start = time.process_time_ns()
            original_crop = extract_rgb24_crop(source_frame, SOURCE_WIDTH, SOURCE_HEIGHT, geometry)
            original_cpu_ns.append(time.process_time_ns() - cpu_start)
            original_wall_ns.append(time.perf_counter_ns() - wall_start)
            direct = _direct_crop(source_frame, SOURCE_WIDTH, SOURCE_HEIGHT, expected_source_box)
            exact_match = (
                original_crop.pixels == direct
                and original_crop.sha256 == item["original_crop_sha256"]
                and len(original_crop.pixels) == item["original_crop_bytes"]
            )
            wall_start = time.perf_counter_ns()
            cpu_start = time.process_time_ns()
            detector_crop = _direct_crop(
                detector_frame, DETECTOR_WIDTH, DETECTOR_HEIGHT, detector_box
            )
            detector_cpu_ns.append(time.process_time_ns() - cpu_start)
            detector_wall_ns.append(time.perf_counter_ns() - wall_start)
            if _sha256(detector_crop) != item["detector_crop_sha256"]:
                _fail("crop_digest_mismatch")
            source_crop_width = expected_source_box[2] - expected_source_box[0]
            source_crop_height = expected_source_box[3] - expected_source_box[1]
            detector_crop_width = detector_box[2] - detector_box[0]
            detector_crop_height = detector_box[3] - detector_box[1]
            labels = _mapping(item["labels"], "invalid_annotations")
            scores: dict[str, object] = {}
            for path, pixels, width, height, outer, prefix in (
                (
                    "original",
                    original_crop.pixels,
                    source_crop_width,
                    source_crop_height,
                    expected_source_box,
                    "source",
                ),
                (
                    "detector",
                    detector_crop,
                    detector_crop_width,
                    detector_crop_height,
                    detector_box,
                    "detector",
                ),
            ):
                path_scores: dict[str, object] = {}
                for control, annotation_prefix in (("resolved", "resolved"), ("absent", "absent")):
                    panel = _box(
                        item[f"{annotation_prefix}_{prefix}_panel_box"],
                        SOURCE_WIDTH if prefix == "source" else DETECTOR_WIDTH,
                        SOURCE_HEIGHT if prefix == "source" else DETECTOR_HEIGHT,
                        "invalid_annotations",
                    )
                    bits = sample_cells(pixels, width, height, _local_box(outer, panel))
                    readable, specialists, tasks = _score_panel(bits, labels)
                    path_scores[f"{control}_readable_cells"] = readable
                    path_scores[f"{control}_specialists_correct"] = specialists
                    path_scores[f"{control}_tasks"] = tasks
                scores[path] = path_scores
            original_bytes.append(len(original_crop.pixels))
            detector_bytes.append(len(detector_crop))
            item_results.append(
                {
                    "detector_crop_bytes": len(detector_crop),
                    "exact_crop_byte_match": exact_match,
                    "item_id": item["item_id"],
                    "mapping_error_milli_pixels": mapping_error * 1000,
                    "original_crop_bytes": len(original_crop.pixels),
                    "scores": scores,
                    "stratum": item["stratum"],
                }
            )
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    metrics, score_diagnostics = _metrics(
        items, item_results, original_wall_ns, original_bytes, peak_rss
    )
    byte_ratio_milli = _median(original_bytes) * 1000 // _median(detector_bytes)
    return {
        "diagnostics": {
            **score_diagnostics,
            "byte_ratio_milli": byte_ratio_milli,
            "detector_crop_bytes_median": _median(detector_bytes),
            "detector_crop_cpu_ns_median": _median(detector_cpu_ns),
            "detector_crop_wall_ns_median": _median(detector_wall_ns),
            "original_crop_bytes_median": _median(original_bytes),
            "original_crop_cpu_ns_median": _median(original_cpu_ns),
            "original_crop_wall_ns_median": _median(original_wall_ns),
        },
        "failed_item_count": 0,
        "failure_code": None,
        "item_results": sorted(item_results, key=lambda value: cast(str, value["item_id"])),
        "metrics": metrics,
        "processed_item_count": len(items),
        "seed": seed,
        "status": "pass",
    }


def _aggregate(repetitions: list[dict[str, object]]) -> dict[str, object]:
    metric_names = cast(dict[str, dict[str, int]], repetitions[0]["metrics"])
    metrics: dict[str, dict[str, int]] = {}
    dispersion: dict[str, dict[str, int]] = {}
    for name, strata in metric_names.items():
        metrics[name] = {}
        dispersion[name] = {}
        for stratum in strata:
            values = sorted(
                cast(dict[str, dict[str, int]], repetition["metrics"])[name][stratum]
                for repetition in repetitions
            )
            center = values[len(values) // 2]
            metrics[name][stratum] = max(values) if name == "peak_rss_bytes" else center
            deviations = sorted(abs(value - center) for value in values)
            dispersion[name][stratum] = deviations[len(deviations) // 2]
    return {"dispersion": dispersion, "metrics": metrics}


def _receipt_repetition(repetition: dict[str, object]) -> dict[str, object]:
    return {
        "failed_item_count": repetition["failed_item_count"],
        "failure_code": repetition["failure_code"],
        "metrics": repetition["metrics"],
        "processed_item_count": repetition["processed_item_count"],
        "seed": repetition["seed"],
        "status": repetition["status"],
    }


def _raw_repetitions(repetitions: list[dict[str, object]]) -> list[dict[str, object]]:
    compact: list[dict[str, object]] = []
    first_items_sha256 = _sha256(_canonical(repetitions[0]["item_results"]))
    for index, repetition in enumerate(repetitions):
        selected = {key: value for key, value in repetition.items() if key != "item_results"}
        item_sha256 = _sha256(_canonical(repetition["item_results"]))
        selected["item_results_sha256"] = item_sha256
        if index == 0 or item_sha256 != first_items_sha256:
            selected["representative_item_results"] = repetition["item_results"]
        compact.append(selected)
    return compact


def _python_artifact(
    selected_detector: dict[str, object],
    runtime: dict[str, object],
    evaluated_on: str,
) -> dict[str, object]:
    artifacts = detection._candidate_artifacts(selected_detector, runtime, evaluated_on)
    matches = [
        artifact for artifact in artifacts if cast(str, artifact["name"]).startswith("cpython-")
    ]
    if len(matches) != 1:
        _fail("runtime_verification_failed")
    return matches[0]


def run_benchmark(
    *,
    dataset_root: Path,
    base_python: Path,
    output_root: Path,
    source_revision: str,
    evaluated_on: str,
    calibration_only: bool = False,
) -> dict[str, object]:
    if _REVISION.fullmatch(source_revision) is None:
        _fail("invalid_source_revision")
    try:
        if not Path(sys.executable).samefile(base_python):
            _fail("interpreter_mismatch")
    except OSError as error:
        raise CropBenchmarkError("interpreter_mismatch") from error
    annotations, annotations_sha256 = _load_json(ANNOTATIONS_PATH, "invalid_annotations")
    candidates, candidates_sha256 = _load_json(CANDIDATES_PATH, "invalid_candidate_manifest")
    source_manifest, source_manifest_sha256 = _load_json(
        SOURCE_MANIFEST_PATH, "invalid_source_manifest"
    )
    detection_candidates, detection_candidates_sha256 = _load_json(
        DETECTION_CANDIDATES_PATH, "invalid_detector_manifest"
    )
    detection_annotations, detection_annotation_sha256 = _load_json(
        DETECTION_ANNOTATIONS_PATH, "invalid_source_manifest"
    )
    _, detection_source_sha256 = _load_json(
        DETECTION_SOURCE_MANIFEST_PATH, "invalid_source_manifest"
    )
    crop_prep._validate_candidates(candidates)
    crop_prep._validate_source_manifest(
        source_manifest,
        detection_annotations,
        detection_annotation_sha256,
        detection_source_sha256,
    )
    if (
        detection_candidates_sha256
        != _mapping(candidates["detector"], "invalid_candidate_manifest")[
            "candidate_manifest_sha256"
        ]
    ):
        _fail("detector_manifest_mismatch")
    try:
        detector_values, runtime, _, _ = detection._validate_candidate_manifest(
            detection_candidates
        )
        python_attestation = detection._verify_base_python(base_python, runtime)
        policy, policy_sha256 = gate_evaluator.load_policy(POLICY_PATH)
        dataset_manifest, dataset_manifest_sha256 = gate_evaluator.load_manifest(
            DATASET_MANIFEST_PATH, policy
        )
        evaluation_day = date.fromisoformat(evaluated_on)
    except (ValueError, detection.BenchmarkError, gate_evaluator.EvaluationError) as error:
        raise CropBenchmarkError("invalid_gate_input") from error
    acquisition = _mapping(dataset_manifest["acquisition"], "invalid_dataset")
    if acquisition["sha256"] != source_manifest_sha256:
        _fail("source_manifest_mismatch")
    _validate_annotations(
        annotations,
        annotations_sha256,
        source_manifest_sha256,
        candidates_sha256,
        dataset_manifest,
    )
    calibration_clips, calibration_items, _, _ = _split_payload(annotations, "calibration")
    test_clips, test_items, _, _ = _split_payload(annotations, "test")
    _evaluate_split(dataset_root, calibration_clips, calibration_items, SEEDS[0])
    calibration_repetitions = [
        _evaluate_split(dataset_root, calibration_clips, calibration_items, seed) for seed in SEEDS
    ]
    harness_sha256 = _sha256(Path(__file__).read_bytes())
    geometry_sha256 = _sha256(GEOMETRY_PATH.read_bytes())
    provenance = {
        "annotation_lock_sha256": annotations_sha256,
        "candidate_manifest_sha256": candidates_sha256,
        "dataset_manifest_sha256": dataset_manifest_sha256,
        "detection_candidate_manifest_sha256": detection_candidates_sha256,
        "detection_source_annotation_sha256": detection_annotation_sha256,
        "detection_source_manifest_sha256": detection_source_sha256,
        "evaluation_harness_sha256": harness_sha256,
        "geometry_sha256": geometry_sha256,
        "policy_sha256": policy_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "source_revision": source_revision,
    }
    profile = detection._profile()
    if calibration_only:
        output_descriptor = detection._create_private_directory(output_root)
        result = {
            "aggregate": _aggregate(calibration_repetitions),
            "environment": {"profile": profile, "python": python_attestation},
            "provenance": provenance,
            "repetitions": _raw_repetitions(calibration_repetitions),
            "schema": "visualworld.v02-crop-calibration-results",
            "schema_version": 1,
        }
        try:
            result_sha256 = detection._write_new(
                output_descriptor, "calibration-results.json", result
            )
        except detection.BenchmarkError as error:
            raise CropBenchmarkError("output_failed") from error
        finally:
            detection._close_once(output_descriptor)
        return {"calibration_results_sha256": result_sha256, "status": "pass"}
    _evaluate_split(dataset_root, test_clips, test_items, SEEDS[0])
    test_repetitions = [
        _evaluate_split(dataset_root, test_clips, test_items, seed) for seed in SEEDS
    ]
    receipt_repetitions = [_receipt_repetition(item) for item in test_repetitions]
    aggregate = _aggregate(receipt_repetitions)
    selected_detector = next(
        candidate
        for candidate in detector_values
        if candidate["name"]
        == _mapping(candidates["detector"], "invalid_candidate_manifest")["name"]
    )
    runtime_closure_sha256 = _sha256(
        _canonical(
            {
                "candidate_manifest_sha256": candidates_sha256,
                "geometry_sha256": geometry_sha256,
                "python": python_attestation,
            }
        )
    )
    code_artifact = detection._artifact(
        name="visualworld-original-rgb24-crop",
        kind="code",
        artifact_format="python-source",
        license_expression="Apache-2.0",
        revision=source_revision,
        sha256=geometry_sha256,
        source_url=(
            "https://github.com/mayank-gupta16/vision-query-system/blob/"
            f"{source_revision}/src/visualworld/geometry.py"
        ),
        terms_url=(
            f"https://github.com/mayank-gupta16/vision-query-system/blob/{source_revision}/LICENSE"
        ),
        use="exact detector-to-source mapping and packed RGB24 crop evaluation",
        reviewed_on=evaluated_on,
    )
    code_artifact["isolation"] = "in-process-reviewed"
    artifacts = [
        code_artifact,
        _python_artifact(selected_detector, runtime, evaluated_on),
    ]
    receipt = {
        "aggregate": aggregate,
        "candidate": {
            "artifacts": artifacts,
            "configuration_sha256": candidates_sha256,
            "name": "original-source-rgb24",
            "runtime_closure_sha256": runtime_closure_sha256,
        },
        "dataset_manifest_sha256": dataset_manifest_sha256,
        "evaluated_on": evaluated_on,
        "evaluation_phase": "selection",
        "experiment": "crop_value",
        "implementation": {
            "evaluation_harness": {
                "license_expression": "Apache-2.0",
                "name": "visualworld-v02-crop-harness",
                "revision": source_revision,
                "sha256": harness_sha256,
                "source_url": (
                    "https://github.com/mayank-gupta16/vision-query-system/blob/"
                    f"{source_revision}/scripts/run_v02_crop_benchmark.py"
                ),
            },
            "executable_implementation": "cpython",
            "metric_implementation": {
                "license_expression": "Apache-2.0",
                "name": "visualworld-v02-crop-metrics",
                "revision": source_revision,
                "sha256": harness_sha256,
                "source_url": (
                    "https://github.com/mayank-gupta16/vision-query-system/blob/"
                    f"{source_revision}/scripts/run_v02_crop_benchmark.py"
                ),
            },
            "python_version": "3.13.15",
            "source_revision": source_revision,
        },
        "policy_sha256": policy_sha256,
        "policy_version": policy["policy_version"],
        "profile": profile,
        "repetitions": receipt_repetitions,
        "schema": "visualworld.v02-evaluation-receipt",
        "schema_version": 1,
        "split": "test",
        "waiver": None,
    }
    output_descriptor = detection._create_private_directory(output_root)
    try:
        receipt_sha256 = detection._write_new(
            output_descriptor, "original-source-rgb24-receipt.json", receipt
        )
        validated = gate_evaluator.validate_receipt(
            receipt,
            policy,
            policy_sha256,
            dataset_manifest,
            dataset_manifest_sha256,
        )
        gate = gate_evaluator.evaluate(
            policy,
            validated,
            baseline=None,
            baseline_receipt_sha256=None,
            receipt_sha256=receipt_sha256,
            as_of=evaluation_day,
        )
        gate_sha256 = detection._write_new(
            output_descriptor, "original-source-rgb24-gate.json", gate
        )
        raw_result = {
            "calibration": {
                "aggregate": _aggregate(calibration_repetitions),
                "repetitions": _raw_repetitions(calibration_repetitions),
            },
            "environment": {
                "profile": profile,
                "python": python_attestation,
                "runtime_closure_sha256": runtime_closure_sha256,
            },
            "evaluated_on": evaluated_on,
            "outputs": {
                "gate": "original-source-rgb24-gate.json",
                "gate_sha256": gate_sha256,
                "gate_status": gate["status"],
                "receipt": "original-source-rgb24-receipt.json",
                "receipt_sha256": receipt_sha256,
            },
            "provenance": provenance,
            "schema": "visualworld.v02-crop-raw-results",
            "schema_version": 1,
            "test": {
                "aggregate": aggregate,
                "repetitions": _raw_repetitions(test_repetitions),
            },
        }
        raw_sha256 = detection._write_new(output_descriptor, "raw-results.json", raw_result)
    except (detection.BenchmarkError, gate_evaluator.EvaluationError) as error:
        raise CropBenchmarkError("generated_result_invalid") from error
    finally:
        detection._close_once(output_descriptor)
    return {
        "gate_sha256": gate_sha256,
        "gate_status": gate["status"],
        "raw_results_sha256": raw_sha256,
        "receipt_sha256": receipt_sha256,
        "status": "pass",
    }


def main() -> int:
    parser = _StableArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--base-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--evaluated-on", required=True)
    parser.add_argument("--calibration-only", action="store_true")
    try:
        arguments = parser.parse_args()
        result = run_benchmark(
            dataset_root=arguments.dataset_root.resolve(),
            base_python=arguments.base_python.resolve(),
            output_root=Path(os.path.abspath(arguments.output)),
            source_revision=arguments.source_revision,
            evaluated_on=arguments.evaluated_on,
            calibration_only=arguments.calibration_only,
        )
    except (OSError, CropBenchmarkError) as error:
        code = str(error) if isinstance(error, CropBenchmarkError) else "filesystem_failed"
        _emit({"error": code, "status": "error"}, sys.stdout)
        return 1
    return 0 if _emit(result, sys.stdout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
