#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Measure locked sampling policies with the selected v0.2 detector."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import struct
import sys
import tempfile
import time
from datetime import date
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import NoReturn, TextIO, cast

import evaluate_v02_gates as gate_evaluator
import run_v02_detection_benchmark as detection

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "fixtures" / "v02-sampling-research"
ANNOTATIONS_PATH = FIXTURE_ROOT / "annotations.json"
CANDIDATES_PATH = FIXTURE_ROOT / "candidates.json"
DATASET_MANIFEST_PATH = FIXTURE_ROOT / "dataset-manifest.json"
SOURCE_MANIFEST_PATH = FIXTURE_ROOT / "source-manifest.json"
DETECTION_CANDIDATES_PATH = ROOT / "fixtures" / "v02-detection-research" / "candidates.json"
DETECTION_WORKER_PATH = ROOT / "scripts" / "run_v02_detection_benchmark.py"
POLICY_PATH = ROOT / "fixtures" / "v02-evaluation" / "policy-v0.2-gates-2.json"
WIDTH = 384
HEIGHT = 216
FRAME_BYTES = WIDTH * HEIGHT * 3
MAX_CLIP_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
SEEDS = (1729, 3253, 5081, 7919, 104729)
STRATA = ("overall", "small_fast", "camera_motion", "cut_adjacent", "vfr_gap")
_IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class SamplingBenchmarkError(RuntimeError):
    """A stable sampling benchmark failure."""


def _fail(code: str) -> NoReturn:
    raise SamplingBenchmarkError(code)


def _emit(value: object, stream: TextIO) -> bool:
    try:
        stream.write(json.dumps(value, allow_nan=False, sort_keys=True) + "\n")
        stream.flush()
    except (AttributeError, OSError, UnicodeError, ValueError):
        return False
    return True


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
        raise SamplingBenchmarkError("invalid_json") from error


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_json(path: Path, code: str) -> tuple[dict[str, object], str]:
    try:
        raw = detection._read_regular(path, MAX_JSON_BYTES, code)
        value = detection._decode_json(raw)
    except detection.BenchmarkError as error:
        raise SamplingBenchmarkError(code) from error
    except (UnicodeError, ValueError) as error:
        raise SamplingBenchmarkError(code) from error
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
    if type(value) is not str or not value or len(value) > maximum:
        _fail(code)
    return value


def _validate_configuration(value: object) -> tuple[list[dict[str, object]], list[int]]:
    manifest = _mapping(value, "invalid_candidate_manifest")
    if set(manifest) != {
        "adaptive",
        "detector",
        "fixed_rates_fps",
        "metric",
        "schema",
        "schema_version",
        "selection",
    }:
        _fail("invalid_candidate_manifest")
    if (
        manifest["schema"] != "visualworld.v02-sampling-candidates"
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["fixed_rates_fps"] != [1, 2, 3, 5, 8]
    ):
        _fail("invalid_candidate_manifest")
    detector_manifest = _mapping(manifest["detector"], "invalid_candidate_manifest")
    if detector_manifest != {
        "candidate_manifest_sha256": (
            "38afb812c6681700c15b30874e91ac649f1dde521354a5ce5c27d1b4e7bafcee"
        ),
        "confidence_millionths": 950000,
        "name": "vehicle-detection-0201-fp32-openvino-2026.3.1",
    }:
        _fail("invalid_candidate_manifest")
    adaptive = _mapping(manifest["adaptive"], "invalid_candidate_manifest")
    if set(adaptive) != {
        "base_fps",
        "burst_duration_ms",
        "maximum_fps",
        "motion_score",
        "threshold_grid_millionths",
    }:
        _fail("invalid_candidate_manifest")
    if (
        adaptive["base_fps"] != 3
        or adaptive["maximum_fps"] != 8
        or adaptive["burst_duration_ms"] != 750
        or adaptive["motion_score"]
        != "maximum 32x24 tile mean absolute RGB delta in millionths of 255"
    ):
        _fail("invalid_candidate_manifest")
    grid_value = adaptive["threshold_grid_millionths"]
    if not isinstance(grid_value, list):
        _fail("invalid_candidate_manifest")
    grid = [_integer(item, "invalid_candidate_manifest", 1, 1_000_000) for item in grid_value]
    if grid != sorted(set(grid)) or len(grid) != 4:
        _fail("invalid_candidate_manifest")
    fixed = [
        {"fps": fps, "kind": "fixed", "name": f"fixed-{fps}-fps"}
        for fps in cast(list[int], manifest["fixed_rates_fps"])
    ]
    return fixed, grid


def _split_payload(
    annotations: dict[str, object], split: str
) -> tuple[list[dict[str, object]], list[dict[str, object]], bytes, bytes]:
    clips_value = annotations.get("clips")
    events_value = annotations.get("events")
    if (
        annotations.get("schema") != "visualworld.v02-sampling-annotation-lock"
        or type(annotations.get("schema_version")) is not int
        or annotations.get("schema_version") != 1
        or not isinstance(clips_value, list)
        or not isinstance(events_value, list)
    ):
        _fail("invalid_annotations")
    clips = [
        cast(dict[str, object], clip)
        for clip in clips_value
        if isinstance(clip, dict) and clip.get("split") == split
    ]
    events = [
        cast(dict[str, object], event)
        for event in events_value
        if isinstance(event, dict) and event.get("split") == split
    ]
    if not clips or not events:
        _fail("invalid_annotations")
    return (
        clips,
        events,
        _canonical({"clips": clips, "events": events}),
        _canonical([event["event_id"] for event in events]),
    )


def _validate_annotations(
    annotations: dict[str, object], dataset_manifest: dict[str, object]
) -> None:
    split_sources: dict[str, set[str]] = {}
    split_data = _mapping(dataset_manifest["splits"], "invalid_dataset")
    for split in ("calibration", "test"):
        clips, events, annotation_payload, ids_payload = _split_payload(annotations, split)
        expected = _mapping(split_data[split], "invalid_dataset")
        if (
            len(events) != expected["item_count"]
            or _sha256(annotation_payload) != expected["annotation_sha256"]
            or _sha256(ids_payload) != expected["item_ids_sha256"]
        ):
            _fail("dataset_lock_mismatch")
        clip_ids: set[str] = set()
        sources: set[str] = set()
        for clip in clips:
            if set(clip) != {
                "byte_count",
                "clip_id",
                "cut_ms",
                "duration_ms",
                "frame_count",
                "frames",
                "relative_path",
                "sha256",
                "source_id",
                "split",
            }:
                _fail("invalid_annotations")
            clip_id = _text(clip["clip_id"], "invalid_annotations")
            source_id = _text(clip["source_id"], "invalid_annotations")
            if (
                _IDENTIFIER.fullmatch(clip_id) is None
                or _IDENTIFIER.fullmatch(source_id) is None
                or clip_id in clip_ids
                or source_id in sources
            ):
                _fail("invalid_annotations")
            clip_ids.add(clip_id)
            sources.add(source_id)
            _integer(clip["byte_count"], "invalid_annotations", 1, MAX_CLIP_BYTES)
            if clip["duration_ms"] != 6000 or clip["frame_count"] != 86:
                _fail("invalid_annotations")
            relative = PurePosixPath(_text(clip["relative_path"], "invalid_annotations"))
            if relative.is_absolute() or ".." in relative.parts:
                _fail("invalid_annotations")
            frames = clip["frames"]
            if not isinstance(frames, list) or len(frames) != 86:
                _fail("invalid_annotations")
            expected_pts = 0
            for index, raw_frame in enumerate(frames):
                frame = _mapping(raw_frame, "invalid_annotations")
                if (
                    frame.get("frame_index") != index
                    or type(frame.get("frame_index")) is not int
                    or frame.get("pts_ms") != expected_pts
                    or type(frame.get("pts_ms")) is not int
                ):
                    _fail("invalid_annotations")
                expected_pts += _integer(frame.get("duration_ms"), "invalid_annotations", 1, 1000)
                digest = _text(frame.get("rgb24_sha256"), "invalid_annotations", 64)
                if len(digest) != 64:
                    _fail("invalid_annotations")
            if expected_pts != 6000:
                _fail("invalid_annotations")
        split_sources[split] = sources
        event_ids: set[str] = set()
        counts = {stratum: 0 for stratum in STRATA}
        for event in events:
            if set(event) != {
                "clip_id",
                "end_ms",
                "event_id",
                "source_id",
                "split",
                "start_ms",
                "strata",
            }:
                _fail("invalid_annotations")
            event_id = _text(event["event_id"], "invalid_annotations")
            strata = event["strata"]
            if (
                _IDENTIFIER.fullmatch(event_id) is None
                or event_id in event_ids
                or event["clip_id"] not in clip_ids
                or event["source_id"] not in sources
                or not isinstance(strata, list)
                or len(strata) != 2
                or strata[0] != "overall"
                or strata[1] not in STRATA[1:]
            ):
                _fail("invalid_annotations")
            event_ids.add(event_id)
            counts["overall"] += 1
            counts[cast(str, strata[1])] += 1
        if counts != expected["stratum_item_counts"]:
            _fail("dataset_lock_mismatch")
    if split_sources["calibration"] & split_sources["test"]:
        _fail("dataset_lock_mismatch")


def _read_clip(
    dataset_root: Path, clip: dict[str, object]
) -> list[tuple[dict[str, object], bytes]]:
    relative = PurePosixPath(cast(str, clip["relative_path"]))
    try:
        raw = detection._read_regular(
            dataset_root.joinpath(*relative.parts), MAX_CLIP_BYTES, "invalid_clip"
        )
    except detection.BenchmarkError as error:
        raise SamplingBenchmarkError("invalid_clip") from error
    if len(raw) != clip["byte_count"] or _sha256(raw) != clip["sha256"]:
        _fail("clip_digest_mismatch")
    if len(raw) < 28 or raw[4:8] != b"ftyp" or raw[24:28] != b"mdat":
        _fail("invalid_clip")
    ftyp_size = struct.unpack(">I", raw[:4])[0]
    mdat_size = struct.unpack(">I", raw[ftyp_size : ftyp_size + 4])[0]
    if ftyp_size != 20 or mdat_size != 8 + FRAME_BYTES * 86:
        _fail("invalid_clip")
    start = ftyp_size + 8
    frames_value = cast(list[dict[str, object]], clip["frames"])
    frames: list[tuple[dict[str, object], bytes]] = []
    for index, frame in enumerate(frames_value):
        pixels = raw[start + index * FRAME_BYTES : start + (index + 1) * FRAME_BYTES]
        if len(pixels) != FRAME_BYTES or _sha256(pixels) != frame["rgb24_sha256"]:
            _fail("frame_digest_mismatch")
        frames.append((frame, pixels))
    return frames


def _fixed_targets(
    frames: list[tuple[dict[str, object], bytes]], fps: int
) -> list[tuple[int, int]]:
    horizon = Fraction(6000, 1000)
    target = Fraction(0)
    step = Fraction(1, fps)
    targets: list[tuple[int, int]] = []
    while target < horizon:
        chosen_index = min(
            range(len(frames)),
            key=lambda index: (
                abs(Fraction(cast(int, frames[index][0]["pts_ms"]), 1000) - target),
                index,
            ),
        )
        error = abs(Fraction(cast(int, frames[chosen_index][0]["pts_ms"]), 1000) - target)
        error_ms = (error.numerator * 1000 + error.denominator - 1) // error.denominator
        targets.append((chosen_index, error_ms))
        target += step
    return targets


def _fixed_selection(
    frames: list[tuple[dict[str, object], bytes]], fps: int
) -> tuple[set[int], list[int]]:
    targets = _fixed_targets(frames, fps)
    return {index for index, _ in targets}, [error for _, error in targets]


def _motion_scores(frames: list[tuple[dict[str, object], bytes]]) -> list[int]:
    scores = [0]
    prior = frames[0][1]
    for _, pixels in frames[1:]:
        maximum = 0
        for tile_top in range(0, HEIGHT, 24):
            for tile_left in range(0, WIDTH, 32):
                difference = 0
                samples = 0
                for y in range(tile_top, min(HEIGHT, tile_top + 24), 4):
                    for x in range(tile_left, min(WIDTH, tile_left + 32), 4):
                        offset = (y * WIDTH + x) * 3
                        difference += sum(
                            abs(pixels[offset + channel] - prior[offset + channel])
                            for channel in range(3)
                        )
                        samples += 3
                maximum = max(maximum, difference * 1_000_000 // (samples * 255))
        scores.append(maximum)
        prior = pixels
    return scores


def _adaptive_selection(
    frames: list[tuple[dict[str, object], bytes]], threshold: int
) -> tuple[set[int], list[int], list[int]]:
    base_targets = _fixed_targets(frames, 3)
    high_targets = _fixed_targets(frames, 8)
    scores = _motion_scores(frames)
    windows: list[tuple[int, int]] = []
    for index, score in enumerate(scores):
        if score >= threshold:
            start = cast(int, frames[index][0]["pts_ms"])
            end = start + 750
            if windows and start <= windows[-1][1]:
                windows[-1] = (windows[-1][0], max(windows[-1][1], end))
            else:
                windows.append((start, end))
    selected_targets: list[tuple[int, int]] = []
    for index, error in base_targets:
        pts = cast(int, frames[index][0]["pts_ms"])
        if not any(start <= pts <= end for start, end in windows):
            selected_targets.append((index, error))
    for index, error in high_targets:
        pts = cast(int, frames[index][0]["pts_ms"])
        if any(start <= pts <= end for start, end in windows):
            selected_targets.append((index, error))
    return (
        {index for index, _ in selected_targets},
        [error for _, error in selected_targets],
        scores,
    )


def _write_bytes(path: Path, raw: bytes) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        written = 0
        while written < len(raw):
            written += os.write(descriptor, raw[written:])
    except OSError as error:
        raise SamplingBenchmarkError("scratch_write_failed") from error
    finally:
        if descriptor >= 0:
            detection._close_once(descriptor)


def _run_detection_worker(
    runtime_python: Path,
    runtime_root: Path,
    model_root: Path,
    dataset_root: Path,
    annotations_path: Path,
    split: str,
    seed: int,
) -> dict[str, object]:
    interpreter_root = runtime_python.resolve().parent.parent
    model_xmls = sorted(model_root.glob("*.xml"))
    if len(model_xmls) != 1:
        _fail("invalid_model_root")
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
        os.fspath(runtime_root),
        "/runtime",
        "--setenv",
        "PYTHONPATH",
        "/runtime",
        "--setenv",
        "PYTHONNOUSERSITE",
        "1",
        "--setenv",
        "PYTHONPYCACHEPREFIX",
        "/tmp/pycache",
        "--ro-bind",
        os.fspath(DETECTION_WORKER_PATH),
        "/work/run.py",
        "--ro-bind",
        os.fspath(annotations_path),
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
        "950000",
    ]
    started = time.perf_counter_ns()
    try:
        returncode, stdout, stderr = detection._run_bounded(
            command,
            timeout_seconds=180,
            maximum_output_bytes=MAX_JSON_BYTES,
            code="worker_failed",
        )
    except detection.BenchmarkError as error:
        raise SamplingBenchmarkError("worker_failed") from error
    process_wall_ns = time.perf_counter_ns() - started
    if returncode or stderr:
        _fail("worker_failed")
    try:
        value = detection._decode_json(stdout)
    except (UnicodeError, ValueError) as error:
        raise SamplingBenchmarkError("worker_failed") from error
    if not isinstance(value, dict):
        _fail("worker_failed")
    result = cast(dict[str, object], value)
    result["process_wall_ns"] = process_wall_ns
    return result


def _children_cpu_ns(usage: resource.struct_rusage) -> int:
    return int((usage.ru_utime + usage.ru_stime) * 1_000_000_000)


def _event_metrics(
    events: list[dict[str, object]],
    samples: list[dict[str, object]],
    predictions: dict[str, list[dict[str, object]]],
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    matches = {cast(str, event["event_id"]): 0 for event in events}
    for sample in samples:
        event_id = sample["event_id"]
        object_box = sample["object_box"]
        if event_id is None or object_box is None:
            continue
        ground_truth = detection._milli_box(object_box)
        for result in predictions[cast(str, sample["item_id"])]:
            raw_box = result["box_milli_pixels"]
            if (
                not isinstance(raw_box, list)
                or len(raw_box) != 4
                or any(type(coordinate) is not int for coordinate in raw_box)
            ):
                _fail("invalid_worker_result")
            coordinates = cast(list[int], raw_box)
            box = (coordinates[0], coordinates[1], coordinates[2], coordinates[3])
            if detection.iou_basis_points(ground_truth, box) >= 5000:
                matches[cast(str, event_id)] += 1
                break
    recall: dict[str, int] = {}
    missed: dict[str, int] = {}
    counts: dict[str, int] = {}
    for stratum in STRATA:
        selected = [event for event in events if stratum in cast(list[str], event["strata"])]
        counts[stratum] = len(selected)
        recalled = sum(matches[cast(str, event["event_id"])] >= 1 for event in selected)
        missed_count = sum(matches[cast(str, event["event_id"])] < 2 for event in selected)
        recall[stratum] = recalled * 10_000 // len(selected)
        missed[stratum] = missed_count * 10_000 // len(selected)
    return recall, missed, counts


def _evaluate_policy(
    *,
    policy: dict[str, object],
    split: str,
    seed: int,
    dataset_root: Path,
    clips: list[dict[str, object]],
    events: list[dict[str, object]],
    runtime_python: Path,
    runtime_root: Path,
    model_root: Path,
) -> dict[str, object]:
    started_wall = time.perf_counter_ns()
    started_parent_cpu = time.process_time_ns()
    started_children = _children_cpu_ns(resource.getrusage(resource.RUSAGE_CHILDREN))
    scratch = tempfile.TemporaryDirectory(prefix="visualworld-v02-sampling-")
    scratch_root = Path(scratch.name)
    samples: list[dict[str, object]] = []
    target_errors: list[int] = []
    clip_diagnostics: list[dict[str, object]] = []
    decode_wall_ns = 0
    stored_rgb_bytes = 0
    for clip in clips:
        decode_start = time.perf_counter_ns()
        frames = _read_clip(dataset_root, clip)
        if policy["kind"] == "fixed":
            selected, errors = _fixed_selection(frames, cast(int, policy["fps"]))
            scores: list[int] = []
        else:
            selected, errors, scores = _adaptive_selection(
                frames, cast(int, policy["motion_threshold_millionths"])
            )
        decode_wall_ns += time.perf_counter_ns() - decode_start
        target_errors.extend(errors)
        clip_diagnostics.append(
            {
                "clip_id": clip["clip_id"],
                "maximum_motion_score_millionths": max(scores, default=0),
                "motion_trigger_count": sum(
                    score >= cast(int, policy.get("motion_threshold_millionths", 1_000_001))
                    for score in scores
                ),
                "sample_count": len(selected),
            }
        )
        for frame_index in sorted(selected):
            frame, pixels = frames[frame_index]
            item_id = f"{clip['clip_id']}-frame-{frame_index:03d}"
            ppm = f"P6\n{WIDTH} {HEIGHT}\n255\n".encode("ascii") + pixels
            filename = f"{item_id}.ppm"
            _write_bytes(scratch_root / filename, ppm)
            stored_rgb_bytes += len(pixels)
            samples.append(
                {
                    "duration_ms": frame["duration_ms"],
                    "event_id": frame["event_id"],
                    "frame_index": frame_index,
                    "frame_sha256": _sha256(ppm),
                    "height": HEIGHT,
                    "item_id": item_id,
                    "object_box": frame["object_box"],
                    "pts_ms": frame["pts_ms"],
                    "relative_path": filename,
                    "source_id": clip["source_id"],
                    "split": split,
                    "stratum": frame["stratum"] or "no_event",
                    "width": WIDTH,
                }
            )
    worker_items = [
        {
            **sample,
            "object_box": sample["object_box"] or [0, 0, 1, 1],
        }
        for sample in samples
    ]
    worker_annotations = {
        "items": worker_items,
        "schema": "visualworld.v02-detection-annotation-lock",
        "schema_version": 1,
    }
    annotation_raw = (
        json.dumps(worker_annotations, allow_nan=False, indent=2, sort_keys=True).encode("ascii")
        + b"\n"
    )
    annotation_path = scratch_root / "annotations.json"
    _write_bytes(annotation_path, annotation_raw)
    worker = _run_detection_worker(
        runtime_python,
        runtime_root,
        model_root,
        scratch_root,
        annotation_path,
        split,
        seed,
    )
    try:
        detection._validate_worker_result(
            worker,
            worker_items,
            split=split,
            seed=seed,
            annotation_sha256=_sha256(annotation_raw),
        )
    except detection.BenchmarkError as error:
        raise SamplingBenchmarkError("invalid_worker_result") from error
    predictions = cast(dict[str, list[dict[str, object]]], worker["predictions"])
    recall, missed, event_counts = _event_metrics(events, samples, predictions)
    wall_ns = time.perf_counter_ns() - started_wall
    parent_cpu_ns = time.process_time_ns() - started_parent_cpu
    child_cpu_ns = _children_cpu_ns(resource.getrusage(resource.RUSAGE_CHILDREN)) - started_children
    source_ms = sum(cast(int, clip["duration_ms"]) for clip in clips)
    sample_rate = len(samples) * 60_000 // source_ms
    store_rate = stored_rgb_bytes * 60_000 // source_ms
    metrics = {
        "failure_rate_basis_points": {"overall": 0},
        "missed_track_opportunity_rate_basis_points": missed,
        "object_event_recall_basis_points": recall,
        "peak_rss_bytes": {
            "overall": max(
                cast(int, worker["peak_rss_bytes"]),
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            )
        },
        "real_time_factor_milli": {"overall": source_ms * 1_000_000_000 // wall_ns},
        "samples_per_source_minute": {"overall": sample_rate},
        "store_bytes_per_source_minute": {"overall": store_rate},
    }
    output = {
        "clip_diagnostics": clip_diagnostics,
        "cpu_ns": parent_cpu_ns + child_cpu_ns,
        "decode_and_motion_wall_ns": decode_wall_ns,
        "detection_cpu_ns": worker["cpu_ns"],
        "detection_process_wall_ns": worker["process_wall_ns"],
        "event_counts": event_counts,
        "failed_item_count": 0,
        "failure_code": None,
        "maximum_nearest_frame_error_ms": max(target_errors),
        "median_nearest_frame_error_ms": sorted(target_errors)[len(target_errors) // 2],
        "metrics": metrics,
        "policy": policy,
        "predictions": predictions,
        "processed_item_count": len(events),
        "sample_count": len(samples),
        "samples": samples,
        "seed": seed,
        "status": "pass",
        "wall_ns": wall_ns,
        "worker_environment": {
            "numpy_version": worker["numpy_version"],
            "openvino_version": worker["openvino_version"],
            "python_version": worker["python_version"],
            "telemetry_version": worker["telemetry_version"],
        },
    }
    scratch.cleanup()
    return output


def _passes_sampling_floors(metrics: dict[str, dict[str, int]]) -> bool:
    recall = metrics["object_event_recall_basis_points"]
    missed = metrics["missed_track_opportunity_rate_basis_points"]
    return (
        recall["overall"] >= 9500
        and recall["small_fast"] >= 9000
        and recall["camera_motion"] >= 9000
        and recall["cut_adjacent"] >= 9500
        and recall["vfr_gap"] >= 9500
        and missed["overall"] <= 500
        and missed["small_fast"] <= 1000
        and missed["camera_motion"] <= 1000
        and missed["cut_adjacent"] <= 500
        and missed["vfr_gap"] <= 500
    )


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
    first_prediction_sha256 = _sha256(_canonical(repetitions[0]["predictions"]))
    first_samples_sha256 = _sha256(_canonical(repetitions[0]["samples"]))
    for index, repetition in enumerate(repetitions):
        raw = {
            key: value for key, value in repetition.items() if key not in {"predictions", "samples"}
        }
        prediction_sha256 = _sha256(_canonical(repetition["predictions"]))
        samples_sha256 = _sha256(_canonical(repetition["samples"]))
        raw["predictions_sha256"] = prediction_sha256
        raw["samples_sha256"] = samples_sha256
        if index == 0 or prediction_sha256 != first_prediction_sha256:
            raw["representative_predictions"] = repetition["predictions"]
        if index == 0 or samples_sha256 != first_samples_sha256:
            raw["representative_samples"] = repetition["samples"]
        compact.append(raw)
    return compact


def run_benchmark(
    *,
    dataset_root: Path,
    models_root: Path,
    base_python: Path,
    wheels_root: Path,
    output_root: Path,
    source_revision: str,
    evaluated_on: str,
) -> dict[str, object]:
    if detection._REVISION.fullmatch(source_revision) is None:
        _fail("invalid_source_revision")
    annotations, annotations_sha256 = _load_json(ANNOTATIONS_PATH, "invalid_annotations")
    candidates_manifest, candidates_sha256 = _load_json(
        CANDIDATES_PATH, "invalid_candidate_manifest"
    )
    source_manifest, source_manifest_sha256 = _load_json(
        SOURCE_MANIFEST_PATH, "invalid_source_manifest"
    )
    detection_candidates_manifest, detection_candidates_sha256 = _load_json(
        DETECTION_CANDIDATES_PATH, "invalid_detector_manifest"
    )
    fixed_policies, adaptive_grid = _validate_configuration(candidates_manifest)
    try:
        detection_candidates, runtime, _, _ = detection._validate_candidate_manifest(
            detection_candidates_manifest
        )
    except detection.BenchmarkError as error:
        raise SamplingBenchmarkError("invalid_detector_manifest") from error
    if (
        detection_candidates_sha256
        != cast(dict[str, object], candidates_manifest["detector"])["candidate_manifest_sha256"]
    ):
        _fail("detector_manifest_mismatch")
    selected_detector = next(
        candidate
        for candidate in detection_candidates
        if candidate["name"] == cast(dict[str, object], candidates_manifest["detector"])["name"]
    )
    try:
        policy, policy_sha256 = gate_evaluator.load_policy(POLICY_PATH)
        dataset_manifest, dataset_manifest_sha256 = gate_evaluator.load_manifest(
            DATASET_MANIFEST_PATH, policy
        )
        evaluation_day = date.fromisoformat(evaluated_on)
    except (ValueError, gate_evaluator.EvaluationError) as error:
        raise SamplingBenchmarkError("invalid_gate_input") from error
    _validate_annotations(annotations, dataset_manifest)
    acquisition = _mapping(dataset_manifest["acquisition"], "invalid_dataset")
    source_clips = source_manifest.get("clips")
    if (
        source_manifest.get("schema") != "visualworld.v02-sampling-source-manifest"
        or source_manifest.get("schema_version") != 1
        or not isinstance(source_clips, list)
        or len(source_clips) != 10
        or source_manifest_sha256 != acquisition["sha256"]
        or annotations["source_manifest_sha256"] != source_manifest_sha256
    ):
        _fail("source_manifest_mismatch")
    calibration_clips, calibration_events, _, _ = _split_payload(annotations, "calibration")
    test_clips, test_events, _, _ = _split_payload(annotations, "test")
    try:
        wheels = detection._verify_runtime_wheels(runtime, wheels_root)
        python_attestation = detection._verify_base_python(base_python, runtime)
    except detection.BenchmarkError as error:
        raise SamplingBenchmarkError("runtime_verification_failed") from error
    runtime_temporary = tempfile.TemporaryDirectory(prefix="visualworld-v02-sampling-runtime-")
    runtime_root = Path(runtime_temporary.name) / "site-packages"
    try:
        runtime_tree_sha256 = detection._extract_runtime(wheels, runtime_root)
    except detection.BenchmarkError as error:
        raise SamplingBenchmarkError("runtime_verification_failed") from error
    runtime_closure_sha256 = _sha256(
        _canonical(
            {
                "python": python_attestation,
                "runtime_tree_sha256": runtime_tree_sha256,
                "wheels": {name: _sha256(raw) for name, raw in sorted(wheels.items())},
            }
        )
    )
    model_short = "vehicle-detection-0201"
    model_root = models_root / model_short
    for extension in ("xml", "bin"):
        expected = cast(dict[str, object], selected_detector[f"model_{extension}"])
        paths = sorted(model_root.glob(f"*.{extension}"))
        if len(paths) != 1:
            _fail("model_missing")
        try:
            raw = detection._read_regular(paths[0], 16 * 1024 * 1024, "invalid_model")
        except detection.BenchmarkError as error:
            raise SamplingBenchmarkError("invalid_model") from error
        if len(raw) != expected["size"] or _sha256(raw) != expected["sha256"]:
            _fail("model_digest_mismatch")

    calibration: list[dict[str, object]] = []
    eligible_thresholds: list[tuple[int, int]] = []
    for threshold in adaptive_grid:
        adaptive_policy = {
            "base_fps": 3,
            "burst_duration_ms": 750,
            "kind": "adaptive",
            "maximum_fps": 8,
            "motion_threshold_millionths": threshold,
            "name": "adaptive-3-to-8-fps",
        }
        _evaluate_policy(
            policy=adaptive_policy,
            split="calibration",
            seed=SEEDS[0],
            dataset_root=dataset_root,
            clips=calibration_clips,
            events=calibration_events,
            runtime_python=base_python,
            runtime_root=runtime_root,
            model_root=model_root,
        )
        repetitions = [
            _evaluate_policy(
                policy=adaptive_policy,
                split="calibration",
                seed=seed,
                dataset_root=dataset_root,
                clips=calibration_clips,
                events=calibration_events,
                runtime_python=base_python,
                runtime_root=runtime_root,
                model_root=model_root,
            )
            for seed in SEEDS
        ]
        aggregate = _aggregate(repetitions)
        passed = _passes_sampling_floors(cast(dict[str, dict[str, int]], aggregate["metrics"]))
        sample_counts = sorted(cast(int, repetition["sample_count"]) for repetition in repetitions)
        median_sample_count = sample_counts[len(sample_counts) // 2]
        calibration.append(
            {
                "aggregate": aggregate,
                "passes_accuracy_floors": passed,
                "repetitions": _raw_repetitions(repetitions),
                "sample_count": median_sample_count,
                "threshold_millionths": threshold,
            }
        )
        if passed:
            eligible_thresholds.append((median_sample_count, threshold))
    if not eligible_thresholds:
        _fail("no_adaptive_calibration_configuration")
    adaptive_threshold = min(eligible_thresholds)[1]
    policies = [
        *fixed_policies,
        {
            "base_fps": 3,
            "burst_duration_ms": 750,
            "kind": "adaptive",
            "maximum_fps": 8,
            "motion_threshold_millionths": adaptive_threshold,
            "name": "adaptive-3-to-8-fps",
        },
    ]
    profile = detection._profile()
    harness_sha256 = _sha256(Path(__file__).read_bytes())
    detector_harness_sha256 = _sha256(DETECTION_WORKER_PATH.read_bytes())
    output_descriptor = detection._create_private_directory(output_root)
    raw_candidates: dict[str, object] = {}
    output_index: dict[str, object] = {}
    try:
        for selected_policy in policies:
            _evaluate_policy(
                policy=selected_policy,
                split="test",
                seed=SEEDS[0],
                dataset_root=dataset_root,
                clips=test_clips,
                events=test_events,
                runtime_python=base_python,
                runtime_root=runtime_root,
                model_root=model_root,
            )
            repetitions = [
                _evaluate_policy(
                    policy=selected_policy,
                    split="test",
                    seed=seed,
                    dataset_root=dataset_root,
                    clips=test_clips,
                    events=test_events,
                    runtime_python=base_python,
                    runtime_root=runtime_root,
                    model_root=model_root,
                )
                for seed in SEEDS
            ]
            receipt_repetitions = [_receipt_repetition(item) for item in repetitions]
            aggregate = _aggregate(receipt_repetitions)
            configuration = {
                "detector_confidence_millionths": 950000,
                "detector_name": selected_detector["name"],
                "sampling_policy": selected_policy,
            }
            candidate_name = cast(str, selected_policy["name"])
            receipt = {
                "aggregate": aggregate,
                "candidate": {
                    "artifacts": detection._candidate_artifacts(
                        selected_detector, runtime, evaluated_on
                    ),
                    "configuration_sha256": _sha256(_canonical(configuration)),
                    "name": candidate_name,
                    "runtime_closure_sha256": runtime_closure_sha256,
                },
                "dataset_manifest_sha256": dataset_manifest_sha256,
                "evaluated_on": evaluated_on,
                "evaluation_phase": "selection",
                "experiment": "sampling",
                "implementation": {
                    "evaluation_harness": {
                        "license_expression": "Apache-2.0",
                        "name": "visualworld-v02-sampling-harness",
                        "revision": source_revision,
                        "sha256": harness_sha256,
                        "source_url": (
                            "https://github.com/mayank-gupta16/vision-query-system/blob/"
                            f"{source_revision}/scripts/run_v02_sampling_benchmark.py"
                        ),
                    },
                    "executable_implementation": "cpython",
                    "metric_implementation": {
                        "license_expression": "Apache-2.0",
                        "name": "visualworld-v02-sampling-metrics",
                        "revision": source_revision,
                        "sha256": harness_sha256,
                        "source_url": (
                            "https://github.com/mayank-gupta16/vision-query-system/blob/"
                            f"{source_revision}/scripts/run_v02_sampling_benchmark.py"
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
            receipt_name = f"{candidate_name}-receipt.json"
            try:
                receipt_sha256 = detection._write_new(output_descriptor, receipt_name, receipt)
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
            except (detection.BenchmarkError, gate_evaluator.EvaluationError) as error:
                raise SamplingBenchmarkError("generated_receipt_invalid") from error
            gate_name = f"{candidate_name}-gate.json"
            try:
                gate_sha256 = detection._write_new(output_descriptor, gate_name, gate)
            except detection.BenchmarkError as error:
                raise SamplingBenchmarkError("output_failed") from error
            raw_candidates[candidate_name] = {
                "configuration": configuration,
                "repetitions": _raw_repetitions(repetitions),
            }
            output_index[candidate_name] = {
                "gate": gate_name,
                "gate_sha256": gate_sha256,
                "gate_status": gate["status"],
                "receipt": receipt_name,
                "receipt_sha256": receipt_sha256,
            }
        raw_result = {
            "adaptive_calibration": calibration,
            "adaptive_threshold_millionths": adaptive_threshold,
            "candidates": raw_candidates,
            "environment": {
                "detector_runtime_tree_sha256": runtime_tree_sha256,
                "profile": profile,
                "python": python_attestation,
                "runtime_closure_sha256": runtime_closure_sha256,
            },
            "evaluated_on": evaluated_on,
            "outputs": output_index,
            "provenance": {
                "annotation_lock_sha256": annotations_sha256,
                "candidate_manifest_sha256": candidates_sha256,
                "dataset_manifest_sha256": dataset_manifest_sha256,
                "detection_candidate_manifest_sha256": detection_candidates_sha256,
                "detection_worker_sha256": detector_harness_sha256,
                "evaluation_harness_sha256": harness_sha256,
                "policy_sha256": policy_sha256,
                "source_manifest_sha256": source_manifest_sha256,
                "source_revision": source_revision,
            },
            "schema": "visualworld.v02-sampling-raw-results",
            "schema_version": 1,
        }
        try:
            raw_sha256 = detection._write_new(output_descriptor, "raw-results.json", raw_result)
        except detection.BenchmarkError as error:
            raise SamplingBenchmarkError("output_failed") from error
    finally:
        detection._close_once(output_descriptor)
        runtime_temporary.cleanup()
    return {"outputs": output_index, "raw_results_sha256": raw_sha256, "status": "pass"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--models-root", required=True, type=Path)
    parser.add_argument("--base-python", required=True, type=Path)
    parser.add_argument("--wheels-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--evaluated-on", required=True)
    arguments = parser.parse_args()
    try:
        result = run_benchmark(
            dataset_root=arguments.dataset_root.resolve(),
            models_root=arguments.models_root.resolve(),
            base_python=Path(os.path.abspath(arguments.base_python)),
            wheels_root=arguments.wheels_root.resolve(),
            output_root=Path(os.path.abspath(arguments.output)),
            source_revision=arguments.source_revision,
            evaluated_on=arguments.evaluated_on,
        )
    except (OSError, SamplingBenchmarkError) as error:
        code = str(error) if isinstance(error, SamplingBenchmarkError) else "filesystem_failed"
        _emit({"error": code, "status": "error"}, sys.stdout)
        return 1
    return 0 if _emit(result, sys.stdout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
