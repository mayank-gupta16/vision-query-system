# SPDX-License-Identifier: Apache-2.0
"""Contracts for the locked frame-sampling research harness."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import evaluate_v02_gates as evaluator
import prepare_v02_sampling_dataset as preparation
import pytest
import run_v02_sampling_benchmark as benchmark

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "fixtures" / "v02-sampling-research"


def _json(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def test_sampling_dataset_lock_binds_sources_splits_and_gate_policy() -> None:
    source_path = FIXTURE_ROOT / "source-manifest.json"
    annotations_path = FIXTURE_ROOT / "annotations.json"
    dataset_path = FIXTURE_ROOT / "dataset-manifest.json"
    source = _json(source_path)
    annotations = _json(annotations_path)
    dataset = _json(dataset_path)
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()

    assert annotations["source_manifest_sha256"] == source_sha256
    assert cast(dict[str, object], dataset["acquisition"])["sha256"] == source_sha256
    policy, _ = evaluator.load_policy(
        ROOT / "fixtures" / "v02-evaluation" / "policy-v0.2-gates-2.json"
    )
    validated, digest = evaluator.load_manifest(dataset_path, policy)
    assert validated == dataset
    assert digest == hashlib.sha256(dataset_path.read_bytes()).hexdigest()

    source_clips = cast(list[dict[str, object]], source["clips"])
    annotation_clips = cast(list[dict[str, object]], annotations["clips"])
    assert len(source_clips) == len(annotation_clips) == 10
    assert {(clip["clip_id"], clip["source_id"], clip["split"]) for clip in source_clips} == {
        (clip["clip_id"], clip["source_id"], clip["split"]) for clip in annotation_clips
    }

    split_sources: dict[str, set[object]] = {}
    for split, expected_event_count in (("calibration", 16), ("test", 24)):
        clips, events, annotation_payload, ids_payload = benchmark._split_payload(
            annotations, split
        )
        split_sources[split] = {clip["source_id"] for clip in clips}
        contract = cast(dict[str, object], cast(dict[str, object], dataset["splits"])[split])
        assert len(events) == expected_event_count == contract["item_count"]
        assert hashlib.sha256(annotation_payload).hexdigest() == contract["annotation_sha256"]
        assert hashlib.sha256(ids_payload).hexdigest() == contract["item_ids_sha256"]
    assert split_sources["calibration"].isdisjoint(split_sources["test"])
    benchmark._validate_annotations(annotations, dataset)


def test_sampling_sources_are_cc0_privacy_sanitized_and_bounded() -> None:
    source = _json(FIXTURE_ROOT / "source-manifest.json")
    dataset = _json(FIXTURE_ROOT / "dataset-manifest.json")
    assert source["license_expression"] == "CC0-1.0"
    assert source["terms_url"] == "https://creativecommons.org/publicdomain/zero/1.0/legalcode"
    privacy = cast(dict[str, object], source["privacy"])
    assert privacy == {
        "contains_faces": False,
        "contains_personal_data": False,
        "contains_plates": False,
        "contains_real_people": False,
        "source_boundary": "only plate-sanitized issue-21 RGB derivatives are accepted",
    }
    assert cast(dict[str, object], dataset["rights"])["license_expression"] == "CC0-1.0"
    assert cast(dict[str, object], dataset["privacy"])["contains_personal_data"] is False

    detection_annotations = _json(ROOT / "fixtures" / "v02-detection-research" / "annotations.json")
    clips, events = preparation._validate_source_manifest(source, detection_annotations)
    assert len(clips) == 10
    assert [event["stratum"] for event in events] == list(benchmark.STRATA[1:])
    assert events[0]["target_width"] == 144
    assert preparation._durations()[70] == 333
    assert sum(preparation._durations()) == 6000


def test_sampling_candidate_list_is_exact_and_bounded() -> None:
    manifest = _json(FIXTURE_ROOT / "candidates.json")
    fixed, grid = benchmark._validate_configuration(manifest)
    assert [policy["fps"] for policy in fixed] == [1, 2, 3, 5, 8]
    assert grid == [30_000, 50_000, 70_000, 90_000]
    assert cast(dict[str, object], manifest["adaptive"]) == {
        "base_fps": 3,
        "burst_duration_ms": 750,
        "maximum_fps": 8,
        "motion_score": "maximum 32x24 tile mean absolute RGB delta in millionths of 255",
        "threshold_grid_millionths": grid,
    }


def test_sampling_json_loaders_reject_duplicate_and_nonfinite_values(tmp_path: Path) -> None:
    for name, raw in {
        "duplicate.json": b'{"schema":1,"schema":2}',
        "nonfinite.json": b'{"value":NaN}',
    }.items():
        path = tmp_path / name
        path.write_bytes(raw)
        with pytest.raises(preparation.SamplingPreparationError, match="invalid_manifest"):
            preparation._load_json(path, "invalid_manifest")
        with pytest.raises(benchmark.SamplingBenchmarkError, match="invalid_input"):
            benchmark._load_json(path, "invalid_input")


@pytest.mark.parametrize("bad_version", [True, 1.0])
def test_sampling_schema_versions_require_exact_integers(bad_version: object) -> None:
    candidates = _json(FIXTURE_ROOT / "candidates.json")
    candidates["schema_version"] = bad_version
    with pytest.raises(benchmark.SamplingBenchmarkError, match="invalid_candidate_manifest"):
        benchmark._validate_configuration(candidates)

    annotations = _json(FIXTURE_ROOT / "annotations.json")
    dataset = _json(FIXTURE_ROOT / "dataset-manifest.json")
    annotations["schema_version"] = bad_version
    with pytest.raises(benchmark.SamplingBenchmarkError, match="invalid_annotations"):
        benchmark._validate_annotations(annotations, dataset)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("clip_id", "../../escape"),
        ("source_id", "/absolute"),
        ("input_relative_path", "../../private.ppm"),
    ],
)
def test_sampling_source_paths_and_identifiers_cannot_escape_roots(field: str, value: str) -> None:
    source = _json(FIXTURE_ROOT / "source-manifest.json")
    detection_annotations = _json(ROOT / "fixtures" / "v02-detection-research" / "annotations.json")
    cast(list[dict[str, object]], source["clips"])[0][field] = value
    with pytest.raises(preparation.SamplingPreparationError, match="invalid_manifest"):
        preparation._validate_source_manifest(source, detection_annotations)


def test_fixed_sampling_uses_nearest_pts_and_reports_vfr_error_separately() -> None:
    annotations = _json(FIXTURE_ROOT / "annotations.json")
    clip = cast(list[dict[str, object]], annotations["clips"])[0]
    frames = [(cast(dict[str, object], frame), b"") for frame in cast(list[object], clip["frames"])]

    one_fps, one_errors = benchmark._fixed_selection(frames, 1)
    five_fps, five_errors = benchmark._fixed_selection(frames, 5)
    eight_fps, eight_errors = benchmark._fixed_selection(frames, 8)
    assert sorted(one_fps) == [0, 15, 30, 45, 60, 71]
    assert one_errors == [0] * 6
    assert len(five_fps) == 30
    assert max(five_errors) == 133
    assert len(eight_fps) == 47
    assert max(eight_errors) == 125


def test_adaptive_sampling_replaces_base_schedule_during_bounded_bursts() -> None:
    annotations = _json(FIXTURE_ROOT / "annotations.json")
    clip = cast(list[dict[str, object]], annotations["clips"])[0]
    metadata = cast(list[dict[str, object]], clip["frames"])
    black = bytes(benchmark.FRAME_BYTES)
    white = bytes((255,)) * benchmark.FRAME_BYTES
    pixels = [black] * len(metadata)
    pixels[10] = white
    frames = list(zip(metadata, pixels, strict=True))

    base, base_errors = benchmark._fixed_selection(frames, 3)
    maximum, _ = benchmark._fixed_selection(frames, 8)
    quiet, quiet_errors, quiet_scores = benchmark._adaptive_selection(frames, 1_000_001)
    burst, burst_errors, burst_scores = benchmark._adaptive_selection(frames, 1_000)

    assert quiet == base
    assert quiet_errors == base_errors
    assert max(quiet_scores) == 1_000_000
    assert burst != base
    assert burst <= base | maximum
    assert len(base) < len(burst) <= len(maximum)
    assert max(burst_scores) == 1_000_000
    assert len(burst_errors) <= 48 + 18


def test_object_event_metrics_count_recall_and_track_opportunities() -> None:
    strata = list(benchmark.STRATA[1:])
    events: list[dict[str, object]] = [
        {"event_id": f"event-{index}", "strata": ["overall", stratum]}
        for index, stratum in enumerate(strata)
    ]
    samples: list[dict[str, object]] = []
    predictions: dict[str, list[dict[str, object]]] = {}
    matches_per_event = (2, 1, 0, 2)
    for event_index, match_count in enumerate(matches_per_event):
        for sample_index in range(2):
            item_id = f"item-{event_index}-{sample_index}"
            samples.append(
                {
                    "event_id": f"event-{event_index}",
                    "item_id": item_id,
                    "object_box": [0, 0, 10, 10],
                }
            )
            predictions[item_id] = (
                [{"box_milli_pixels": [0, 0, 10_000, 10_000]}]
                if sample_index < match_count
                else [{"box_milli_pixels": [20_000, 20_000, 30_000, 30_000]}]
            )

    recall, missed, counts = benchmark._event_metrics(events, samples, predictions)
    assert counts == {
        "overall": 4,
        "small_fast": 1,
        "camera_motion": 1,
        "cut_adjacent": 1,
        "vfr_gap": 1,
    }
    assert recall == {
        "overall": 7500,
        "small_fast": 10_000,
        "camera_motion": 10_000,
        "cut_adjacent": 0,
        "vfr_gap": 10_000,
    }
    assert missed == {
        "overall": 5000,
        "small_fast": 0,
        "camera_motion": 10_000,
        "cut_adjacent": 10_000,
        "vfr_gap": 0,
    }


def test_sampling_benchmark_cli_redacts_paths_and_has_stable_errors(tmp_path: Path) -> None:
    marker = "private-token-never-print"
    command = [
        sys.executable,
        os.fspath(ROOT / "scripts" / "run_v02_sampling_benchmark.py"),
        "--dataset-root",
        os.fspath(tmp_path / marker),
        "--models-root",
        os.fspath(tmp_path),
        "--base-python",
        os.fspath(tmp_path / "python"),
        "--wheels-root",
        os.fspath(tmp_path),
        "--output",
        os.fspath(tmp_path / "output"),
        "--source-revision",
        "invalid",
        "--evaluated-on",
        "2026-09-07",
    ]
    completed = subprocess.run(command, check=False, capture_output=True, timeout=10)
    assert completed.returncode == 1
    assert completed.stderr == b""
    assert json.loads(completed.stdout) == {
        "error": "invalid_source_revision",
        "status": "error",
    }
    assert marker.encode() not in completed.stdout
    assert b"Traceback" not in completed.stdout


def test_local_generated_clip_matches_every_locked_frame_when_available() -> None:
    dataset_root = ROOT / "artifacts" / "issue22" / "dataset-v8"
    if not dataset_root.is_dir():
        pytest.skip("generated sampling clips are optional research prerequisites")
    annotations = _json(FIXTURE_ROOT / "annotations.json")
    first_clip = cast(list[dict[str, object]], annotations["clips"])[0]
    frames = benchmark._read_clip(dataset_root, first_clip)
    assert len(frames) == 86
    assert [frame[0]["pts_ms"] for frame in frames][-1] == 5933
