#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Calibrate and measure bounded short-term trackers for issue #24."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import sys
import tempfile
import time
from contextlib import suppress
from datetime import date
from pathlib import Path, PurePosixPath
from typing import NoReturn, TextIO, cast

import evaluate_v02_gates as gate_evaluator
import prepare_v02_tracking_dataset as preparation
import run_v02_detection_benchmark as detection
import run_v02_sampling_benchmark as sampling
import v02_tracking_metrics as tracking

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "fixtures" / "v02-tracking-research"
ANNOTATIONS_PATH = FIXTURE_ROOT / "annotations.json"
CANDIDATES_PATH = FIXTURE_ROOT / "candidates.json"
DATASET_MANIFEST_PATH = FIXTURE_ROOT / "dataset-manifest.json"
SOURCE_MANIFEST_PATH = FIXTURE_ROOT / "source-manifest.json"
CALIBRATION_RESULTS_PATH = FIXTURE_ROOT / "results" / "calibration-results.json"
CALIBRATION_SELECTION_PATH = FIXTURE_ROOT / "calibration-selection.json"
DETECTION_ROOT = ROOT / "fixtures" / "v02-detection-research"
SAMPLING_ROOT = ROOT / "fixtures" / "v02-sampling-research"
DETECTION_CANDIDATES_PATH = DETECTION_ROOT / "candidates.json"
DETECTION_ANNOTATIONS_PATH = DETECTION_ROOT / "annotations.json"
DETECTION_SOURCE_PATH = DETECTION_ROOT / "source-manifest.json"
DETECTION_RECEIPT_PATH = DETECTION_ROOT / "results" / "vehicle-detection-0201-receipt.json"
DETECTION_GATE_PATH = DETECTION_ROOT / "results" / "vehicle-detection-0201-gate.json"
SAMPLING_ANNOTATIONS_PATH = SAMPLING_ROOT / "annotations.json"
SAMPLING_CANDIDATES_PATH = SAMPLING_ROOT / "candidates.json"
SAMPLING_DATASET_PATH = SAMPLING_ROOT / "dataset-manifest.json"
SAMPLING_SOURCE_PATH = SAMPLING_ROOT / "source-manifest.json"
SAMPLING_RECEIPT_PATH = SAMPLING_ROOT / "results" / "fixed-5-fps-receipt.json"
SAMPLING_GATE_PATH = SAMPLING_ROOT / "results" / "fixed-5-fps-gate.json"
DETECTION_WORKER_PATH = ROOT / "scripts" / "run_v02_detection_benchmark.py"
SAMPLING_HARNESS_PATH = ROOT / "scripts" / "run_v02_sampling_benchmark.py"
GATE_EVALUATOR_PATH = ROOT / "scripts" / "evaluate_v02_gates.py"
DATASET_VALIDATOR_PATH = ROOT / "scripts" / "prepare_v02_tracking_dataset.py"
DETECTION_DATASET_HELPER_PATH = ROOT / "scripts" / "prepare_v02_detection_dataset.py"
SAMPLING_DATASET_HELPER_PATH = ROOT / "scripts" / "prepare_v02_sampling_dataset.py"
METRIC_PATH = ROOT / "scripts" / "v02_tracking_metrics.py"
POLICY_PATH = ROOT / "fixtures" / "v02-evaluation" / "policy-v0.2-gates-2.json"
WIDTH = 640
HEIGHT = 360
FRAME_BYTES = WIDTH * HEIGHT * 3
MAX_CLIP_BYTES = 48 * 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_DETECTIONS = 64
SEEDS = (1729, 3253, 5081, 7919, 104729)
STRATA = ("overall", "camera_motion", "cuts", "occlusion", "dense_crossing")
METRIC_STRATA = {
    "failure_rate_basis_points": ("overall",),
    "false_continuations_across_cuts": ("cuts",),
    "fragmentations_per_1000_track_frames": STRATA,
    "hota_basis_points": STRATA,
    "idf1_basis_points": STRATA,
    "id_switches_per_1000_track_frames": STRATA,
    "peak_rss_bytes": ("overall",),
    "real_time_factor_milli": ("overall",),
}
EXPECTED_CANDIDATE_MANIFEST_SHA256 = (
    "1dac06c203ae13f4610e9235c4fd8affcc13af3e085c282cc9c6e13bf425ea65"
)
MAX_FAILURE_EXAMPLES_PER_KIND = 8
_IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class TrackingBenchmarkError(RuntimeError):
    """A stable tracking benchmark failure."""


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise TrackingBenchmarkError("invalid_arguments")

    def _print_message(self, message: str, file: object | None = None) -> None:
        if message:
            _write_text(message, sys.stderr if file is None else cast(TextIO, file))


def _fail(code: str) -> NoReturn:
    raise TrackingBenchmarkError(code)


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
        raw = json.dumps(value, allow_nan=False, sort_keys=True) + "\n"
    except (TypeError, UnicodeError, ValueError):
        _silence_stream(stream)
        return False
    return _write_text(raw, stream)


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
        raise TrackingBenchmarkError("invalid_json") from error


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_json(path: Path, code: str) -> tuple[dict[str, object], str]:
    try:
        raw = detection._read_regular(path, MAX_JSON_BYTES, code)
        value = detection._decode_json(raw)
    except (detection.BenchmarkError, UnicodeError, ValueError) as error:
        raise TrackingBenchmarkError(code) from error
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        _fail(code)
    return cast(dict[str, object], value), _sha256(raw)


def _mapping(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        _fail(code)
    return cast(dict[str, object], value)


def _integer(value: object, code: str, minimum: int = 0, maximum: int = (1 << 63) - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(code)
    return value


def _text(value: object, code: str, maximum: int = 256) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        _fail(code)
    return value


def _digest(value: object, code: str) -> str:
    selected = _text(value, code, 64)
    if re.fullmatch(r"[0-9a-f]{64}", selected) is None:
        _fail(code)
    return selected


def _validate_candidates(
    value: dict[str, object], digest: str
) -> tuple[list[dict[str, object]], list[int], list[int], list[int]]:
    code = "invalid_candidate_manifest"
    if set(value) != {
        "calibration",
        "candidates",
        "dataset",
        "detector",
        "metric_reference",
        "sampling",
        "schema",
        "schema_version",
        "termination",
    } or (
        value["schema"] != "visualworld.v02-tracking-candidate-list"
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        _fail(code)
    calibration = _mapping(value["calibration"], code)
    if set(calibration) != {
        "cut_threshold_grid_basis_points",
        "iou_threshold_grid_basis_points",
        "max_missed_samples_grid",
        "selection",
        "split",
    } or (
        calibration["split"] != "calibration"
        or calibration["selection"]
        != (
            "for each tracker family, configurations passing every calibration gate are ordered "
            "by higher overall HOTA, higher overall IDF1, higher worst hard-stratum HOTA, fewer "
            "overall ID switches, fewer overall fragmentations, fewer maximum missed samples, "
            "higher IoU threshold, and lower cut threshold"
        )
    ):
        _fail(code)
    grids: list[list[int]] = []
    for name, expected in (
        ("iou_threshold_grid_basis_points", [1000, 3000, 5000]),
        ("max_missed_samples_grid", [0, 2, 5]),
        ("cut_threshold_grid_basis_points", [1500, 2500, 3500]),
    ):
        raw_grid = calibration[name]
        if not isinstance(raw_grid, list):
            _fail(code)
        grid = [_integer(item, code, 0, 10_000) for item in raw_grid]
        if grid != expected:
            _fail(code)
        grids.append(grid)
    candidate_values = value["candidates"]
    expected_candidates = [
        {
            "association": (
                "exact maximum-total-IoU one-to-one assignment with deterministic tie-breaks"
            ),
            "motion": "last observed box",
            "name": "global-last-iou",
        },
        {
            "association": (
                "exact maximum-total-IoU one-to-one assignment with deterministic tie-breaks"
            ),
            "motion": (
                "constant center velocity from the two latest observations, bounded to the canvas"
            ),
            "name": "global-velocity-iou",
        },
    ]
    if not isinstance(candidate_values, list) or _canonical(candidate_values) != _canonical(
        expected_candidates
    ):
        _fail(code)
    detector = _mapping(value["detector"], code)
    expected_detector = {
        "annotation_sha256": ("6c35c9d961fa274d4cdf3682d927ac9c22cb8587832dd099a3f014662363bbe8"),
        "candidate_manifest_sha256": (
            "38afb812c6681700c15b30874e91ac649f1dde521354a5ce5c27d1b4e7bafcee"
        ),
        "confidence_millionths": 950000,
        "dataset_manifest_sha256": (
            "7c41fda4b14553922727e31801478b569a17dda176dd85a7c2fe8789ae9c1599"
        ),
        "gate_sha256": "998821517c254e305e00ebbfdf0e67e04897550eb2f48742851ea624757b5085",
        "name": "vehicle-detection-0201-fp32-openvino-2026.3.1",
        "receipt_sha256": ("e7f25d13788d657aea46b0c3ba9b7cdbbcdbbcfa017b80c2da53397bc8e5bb33"),
        "source_manifest_sha256": (
            "d85631980f31fb2bec6b5a9ec098b35c336365ed263efed5cd17e01d1fa12f66"
        ),
    }
    dataset = _mapping(value["dataset"], code)
    expected_dataset = {
        "annotation_sha256": "531132855a677b97bf76545a07eb10626b8c5b4187c21e047c338df459e5eb3c",
        "dataset_manifest_sha256": (
            "38debc4c0b6e4ec15e84170c56ff1b896f74e39743be9b6dfb2a154db88c25f9"
        ),
        "source_manifest_sha256": (
            "07a02287ea612a5baf28245f1be98867cff270604c4e4e21aa78b6ffa10a005b"
        ),
    }
    sampling_lock = _mapping(value["sampling"], code)
    expected_sampling = {
        "annotation_sha256": ("46e2d5f6511d972b769d93df3d03854f54b64c9f6bce39854cf2461c6fe21089"),
        "candidate_manifest_sha256": (
            "cd1ea1d6a7a2d2b63d32f8ea1c416c533551c3e6423d92e54ecc7365c4aac40a"
        ),
        "dataset_manifest_sha256": (
            "e51ebe8ae7c1732e4c2344e2b409cd22e60a51e41ac3f22d341353403d6ae4ef"
        ),
        "gate_sha256": "a3289a458f42ca4a54f09469507cc50ee43de62740bb95a34658e18af1545945",
        "name": "fixed-5-fps",
        "receipt_sha256": ("6f9b37f6b5e5e660d7034cd37a7bdeee223681733a27f7684a2bc4cce58108a6"),
        "source_manifest_sha256": (
            "4a53262d93f2473a747bfc3c36b38209d42cafff38c9d8251cc3e503eba81139"
        ),
    }
    reference = _mapping(value["metric_reference"], code)
    expected_reference = {
        "hota_sha256": "c582255c3d36bdb49c3ceeb2a5bd912cee009190dfcfe465b4173d03aaa97d6a",
        "identity_sha256": ("e76b1bdcbf5b193662431596a984cf70a5be32e903eb5f2a3d9921b4762f93ba"),
        "license_expression": "MIT",
        "license_sha256": "f7e548c5729e464d878dd4abf691e323bbea028a756d08395d88b83e43dd2d86",
        "name": "TrackEval",
        "revision": "12c8791b303e0a0b50f753af204249e622d0281a",
        "source_url": "https://github.com/JonathonLuiten/TrackEval",
    }
    termination = _mapping(value["termination"], code)
    expected_termination = {
        "cut_score": ("mean absolute RGB delta sampled every fourth pixel, in basis points of 255"),
        "cut_semantics": (
            "a score meeting the calibrated threshold terminates every active track before "
            "processing the first post-cut frame"
        ),
        "gap_semantics": (
            "associate first, increment misses for each unmatched active track, and terminate "
            "when misses become max_missed_samples plus one; never emit extrapolated observations"
        ),
        "scope": (
            "clip-local occurrence tracklets only; no appearance embedding, cross-clip identity, "
            "or persistent entity ReID"
        ),
    }
    if (
        _canonical(detector) != _canonical(expected_detector)
        or _canonical(dataset) != _canonical(expected_dataset)
        or _canonical(sampling_lock) != _canonical(expected_sampling)
        or _canonical(reference) != _canonical(expected_reference)
        or _canonical(termination) != _canonical(expected_termination)
        or digest != EXPECTED_CANDIDATE_MANIFEST_SHA256
    ):
        _fail(code)
    return cast(list[dict[str, object]], candidate_values), grids[0], grids[1], grids[2]


def _verify_tracking_dataset_lock(
    candidates: dict[str, object],
    *,
    source_sha256: str,
    annotations_sha256: str,
    dataset_sha256: str,
) -> None:
    lock = _mapping(candidates["dataset"], "tracking_dataset_mismatch")
    observed = {
        "annotation_sha256": annotations_sha256,
        "dataset_manifest_sha256": dataset_sha256,
        "source_manifest_sha256": source_sha256,
    }
    if _canonical(lock) != _canonical(observed):
        _fail("tracking_dataset_mismatch")


def _split_payload(
    annotations: dict[str, object], split: str
) -> tuple[list[dict[str, object]], list[dict[str, object]], bytes, bytes]:
    clips = [
        cast(dict[str, object], clip)
        for clip in cast(list[object], annotations["clips"])
        if isinstance(clip, dict) and clip.get("split") == split
    ]
    tracks = [
        cast(dict[str, object], track)
        for track in cast(list[object], annotations["tracks"])
        if isinstance(track, dict) and track.get("split") == split
    ]
    identifiers = [cast(str, track["track_id"]) for track in tracks]
    return (
        clips,
        tracks,
        _canonical({"clips": clips, "tracks": tracks}),
        ("\n".join(identifiers) + "\n").encode("ascii"),
    )


def _validate_annotations(
    annotations: dict[str, object],
    dataset: dict[str, object],
    source_sha256: str,
) -> None:
    code = "invalid_annotations"
    if (
        set(annotations)
        != {"clips", "schema", "schema_version", "source_manifest_sha256", "tracks"}
        or annotations["schema"] != "visualworld.v02-tracking-annotation-lock"
        or type(annotations["schema_version"]) is not int
        or annotations["schema_version"] != 1
        or annotations["source_manifest_sha256"] != source_sha256
        or not isinstance(annotations["clips"], list)
        or not isinstance(annotations["tracks"], list)
    ):
        _fail(code)
    clips = cast(list[object], annotations["clips"])
    tracks = cast(list[object], annotations["tracks"])
    if len(clips) != 15 or len(tracks) != 72:
        _fail(code)
    track_ids: set[str] = set()
    track_map: dict[str, dict[str, object]] = {}
    for raw_track in tracks:
        track = _mapping(raw_track, code)
        if set(track) != {
            "clip_id",
            "occurrence_transform",
            "source_id",
            "split",
            "strata",
            "track_id",
        }:
            _fail(code)
        track_id = _text(track["track_id"], code, 128)
        clip_id = _text(track["clip_id"], code, 96)
        strata = track["strata"]
        if (
            _IDENTIFIER.fullmatch(track_id) is None
            or _IDENTIFIER.fullmatch(clip_id) is None
            or track_id in track_ids
            or not track_id.startswith(f"{clip_id}-")
            or track["split"] not in {"calibration", "test"}
            or track["occurrence_transform"] not in {"original", "horizontal-mirror"}
            or not isinstance(strata, list)
            or not all(type(item) is str and item in STRATA for item in strata)
            or not cast(list[str], strata)
            or strata[0] != "overall"
        ):
            _fail(code)
        track_ids.add(track_id)
        track_map[track_id] = track
    observed_track_ids: set[str] = set()
    clip_ids: set[str] = set()
    for raw_clip in clips:
        clip = _mapping(raw_clip, code)
        if set(clip) != {
            "byte_count",
            "clip_id",
            "cut_before_frame_indices",
            "duration_ms",
            "frame_count",
            "frames",
            "primary_stratum",
            "relative_path",
            "sha256",
            "source_ids",
            "split",
        }:
            _fail(code)
        clip_id = _text(clip["clip_id"], code, 96)
        split = _text(clip["split"], code, 16)
        primary = _text(clip["primary_stratum"], code, 32)
        relative = PurePosixPath(_text(clip["relative_path"], code, 160))
        expected_strata = ["overall"] if primary == "stationary_camera" else ["overall", primary]
        cuts = clip["cut_before_frame_indices"]
        frames = clip["frames"]
        source_ids = clip["source_ids"]
        if (
            _IDENTIFIER.fullmatch(clip_id) is None
            or clip_id in clip_ids
            or split not in {"calibration", "test"}
            or not clip_id.startswith(f"{split}-")
            or primary
            not in {"stationary_camera", "camera_motion", "cuts", "occlusion", "dense_crossing"}
            or relative.as_posix() != f"{split}/{clip_id}.mov"
            or cuts != ([20, 40] if primary == "cuts" else [])
            or not isinstance(frames, list)
            or len(frames) != 60
            or not isinstance(source_ids, list)
            or len(source_ids) != 2
            or any(type(source_id) is not str for source_id in source_ids)
            or _integer(clip["frame_count"], code, 60, 60) != 60
            or _integer(clip["duration_ms"], code, 12_000, 12_000) != 12_000
            or _integer(clip["byte_count"], code, FRAME_BYTES * 60, MAX_CLIP_BYTES)
            != clip["byte_count"]
        ):
            _fail(code)
        _digest(clip["sha256"], code)
        for frame_index, raw_frame in enumerate(frames):
            frame = _mapping(raw_frame, code)
            if set(frame) != {
                "duration_ms",
                "frame_index",
                "objects",
                "pts_ms",
                "rgb24_sha256",
                "strata",
            }:
                _fail(code)
            objects = frame["objects"]
            if (
                _integer(frame["frame_index"], code, frame_index, frame_index) != frame_index
                or _integer(frame["pts_ms"], code, frame_index * 200, frame_index * 200)
                != frame_index * 200
                or _integer(frame["duration_ms"], code, 200, 200) != 200
                or frame["strata"] != expected_strata
                or not isinstance(objects, list)
                or len(objects) > 6
            ):
                _fail(code)
            _digest(frame["rgb24_sha256"], code)
            frame_ids: set[str] = set()
            for raw_object in objects:
                item = _mapping(raw_object, code)
                if set(item) != {"box", "track_id", "visibility_millionths"}:
                    _fail(code)
                item_track_id = _text(item["track_id"], code, 128)
                box = item["box"]
                if (
                    item_track_id in frame_ids
                    or item_track_id not in track_map
                    or track_map[item_track_id]["clip_id"] != clip_id
                    or track_map[item_track_id]["split"] != split
                    or not isinstance(box, list)
                    or len(box) != 4
                    or any(type(coordinate) is not int for coordinate in box)
                    or not (0 <= box[0] < box[2] <= WIDTH)
                    or not (0 <= box[1] < box[3] <= HEIGHT)
                    or _integer(item["visibility_millionths"], code, 1, 1_000_000)
                    != item["visibility_millionths"]
                ):
                    _fail(code)
                frame_ids.add(item_track_id)
                observed_track_ids.add(item_track_id)
        clip_ids.add(clip_id)
    if observed_track_ids != track_ids:
        _fail(code)
    split_contracts = _mapping(dataset["splits"], code)
    split_sources: dict[str, set[str]] = {}
    for split in ("calibration", "test"):
        selected_clips, selected_tracks, payload, ids_payload = _split_payload(annotations, split)
        contract = _mapping(split_contracts[split], code)
        expected_counts = {
            stratum: sum(
                stratum in cast(list[str], track_item["strata"]) for track_item in selected_tracks
            )
            for stratum in STRATA
        }
        if (
            contract["annotation_sha256"] != _sha256(payload)
            or contract["item_ids_sha256"] != _sha256(ids_payload)
            or contract["item_count"] != len(selected_tracks)
            or contract["stratum_item_counts"] != expected_counts
        ):
            _fail(code)
        split_sources[split] = {
            source_id
            for clip in selected_clips
            for source_id in cast(list[str], clip["source_ids"])
        }
    if split_sources["calibration"] & split_sources["test"]:
        _fail(code)


def _read_clip(
    dataset_root: Path, clip: dict[str, object]
) -> list[tuple[dict[str, object], bytes]]:
    try:
        raw = detection._read_regular(
            dataset_root / cast(str, clip["relative_path"]), MAX_CLIP_BYTES, "invalid_clip"
        )
    except detection.BenchmarkError as error:
        raise TrackingBenchmarkError("invalid_clip") from error
    if len(raw) != clip["byte_count"] or _sha256(raw) != clip["sha256"]:
        _fail("clip_digest_mismatch")
    if len(raw) < 28 or raw[4:8] != b"ftyp":
        _fail("invalid_clip")
    first_size = int.from_bytes(raw[:4], "big")
    if (
        first_size < 8
        or first_size + 8 > len(raw)
        or raw[first_size + 4 : first_size + 8] != b"mdat"
    ):
        _fail("invalid_clip")
    mdat_size = int.from_bytes(raw[first_size : first_size + 4], "big")
    payload = raw[first_size + 8 : first_size + mdat_size]
    frames = cast(list[dict[str, object]], clip["frames"])
    if len(payload) != FRAME_BYTES * len(frames):
        _fail("invalid_clip")
    output: list[tuple[dict[str, object], bytes]] = []
    for frame_index, frame in enumerate(frames):
        pixels = payload[frame_index * FRAME_BYTES : (frame_index + 1) * FRAME_BYTES]
        if _sha256(pixels) != frame["rgb24_sha256"]:
            _fail("frame_digest_mismatch")
        output.append((frame, pixels))
    return output


def _cut_score_basis_points(previous: bytes | None, current: bytes) -> int:
    if previous is None:
        return 0
    if len(previous) != FRAME_BYTES or len(current) != FRAME_BYTES:
        _fail("invalid_frame")
    total = 0
    samples = 0
    for offset in range(0, FRAME_BYTES, 12):
        total += abs(previous[offset] - current[offset])
        total += abs(previous[offset + 1] - current[offset + 1])
        total += abs(previous[offset + 2] - current[offset + 2])
        samples += 3
    return total * 10_000 // (samples * 255)


def _children_cpu_ns(usage: resource.struct_rusage) -> int:
    return int((usage.ru_utime + usage.ru_stime) * 1_000_000_000)


def _detect_split(
    *,
    dataset_root: Path,
    clips: list[dict[str, object]],
    split: str,
    seed: int,
    runtime_python: Path,
    runtime_root: Path,
    model_root: Path,
) -> dict[str, object]:
    started_wall = time.perf_counter_ns()
    started_parent_cpu = time.process_time_ns()
    started_children = _children_cpu_ns(resource.getrusage(resource.RUSAGE_CHILDREN))
    scratch = tempfile.TemporaryDirectory(prefix="visualworld-v02-tracking-")
    scratch_root = Path(scratch.name)
    items: list[dict[str, object]] = []
    sequence_frames: list[list[tuple[dict[str, object], bytes, str]]] = []
    counter = 0
    try:
        for sequence_index, clip in enumerate(clips):
            clip_frames_data = _read_clip(dataset_root, clip)
            selected: list[tuple[dict[str, object], bytes, str]] = []
            for frame, pixels in clip_frames_data:
                item_id = f"item-{counter:04d}"
                relative_path = f"frame-{counter:04d}.ppm"
                ppm = f"P6\n{WIDTH} {HEIGHT}\n255\n".encode("ascii") + pixels
                try:
                    sampling._write_bytes(scratch_root / relative_path, ppm)
                except sampling.SamplingBenchmarkError as error:
                    raise TrackingBenchmarkError("scratch_write_failed") from error
                items.append(
                    {
                        "frame_sha256": _sha256(ppm),
                        "height": HEIGHT,
                        "item_id": item_id,
                        "object_box": [0, 0, 1, 1],
                        "relative_path": relative_path,
                        "source_id": f"opaque-{sequence_index:02d}",
                        "split": split,
                        "stratum": "easy",
                        "width": WIDTH,
                    }
                )
                selected.append((frame, pixels, item_id))
                counter += 1
            sequence_frames.append(selected)
        worker_annotations = {
            "items": items,
            "schema": "visualworld.v02-detection-annotation-lock",
            "schema_version": 1,
        }
        annotation_raw = (
            json.dumps(worker_annotations, allow_nan=False, indent=2, sort_keys=True).encode(
                "ascii"
            )
            + b"\n"
        )
        annotation_path = scratch_root / "annotations.json"
        try:
            sampling._write_bytes(annotation_path, annotation_raw)
            worker = sampling._run_detection_worker(
                runtime_python,
                runtime_root,
                model_root,
                scratch_root,
                annotation_path,
                split,
                seed,
            )
        except sampling.SamplingBenchmarkError as error:
            raise TrackingBenchmarkError("worker_failed") from error
        try:
            detection._validate_worker_result(
                worker,
                items,
                split=split,
                seed=seed,
                annotation_sha256=_sha256(annotation_raw),
            )
        except detection.BenchmarkError as error:
            raise TrackingBenchmarkError("invalid_worker_result") from error
        predictions = cast(dict[str, list[dict[str, object]]], worker["predictions"])
        sequences: list[dict[str, object]] = []
        detection_count = 0
        for sequence_index, sequence_items in enumerate(sequence_frames):
            previous: bytes | None = None
            output_frames: list[dict[str, object]] = []
            for frame, pixels, item_id in sequence_items:
                raw_predictions = predictions[item_id]
                selected_predictions = sorted(
                    raw_predictions,
                    key=lambda item: (
                        -cast(int, item["confidence_millionths"]),
                        cast(list[int], item["box_milli_pixels"]),
                    ),
                )[:MAX_DETECTIONS]
                boxes: list[list[int]] = []
                for prediction in selected_predictions:
                    raw_box = prediction["box_milli_pixels"]
                    if (
                        not isinstance(raw_box, list)
                        or len(raw_box) != 4
                        or any(type(coordinate) is not int for coordinate in raw_box)
                        or not (0 <= raw_box[0] < raw_box[2] <= WIDTH * 1000)
                        or not (0 <= raw_box[1] < raw_box[3] <= HEIGHT * 1000)
                    ):
                        _fail("invalid_worker_result")
                    boxes.append(cast(list[int], raw_box))
                detection_count += len(boxes)
                output_frames.append(
                    {
                        "cut_score_basis_points": _cut_score_basis_points(previous, pixels),
                        "detections": boxes,
                        "pts_ms": frame["pts_ms"],
                    }
                )
                previous = pixels
            sequences.append(
                {"frames": output_frames, "sequence_id": f"sequence-{sequence_index:02d}"}
            )
        wall_ns = time.perf_counter_ns() - started_wall
        parent_cpu_ns = time.process_time_ns() - started_parent_cpu
        child_cpu_ns = (
            _children_cpu_ns(resource.getrusage(resource.RUSAGE_CHILDREN)) - started_children
        )
        return {
            "cpu_ns": parent_cpu_ns + child_cpu_ns,
            "detection_count": detection_count,
            "peak_rss_bytes": max(
                cast(int, worker["peak_rss_bytes"]),
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            ),
            "prediction_sha256": _sha256(_canonical(sequences)),
            "process_wall_ns": worker["process_wall_ns"],
            "seed": seed,
            "sequences": sequences,
            "wall_ns": wall_ns,
            "worker_environment": {
                "numpy_version": worker["numpy_version"],
                "openvino_version": worker["openvino_version"],
                "python_version": worker["python_version"],
                "telemetry_version": worker["telemetry_version"],
            },
        }
    finally:
        scratch.cleanup()


def _configuration(family: dict[str, object], iou: int, missed: int, cut: int) -> dict[str, object]:
    return {
        "association": "global",
        "cut_threshold_basis_points": cut,
        "iou_threshold_basis_points": iou,
        "max_missed_samples": missed,
        "motion": "velocity" if family["name"] == "global-velocity-iou" else "last",
        "name": family["name"],
    }


def _validate_configuration(value: object, code: str) -> dict[str, object]:
    configuration = _mapping(value, code)
    if set(configuration) != {
        "association",
        "cut_threshold_basis_points",
        "iou_threshold_basis_points",
        "max_missed_samples",
        "motion",
        "name",
    }:
        _fail(code)
    name = _text(configuration["name"], code, 32)
    expected_motion = {
        "global-last-iou": "last",
        "global-velocity-iou": "velocity",
    }.get(name)
    iou_threshold = _integer(configuration["iou_threshold_basis_points"], code, 0, 10_000)
    missed_samples = _integer(configuration["max_missed_samples"], code, 0, 64)
    cut_threshold = _integer(configuration["cut_threshold_basis_points"], code, 0, 10_000)
    if (
        expected_motion is None
        or configuration["association"] != "global"
        or configuration["motion"] != expected_motion
        or iou_threshold not in {1000, 3000, 5000}
        or missed_samples not in {0, 2, 5}
        or cut_threshold not in {1500, 2500, 3500}
    ):
        _fail(code)
    return configuration


def _configuration_id(configuration: dict[str, object]) -> str:
    return (
        f"{configuration['name']}-iou-{configuration['iou_threshold_basis_points']}-"
        f"miss-{configuration['max_missed_samples']}-cut-"
        f"{configuration['cut_threshold_basis_points']}"
    )


def _run_tracker(
    detection_result: dict[str, object], configuration: dict[str, object]
) -> dict[str, object]:
    started_wall = time.perf_counter_ns()
    started_cpu = time.process_time_ns()
    sequence_outputs: list[dict[str, object]] = []
    termination_counts = {"cut": 0, "miss_timeout": 0, "source_end": 0}
    maximum_active_tracks = 0
    for raw_sequence in cast(list[object], detection_result["sequences"]):
        sequence = _mapping(raw_sequence, "invalid_detection_payload")
        sequence_id = _text(sequence["sequence_id"], "invalid_detection_payload", 32)
        tracker = tracking.GeometryTracker(
            association="global",
            motion=cast(tracking.Motion, configuration["motion"]),
            iou_threshold_basis_points=cast(int, configuration["iou_threshold_basis_points"]),
            max_missed_samples=cast(int, configuration["max_missed_samples"]),
            width_milli=WIDTH * 1000,
            height_milli=HEIGHT * 1000,
        )
        output_frames: list[dict[str, object]] = []
        detected_cut_frames: list[int] = []
        frames = cast(list[dict[str, object]], sequence["frames"])
        for frame_index, frame in enumerate(frames):
            score = _integer(
                frame["cut_score_basis_points"], "invalid_detection_payload", 0, 10_000
            )
            if score >= cast(int, configuration["cut_threshold_basis_points"]):
                tracker.reset()
                detected_cut_frames.append(frame_index)
            boxes = tuple(
                cast(tracking.Box, tuple(box)) for box in cast(list[list[int]], frame["detections"])
            )
            observations = tracker.update(boxes, cast(int, frame["pts_ms"]))
            output_frames.append(
                {
                    "observations": [
                        {"box_milli_pixels": list(box), "track_id": f"{sequence_id}-{track_id}"}
                        for track_id, box in observations
                    ],
                    "pts_ms": frame["pts_ms"],
                }
            )
        tracker.finish()
        maximum_active_tracks = max(maximum_active_tracks, tracker.maximum_active_tracks)
        for reason, count in tracker.termination_counts.items():
            termination_counts[reason] += count
        sequence_outputs.append(
            {
                "detected_cut_frames": detected_cut_frames,
                "frames": output_frames,
                "sequence_id": sequence_id,
            }
        )
    return {
        "cpu_ns": time.process_time_ns() - started_cpu,
        "maximum_active_tracks": maximum_active_tracks,
        "sequences": sequence_outputs,
        "termination_counts": termination_counts,
        "wall_ns": time.perf_counter_ns() - started_wall,
    }


def _milli_box(value: object) -> tracking.Box:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(coordinate) is not int for coordinate in value)
    ):
        _fail("invalid_annotations")
    left, top, right, bottom = cast(list[int], value)
    if not 0 <= left < right <= WIDTH or not 0 <= top < bottom <= HEIGHT:
        _fail("invalid_annotations")
    return left * 1000, top * 1000, right * 1000, bottom * 1000


def _scored_frames(
    clips: list[dict[str, object]], tracker_result: dict[str, object]
) -> dict[str, list[list[tracking.FrameDetections]]]:
    outputs = cast(list[dict[str, object]], tracker_result["sequences"])
    if len(outputs) != len(clips):
        _fail("invalid_tracker_result")
    selected: dict[str, list[list[tracking.FrameDetections]]] = {stratum: [] for stratum in STRATA}
    for sequence_index, clip in enumerate(clips):
        output_frames = cast(list[dict[str, object]], outputs[sequence_index]["frames"])
        annotation_frames = cast(list[dict[str, object]], clip["frames"])
        if len(output_frames) != len(annotation_frames):
            _fail("invalid_tracker_result")
        clip_frames: list[tracking.FrameDetections] = []
        for frame_index, annotation in enumerate(annotation_frames):
            raw_observations = cast(
                list[dict[str, object]], output_frames[frame_index]["observations"]
            )
            ground_truth = tuple(
                (
                    cast(str, item["track_id"]),
                    _milli_box(item["box"]),
                )
                for item in cast(list[dict[str, object]], annotation["objects"])
            )
            predictions = tuple(
                (
                    cast(str, item["track_id"]),
                    cast(tracking.Box, tuple(cast(list[int], item["box_milli_pixels"]))),
                )
                for item in raw_observations
            )
            clip_frames.append((ground_truth, predictions))
        selected["overall"].append(clip_frames)
        primary = cast(str, clip["primary_stratum"])
        if primary != "stationary_camera":
            selected[primary].append(clip_frames)
    return selected


def _ceil_rate(count: int, denominator: int) -> int:
    if denominator <= 0:
        _fail("empty_metric_denominator")
    return (count * 1000 + denominator - 1) // denominator


def _representative_failures(
    clips: list[dict[str, object]], tracker_result: dict[str, object]
) -> dict[str, list[dict[str, object]]]:
    examples: dict[str, list[dict[str, object]]] = {
        "false_cut_continuation": [],
        "fragmentation_reacquisition": [],
        "identity_switch": [],
        "missed_visible": [],
    }
    outputs = cast(list[dict[str, object]], tracker_result["sequences"])
    if len(outputs) != len(clips):
        _fail("invalid_tracker_result")
    for sequence_index, clip in enumerate(clips):
        sequence = outputs[sequence_index]
        sequence_id = _text(sequence["sequence_id"], "invalid_tracker_result", 32)
        output_frames = cast(list[dict[str, object]], sequence["frames"])
        annotation_frames = cast(list[dict[str, object]], clip["frames"])
        if len(output_frames) != len(annotation_frames):
            _fail("invalid_tracker_result")
        last_prediction: dict[str, str] = {}
        gap_after_match: set[str] = set()
        scored_frames: list[tracking.FrameDetections] = []
        for frame_index, annotation in enumerate(annotation_frames):
            output_frame = output_frames[frame_index]
            ground_truth = tuple(
                (cast(str, item["track_id"]), _milli_box(item["box"]))
                for item in cast(list[dict[str, object]], annotation["objects"])
            )
            predictions = tuple(
                (
                    cast(str, item["track_id"]),
                    cast(tracking.Box, tuple(cast(list[int], item["box_milli_pixels"]))),
                )
                for item in cast(list[dict[str, object]], output_frame["observations"])
            )
            frame = (ground_truth, predictions)
            scored_frames.append(frame)
            matches = dict(tracking.frame_matches(frame))
            for ground_truth_id, _ in ground_truth:
                prediction_id = matches.get(ground_truth_id)
                common = {
                    "frame_index": frame_index,
                    "ground_truth_track_id": ground_truth_id,
                    "pts_ms": output_frame["pts_ms"],
                    "sequence_id": sequence_id,
                }
                if prediction_id is None:
                    if ground_truth_id in last_prediction:
                        gap_after_match.add(ground_truth_id)
                    if len(examples["missed_visible"]) < MAX_FAILURE_EXAMPLES_PER_KIND:
                        examples["missed_visible"].append(common)
                    continue
                previous = last_prediction.get(ground_truth_id)
                if (
                    previous is not None
                    and previous != prediction_id
                    and len(examples["identity_switch"]) < MAX_FAILURE_EXAMPLES_PER_KIND
                ):
                    examples["identity_switch"].append(
                        {
                            **common,
                            "current_prediction_id": prediction_id,
                            "previous_prediction_id": previous,
                        }
                    )
                if (
                    ground_truth_id in gap_after_match
                    and len(examples["fragmentation_reacquisition"]) < MAX_FAILURE_EXAMPLES_PER_KIND
                ):
                    examples["fragmentation_reacquisition"].append(
                        {**common, "prediction_id": prediction_id}
                    )
                last_prediction[ground_truth_id] = prediction_id
                gap_after_match.discard(ground_truth_id)
        for cut_frame in cast(list[int], clip["cut_before_frame_indices"]):
            start = max(0, cut_frame - 20)
            end = min(len(scored_frames), cut_frame + 20)
            before = {
                prediction_id
                for _, predictions in scored_frames[start:cut_frame]
                for prediction_id, _ in predictions
            }
            after = {
                prediction_id
                for _, predictions in scored_frames[cut_frame:end]
                for prediction_id, _ in predictions
            }
            for prediction_id in sorted(before & after):
                if len(examples["false_cut_continuation"]) >= MAX_FAILURE_EXAMPLES_PER_KIND:
                    break
                examples["false_cut_continuation"].append(
                    {
                        "cut_frame_index": cut_frame,
                        "prediction_id": prediction_id,
                        "sequence_id": sequence_id,
                    }
                )
    return examples


def _evaluate_candidate(
    *,
    clips: list[dict[str, object]],
    track_count: int,
    detection_result: dict[str, object],
    configuration: dict[str, object],
) -> dict[str, object]:
    tracker_result = _run_tracker(detection_result, configuration)
    scored = _scored_frames(clips, tracker_result)
    hota: dict[str, int] = {}
    idf1: dict[str, int] = {}
    switches: dict[str, int] = {}
    fragmentations: dict[str, int] = {}
    metric_details: dict[str, object] = {}
    for stratum, sequences in scored.items():
        try:
            hota_detail = tracking.combined_hota_details(sequences)
            identity_detail = tracking.combined_identity_details(sequences)
            diagnostics = [tracking.identity_diagnostics(frames) for frames in sequences]
        except ValueError as error:
            raise TrackingBenchmarkError("metric_input_exceeded") from error
        switch_count = sum(item[0] for item in diagnostics)
        fragmentation_count = sum(item[1] for item in diagnostics)
        track_frames = sum(item[2] for item in diagnostics)
        hota[stratum] = cast(int, hota_detail["hota_basis_points"])
        idf1[stratum] = identity_detail["idf1_basis_points"]
        switches[stratum] = _ceil_rate(switch_count, track_frames)
        fragmentations[stratum] = _ceil_rate(fragmentation_count, track_frames)
        metric_details[stratum] = {
            "fragmentation_count": fragmentation_count,
            "hota": hota_detail,
            "identity": identity_detail,
            "id_switch_count": switch_count,
            "visible_gt_track_frames": track_frames,
        }
    cut_continuations = 0
    outputs = cast(list[dict[str, object]], tracker_result["sequences"])
    cut_details: list[dict[str, object]] = []
    for sequence_index, clip in enumerate(clips):
        if clip["primary_stratum"] != "cuts":
            continue
        clip_frames: list[tracking.FrameDetections] = []
        annotation_frames = cast(list[dict[str, object]], clip["frames"])
        output_frames = cast(list[dict[str, object]], outputs[sequence_index]["frames"])
        for frame_index, annotation in enumerate(annotation_frames):
            ground_truth = tuple(
                (cast(str, item["track_id"]), _milli_box(item["box"]))
                for item in cast(list[dict[str, object]], annotation["objects"])
            )
            predictions = tuple(
                (
                    cast(str, item["track_id"]),
                    cast(tracking.Box, tuple(cast(list[int], item["box_milli_pixels"]))),
                )
                for item in cast(
                    list[dict[str, object]], output_frames[frame_index]["observations"]
                )
            )
            clip_frames.append((ground_truth, predictions))
        for cut_frame in cast(list[int], clip["cut_before_frame_indices"]):
            count = tracking.false_cut_continuations(clip_frames, cut_frame, 20)
            cut_continuations += count
            cut_details.append(
                {
                    "continuation_count": count,
                    "cut_frame_index": cut_frame,
                    "sequence_id": outputs[sequence_index]["sequence_id"],
                }
            )
    source_ms = len(clips) * 12_000
    integrated_wall_ns = cast(int, detection_result["wall_ns"]) + cast(
        int, tracker_result["wall_ns"]
    )
    metrics = {
        "failure_rate_basis_points": {"overall": 0},
        "false_continuations_across_cuts": {"cuts": cut_continuations},
        "fragmentations_per_1000_track_frames": fragmentations,
        "hota_basis_points": hota,
        "idf1_basis_points": idf1,
        "id_switches_per_1000_track_frames": switches,
        "peak_rss_bytes": {"overall": detection_result["peak_rss_bytes"]},
        "real_time_factor_milli": {"overall": source_ms * 1_000_000_000 // integrated_wall_ns},
    }
    return {
        "configuration": configuration,
        "cpu_ns": cast(int, detection_result["cpu_ns"]) + cast(int, tracker_result["cpu_ns"]),
        "cut_details": cut_details,
        "detection_count": detection_result["detection_count"],
        "detection_prediction_sha256": detection_result["prediction_sha256"],
        "failed_item_count": 0,
        "failure_examples": _representative_failures(clips, tracker_result),
        "failure_code": None,
        "integrated_wall_ns": integrated_wall_ns,
        "maximum_active_tracks": tracker_result["maximum_active_tracks"],
        "metric_details": metric_details,
        "metrics": metrics,
        "processed_item_count": track_count,
        "seed": detection_result["seed"],
        "status": "pass",
        "termination_counts": tracker_result["termination_counts"],
        "tracker_output_sha256": _sha256(_canonical(tracker_result["sequences"])),
        "tracker_result": tracker_result,
        "tracker_wall_ns": tracker_result["wall_ns"],
    }


def _aggregate(repetitions: list[dict[str, object]]) -> dict[str, object]:
    metrics: dict[str, dict[str, int]] = {}
    dispersion: dict[str, dict[str, int]] = {}
    first = cast(dict[str, dict[str, int]], repetitions[0]["metrics"])
    for name, strata in first.items():
        metrics[name] = {}
        dispersion[name] = {}
        for stratum in strata:
            values = sorted(
                cast(dict[str, dict[str, int]], repetition["metrics"])[name][stratum]
                for repetition in repetitions
            )
            center = values[len(values) // 2]
            if name == "peak_rss_bytes":
                aggregate = max(values)
            elif name in {"failure_rate_basis_points", "false_continuations_across_cuts"}:
                aggregate = (sum(values) + len(values) - 1) // len(values)
            else:
                aggregate = center
            metrics[name][stratum] = aggregate
            deviations = sorted(abs(value - center) for value in values)
            dispersion[name][stratum] = deviations[len(deviations) // 2]
    return {"dispersion": dispersion, "metrics": metrics}


def _validate_metrics(value: object, code: str) -> dict[str, dict[str, int]]:
    metrics = _mapping(value, code)
    if set(metrics) != set(METRIC_STRATA):
        _fail(code)
    validated: dict[str, dict[str, int]] = {}
    for name, expected_strata in METRIC_STRATA.items():
        raw_strata = _mapping(metrics[name], code)
        if set(raw_strata) != set(expected_strata):
            _fail(code)
        maximum = (
            10_000
            if name in {"failure_rate_basis_points", "hota_basis_points", "idf1_basis_points"}
            else (1 << 63) - 1
        )
        validated[name] = {
            stratum: _integer(raw_strata[stratum], code, 0, maximum) for stratum in expected_strata
        }
    return validated


def _passes_gates(aggregate: dict[str, object], policy: dict[str, object]) -> bool:
    definitions = cast(
        dict[str, dict[str, object]],
        cast(dict[str, object], cast(dict[str, object], policy["experiments"])["tracking"])[
            "metrics"
        ],
    )
    observed = cast(dict[str, dict[str, int]], aggregate["metrics"])
    for name, definition in definitions.items():
        minimum = cast(dict[str, int] | None, definition["minimum"])
        maximum = cast(dict[str, int] | None, definition["maximum"])
        if minimum is not None and any(
            observed[name][stratum] < value for stratum, value in minimum.items()
        ):
            return False
        if maximum is not None and any(
            observed[name][stratum] > value for stratum, value in maximum.items()
        ):
            return False
    return True


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
    output: list[dict[str, object]] = []
    for index, repetition in enumerate(repetitions):
        selected = {
            key: value
            for key, value in repetition.items()
            if key not in {"failure_examples", "tracker_result"}
        }
        if index == 0:
            selected["representative_failures"] = repetition["failure_examples"]
            selected["representative_tracker_result"] = repetition["tracker_result"]
        output.append(selected)
    return output


def _verify_file_digest(path: Path, expected: object, code: str) -> None:
    try:
        raw = detection._read_regular(path, MAX_JSON_BYTES, code)
    except detection.BenchmarkError as error:
        raise TrackingBenchmarkError(code) from error
    if _sha256(raw) != expected:
        _fail(code)


def _trusted_code_digests() -> dict[str, str]:
    digests: dict[str, str] = {}
    for name, path in (
        ("dataset_validator_sha256", DATASET_VALIDATOR_PATH),
        ("detection_dataset_helper_sha256", DETECTION_DATASET_HELPER_PATH),
        ("detection_worker_sha256", DETECTION_WORKER_PATH),
        ("gate_evaluator_sha256", GATE_EVALUATOR_PATH),
        ("sampling_dataset_helper_sha256", SAMPLING_DATASET_HELPER_PATH),
        ("sampling_harness_sha256", SAMPLING_HARNESS_PATH),
    ):
        try:
            raw = detection._read_regular(path, 4 * 1024 * 1024, "trusted_code_mismatch")
        except detection.BenchmarkError as error:
            raise TrackingBenchmarkError("trusted_code_mismatch") from error
        digests[name] = _sha256(raw)
    return digests


def _prepare_context(
    *,
    models_root: Path,
    base_python: Path,
    wheels_root: Path,
    evaluated_on: str,
) -> tuple[dict[str, object], tempfile.TemporaryDirectory[str]]:
    try:
        evaluation_day = date.fromisoformat(evaluated_on)
    except ValueError as error:
        raise TrackingBenchmarkError("invalid_evaluation_day") from error
    candidates, candidates_sha256 = _load_json(CANDIDATES_PATH, "invalid_candidate_manifest")
    families, iou_grid, missed_grid, cut_grid = _validate_candidates(candidates, candidates_sha256)
    annotations, annotations_sha256 = _load_json(ANNOTATIONS_PATH, "invalid_annotations")
    source, source_sha256 = _load_json(SOURCE_MANIFEST_PATH, "invalid_source_manifest")
    detection_annotations, detection_annotation_sha256 = _load_json(
        DETECTION_ANNOTATIONS_PATH, "invalid_source_manifest"
    )
    _, detection_source_sha256 = _load_json(DETECTION_SOURCE_PATH, "invalid_source_manifest")
    try:
        preparation._validate_source_manifest(source, detection_annotations)
    except preparation.TrackingPreparationError as error:
        raise TrackingBenchmarkError("invalid_source_manifest") from error
    if (
        source["source_detection_annotation_sha256"] != detection_annotation_sha256
        or source["source_detection_manifest_sha256"] != detection_source_sha256
        or detection_annotations["source_manifest_sha256"] != detection_source_sha256
    ):
        _fail("source_manifest_mismatch")
    try:
        policy, policy_sha256 = gate_evaluator.load_policy(POLICY_PATH)
        dataset, dataset_sha256 = gate_evaluator.load_manifest(DATASET_MANIFEST_PATH, policy)
    except gate_evaluator.EvaluationError as error:
        raise TrackingBenchmarkError("invalid_gate_input") from error
    if cast(dict[str, object], dataset["acquisition"])["sha256"] != source_sha256:
        _fail("source_manifest_mismatch")
    _verify_tracking_dataset_lock(
        candidates,
        source_sha256=source_sha256,
        annotations_sha256=annotations_sha256,
        dataset_sha256=dataset_sha256,
    )
    _validate_annotations(annotations, dataset, source_sha256)

    detector_lock = _mapping(candidates["detector"], "invalid_candidate_manifest")
    sampling_lock = _mapping(candidates["sampling"], "invalid_candidate_manifest")
    for path, expected in (
        (DETECTION_ANNOTATIONS_PATH, detector_lock["annotation_sha256"]),
        (DETECTION_CANDIDATES_PATH, detector_lock["candidate_manifest_sha256"]),
        (DETECTION_ROOT / "dataset-manifest.json", detector_lock["dataset_manifest_sha256"]),
        (DETECTION_SOURCE_PATH, detector_lock["source_manifest_sha256"]),
        (DETECTION_RECEIPT_PATH, detector_lock["receipt_sha256"]),
        (DETECTION_GATE_PATH, detector_lock["gate_sha256"]),
        (SAMPLING_ANNOTATIONS_PATH, sampling_lock["annotation_sha256"]),
        (SAMPLING_CANDIDATES_PATH, sampling_lock["candidate_manifest_sha256"]),
        (SAMPLING_DATASET_PATH, sampling_lock["dataset_manifest_sha256"]),
        (SAMPLING_SOURCE_PATH, sampling_lock["source_manifest_sha256"]),
        (SAMPLING_RECEIPT_PATH, sampling_lock["receipt_sha256"]),
        (SAMPLING_GATE_PATH, sampling_lock["gate_sha256"]),
    ):
        _verify_file_digest(path, expected, "upstream_provenance_mismatch")
    detection_gate, _ = _load_json(DETECTION_GATE_PATH, "upstream_provenance_mismatch")
    sampling_gate, _ = _load_json(SAMPLING_GATE_PATH, "upstream_provenance_mismatch")
    if detection_gate.get("status") != "pass" or sampling_gate.get("status") != "pass":
        _fail("upstream_gate_failed")

    detector_manifest, _ = _load_json(DETECTION_CANDIDATES_PATH, "invalid_detector_manifest")
    try:
        detector_candidates, runtime, _, _ = detection._validate_candidate_manifest(
            detector_manifest
        )
    except detection.BenchmarkError as error:
        raise TrackingBenchmarkError("invalid_detector_manifest") from error
    selected_detector = next(
        candidate for candidate in detector_candidates if candidate["name"] == detector_lock["name"]
    )
    try:
        wheels = detection._verify_runtime_wheels(runtime, wheels_root)
        python_attestation = detection._verify_base_python(base_python, runtime)
    except detection.BenchmarkError as error:
        raise TrackingBenchmarkError("runtime_verification_failed") from error
    runtime_temporary = tempfile.TemporaryDirectory(prefix="visualworld-v02-tracking-runtime-")
    runtime_root = Path(runtime_temporary.name) / "site-packages"
    try:
        runtime_tree_sha256 = detection._extract_runtime(wheels, runtime_root)
    except detection.BenchmarkError as error:
        runtime_temporary.cleanup()
        raise TrackingBenchmarkError("runtime_verification_failed") from error
    runtime_closure_sha256 = _sha256(
        _canonical(
            {
                "python": python_attestation,
                "runtime_tree_sha256": runtime_tree_sha256,
                "wheels": {name: _sha256(raw) for name, raw in sorted(wheels.items())},
            }
        )
    )
    model_root = models_root / "vehicle-detection-0201"
    for extension in ("xml", "bin"):
        expected = cast(dict[str, object], selected_detector[f"model_{extension}"])
        paths = sorted(model_root.glob(f"*.{extension}"))
        if len(paths) != 1:
            runtime_temporary.cleanup()
            _fail("model_missing")
        try:
            raw = detection._read_regular(paths[0], 16 * 1024 * 1024, "invalid_model")
        except detection.BenchmarkError as error:
            runtime_temporary.cleanup()
            raise TrackingBenchmarkError("invalid_model") from error
        if len(raw) != expected["size"] or _sha256(raw) != expected["sha256"]:
            runtime_temporary.cleanup()
            _fail("model_digest_mismatch")
    try:
        profile = detection._profile()
    except detection.BenchmarkError as error:
        runtime_temporary.cleanup()
        raise TrackingBenchmarkError("profile_mismatch") from error
    return (
        {
            "annotations": annotations,
            "annotations_sha256": annotations_sha256,
            "candidates": candidates,
            "candidates_sha256": candidates_sha256,
            "cut_grid": cut_grid,
            "dataset": dataset,
            "dataset_sha256": dataset_sha256,
            "evaluation_day": evaluation_day,
            "families": families,
            "iou_grid": iou_grid,
            "missed_grid": missed_grid,
            "model_root": model_root,
            "policy": policy,
            "policy_sha256": policy_sha256,
            "profile": profile,
            "python_attestation": python_attestation,
            "runtime": runtime,
            "runtime_closure_sha256": runtime_closure_sha256,
            "runtime_root": runtime_root,
            "runtime_tree_sha256": runtime_tree_sha256,
            "selected_detector": selected_detector,
            "source_sha256": source_sha256,
        },
        runtime_temporary,
    )


def _calibration_selection_key(result: dict[str, object]) -> tuple[int, ...]:
    aggregate = cast(dict[str, object], result["aggregate"])
    values = cast(dict[str, dict[str, int]], aggregate["metrics"])
    configuration = cast(dict[str, object], result["configuration"])
    return (
        -values["hota_basis_points"]["overall"],
        -values["idf1_basis_points"]["overall"],
        -min(values["hota_basis_points"][stratum] for stratum in STRATA[1:]),
        values["id_switches_per_1000_track_frames"]["overall"],
        values["fragmentations_per_1000_track_frames"]["overall"],
        cast(int, configuration["max_missed_samples"]),
        -cast(int, configuration["iou_threshold_basis_points"]),
        cast(int, configuration["cut_threshold_basis_points"]),
    )


def _compact_calibration_repetition(repetition: dict[str, object]) -> dict[str, object]:
    return {
        "cpu_ns": repetition["cpu_ns"],
        "detection_prediction_sha256": repetition["detection_prediction_sha256"],
        "integrated_wall_ns": repetition["integrated_wall_ns"],
        "maximum_active_tracks": repetition["maximum_active_tracks"],
        "metrics": repetition["metrics"],
        "seed": repetition["seed"],
        "termination_counts": repetition["termination_counts"],
        "tracker_output_sha256": repetition["tracker_output_sha256"],
        "tracker_wall_ns": repetition["tracker_wall_ns"],
    }


def run_calibration(
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
    context, runtime_temporary = _prepare_context(
        models_root=models_root,
        base_python=base_python,
        wheels_root=wheels_root,
        evaluated_on=evaluated_on,
    )
    annotations = cast(dict[str, object], context["annotations"])
    clips, tracks, _, _ = _split_payload(annotations, "calibration")
    families = cast(list[dict[str, object]], context["families"])
    configurations = [
        _configuration(family, iou, missed, cut)
        for family in families
        for iou in cast(list[int], context["iou_grid"])
        for missed in cast(list[int], context["missed_grid"])
        for cut in cast(list[int], context["cut_grid"])
    ]
    try:
        warmup = _detect_split(
            dataset_root=dataset_root,
            clips=clips,
            split="calibration",
            seed=SEEDS[0],
            runtime_python=base_python,
            runtime_root=cast(Path, context["runtime_root"]),
            model_root=cast(Path, context["model_root"]),
        )
        for configuration in configurations:
            _evaluate_candidate(
                clips=clips,
                track_count=len(tracks),
                detection_result=warmup,
                configuration=configuration,
            )
        detections = [
            _detect_split(
                dataset_root=dataset_root,
                clips=clips,
                split="calibration",
                seed=seed,
                runtime_python=base_python,
                runtime_root=cast(Path, context["runtime_root"]),
                model_root=cast(Path, context["model_root"]),
            )
            for seed in SEEDS
        ]
        policy = cast(dict[str, object], context["policy"])
        results: list[dict[str, object]] = []
        for configuration in configurations:
            repetitions = [
                _evaluate_candidate(
                    clips=clips,
                    track_count=len(tracks),
                    detection_result=detection_result,
                    configuration=configuration,
                )
                for detection_result in detections
            ]
            aggregate = _aggregate(repetitions)
            results.append(
                {
                    "aggregate": aggregate,
                    "configuration": configuration,
                    "configuration_id": _configuration_id(configuration),
                    "passes_gates": _passes_gates(aggregate, policy),
                    "repetitions": [
                        _compact_calibration_repetition(repetition) for repetition in repetitions
                    ],
                }
            )
        selected: list[dict[str, object]] = []
        for family in families:
            eligible = [
                result
                for result in results
                if result["passes_gates"] is True
                and cast(dict[str, object], result["configuration"])["name"] == family["name"]
            ]
            if not eligible:
                _fail("no_calibration_configuration")
            selected.append(
                cast(
                    dict[str, object],
                    min(eligible, key=_calibration_selection_key)["configuration"],
                )
            )
        harness_sha256 = _sha256(Path(__file__).read_bytes())
        metric_sha256 = _sha256(METRIC_PATH.read_bytes())
        trusted_code_digests = _trusted_code_digests()
        raw_result = {
            "configurations": results,
            "detection_repetitions": [
                {
                    "cpu_ns": item["cpu_ns"],
                    "detection_count": item["detection_count"],
                    "peak_rss_bytes": item["peak_rss_bytes"],
                    "prediction_sha256": item["prediction_sha256"],
                    "process_wall_ns": item["process_wall_ns"],
                    "seed": item["seed"],
                    "wall_ns": item["wall_ns"],
                    "worker_environment": item["worker_environment"],
                }
                for item in detections
            ],
            "evaluated_on": evaluated_on,
            "phase": "calibration",
            "provenance": {
                "annotation_lock_sha256": context["annotations_sha256"],
                "candidate_manifest_sha256": context["candidates_sha256"],
                "dataset_manifest_sha256": context["dataset_sha256"],
                "evaluation_harness_sha256": harness_sha256,
                "metric_implementation_sha256": metric_sha256,
                "policy_sha256": context["policy_sha256"],
                "source_manifest_sha256": context["source_sha256"],
                "source_revision": source_revision,
                **trusted_code_digests,
            },
            "schema": "visualworld.v02-tracking-calibration-results",
            "schema_version": 1,
            "selected_configurations": selected,
        }
        output_descriptor = detection._create_private_directory(output_root)
        try:
            raw_sha256 = detection._write_new(
                output_descriptor, "calibration-results.json", raw_result
            )
            selection = {
                "calibration_results_sha256": raw_sha256,
                "candidate_manifest_sha256": context["candidates_sha256"],
                "evaluation_harness_sha256": harness_sha256,
                "metric_implementation_sha256": metric_sha256,
                "schema": "visualworld.v02-tracking-calibration-selection",
                "schema_version": 1,
                "selected_configurations": selected,
                "source_revision": source_revision,
                **trusted_code_digests,
            }
            selection_sha256 = detection._write_new(
                output_descriptor, "calibration-selection.generated.json", selection
            )
        finally:
            detection._close_once(output_descriptor)
        return {
            "calibration_results_sha256": raw_sha256,
            "calibration_selection_sha256": selection_sha256,
            "selected_configurations": selected,
            "status": "pass",
        }
    finally:
        runtime_temporary.cleanup()


def _validate_calibration_results(
    value: dict[str, object],
    *,
    candidates_sha256: str,
    annotations_sha256: str,
    dataset_sha256: str,
    harness_sha256: str,
    metric_sha256: str,
    policy: dict[str, object],
    policy_sha256: str,
    source_manifest_sha256: str,
    source_revision: str,
    trusted_code_digests: dict[str, str],
) -> list[dict[str, object]]:
    code = "invalid_calibration_results"
    if set(value) != {
        "configurations",
        "detection_repetitions",
        "evaluated_on",
        "phase",
        "provenance",
        "schema",
        "schema_version",
        "selected_configurations",
    } or (
        value["schema"] != "visualworld.v02-tracking-calibration-results"
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["phase"] != "calibration"
    ):
        _fail(code)
    try:
        date.fromisoformat(_text(value["evaluated_on"], code, 10))
    except ValueError as error:
        raise TrackingBenchmarkError(code) from error
    expected_provenance = {
        "annotation_lock_sha256": annotations_sha256,
        "candidate_manifest_sha256": candidates_sha256,
        "dataset_manifest_sha256": dataset_sha256,
        "evaluation_harness_sha256": harness_sha256,
        "metric_implementation_sha256": metric_sha256,
        "policy_sha256": policy_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "source_revision": source_revision,
        **trusted_code_digests,
    }
    if _canonical(_mapping(value["provenance"], code)) != _canonical(expected_provenance):
        _fail(code)

    raw_detection_repetitions = value["detection_repetitions"]
    if not isinstance(raw_detection_repetitions, list) or len(raw_detection_repetitions) != len(
        SEEDS
    ):
        _fail(code)
    detection_hashes: dict[int, str] = {}
    for index, raw_repetition in enumerate(raw_detection_repetitions):
        repetition = _mapping(raw_repetition, code)
        if set(repetition) != {
            "cpu_ns",
            "detection_count",
            "peak_rss_bytes",
            "prediction_sha256",
            "process_wall_ns",
            "seed",
            "wall_ns",
            "worker_environment",
        }:
            _fail(code)
        seed = _integer(repetition["seed"], code, SEEDS[index], SEEDS[index])
        for field in (
            "cpu_ns",
            "detection_count",
            "peak_rss_bytes",
            "process_wall_ns",
            "wall_ns",
        ):
            _integer(repetition[field], code)
        prediction_sha256 = _digest(repetition["prediction_sha256"], code)
        environment = _mapping(repetition["worker_environment"], code)
        if set(environment) != {
            "numpy_version",
            "openvino_version",
            "python_version",
            "telemetry_version",
        }:
            _fail(code)
        for item in environment.values():
            _text(item, code, 128)
        detection_hashes[seed] = prediction_sha256

    expected_configurations = [
        _configuration({"name": name}, iou, missed, cut)
        for name in ("global-last-iou", "global-velocity-iou")
        for iou in (1000, 3000, 5000)
        for missed in (0, 2, 5)
        for cut in (1500, 2500, 3500)
    ]
    raw_results = value["configurations"]
    if not isinstance(raw_results, list) or len(raw_results) != len(expected_configurations):
        _fail(code)
    validated_results: list[dict[str, object]] = []
    for index, raw_result in enumerate(raw_results):
        result = _mapping(raw_result, code)
        if set(result) != {
            "aggregate",
            "configuration",
            "configuration_id",
            "passes_gates",
            "repetitions",
        }:
            _fail(code)
        configuration = _validate_configuration(result["configuration"], code)
        if (
            _canonical(configuration) != _canonical(expected_configurations[index])
            or result["configuration_id"] != _configuration_id(configuration)
            or type(result["passes_gates"]) is not bool
        ):
            _fail(code)
        raw_repetitions = result["repetitions"]
        if not isinstance(raw_repetitions, list) or len(raw_repetitions) != len(SEEDS):
            _fail(code)
        repetitions: list[dict[str, object]] = []
        for repetition_index, raw_repetition in enumerate(raw_repetitions):
            repetition = _mapping(raw_repetition, code)
            if set(repetition) != {
                "cpu_ns",
                "detection_prediction_sha256",
                "integrated_wall_ns",
                "maximum_active_tracks",
                "metrics",
                "seed",
                "termination_counts",
                "tracker_output_sha256",
                "tracker_wall_ns",
            }:
                _fail(code)
            seed = _integer(
                repetition["seed"], code, SEEDS[repetition_index], SEEDS[repetition_index]
            )
            if _digest(repetition["detection_prediction_sha256"], code) != detection_hashes[seed]:
                _fail(code)
            _digest(repetition["tracker_output_sha256"], code)
            _integer(repetition["cpu_ns"], code)
            integrated_wall_ns = _integer(repetition["integrated_wall_ns"], code, 1)
            tracker_wall_ns = _integer(repetition["tracker_wall_ns"], code)
            if tracker_wall_ns > integrated_wall_ns:
                _fail(code)
            _integer(
                repetition["maximum_active_tracks"],
                code,
                0,
                MAX_DETECTIONS * (cast(int, configuration["max_missed_samples"]) + 1),
            )
            termination = _mapping(repetition["termination_counts"], code)
            if set(termination) != {"cut", "miss_timeout", "source_end"}:
                _fail(code)
            for count in termination.values():
                _integer(count, code, 0, tracking.MAX_OBSERVATIONS)
            repetition["metrics"] = _validate_metrics(repetition["metrics"], code)
            repetitions.append(repetition)
        aggregate = _aggregate(repetitions)
        if _canonical(_mapping(result["aggregate"], code)) != _canonical(aggregate) or result[
            "passes_gates"
        ] is not _passes_gates(aggregate, policy):
            _fail(code)
        validated_results.append(result)

    selected: list[dict[str, object]] = []
    for name in ("global-last-iou", "global-velocity-iou"):
        eligible = [
            result
            for result in validated_results
            if result["passes_gates"] is True
            and cast(dict[str, object], result["configuration"])["name"] == name
        ]
        if not eligible:
            _fail(code)
        selected.append(
            cast(dict[str, object], min(eligible, key=_calibration_selection_key)["configuration"])
        )
    raw_selected = value["selected_configurations"]
    if not isinstance(raw_selected, list) or len(raw_selected) != 2:
        _fail(code)
    validated_selected = [_validate_configuration(item, code) for item in raw_selected]
    if _canonical(validated_selected) != _canonical(selected):
        _fail(code)
    return selected


def _validate_selection(
    value: dict[str, object],
    *,
    selection_sha256: str,
    candidates_sha256: str,
    calibration_results_sha256: str,
    calibration_selected_configurations: list[dict[str, object]],
    harness_sha256: str,
    metric_sha256: str,
    source_revision: str,
    trusted_code_digests: dict[str, str],
) -> list[dict[str, object]]:
    code = "invalid_calibration_selection"
    if set(value) != {
        "calibration_results_sha256",
        "candidate_manifest_sha256",
        "dataset_validator_sha256",
        "detection_dataset_helper_sha256",
        "detection_worker_sha256",
        "evaluation_harness_sha256",
        "gate_evaluator_sha256",
        "metric_implementation_sha256",
        "sampling_dataset_helper_sha256",
        "sampling_harness_sha256",
        "schema",
        "schema_version",
        "selected_configurations",
        "source_revision",
    } or (
        value["schema"] != "visualworld.v02-tracking-calibration-selection"
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["candidate_manifest_sha256"] != candidates_sha256
        or value["calibration_results_sha256"] != calibration_results_sha256
        or value["evaluation_harness_sha256"] != harness_sha256
        or value["metric_implementation_sha256"] != metric_sha256
        or value["source_revision"] != source_revision
        or any(value[name] != digest for name, digest in trusted_code_digests.items())
    ):
        _fail(code)
    _digest(selection_sha256, code)
    configurations = value["selected_configurations"]
    if not isinstance(configurations, list) or len(configurations) != 2:
        _fail(code)
    selected = [_validate_configuration(configuration, code) for configuration in configurations]
    if _canonical(selected) != _canonical(calibration_selected_configurations):
        _fail(code)
    return selected


def _test_selection_key(result: dict[str, object]) -> tuple[object, ...]:
    aggregate = cast(dict[str, object], result["aggregate"])
    values = cast(dict[str, dict[str, int]], aggregate["metrics"])
    return (
        -values["hota_basis_points"]["overall"],
        -values["idf1_basis_points"]["overall"],
        -min(values["hota_basis_points"][stratum] for stratum in STRATA[1:]),
        values["id_switches_per_1000_track_frames"]["overall"],
        values["fragmentations_per_1000_track_frames"]["overall"],
        -values["real_time_factor_milli"]["overall"],
        cast(dict[str, object], result["configuration"])["name"],
    )


def _tracking_artifact(
    configuration: dict[str, object],
    source_revision: str,
    evaluated_on: str,
    metric_sha256: str,
) -> dict[str, object]:
    artifact = detection._artifact(
        name=f"visualworld-{configuration['name']}-tracker",
        kind="code",
        artifact_format="python-source",
        license_expression="MIT",
        revision=source_revision,
        sha256=metric_sha256,
        source_url=(
            "https://github.com/mayank-gupta16/vision-query-system/blob/"
            f"{source_revision}/scripts/v02_tracking_metrics.py"
        ),
        terms_url=(
            "https://github.com/JonathonLuiten/TrackEval/blob/"
            "12c8791b303e0a0b50f753af204249e622d0281a/LICENSE"
        ),
        use="clip-local geometry-only vehicle tracklet evaluation",
        reviewed_on=evaluated_on,
    )
    artifact["isolation"] = "in-process-reviewed"
    return artifact


def run_test(
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
    context, runtime_temporary = _prepare_context(
        models_root=models_root,
        base_python=base_python,
        wheels_root=wheels_root,
        evaluated_on=evaluated_on,
    )
    harness_sha256 = _sha256(Path(__file__).read_bytes())
    metric_sha256 = _sha256(METRIC_PATH.read_bytes())
    trusted_code_digests = _trusted_code_digests()
    selection, selection_sha256 = _load_json(
        CALIBRATION_SELECTION_PATH, "invalid_calibration_selection"
    )
    calibration_results, calibration_results_sha256 = _load_json(
        CALIBRATION_RESULTS_PATH, "invalid_calibration_results"
    )
    calibration_selected = _validate_calibration_results(
        calibration_results,
        candidates_sha256=cast(str, context["candidates_sha256"]),
        annotations_sha256=cast(str, context["annotations_sha256"]),
        dataset_sha256=cast(str, context["dataset_sha256"]),
        harness_sha256=harness_sha256,
        metric_sha256=metric_sha256,
        policy=cast(dict[str, object], context["policy"]),
        policy_sha256=cast(str, context["policy_sha256"]),
        source_manifest_sha256=cast(str, context["source_sha256"]),
        source_revision=source_revision,
        trusted_code_digests=trusted_code_digests,
    )
    configurations = _validate_selection(
        selection,
        selection_sha256=selection_sha256,
        candidates_sha256=cast(str, context["candidates_sha256"]),
        calibration_results_sha256=calibration_results_sha256,
        calibration_selected_configurations=calibration_selected,
        harness_sha256=harness_sha256,
        metric_sha256=metric_sha256,
        source_revision=source_revision,
        trusted_code_digests=trusted_code_digests,
    )
    annotations = cast(dict[str, object], context["annotations"])
    clips, tracks, _, _ = _split_payload(annotations, "test")
    try:
        warmup = _detect_split(
            dataset_root=dataset_root,
            clips=clips,
            split="test",
            seed=SEEDS[0],
            runtime_python=base_python,
            runtime_root=cast(Path, context["runtime_root"]),
            model_root=cast(Path, context["model_root"]),
        )
        for configuration in configurations:
            _evaluate_candidate(
                clips=clips,
                track_count=len(tracks),
                detection_result=warmup,
                configuration=configuration,
            )
        detections = [
            _detect_split(
                dataset_root=dataset_root,
                clips=clips,
                split="test",
                seed=seed,
                runtime_python=base_python,
                runtime_root=cast(Path, context["runtime_root"]),
                model_root=cast(Path, context["model_root"]),
            )
            for seed in SEEDS
        ]
        results: list[dict[str, object]] = []
        for configuration in configurations:
            repetitions = [
                _evaluate_candidate(
                    clips=clips,
                    track_count=len(tracks),
                    detection_result=detection_result,
                    configuration=configuration,
                )
                for detection_result in detections
            ]
            results.append(
                {
                    "aggregate": _aggregate(repetitions),
                    "configuration": configuration,
                    "repetitions": repetitions,
                }
            )
        output_descriptor = detection._create_private_directory(output_root)
        outputs: dict[str, object] = {}
        gate_results: dict[str, dict[str, object]] = {}
        try:
            for result in results:
                configuration = cast(dict[str, object], result["configuration"])
                candidate_name = cast(str, configuration["name"])
                repetitions = cast(list[dict[str, object]], result["repetitions"])
                receipt_repetitions = [_receipt_repetition(item) for item in repetitions]
                aggregate = _aggregate(receipt_repetitions)
                artifacts = detection._candidate_artifacts(
                    cast(dict[str, object], context["selected_detector"]),
                    cast(dict[str, object], context["runtime"]),
                    evaluated_on,
                )
                artifacts.append(
                    _tracking_artifact(configuration, source_revision, evaluated_on, metric_sha256)
                )
                receipt = {
                    "aggregate": aggregate,
                    "candidate": {
                        "artifacts": artifacts,
                        "configuration_sha256": _sha256(_canonical(configuration)),
                        "name": candidate_name,
                        "runtime_closure_sha256": context["runtime_closure_sha256"],
                    },
                    "dataset_manifest_sha256": context["dataset_sha256"],
                    "evaluated_on": evaluated_on,
                    "evaluation_phase": "selection",
                    "experiment": "tracking",
                    "implementation": {
                        "evaluation_harness": {
                            "license_expression": "Apache-2.0",
                            "name": "visualworld-v02-tracking-harness",
                            "revision": source_revision,
                            "sha256": harness_sha256,
                            "source_url": (
                                "https://github.com/mayank-gupta16/vision-query-system/blob/"
                                f"{source_revision}/scripts/run_v02_tracking_benchmark.py"
                            ),
                        },
                        "executable_implementation": "cpython",
                        "metric_implementation": {
                            "license_expression": "MIT",
                            "name": "visualworld-trackeval-compatible-metrics",
                            "revision": source_revision,
                            "sha256": metric_sha256,
                            "source_url": (
                                "https://github.com/mayank-gupta16/vision-query-system/blob/"
                                f"{source_revision}/scripts/v02_tracking_metrics.py"
                            ),
                        },
                        "python_version": "3.13.15",
                        "source_revision": source_revision,
                    },
                    "policy_sha256": context["policy_sha256"],
                    "policy_version": cast(dict[str, object], context["policy"])["policy_version"],
                    "profile": context["profile"],
                    "repetitions": receipt_repetitions,
                    "schema": "visualworld.v02-evaluation-receipt",
                    "schema_version": 1,
                    "split": "test",
                    "waiver": None,
                }
                try:
                    receipt_name = f"{candidate_name}-receipt.json"
                    receipt_sha256 = detection._write_new(output_descriptor, receipt_name, receipt)
                    validated = gate_evaluator.validate_receipt(
                        receipt,
                        cast(dict[str, object], context["policy"]),
                        cast(str, context["policy_sha256"]),
                        cast(dict[str, object], context["dataset"]),
                        cast(str, context["dataset_sha256"]),
                    )
                    gate = gate_evaluator.evaluate(
                        cast(dict[str, object], context["policy"]),
                        validated,
                        baseline=None,
                        baseline_receipt_sha256=None,
                        receipt_sha256=receipt_sha256,
                        as_of=cast(date, context["evaluation_day"]),
                    )
                    gate_name = f"{candidate_name}-gate.json"
                    gate_sha256 = detection._write_new(output_descriptor, gate_name, gate)
                except (detection.BenchmarkError, gate_evaluator.EvaluationError) as error:
                    raise TrackingBenchmarkError("generated_receipt_invalid") from error
                gate_results[candidate_name] = gate
                outputs[candidate_name] = {
                    "gate": gate_name,
                    "gate_sha256": gate_sha256,
                    "gate_status": gate["status"],
                    "receipt": receipt_name,
                    "receipt_sha256": receipt_sha256,
                }
            eligible = [
                result
                for result in results
                if gate_results[
                    cast(str, cast(dict[str, object], result["configuration"])["name"])
                ]["status"]
                == "pass"
            ]
            recommended = (
                None
                if not eligible
                else cast(
                    dict[str, object], min(eligible, key=_test_selection_key)["configuration"]
                )["name"]
            )
            detection_evidence: list[dict[str, object]] = []
            for index, item in enumerate(detections):
                evidence = {
                    "cpu_ns": item["cpu_ns"],
                    "detection_count": item["detection_count"],
                    "peak_rss_bytes": item["peak_rss_bytes"],
                    "prediction_sha256": item["prediction_sha256"],
                    "process_wall_ns": item["process_wall_ns"],
                    "seed": item["seed"],
                    "wall_ns": item["wall_ns"],
                    "worker_environment": item["worker_environment"],
                }
                if index == 0:
                    evidence["representative_predictions"] = item["sequences"]
                detection_evidence.append(evidence)
            raw_result = {
                "candidates": {
                    cast(str, cast(dict[str, object], result["configuration"])["name"]): {
                        "aggregate": result["aggregate"],
                        "configuration": result["configuration"],
                        "repetitions": _raw_repetitions(
                            cast(list[dict[str, object]], result["repetitions"])
                        ),
                    }
                    for result in results
                },
                "detection_repetitions": detection_evidence,
                "evaluated_on": evaluated_on,
                "outputs": outputs,
                "phase": "test",
                "provenance": {
                    "annotation_lock_sha256": context["annotations_sha256"],
                    "calibration_results_sha256": calibration_results_sha256,
                    "calibration_selection_sha256": selection_sha256,
                    "candidate_manifest_sha256": context["candidates_sha256"],
                    "dataset_manifest_sha256": context["dataset_sha256"],
                    "evaluation_harness_sha256": harness_sha256,
                    "metric_implementation_sha256": metric_sha256,
                    "policy_sha256": context["policy_sha256"],
                    "source_manifest_sha256": context["source_sha256"],
                    "source_revision": source_revision,
                    **trusted_code_digests,
                },
                "recommended_candidate": recommended,
                "schema": "visualworld.v02-tracking-raw-results",
                "schema_version": 1,
            }
            raw_sha256 = detection._write_new(output_descriptor, "raw-results.json", raw_result)
        finally:
            detection._close_once(output_descriptor)
        return {
            "outputs": outputs,
            "raw_results_sha256": raw_sha256,
            "recommended_candidate": recommended,
            "status": "pass",
        }
    finally:
        runtime_temporary.cleanup()


def main() -> int:
    parser = _StableArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("calibration", "test"), required=True)
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--models-root", required=True, type=Path)
    parser.add_argument("--base-python", required=True, type=Path)
    parser.add_argument("--wheels-root", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--evaluated-on", required=True)
    parser.add_argument("--output", required=True, type=Path)
    try:
        arguments = parser.parse_args()
        function = run_calibration if arguments.phase == "calibration" else run_test
        result = function(
            dataset_root=arguments.dataset_root.resolve(),
            models_root=arguments.models_root.resolve(),
            base_python=arguments.base_python.resolve(),
            wheels_root=arguments.wheels_root.resolve(),
            output_root=Path(os.path.abspath(arguments.output)),
            source_revision=arguments.source_revision,
            evaluated_on=arguments.evaluated_on,
        )
    except (OSError, TrackingBenchmarkError, detection.BenchmarkError) as error:
        code = str(error) if isinstance(error, TrackingBenchmarkError) else "filesystem_failed"
        _emit({"error": code, "status": "error"}, sys.stdout)
        return 1
    return 0 if _emit(result, sys.stdout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
