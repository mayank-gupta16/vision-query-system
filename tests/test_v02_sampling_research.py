# SPDX-License-Identifier: Apache-2.0
"""Contracts for the locked frame-sampling research harness."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import cast

import evaluate_v02_gates as evaluator
import prepare_v02_sampling_dataset as preparation
import pytest
import run_v02_sampling_benchmark as benchmark

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "fixtures" / "v02-sampling-research"
RESULT_ROOT = FIXTURE_ROOT / "results"


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
        "schedule": (
            "3 FPS targets quantized to the nearest 8 FPS lattice frame outside bursts; "
            "8 FPS lattice inside bursts"
        ),
        "threshold_grid_millionths": grid,
    }


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("fixed", "0", True),
        ("fixed", "0", 1.0),
        ("adaptive", "base_fps", 3.0),
        ("adaptive", "maximum_fps", True),
        ("adaptive", "burst_duration_ms", 750.0),
        ("detector", "confidence_millionths", 950_000.0),
    ],
)
def test_sampling_candidate_numbers_require_exact_integer_types(
    section: str, field: str, value: object
) -> None:
    manifest = _json(FIXTURE_ROOT / "candidates.json")
    if section == "fixed":
        cast(list[object], manifest["fixed_rates_fps"])[int(field)] = value
    else:
        cast(dict[str, object], manifest[section])[field] = value
    with pytest.raises(benchmark.SamplingBenchmarkError, match="invalid_candidate_manifest"):
        benchmark._validate_configuration(manifest)


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


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("event", "target_width", 10**9),
        ("event", "target_width", 144.0),
        ("event", "duration_ms", True),
        ("canvas", "width", 384.0),
        ("derivation", "clip_duration_ms", 6000.0),
        ("derivation", "timing", "unbounded caller timing"),
        ("derivation", "interpolation", "unknown"),
        ("privacy", "contains_faces", True),
        ("privacy", "contains_personal_data", 0),
    ],
)
def test_sampling_source_derivation_and_privacy_are_fully_locked(
    section: str, field: str, value: object
) -> None:
    source = _json(FIXTURE_ROOT / "source-manifest.json")
    detection_annotations = _json(ROOT / "fixtures" / "v02-detection-research" / "annotations.json")
    if section == "event":
        events = cast(
            list[dict[str, object]],
            cast(dict[str, object], source["derivation"])["events"],
        )
        events[0][field] = value
    else:
        cast(dict[str, object], source[section])[field] = value
    with pytest.raises(preparation.SamplingPreparationError, match="invalid_manifest"):
        preparation._validate_source_manifest(source, detection_annotations)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("width", 640.0),
        ("height", True),
        ("object_box", [174.0, 108, 447, 348]),
    ],
)
def test_sampling_detection_source_annotations_are_strict(field: str, value: object) -> None:
    source = _json(FIXTURE_ROOT / "source-manifest.json")
    detection_annotations = _json(ROOT / "fixtures" / "v02-detection-research" / "annotations.json")
    cast(list[dict[str, object]], detection_annotations["items"])[0][field] = value
    with pytest.raises(preparation.SamplingPreparationError, match="invalid_manifest"):
        preparation._validate_source_manifest(source, detection_annotations)


def _relock_sampling_split(
    annotations: dict[str, object], dataset: dict[str, object], split: str
) -> None:
    _, _, annotation_payload, ids_payload = benchmark._split_payload(annotations, split)
    contract = cast(dict[str, object], cast(dict[str, object], dataset["splits"])[split])
    contract["annotation_sha256"] = hashlib.sha256(annotation_payload).hexdigest()
    contract["item_ids_sha256"] = hashlib.sha256(ids_payload).hexdigest()


@pytest.mark.parametrize("mutation", ["event_interval", "event_type", "frame_box"])
def test_sampling_annotations_bind_intervals_frames_and_boxes(mutation: str) -> None:
    annotations = _json(FIXTURE_ROOT / "annotations.json")
    dataset = _json(FIXTURE_ROOT / "dataset-manifest.json")
    clips = cast(list[dict[str, object]], annotations["clips"])
    events = cast(list[dict[str, object]], annotations["events"])
    if mutation == "event_interval":
        events[0]["start_ms"] = 335
    elif mutation == "event_type":
        events[0]["start_ms"] = True
    else:
        frames = cast(list[dict[str, object]], clips[0]["frames"])
        frames[5]["object_box"] = [19.0, 90, 163, 216]
    _relock_sampling_split(annotations, dataset, "calibration")
    with pytest.raises(benchmark.SamplingBenchmarkError, match="invalid_annotations"):
        benchmark._validate_annotations(annotations, dataset)


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

    maximum, _ = benchmark._fixed_selection(frames, 8)
    quiet, quiet_errors, quiet_scores = benchmark._adaptive_selection(frames, 1_000_001)
    burst, burst_errors, burst_scores = benchmark._adaptive_selection(frames, 1_000)

    assert len(quiet) == 18
    assert quiet <= maximum
    assert max(quiet_scores) == 1_000_000
    assert len(quiet_errors) == 18
    assert burst != quiet
    assert burst <= maximum
    assert len(quiet) < len(burst) <= len(maximum)
    assert max(burst_scores) == 1_000_000
    assert len(burst_errors) <= 48


def test_adaptive_sampling_never_escapes_maximum_lattice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    annotations = _json(FIXTURE_ROOT / "annotations.json")
    clip = cast(list[dict[str, object]], annotations["clips"])[0]
    frames = [(cast(dict[str, object], frame), b"") for frame in cast(list[object], clip["frames"])]
    maximum, _ = benchmark._fixed_selection(frames, 8)
    for triggered in (
        (5, 20, 35, 50, 65, 80),
        (1, 20, 40, 60, 80),
        tuple(range(1, 86, 10)),
    ):
        scores = [0] * len(frames)
        for index in triggered:
            scores[index] = 1_000_000
        monkeypatch.setattr(
            benchmark,
            "_motion_scores",
            lambda _, selected_scores=scores: selected_scores,
        )
        selected, _, _ = benchmark._adaptive_selection(frames, 1_000)
        assert selected <= maximum
        assert len(selected) <= len(maximum)


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


def test_raw_repetitions_keep_one_payload_and_hash_every_repetition() -> None:
    repetitions = [
        {
            "metrics": {"value": index},
            "predictions": {"item": [{"confidence_millionths": 1}]},
            "samples": [{"item_id": "item"}],
            "seed": index,
        }
        for index in range(2)
    ]
    repetitions.append(
        {
            "metrics": {"value": 2},
            "predictions": {"item": [{"confidence_millionths": 2}]},
            "samples": [{"item_id": "changed"}],
            "seed": 2,
        }
    )
    compact = benchmark._raw_repetitions(repetitions)
    assert compact[0]["representative_predictions"] == repetitions[0]["predictions"]
    assert compact[0]["representative_samples"] == repetitions[0]["samples"]
    assert "representative_predictions" not in compact[1]
    assert "representative_samples" not in compact[1]
    assert compact[2]["representative_predictions"] == repetitions[2]["predictions"]
    assert compact[2]["representative_samples"] == repetitions[2]["samples"]
    for index, repetition in enumerate(repetitions):
        assert (
            compact[index]["predictions_sha256"]
            == hashlib.sha256(benchmark._canonical(repetition["predictions"])).hexdigest()
        )
        assert (
            compact[index]["samples_sha256"]
            == hashlib.sha256(benchmark._canonical(repetition["samples"])).hexdigest()
        )


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

    preparation_command = [
        sys.executable,
        os.fspath(ROOT / "scripts" / "prepare_v02_sampling_dataset.py"),
        "--input-root",
        os.fspath(tmp_path / marker),
        "--output",
        os.fspath(tmp_path / "unused"),
    ]
    for parser_command in (command, preparation_command):
        for arguments in ([], ["--unknown-private-value", marker]):
            parsed = subprocess.run(
                [*parser_command[:2], *arguments],
                check=False,
                capture_output=True,
                timeout=10,
            )
            assert parsed.returncode == 1
            assert parsed.stderr == b""
            assert json.loads(parsed.stdout) == {
                "error": "invalid_arguments",
                "status": "error",
            }
            assert marker.encode() not in parsed.stdout
            assert marker.encode() not in parsed.stderr
    for failing_command in (command, preparation_command):
        for unbuffered in (False, True):
            environment = {
                key: value for key, value in os.environ.items() if key != "PYTHONUNBUFFERED"
            }
            if unbuffered:
                environment["PYTHONUNBUFFERED"] = "1"
            for redirection in ("exec 1>/dev/full", "exec 1>&-; exec 2>&-"):
                broken = subprocess.run(
                    [
                        "/bin/bash",
                        "-c",
                        f'{redirection}; exec "$@"',
                        "bash",
                        *failing_command,
                    ],
                    check=False,
                    capture_output=True,
                    env=environment,
                    timeout=10,
                )
                assert broken.returncode == 1
                assert b"Traceback" not in broken.stderr
                assert b"Exception ignored" not in broken.stderr


def test_published_sampling_results_reproduce_frozen_gate_outputs() -> None:
    raw_path = RESULT_ROOT / "raw-results.json"
    raw_bytes = raw_path.read_bytes()
    raw = cast(dict[str, object], json.loads(raw_bytes))
    assert hashlib.sha256(raw_bytes).hexdigest() == (
        "eee7dcc181123db5e6316bed317182e2c73e71041743ba8c42efc3694a7a8453"
    )
    assert b"/root" not in raw_bytes
    assert b"P6\n" not in raw_bytes
    provenance = cast(dict[str, object], raw["provenance"])
    assert provenance["source_revision"] == "87ca4562fba5dfac5818ff9c81b75bb0f1be4959"
    assert (
        provenance["evaluation_harness_sha256"]
        == hashlib.sha256(
            (ROOT / "scripts" / "run_v02_sampling_benchmark.py").read_bytes()
        ).hexdigest()
    )
    assert raw["adaptive_threshold_millionths"] == 30_000

    policy, policy_sha256 = evaluator.load_policy(
        ROOT / "fixtures" / "v02-evaluation" / "policy-v0.2-gates-2.json"
    )
    dataset, dataset_sha256 = evaluator.load_manifest(
        FIXTURE_ROOT / "dataset-manifest.json", policy
    )
    outputs = cast(dict[str, dict[str, object]], raw["outputs"])
    expected_status = {
        "adaptive-3-to-8-fps": "pass",
        "fixed-1-fps": "fail",
        "fixed-2-fps": "fail",
        "fixed-3-fps": "fail",
        "fixed-5-fps": "pass",
        "fixed-8-fps": "pass",
    }
    passing_costs: list[tuple[int, str]] = []
    for candidate, status in expected_status.items():
        output = outputs[candidate]
        receipt_path = RESULT_ROOT / cast(str, output["receipt"])
        receipt_bytes = receipt_path.read_bytes()
        receipt = cast(dict[str, object], json.loads(receipt_bytes))
        receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
        assert receipt_sha256 == output["receipt_sha256"]
        validated = evaluator.validate_receipt(
            receipt,
            policy,
            policy_sha256,
            dataset,
            dataset_sha256,
        )
        generated_gate = evaluator.evaluate(
            policy,
            validated,
            baseline=None,
            baseline_receipt_sha256=None,
            receipt_sha256=receipt_sha256,
            as_of=date(2026, 9, 7),
        )
        gate_path = RESULT_ROOT / cast(str, output["gate"])
        gate_bytes = gate_path.read_bytes()
        assert hashlib.sha256(gate_bytes).hexdigest() == output["gate_sha256"]
        assert cast(dict[str, object], json.loads(gate_bytes)) == generated_gate
        assert generated_gate["status"] == status
        if status == "pass":
            aggregate = cast(dict[str, object], receipt["aggregate"])
            metrics = cast(dict[str, dict[str, int]], aggregate["metrics"])
            passing_costs.append((metrics["samples_per_source_minute"]["overall"], candidate))
    assert min(passing_costs) == (300, "fixed-5-fps")


def test_local_generated_clip_matches_every_locked_frame_when_available() -> None:
    dataset_root = ROOT / "artifacts" / "issue22" / "dataset-v8"
    if not dataset_root.is_dir():
        pytest.skip("generated sampling clips are optional research prerequisites")
    annotations = _json(FIXTURE_ROOT / "annotations.json")
    first_clip = cast(list[dict[str, object]], annotations["clips"])[0]
    frames = benchmark._read_clip(dataset_root, first_clip)
    assert len(frames) == 86
    assert [frame[0]["pts_ms"] for frame in frames][-1] == 5933
