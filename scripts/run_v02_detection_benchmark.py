#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Measure the locked v0.2 detector candidates on the CPU-LITE profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import resource
import stat
import subprocess
import time
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any, NoReturn, cast
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "fixtures" / "v02-evaluation" / "policy.json"
DATASET_MANIFEST_PATH = ROOT / "fixtures" / "v02-detection-research" / "dataset-manifest.json"
ANNOTATIONS_PATH = ROOT / "fixtures" / "v02-detection-research" / "annotations.json"
CANDIDATES_PATH = ROOT / "fixtures" / "v02-detection-research" / "candidates.json"
SOURCE_MANIFEST_PATH = ROOT / "fixtures" / "v02-detection-research" / "source-manifest.json"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_SEEDS = (1729, 3253, 5081, 7919, 104729)
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_PPM_BYTES = 4 * 1024 * 1024
_MAX_DETECTIONS_PER_ITEM = 100
_IOU_SCALE = 1000


class BenchmarkError(RuntimeError):
    """A stable detector benchmark failure."""


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
        raise BenchmarkError("invalid_json") from error


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    selected: dict[str, object] = {}
    for key, value in pairs:
        if key in selected:
            raise ValueError("duplicate key")
        selected[key] = value
    return selected


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite number")


def _decode_json(raw: bytes) -> object:
    return json.loads(
        raw,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def _mapping(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        raise BenchmarkError(code)
    return cast(dict[str, object], value)


def _integer(
    value: object,
    code: str,
    *,
    minimum: int = 0,
    maximum: int = (1 << 63) - 1,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise BenchmarkError(code)
    return value


def _text(value: object, code: str, *, maximum: int = 2048) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or not value.isascii()
        or any(ord(character) < 0x20 for character in value)
    ):
        raise BenchmarkError(code)
    return value


def _sha256_text(value: object, code: str) -> str:
    selected = _text(value, code, maximum=64)
    if _DIGEST.fullmatch(selected) is None:
        raise BenchmarkError(code)
    return selected


def _read_regular(path: Path, maximum: int, code: str) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
            raise BenchmarkError(code)
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise BenchmarkError(code)
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError as error:
        raise BenchmarkError(code) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_json(path: Path) -> tuple[dict[str, object], str]:
    raw = _read_regular(path, _MAX_JSON_BYTES, "invalid_input")
    try:
        value = _decode_json(raw)
    except (UnicodeError, ValueError) as error:
        raise BenchmarkError("invalid_input") from error
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        raise BenchmarkError("invalid_input")
    return cast(dict[str, object], value), _sha256_bytes(raw)


def _validate_dataset_locks(annotations: dict[str, object], dataset: dict[str, object]) -> None:
    _, source_sha256 = _load_json(SOURCE_MANIFEST_PATH)
    acquisition = _mapping(dataset.get("acquisition"), "dataset_lock_mismatch")
    if (
        annotations.get("schema") != "visualworld.v02-detection-annotation-lock"
        or annotations.get("schema_version") != 1
        or annotations.get("source_manifest_sha256") != source_sha256
        or acquisition.get("sha256") != source_sha256
    ):
        raise BenchmarkError("dataset_lock_mismatch")
    item_values = annotations.get("items")
    split_values = dataset.get("splits")
    if not isinstance(item_values, list) or not isinstance(split_values, dict):
        raise BenchmarkError("dataset_lock_mismatch")
    items: list[dict[str, object]] = []
    item_fields = {
        "frame_sha256",
        "height",
        "item_id",
        "object_box",
        "relative_path",
        "source_id",
        "split",
        "stratum",
        "width",
    }
    for item_value in item_values:
        item = _mapping(item_value, "dataset_lock_mismatch")
        if set(item) != item_fields:
            raise BenchmarkError("dataset_lock_mismatch")
        item_id = _text(item.get("item_id"), "dataset_lock_mismatch", maximum=128)
        source_id = _text(item.get("source_id"), "dataset_lock_mismatch", maximum=64)
        split = item.get("split")
        stratum = item.get("stratum")
        width = _integer(item.get("width"), "dataset_lock_mismatch", minimum=1, maximum=4096)
        height = _integer(item.get("height"), "dataset_lock_mismatch", minimum=1, maximum=4096)
        box = item.get("object_box")
        if (
            split not in {"calibration", "test"}
            or stratum not in {"easy", "small_distant"}
            or item.get("relative_path") != f"{split}/{item_id}.ppm"
            or not item_id.startswith(f"{split}-{source_id}-")
            or not isinstance(box, list)
            or len(box) != 4
            or any(type(coordinate) is not int for coordinate in box)
        ):
            raise BenchmarkError("dataset_lock_mismatch")
        left, top, right, bottom = cast(list[int], box)
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise BenchmarkError("dataset_lock_mismatch")
        _sha256_text(item.get("frame_sha256"), "dataset_lock_mismatch")
        items.append(item)
    seen_ids: set[object] = set()
    split_sources: dict[str, set[object]] = {}
    for split in ("calibration", "test"):
        selected = [item for item in items if item.get("split") == split]
        split_sources[split] = {item.get("source_id") for item in selected}
        contract = _mapping(split_values.get(split), "dataset_lock_mismatch")
        item_ids = [item.get("item_id") for item in selected]
        if (
            not selected
            or any(type(item_id) is not str or item_id in seen_ids for item_id in item_ids)
            or contract.get("item_count") != len(selected)
            or contract.get("annotation_sha256") != _sha256_bytes(_canonical(selected))
            or contract.get("item_ids_sha256")
            != _sha256_bytes(("\n".join(cast(list[str], item_ids)) + "\n").encode("ascii"))
        ):
            raise BenchmarkError("dataset_lock_mismatch")
        seen_ids.update(item_ids)
        counts = _mapping(contract.get("stratum_item_counts"), "dataset_lock_mismatch")
        if counts != {
            "easy": sum(item.get("stratum") == "easy" for item in selected),
            "overall": len(selected),
            "small_distant": sum(item.get("stratum") == "small_distant" for item in selected),
        }:
            raise BenchmarkError("dataset_lock_mismatch")
    if not split_sources["calibration"].isdisjoint(split_sources["test"]):
        raise BenchmarkError("dataset_lock_mismatch")


def _verify_runtime_wheels(runtime: dict[str, object], wheels_root: Path) -> None:
    expected_names: set[str] = set()
    for name in ("openvino", "numpy", "openvino_telemetry"):
        component = _mapping(runtime.get(name), "invalid_runtime_manifest")
        url = _text(component.get("url"), "invalid_runtime_manifest")
        filename = unquote(Path(urlsplit(url).path).name)
        if not filename.endswith(".whl") or filename in expected_names:
            raise BenchmarkError("invalid_runtime_manifest")
        expected_names.add(filename)
        raw = _read_regular(wheels_root / filename, 128 * 1024 * 1024, "runtime_wheel_missing")
        if len(raw) != _integer(
            component.get("size"), "invalid_runtime_manifest", minimum=1
        ) or _sha256_bytes(raw) != component.get("sha256"):
            raise BenchmarkError("runtime_wheel_mismatch")
    actual_names = {path.name for path in wheels_root.glob("*.whl") if path.is_file()}
    if actual_names != expected_names:
        raise BenchmarkError("runtime_wheel_set_mismatch")


def _validate_candidate_manifest(
    manifest: dict[str, object],
) -> tuple[list[dict[str, object]], dict[str, object], int, list[int]]:
    code = "invalid_candidate_manifest"
    if (
        set(manifest)
        != {"calibration", "candidates", "inference", "runtime", "schema", "schema_version"}
        or manifest.get("schema") != "visualworld.v02-detection-candidate-list"
        or manifest.get("schema_version") != 1
    ):
        raise BenchmarkError(code)
    calibration = _mapping(manifest.get("calibration"), code)
    if (
        set(calibration)
        != {
            "confidence_floor_millionths",
            "confidence_grid_millionths",
            "selection",
            "split",
        }
        or calibration.get("split") != "calibration"
    ):
        raise BenchmarkError(code)
    _text(calibration.get("selection"), code)
    confidence_floor = _integer(
        calibration.get("confidence_floor_millionths"), code, minimum=1, maximum=1_000_000
    )
    grid_value = calibration.get("confidence_grid_millionths")
    if not isinstance(grid_value, list):
        raise BenchmarkError(code)
    grid = [
        _integer(value, code, minimum=confidence_floor, maximum=1_000_000) for value in grid_value
    ]
    if grid != list(range(50_000, 1_000_000, 50_000)):
        raise BenchmarkError(code)
    inference = _mapping(manifest.get("inference"), code)
    if inference != {
        "color_conversion": "RGB-to-BGR",
        "device": "CPU",
        "input_layout": "NHWC-u8",
        "inference_num_threads": 4,
        "model_layout": "NCHW",
        "num_streams": 1,
        "performance_hint": "LATENCY",
        "resize": "OpenVINO RESIZE_LINEAR",
    }:
        raise BenchmarkError(code)
    runtime = _mapping(manifest.get("runtime"), code)
    if (
        set(runtime) != {"numpy", "openvino", "openvino_telemetry", "python"}
        or runtime.get("python") != "3.13.15"
    ):
        raise BenchmarkError(code)
    for name in ("numpy", "openvino", "openvino_telemetry"):
        component = _mapping(runtime.get(name), code)
        if set(component) != {
            "license_expression",
            "revision",
            "sha256",
            "size",
            "url",
            "version",
        } or component.get("license_expression") not in {"Apache-2.0", "BSD-3-Clause"}:
            raise BenchmarkError(code)
        _text(component.get("revision"), code, maximum=256)
        _text(component.get("version"), code, maximum=64)
        _sha256_text(component.get("sha256"), code)
        _integer(component.get("size"), code, minimum=1, maximum=128 * 1024 * 1024)
        url = _text(component.get("url"), code)
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "files.pythonhosted.org"
            or parsed.query
            or parsed.fragment
            or not unquote(Path(parsed.path).name).endswith(".whl")
        ):
            raise BenchmarkError(code)
    candidates_value = manifest.get("candidates")
    if not isinstance(candidates_value, list) or len(candidates_value) != 3:
        raise BenchmarkError(code)
    candidates: list[dict[str, object]] = []
    names: set[str] = set()
    widths: list[int] = []
    for candidate_value in candidates_value:
        candidate = _mapping(candidate_value, code)
        if set(candidate) != {
            "input_height",
            "input_width",
            "model_bin",
            "model_xml",
            "name",
            "omz_reported_ap_50_95_millionths",
            "omz_reported_gflops_millionths",
            "omz_revision",
        }:
            raise BenchmarkError(code)
        name = _text(candidate.get("name"), code, maximum=128)
        width = _integer(candidate.get("input_width"), code, minimum=1, maximum=4096)
        height = _integer(candidate.get("input_height"), code, minimum=1, maximum=4096)
        if (
            name in names
            or width != height
            or _REVISION.fullmatch(_text(candidate.get("omz_revision"), code, maximum=40)) is None
        ):
            raise BenchmarkError(code)
        names.add(name)
        widths.append(width)
        _integer(
            candidate.get("omz_reported_ap_50_95_millionths"),
            code,
            minimum=1,
            maximum=1_000_000,
        )
        _integer(candidate.get("omz_reported_gflops_millionths"), code, minimum=1)
        for artifact_name in ("model_bin", "model_xml"):
            artifact = _mapping(candidate.get(artifact_name), code)
            if set(artifact) != {"download_sha384", "sha256", "size", "url"}:
                raise BenchmarkError(code)
            sha384 = _text(artifact.get("download_sha384"), code, maximum=96)
            if len(sha384) != 96 or any(
                character not in "0123456789abcdef" for character in sha384
            ):
                raise BenchmarkError(code)
            _sha256_text(artifact.get("sha256"), code)
            _integer(artifact.get("size"), code, minimum=1, maximum=16 * 1024 * 1024)
            url = _text(artifact.get("url"), code)
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "storage.openvinotoolkit.org"
                or parsed.query
                or parsed.fragment
            ):
                raise BenchmarkError(code)
        candidates.append(candidate)
    if widths != [256, 384, 512]:
        raise BenchmarkError(code)
    return candidates, runtime, confidence_floor, grid


def _parse_ppm(raw: bytes) -> tuple[int, int, bytes]:
    first, separator, rest = raw.partition(b"\n")
    dimensions, separator_two, body = rest.partition(b"\n")
    maximum, separator_three, pixels = body.partition(b"\n")
    if (
        first != b"P6"
        or not separator
        or not separator_two
        or not separator_three
        or maximum != b"255"
    ):
        raise BenchmarkError("invalid_frame")
    try:
        width_text, height_text = dimensions.split(b" ")
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError) as error:
        raise BenchmarkError("invalid_frame") from error
    if not 1 <= width <= 4096 or not 1 <= height <= 4096 or len(pixels) != width * height * 3:
        raise BenchmarkError("invalid_frame")
    return width, height, pixels


def _items(annotations: dict[str, object], split: str) -> list[dict[str, object]]:
    if (
        annotations.get("schema") != "visualworld.v02-detection-annotation-lock"
        or annotations.get("schema_version") != 1
        or not isinstance(annotations.get("items"), list)
    ):
        raise BenchmarkError("invalid_annotations")
    selected = [
        cast(dict[str, object], item)
        for item in cast(list[object], annotations["items"])
        if isinstance(item, dict) and item.get("split") == split
    ]
    if not selected:
        raise BenchmarkError("invalid_annotations")
    return selected


def _milli_box(value: object) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(coordinate) is not int for coordinate in value)
    ):
        raise BenchmarkError("invalid_box")
    left, top, right, bottom = cast(list[int], value)
    if not (0 <= left < right and 0 <= top < bottom):
        raise BenchmarkError("invalid_box")
    return (
        left * _IOU_SCALE,
        top * _IOU_SCALE,
        right * _IOU_SCALE,
        bottom * _IOU_SCALE,
    )


def iou_basis_points(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> int:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    union = first_area + second_area - intersection
    return 0 if union <= 0 else intersection * 10_000 // union


def _detections_at(
    repetition: dict[str, object],
    item: dict[str, object],
    threshold: int,
) -> list[dict[str, object]]:
    predictions = cast(dict[str, list[dict[str, object]]], repetition["predictions"])
    selected = predictions.get(cast(str, item["item_id"]))
    if selected is None:
        raise BenchmarkError("missing_prediction")
    return [
        detection
        for detection in selected
        if cast(int, detection["confidence_millionths"]) >= threshold
    ][:_MAX_DETECTIONS_PER_ITEM]


def _ap50_basis_points(
    repetition: dict[str, object],
    items: list[dict[str, object]],
    threshold: int,
) -> int:
    ranked: list[tuple[int, str, tuple[int, int, int, int]]] = []
    ground_truth = {cast(str, item["item_id"]): _milli_box(item["object_box"]) for item in items}
    for item in items:
        item_id = cast(str, item["item_id"])
        for detection in _detections_at(repetition, item, threshold):
            ranked.append(
                (
                    cast(int, detection["confidence_millionths"]),
                    item_id,
                    cast(
                        tuple[int, int, int, int],
                        tuple(cast(list[int], detection["box_milli_pixels"])),
                    ),
                )
            )
    ranked.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
    matched: set[str] = set()
    true_positives = 0
    recall_numerators: list[int] = []
    precision: list[Fraction] = []
    for rank, (_, item_id, box) in enumerate(ranked, start=1):
        if item_id not in matched and iou_basis_points(ground_truth[item_id], box) >= 5000:
            matched.add(item_id)
            true_positives += 1
        recall_numerators.append(true_positives)
        precision.append(Fraction(true_positives, rank))
    for index in range(len(precision) - 2, -1, -1):
        precision[index] = max(precision[index], precision[index + 1])
    samples: list[Fraction] = []
    for recall_percent in range(101):
        sample = Fraction(0)
        for index, numerator in enumerate(recall_numerators):
            if numerator * 100 >= recall_percent * len(items):
                sample = precision[index]
                break
        samples.append(sample)
    return int(sum(samples, start=Fraction(0)) * 10_000 / 101)


def accuracy_metrics(
    repetition: dict[str, object],
    items: list[dict[str, object]],
    threshold: int,
) -> dict[str, dict[str, int]]:
    metrics: dict[str, dict[str, int]] = {
        "map50_basis_points": {},
        "precision_basis_points": {},
        "recall_basis_points": {},
    }
    for stratum in ("overall", "easy", "small_distant"):
        stratum_items = (
            items
            if stratum == "overall"
            else [item for item in items if item["stratum"] == stratum]
        )
        true_positives = 0
        false_positives = 0
        for item in stratum_items:
            ground_truth = _milli_box(item["object_box"])
            detections = _detections_at(repetition, item, threshold)
            matched = False
            for detection in detections:
                box = tuple(cast(list[int], detection["box_milli_pixels"]))
                is_match = (
                    not matched
                    and iou_basis_points(ground_truth, cast(tuple[int, int, int, int], box)) >= 5000
                )
                if is_match:
                    matched = True
                    true_positives += 1
                else:
                    false_positives += 1
        denominator = true_positives + false_positives
        metrics["precision_basis_points"][stratum] = (
            0 if denominator == 0 else true_positives * 10_000 // denominator
        )
        metrics["recall_basis_points"][stratum] = true_positives * 10_000 // len(stratum_items)
        metrics["map50_basis_points"][stratum] = _ap50_basis_points(
            repetition, stratum_items, threshold
        )
    return metrics


def choose_threshold(
    repetitions: list[dict[str, object]],
    items: list[dict[str, object]],
    grid: list[int],
) -> tuple[int, list[dict[str, object]]]:
    summaries: list[dict[str, object]] = []
    eligible: list[tuple[int, int, int]] = []
    for threshold in grid:
        results = [accuracy_metrics(repetition, items, threshold) for repetition in repetitions]
        metrics = _median_metric_maps(results)
        precision = metrics["precision_basis_points"]
        recall = metrics["recall_basis_points"]
        passed = (
            precision["overall"] >= 7000
            and precision["easy"] >= 8000
            and precision["small_distant"] >= 5000
            and recall["overall"] >= 7000
            and recall["easy"] >= 8500
            and recall["small_distant"] >= 5000
        )
        denominator = precision["overall"] + recall["overall"]
        f1 = (
            0 if denominator == 0 else (2 * precision["overall"] * recall["overall"] // denominator)
        )
        summaries.append(
            {
                "f1_basis_points": f1,
                "metrics": metrics,
                "passes_calibration_floors": passed,
                "threshold_millionths": threshold,
            }
        )
        if passed:
            eligible.append((f1, recall["small_distant"], threshold))
    if not eligible:
        raise BenchmarkError("no_calibration_configuration")
    return max(eligible)[2], summaries


def _median_metric_maps(values: list[dict[str, dict[str, int]]]) -> dict[str, dict[str, int]]:
    if not values:
        raise BenchmarkError("missing_metrics")
    output: dict[str, dict[str, int]] = {}
    for metric in values[0]:
        output[metric] = {}
        for stratum in values[0][metric]:
            observed = sorted(value[metric][stratum] for value in values)
            output[metric][stratum] = observed[len(observed) // 2]
    return output


def _worker(
    model_xml: Path,
    dataset_root: Path,
    annotations_path: Path,
    split: str,
    seed: int,
    confidence_floor: int,
) -> dict[str, object]:
    start_ns = time.perf_counter_ns()
    try:
        import numpy as np  # type: ignore[import-not-found]
        import openvino as ov  # type: ignore[import-not-found]
        from openvino.preprocess import (  # type: ignore[import-not-found]
            ColorFormat,
            PrePostProcessor,
            ResizeAlgorithm,
        )
    except ImportError as error:
        raise BenchmarkError("runtime_import_failed") from error

    annotations, annotation_sha256 = _load_json(annotations_path)
    selected = _items(annotations, split)
    arrays: list[tuple[dict[str, object], Any]] = []
    for item in selected:
        relative = cast(str, item["relative_path"])
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise BenchmarkError("invalid_annotations")
        raw = _read_regular(dataset_root / relative, _MAX_PPM_BYTES, "invalid_frame")
        if _sha256_bytes(raw) != item["frame_sha256"]:
            raise BenchmarkError("frame_digest_mismatch")
        width, height, pixels = _parse_ppm(raw)
        if (width, height) != (item["width"], item["height"]):
            raise BenchmarkError("frame_dimensions_mismatch")
        array = np.frombuffer(pixels, dtype=np.uint8).reshape((1, height, width, 3))
        arrays.append((item, array))

    core = ov.Core()
    if core.available_devices != ["CPU"]:
        raise BenchmarkError("unexpected_device")
    model = core.read_model(model_xml)
    preprocessing = PrePostProcessor(model)
    preprocessing.input().tensor().set_element_type(ov.Type.u8).set_layout(
        ov.Layout("NHWC")
    ).set_color_format(ColorFormat.RGB).set_spatial_dynamic_shape()
    preprocessing.input().preprocess().convert_color(ColorFormat.BGR).resize(
        ResizeAlgorithm.RESIZE_LINEAR
    )
    preprocessing.input().model().set_layout(ov.Layout("NCHW"))
    compiled = core.compile_model(
        preprocessing.build(),
        "CPU",
        {
            "INFERENCE_NUM_THREADS": 4,
            "NUM_STREAMS": 1,
            "PERFORMANCE_HINT": "LATENCY",
        },
    )
    output_port = compiled.output(0)

    _, first_array = arrays[0]
    first_output = compiled([first_array])[output_port]
    if first_output.shape[-1] != 7:
        raise BenchmarkError("invalid_model_output")
    cold_start_ns = time.perf_counter_ns() - start_ns
    for _, array in arrays[1:]:
        compiled([array])

    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    predictions: dict[str, list[dict[str, object]]] = {}
    item_timings: dict[str, int] = {}
    measurement_start = time.perf_counter_ns()
    for index, (item, array) in enumerate(arrays):
        item_start = time.perf_counter_ns()
        output = compiled([array])[output_port]
        elapsed = time.perf_counter_ns() - item_start
        item_id = cast(str, item["item_id"])
        item_timings[item_id] = elapsed
        width, height = cast(int, item["width"]), cast(int, item["height"])
        detections: list[dict[str, object]] = []
        for row in output.reshape((-1, 7)):
            values = [float(value) for value in row]
            if any(not math.isfinite(value) for value in values):
                raise BenchmarkError("non_finite_model_output")
            if values[0] < 0:
                break
            label = round(values[1])
            confidence = max(0, min(1_000_000, int(values[2] * 1_000_000)))
            if label != 0 or confidence < confidence_floor:
                continue
            left = max(0, min(width * _IOU_SCALE, math.floor(values[3] * width * _IOU_SCALE)))
            top = max(0, min(height * _IOU_SCALE, math.floor(values[4] * height * _IOU_SCALE)))
            right = max(0, min(width * _IOU_SCALE, math.ceil(values[5] * width * _IOU_SCALE)))
            bottom = max(0, min(height * _IOU_SCALE, math.ceil(values[6] * height * _IOU_SCALE)))
            if left >= right or top >= bottom:
                continue
            detections.append(
                {
                    "box_milli_pixels": [left, top, right, bottom],
                    "confidence_millionths": confidence,
                }
            )
        detections.sort(
            key=lambda detection: (
                -cast(int, detection["confidence_millionths"]),
                cast(list[int], detection["box_milli_pixels"]),
            )
        )
        predictions[item_id] = detections
        if index == 0:
            warm_start_ns = elapsed
    measurement_wall_ns = time.perf_counter_ns() - measurement_start
    usage_after = resource.getrusage(resource.RUSAGE_SELF)
    cpu_ns = int(
        (
            usage_after.ru_utime
            + usage_after.ru_stime
            - usage_before.ru_utime
            - usage_before.ru_stime
        )
        * 1_000_000_000
    )
    return {
        "annotation_lock_sha256": annotation_sha256,
        "cold_start_ns": cold_start_ns,
        "cpu_ns": max(0, cpu_ns),
        "item_wall_ns": item_timings,
        "measurement_wall_ns": measurement_wall_ns,
        "openvino_version": ov.__version__,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "predictions": predictions,
        "processed_item_count": len(arrays),
        "python_version": platform.python_version(),
        "seed": seed,
        "split": split,
        "warm_start_ns": warm_start_ns,
    }


def _logical_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_file():
            total += path.stat().st_size
    return total


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            kind = b"link"
            content_digest = _sha256_bytes(os.readlink(path).encode("utf-8"))
            size = len(os.readlink(path).encode("utf-8"))
        elif stat.S_ISREG(metadata.st_mode):
            kind = b"file"
            file_digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    file_digest.update(chunk)
            content_digest = file_digest.hexdigest()
            size = metadata.st_size
        elif stat.S_ISDIR(metadata.st_mode):
            continue
        else:
            raise BenchmarkError("invalid_runtime_tree")
        digest.update(relative + b"\0" + kind + b"\0")
        digest.update(str(size).encode("ascii") + b"\0" + content_digest.encode("ascii") + b"\n")
    return digest.hexdigest()


def _run_sandboxed(
    runtime_python: Path,
    venv_root: Path,
    model_root: Path,
    dataset_root: Path,
    split: str,
    seed: int,
    confidence_floor: int,
) -> dict[str, object]:
    model_xmls = sorted(model_root.glob("*.xml"))
    if len(model_xmls) != 1:
        raise BenchmarkError("invalid_model_root")
    interpreter_root = runtime_python.resolve().parent.parent
    command = [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--dir",
        "/sys",
        "--dir",
        "/sys/devices",
        "--dir",
        "/sys/devices/system",
        "--dir",
        "/work",
        "--dir",
        "/home",
        "--dir",
        "/home/worker",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "OPENVINO_TELEMETRY_CONSENT",
        "NO",
        "--setenv",
        "HOME",
        "/home/worker",
        "--setenv",
        "CI",
        "true",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/lib",
        "/lib",
        "--ro-bind",
        "/lib64",
        "/lib64",
        "--ro-bind",
        "/etc/ld.so.cache",
        "/etc/ld.so.cache",
        "--ro-bind",
        "/sys/devices/system/cpu",
        "/sys/devices/system/cpu",
        "--ro-bind",
        os.fspath(interpreter_root),
        os.fspath(interpreter_root),
        "--ro-bind",
        os.fspath(venv_root),
        os.fspath(venv_root),
        "--ro-bind",
        os.fspath(Path(__file__).resolve()),
        "/work/run.py",
        "--ro-bind",
        os.fspath(ANNOTATIONS_PATH),
        "/work/annotations.json",
        "--ro-bind",
        os.fspath(model_root),
        "/model",
        "--ro-bind",
        os.fspath(dataset_root),
        "/data",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        os.fspath(runtime_python),
        "/work/run.py",
        "--worker",
        "--model-xml",
        f"/model/{model_xmls[0].name}",
        "--dataset-root",
        "/data",
        "--annotations",
        "/work/annotations.json",
        "--split",
        split,
        "--seed",
        str(seed),
        "--confidence-floor",
        str(confidence_floor),
    ]
    started = time.perf_counter_ns()
    try:
        completed = subprocess.run(command, check=False, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BenchmarkError("worker_failed") from error
    process_wall_ns = time.perf_counter_ns() - started
    if completed.returncode or completed.stderr or len(completed.stdout) > _MAX_JSON_BYTES:
        raise BenchmarkError("worker_failed")
    try:
        value = _decode_json(completed.stdout)
    except (UnicodeError, ValueError) as error:
        raise BenchmarkError("worker_failed") from error
    if not isinstance(value, dict):
        raise BenchmarkError("worker_failed")
    result = cast(dict[str, object], value)
    result["process_wall_ns"] = process_wall_ns
    return result


def _validate_worker_result(
    result: dict[str, object],
    items: list[dict[str, object]],
    *,
    split: str,
    seed: int,
    annotation_sha256: str,
) -> None:
    expected_fields = {
        "annotation_lock_sha256",
        "cold_start_ns",
        "cpu_ns",
        "item_wall_ns",
        "measurement_wall_ns",
        "openvino_version",
        "peak_rss_bytes",
        "predictions",
        "process_wall_ns",
        "processed_item_count",
        "python_version",
        "seed",
        "split",
        "warm_start_ns",
    }
    positive_fields = (
        "cold_start_ns",
        "measurement_wall_ns",
        "peak_rss_bytes",
        "process_wall_ns",
        "warm_start_ns",
    )
    if (
        set(result) != expected_fields
        or result.get("annotation_lock_sha256") != annotation_sha256
        or result.get("openvino_version") != "2026.3.1-22476-759c5a6ab8c-releases/2026/3"
        or result.get("python_version") != "3.13.15"
        or result.get("seed") != seed
        or result.get("split") != split
        or result.get("processed_item_count") != len(items)
        or any(
            type(result.get(field)) is not int or cast(int, result[field]) <= 0
            for field in positive_fields
        )
        or type(result.get("cpu_ns")) is not int
        or cast(int, result["cpu_ns"]) < 0
    ):
        raise BenchmarkError("invalid_worker_result")
    expected_ids = {cast(str, item["item_id"]) for item in items}
    timings = result.get("item_wall_ns")
    predictions = result.get("predictions")
    if not isinstance(timings, dict) or not isinstance(predictions, dict):
        raise BenchmarkError("invalid_worker_result")
    if set(timings) != expected_ids or set(predictions) != expected_ids:
        raise BenchmarkError("invalid_worker_result")
    if any(type(value) is not int or value <= 0 for value in timings.values()):
        raise BenchmarkError("invalid_worker_result")
    item_by_id = {cast(str, item["item_id"]): item for item in items}
    for item_id, detections in predictions.items():
        if not isinstance(detections, list) or len(detections) > 200:
            raise BenchmarkError("invalid_worker_result")
        item = item_by_id[cast(str, item_id)]
        maximum_x = cast(int, item["width"]) * _IOU_SCALE
        maximum_y = cast(int, item["height"]) * _IOU_SCALE
        prior_confidence = 1_000_001
        for detection_value in detections:
            if not isinstance(detection_value, dict) or set(detection_value) != {
                "box_milli_pixels",
                "confidence_millionths",
            }:
                raise BenchmarkError("invalid_worker_result")
            detection = cast(dict[str, object], detection_value)
            confidence = detection["confidence_millionths"]
            box = detection["box_milli_pixels"]
            if (
                type(confidence) is not int
                or not 0 <= confidence <= prior_confidence <= 1_000_001
                or not isinstance(box, list)
                or len(box) != 4
                or any(type(coordinate) is not int or coordinate < 0 for coordinate in box)
            ):
                raise BenchmarkError("invalid_worker_result")
            left, top, right, bottom = cast(list[int], box)
            if not (left < right <= maximum_x and top < bottom <= maximum_y):
                raise BenchmarkError("invalid_worker_result")
            prior_confidence = confidence


def _ceil_ms(nanoseconds: int) -> int:
    return max(1, (nanoseconds + 999_999) // 1_000_000)


def _receipt_metrics(
    repetition: dict[str, object],
    items: list[dict[str, object]],
    threshold: int,
    install_and_model_bytes: int,
) -> dict[str, dict[str, int]]:
    accuracy = accuracy_metrics(repetition, items, threshold)
    ranked_accuracy = accuracy_metrics(repetition, items, 0)
    count = cast(int, repetition["processed_item_count"])
    wall_ns = cast(int, repetition["measurement_wall_ns"])
    fps_milli = count * 1_000_000_000_000 // wall_ns
    return {
        "cold_start_wall_ms": {"overall": _ceil_ms(cast(int, repetition["cold_start_ns"]))},
        "failure_rate_basis_points": {"overall": 0},
        "frames_per_second_milli": {"overall": fps_milli},
        "install_and_model_bytes": {"overall": install_and_model_bytes},
        "map50_basis_points": ranked_accuracy["map50_basis_points"],
        "peak_rss_bytes": {"overall": cast(int, repetition["peak_rss_bytes"])},
        "precision_basis_points": accuracy["precision_basis_points"],
        "real_time_factor_milli": {"overall": fps_milli // 5},
        "recall_basis_points": accuracy["recall_basis_points"],
        "warm_start_wall_ms": {"overall": _ceil_ms(cast(int, repetition["warm_start_ns"]))},
    }


def _aggregate(repetitions: list[dict[str, object]]) -> dict[str, object]:
    first_metrics = cast(dict[str, dict[str, int]], repetitions[0]["metrics"])
    metrics: dict[str, dict[str, int]] = {}
    dispersion: dict[str, dict[str, int]] = {}
    maximum_metrics = {"peak_rss_bytes"}
    for name, strata in first_metrics.items():
        metrics[name] = {}
        dispersion[name] = {}
        for stratum in strata:
            values = sorted(
                cast(dict[str, dict[str, int]], repetition["metrics"])[name][stratum]
                for repetition in repetitions
            )
            center = values[len(values) // 2]
            metrics[name][stratum] = max(values) if name in maximum_metrics else center
            deviations = sorted(abs(value - center) for value in values)
            dispersion[name][stratum] = deviations[len(deviations) // 2]
    return {"dispersion": dispersion, "metrics": metrics}


def _artifact(
    *,
    name: str,
    kind: str,
    artifact_format: str,
    license_expression: str,
    revision: str,
    sha256: str,
    source_url: str,
    terms_url: str,
    use: str,
    reviewed_on: str,
) -> dict[str, object]:
    return {
        "commercial_use_allowed": True,
        "executable_serialization": False,
        "format": artifact_format,
        "isolation": "isolated-worker",
        "kind": kind,
        "license_expression": license_expression,
        "name": name,
        "redistribution_allowed": True,
        "review_status": "approved-for-evaluation",
        "reviewed_on": reviewed_on,
        "revision": revision,
        "sha256": sha256,
        "source_url": source_url,
        "terms_url": terms_url,
        "trust_remote_code": False,
        "use": use,
    }


def _candidate_artifacts(
    candidate: dict[str, object], runtime: dict[str, object], evaluated_on: str
) -> list[dict[str, object]]:
    short_name = cast(str, candidate["name"]).split("-fp32", maxsplit=1)[0]
    model_revision = f"{candidate['omz_revision']}/2023.0-fp32"
    omz_terms = (
        "https://github.com/openvinotoolkit/open_model_zoo/blob/"
        f"{candidate['omz_revision']}/LICENSE"
    )
    artifacts: list[dict[str, object]] = []
    for extension in ("xml", "bin"):
        model_artifact = cast(dict[str, object], candidate[f"model_{extension}"])
        artifacts.append(
            _artifact(
                name=f"{short_name}-fp32-{extension}",
                kind="weights",
                artifact_format="openvino-ir",
                license_expression="Apache-2.0",
                revision=model_revision,
                sha256=cast(str, model_artifact["sha256"]),
                source_url=cast(str, model_artifact["url"]),
                terms_url=omz_terms,
                use="vehicle-only object detection evaluation",
                reviewed_on=evaluated_on,
            )
        )
    runtime_terms = {
        "numpy": "https://github.com/numpy/numpy/blob/v2.5.3/LICENSE.txt",
        "openvino": (
            "https://github.com/openvinotoolkit/openvino/blob/"
            "759c5a6ab8c066af5f4bc5ebd04643706012a37d/LICENSE"
        ),
        "openvino_telemetry": "https://pypi.org/project/openvino-telemetry/2025.2.0/",
    }
    formats = {
        "numpy": "native-library",
        "openvino": "native-library",
        "openvino_telemetry": "python-source",
    }
    for name in ("openvino", "numpy", "openvino_telemetry"):
        component = cast(dict[str, object], runtime[name])
        artifacts.append(
            _artifact(
                name=name.replace("_", "-"),
                kind="runtime",
                artifact_format=formats[name],
                license_expression=cast(str, component["license_expression"]),
                revision=cast(str, component["revision"]),
                sha256=cast(str, component["sha256"]),
                source_url=cast(str, component["url"]),
                terms_url=runtime_terms[name],
                use="offline CPU inference runtime closure",
                reviewed_on=evaluated_on,
            )
        )
    return artifacts


def _profile() -> dict[str, object]:
    memory_bytes = 0
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemTotal:"):
            memory_bytes = int(line.split()[1]) * 1024
            break
    cpu_model = "unknown"
    for line in Path("/proc/cpuinfo").read_text(encoding="ascii").splitlines():
        if line.startswith("model name"):
            cpu_model = line.split(":", maxsplit=1)[1].strip()
            break
    if (
        platform.system() != "Linux"
        or platform.machine() != "x86_64"
        or (os.cpu_count() or 0) != 4
        or memory_bytes < 16_000_000_000
        or any(Path("/dev").glob("nvidia*"))
        or any(Path("/dev/dri").glob("render*"))
    ):
        raise BenchmarkError("profile_mismatch")
    return {
        "architecture": "x86_64",
        "cpu_model": cpu_model,
        "gpu_present": False,
        "kernel_release": platform.release(),
        "memory_bytes": memory_bytes,
        "name": "CPU-LITE",
        "operating_system": "Linux",
        "vcpu": 4,
    }


def _write_new(path: Path, value: object) -> str:
    raw = json.dumps(value, allow_nan=False, indent=2, sort_keys=True).encode("ascii") + b"\n"
    descriptor = -1
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        written = 0
        while written < len(raw):
            written += os.write(descriptor, raw[written:])
        os.fsync(descriptor)
    except OSError as error:
        raise BenchmarkError("output_failed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _sha256_bytes(raw)


def run_benchmark(
    dataset_root: Path,
    models_root: Path,
    runtime_python: Path,
    venv_root: Path,
    wheels_root: Path,
    output_root: Path,
    source_revision: str,
    evaluated_on: str,
) -> dict[str, object]:
    if _REVISION.fullmatch(source_revision) is None:
        raise BenchmarkError("invalid_source_revision")
    if output_root.exists() or output_root.is_symlink():
        raise BenchmarkError("output_exists")
    output_root.mkdir(mode=0o700, parents=True)
    try:
        import evaluate_v02_gates as gate_evaluator
    except ImportError as error:
        raise BenchmarkError("gate_evaluator_missing") from error
    annotations, annotations_sha256 = _load_json(ANNOTATIONS_PATH)
    candidate_manifest, candidate_manifest_sha256 = _load_json(CANDIDATES_PATH)
    candidates, runtime, confidence_floor, grid = _validate_candidate_manifest(candidate_manifest)
    try:
        policy, policy_sha256 = gate_evaluator.load_policy(POLICY_PATH)
        dataset_manifest, dataset_manifest_sha256 = gate_evaluator.load_manifest(
            DATASET_MANIFEST_PATH, policy
        )
        evaluation_day = date.fromisoformat(evaluated_on)
    except (ValueError, gate_evaluator.EvaluationError) as error:
        raise BenchmarkError("invalid_gate_input") from error
    _validate_dataset_locks(annotations, dataset_manifest)
    protocol = cast(dict[str, object], policy["protocol"])
    if (
        tuple(cast(list[int], protocol["seed_set"])) != _SEEDS
        or protocol["measured_repetitions"] != 5
    ):
        raise BenchmarkError("protocol_mismatch")
    calibration_items = _items(annotations, "calibration")
    test_items = _items(annotations, "test")
    _verify_runtime_wheels(runtime, wheels_root)
    profile = _profile()
    harness_sha256 = _sha256_bytes(Path(__file__).read_bytes())
    environment_size = _logical_size(venv_root)
    environment_sha256 = _tree_sha256(venv_root)
    raw_candidates: dict[str, object] = {}
    output_index: dict[str, object] = {}

    for candidate in candidates:
        name = cast(str, candidate["name"])
        model_short = name.split("-fp32", maxsplit=1)[0]
        model_root = models_root / model_short
        for extension in ("xml", "bin"):
            expected = cast(dict[str, object], candidate[f"model_{extension}"])
            paths = sorted(model_root.glob(f"*.{extension}"))
            if len(paths) != 1:
                raise BenchmarkError("model_missing")
            raw = _read_regular(paths[0], 16 * 1024 * 1024, "invalid_model")
            if len(raw) != expected["size"] or _sha256_bytes(raw) != expected["sha256"]:
                raise BenchmarkError("model_digest_mismatch")
        calibration_repetitions = [
            _run_sandboxed(
                runtime_python,
                venv_root,
                model_root,
                dataset_root,
                "calibration",
                seed,
                confidence_floor,
            )
            for seed in _SEEDS
        ]
        for seed, repetition in zip(_SEEDS, calibration_repetitions, strict=True):
            _validate_worker_result(
                repetition,
                calibration_items,
                split="calibration",
                seed=seed,
                annotation_sha256=annotations_sha256,
            )
        threshold, calibration_grid = choose_threshold(
            calibration_repetitions, calibration_items, grid
        )
        test_repetitions = [
            _run_sandboxed(
                runtime_python,
                venv_root,
                model_root,
                dataset_root,
                "test",
                seed,
                confidence_floor,
            )
            for seed in _SEEDS
        ]
        for seed, repetition in zip(_SEEDS, test_repetitions, strict=True):
            _validate_worker_result(
                repetition,
                test_items,
                split="test",
                seed=seed,
                annotation_sha256=annotations_sha256,
            )
        install_and_model_bytes = environment_size + sum(
            cast(int, cast(dict[str, object], candidate[key])["size"])
            for key in ("model_xml", "model_bin")
        )
        receipt_repetitions: list[dict[str, object]] = []
        diagnostics: list[dict[str, object]] = []
        for repetition in test_repetitions:
            metrics = _receipt_metrics(repetition, test_items, threshold, install_and_model_bytes)
            receipt_repetitions.append(
                {
                    "failed_item_count": 0,
                    "failure_code": None,
                    "metrics": metrics,
                    "processed_item_count": len(test_items),
                    "seed": repetition["seed"],
                    "status": "pass",
                }
            )
            wall_ns = cast(int, repetition["measurement_wall_ns"])
            cpu_ns = cast(int, repetition["cpu_ns"])
            diagnostics.append(
                {
                    "cpu_nanoseconds_per_frame": cpu_ns // len(test_items),
                    "cpu_time_ns": cpu_ns,
                    "cpu_utilization_basis_points": cpu_ns * 10_000 // wall_ns,
                    "frames_per_cpu_second_milli": (
                        0 if cpu_ns == 0 else len(test_items) * 1_000_000_000_000 // cpu_ns
                    ),
                    "process_wall_ns": repetition["process_wall_ns"],
                    "seed": repetition["seed"],
                }
            )
        configuration = {
            "candidate": name,
            "confidence_threshold_millionths": threshold,
            "inference": candidate_manifest["inference"],
            "model_bin_sha256": cast(dict[str, object], candidate["model_bin"])["sha256"],
            "model_xml_sha256": cast(dict[str, object], candidate["model_xml"])["sha256"],
            "runtime": {
                key: cast(dict[str, object], runtime[key])["sha256"]
                for key in ("numpy", "openvino", "openvino_telemetry")
            },
        }
        receipt = {
            "aggregate": _aggregate(receipt_repetitions),
            "candidate": {
                "artifacts": _candidate_artifacts(candidate, runtime, evaluated_on),
                "configuration_sha256": _sha256_bytes(_canonical(configuration)),
                "name": name,
            },
            "dataset_manifest_sha256": dataset_manifest_sha256,
            "evaluated_on": evaluated_on,
            "evaluation_phase": "selection",
            "experiment": "detection",
            "implementation": {
                "evaluation_harness": {
                    "license_expression": "Apache-2.0",
                    "name": "visualworld-v02-detection-harness",
                    "revision": source_revision,
                    "sha256": harness_sha256,
                    "source_url": (
                        "https://github.com/mayank-gupta16/vision-query-system/blob/"
                        f"{source_revision}/scripts/run_v02_detection_benchmark.py"
                    ),
                },
                "executable_implementation": "cpython",
                "metric_implementation": {
                    "license_expression": "Apache-2.0",
                    "name": "visualworld-v02-detection-metrics",
                    "revision": source_revision,
                    "sha256": harness_sha256,
                    "source_url": (
                        "https://github.com/mayank-gupta16/vision-query-system/blob/"
                        f"{source_revision}/scripts/run_v02_detection_benchmark.py"
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
        receipt_name = f"{model_short}-receipt.json"
        receipt_sha256 = _write_new(output_root / receipt_name, receipt)
        try:
            validated_receipt = gate_evaluator.validate_receipt(
                receipt,
                policy,
                policy_sha256,
                dataset_manifest,
                dataset_manifest_sha256,
            )
            gate_result = gate_evaluator.evaluate(
                policy,
                validated_receipt,
                baseline=None,
                baseline_receipt_sha256=None,
                receipt_sha256=receipt_sha256,
                as_of=evaluation_day,
            )
        except gate_evaluator.EvaluationError as error:
            raise BenchmarkError("generated_receipt_invalid") from error
        gate_name = f"{model_short}-gate.json"
        gate_sha256 = _write_new(output_root / gate_name, gate_result)
        raw_candidates[name] = {
            "calibration": {
                "grid": calibration_grid,
                "repetitions": calibration_repetitions,
                "selected_threshold_millionths": threshold,
            },
            "configuration": configuration,
            "diagnostics": diagnostics,
            "test_repetitions": test_repetitions,
        }
        output_index[name] = {
            "gate": gate_name,
            "gate_sha256": gate_sha256,
            "gate_status": gate_result["status"],
            "receipt": receipt_name,
            "receipt_sha256": receipt_sha256,
        }

    raw_result = {
        "candidates": raw_candidates,
        "environment": {
            "installed_runtime_logical_bytes": environment_size,
            "installed_runtime_tree_sha256": environment_sha256,
            "network_isolation": "bubblewrap-unshare-all",
            "profile": profile,
            "runtime": runtime,
        },
        "evaluated_on": evaluated_on,
        "outputs": output_index,
        "provenance": {
            "annotation_lock_sha256": annotations_sha256,
            "candidate_manifest_sha256": candidate_manifest_sha256,
            "dataset_manifest_sha256": dataset_manifest_sha256,
            "evaluation_harness_sha256": harness_sha256,
            "policy_sha256": policy_sha256,
            "source_revision": source_revision,
        },
        "schema": "visualworld.v02-detection-raw-results",
        "schema_version": 1,
    }
    raw_sha256 = _write_new(output_root / "raw-results.json", raw_result)
    return {"outputs": output_index, "raw_results_sha256": raw_sha256, "status": "pass"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--model-xml", type=Path)
    parser.add_argument("--dataset-root", type=Path)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--split", choices=("calibration", "test"))
    parser.add_argument("--seed", type=int)
    parser.add_argument("--confidence-floor", type=int)
    parser.add_argument("--models-root", type=Path)
    parser.add_argument("--runtime-python", type=Path)
    parser.add_argument("--venv-root", type=Path)
    parser.add_argument("--wheels-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-revision")
    parser.add_argument("--evaluated-on")
    arguments = parser.parse_args()
    try:
        if arguments.worker:
            if None in (
                arguments.model_xml,
                arguments.dataset_root,
                arguments.annotations,
                arguments.split,
                arguments.seed,
                arguments.confidence_floor,
            ):
                raise BenchmarkError("missing_worker_argument")
            result = _worker(
                arguments.model_xml,
                arguments.dataset_root,
                arguments.annotations,
                arguments.split,
                arguments.seed,
                arguments.confidence_floor,
            )
        else:
            if None in (
                arguments.dataset_root,
                arguments.models_root,
                arguments.runtime_python,
                arguments.venv_root,
                arguments.wheels_root,
                arguments.output,
                arguments.source_revision,
                arguments.evaluated_on,
            ):
                raise BenchmarkError("missing_argument")
            result = run_benchmark(
                arguments.dataset_root.resolve(),
                arguments.models_root.resolve(),
                Path(os.path.abspath(arguments.runtime_python)),
                arguments.venv_root.resolve(),
                arguments.wheels_root.resolve(),
                arguments.output.resolve(),
                arguments.source_revision,
                arguments.evaluated_on,
            )
    except BenchmarkError as error:
        print(json.dumps({"error": str(error), "status": "error"}, sort_keys=True))
        return 1
    print(json.dumps(result, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
