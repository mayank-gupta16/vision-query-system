# SPDX-License-Identifier: Apache-2.0
"""Contracts for the locked original-resolution crop research harness."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import cast

import evaluate_v02_gates as evaluator
import prepare_v02_crop_dataset as preparation
import pytest
import run_v02_crop_benchmark as benchmark

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "fixtures" / "v02-crop-research"
RESULT_ROOT = FIXTURE_ROOT / "results"


def _json(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def test_crop_dataset_lock_binds_sources_splits_and_gate_policy() -> None:
    source_path = FIXTURE_ROOT / "source-manifest.json"
    candidate_path = FIXTURE_ROOT / "candidates.json"
    annotations_path = FIXTURE_ROOT / "annotations.json"
    dataset_path = FIXTURE_ROOT / "dataset-manifest.json"
    candidates = _json(candidate_path)
    annotations = _json(annotations_path)
    dataset = _json(dataset_path)
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    candidate_sha256 = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    annotation_sha256 = hashlib.sha256(annotations_path.read_bytes()).hexdigest()

    assert annotations["source_manifest_sha256"] == source_sha256
    assert annotations["candidate_manifest_sha256"] == candidate_sha256
    assert cast(dict[str, object], dataset["acquisition"])["sha256"] == source_sha256
    policy, _ = evaluator.load_policy(
        ROOT / "fixtures" / "v02-evaluation" / "policy-v0.2-gates-2.json"
    )
    validated, digest = evaluator.load_manifest(dataset_path, policy)
    assert validated == dataset
    assert digest == hashlib.sha256(dataset_path.read_bytes()).hexdigest()

    split_sources: dict[str, set[object]] = {}
    for split, expected_count in (("calibration", 12), ("test", 18)):
        clips, items, payload, ids = benchmark._split_payload(annotations, split)
        split_sources[split] = {clip["source_id"] for clip in clips}
        contract = cast(dict[str, object], cast(dict[str, object], dataset["splits"])[split])
        assert len(items) == expected_count == contract["item_count"]
        assert hashlib.sha256(payload).hexdigest() == contract["annotation_sha256"]
        assert hashlib.sha256(ids).hexdigest() == contract["item_ids_sha256"]
    assert split_sources["calibration"].isdisjoint(split_sources["test"])
    benchmark._validate_annotations(
        annotations,
        annotation_sha256,
        source_sha256,
        candidate_sha256,
        dataset,
    )
    preparation._validate_candidates(candidates)


def test_crop_sources_are_cc0_sanitized_and_charts_are_non_identifying() -> None:
    source = _json(FIXTURE_ROOT / "source-manifest.json")
    privacy = cast(dict[str, object], source["privacy"])
    assert source["license_expression"] == "CC0-1.0"
    assert privacy == {
        "contains_faces": False,
        "contains_personal_data": False,
        "contains_plates": False,
        "contains_real_people": False,
        "generated_detail_is_identifying": False,
        "sensitive_derived_outputs_retained": False,
        "source_boundary": "only plate-sanitized issue-21 RGB derivatives are accepted",
    }
    assert "no real identifier or biometric content" in cast(
        str, cast(dict[str, object], source["derivation"])["specialist_panels"]
    )
    dataset = _json(FIXTURE_ROOT / "dataset-manifest.json")
    assert cast(dict[str, object], dataset["privacy"])["contains_personal_data"] is False


def test_crop_candidate_and_decision_thresholds_are_exact() -> None:
    candidates = _json(FIXTURE_ROOT / "candidates.json")
    preparation._validate_candidates(candidates)
    assert cast(dict[str, object], candidates["decision"]) == {
        "absent_control_maximum_absolute_gain_basis_points": 500,
        "eager_retention_maximum_byte_ratio_milli": 4000,
        "material_readable_gain_basis_points": 500,
        "material_specialist_gain_basis_points": 1000,
    }
    detector = cast(dict[str, object], candidates["detector"])
    assert (detector["width"], detector["height"]) == (384, 384)
    assert detector["confidence_millionths"] == 950_000


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("detector", "width", 384.0),
        ("detector", "height", True),
        ("detector", "confidence_millionths", 950_000.0),
        ("specialist", "threshold", 127.0),
        ("specialist", "labels_per_task", True),
    ],
)
def test_crop_candidate_rejects_non_integer_numeric_types(
    section: str, field: str, value: object
) -> None:
    candidates = _json(FIXTURE_ROOT / "candidates.json")
    cast(dict[str, object], candidates[section])[field] = value
    with pytest.raises(preparation.CropPreparationError, match="invalid_candidate_manifest"):
        preparation._validate_candidates(candidates)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("baseline", "crop", "another crop"),
        ("candidate", "mapping", "another mapping"),
        ("decision", "material_readable_gain_basis_points", 501),
        ("detector", "preprocessing", "another resize"),
        ("metric", "readable_detail", "another metric"),
        ("specialist", "tie_break", "largest-label"),
    ],
)
def test_crop_candidate_rejects_semantic_changes(section: str, field: str, value: object) -> None:
    candidates = _json(FIXTURE_ROOT / "candidates.json")
    cast(dict[str, object], candidates[section])[field] = value
    with pytest.raises(preparation.CropPreparationError, match="invalid_candidate_manifest"):
        preparation._validate_candidates(candidates)


def test_crop_json_loaders_reject_duplicate_and_nonfinite_values(tmp_path: Path) -> None:
    for name, payload in {
        "duplicate.json": b'{"schema":1,"schema":2}',
        "nonfinite.json": b'{"value":NaN}',
    }.items():
        path = tmp_path / name
        path.write_bytes(payload)
        with pytest.raises(preparation.CropPreparationError, match="invalid_test_json"):
            preparation._load_json(path, "invalid_test_json")
        with pytest.raises(benchmark.CropBenchmarkError, match="invalid_test_json"):
            benchmark._load_json(path, "invalid_test_json")


def test_crop_source_manifest_rejects_changed_derivation() -> None:
    source_path = FIXTURE_ROOT / "source-manifest.json"
    detection_annotations_path = ROOT / "fixtures" / "v02-detection-research" / "annotations.json"
    detection_source_path = ROOT / "fixtures" / "v02-detection-research" / "source-manifest.json"
    source = _json(source_path)
    cast(dict[str, object], source["derivation"])["specialist_panels"] = "unlocked"
    detection_annotations = _json(detection_annotations_path)
    with pytest.raises(preparation.CropPreparationError, match="invalid_source_manifest"):
        preparation._validate_source_manifest(
            source,
            detection_annotations,
            hashlib.sha256(detection_annotations_path.read_bytes()).hexdigest(),
            hashlib.sha256(detection_source_path.read_bytes()).hexdigest(),
        )


def test_crop_annotation_source_binding_is_exact() -> None:
    source_path = FIXTURE_ROOT / "source-manifest.json"
    candidate_path = FIXTURE_ROOT / "candidates.json"
    annotations_path = FIXTURE_ROOT / "annotations.json"
    annotations = _json(annotations_path)
    first = cast(list[dict[str, object]], annotations["items"])[0]
    first["source_id"] = "buggy"
    dataset = _json(FIXTURE_ROOT / "dataset-manifest.json")
    _, _, payload, _ = benchmark._split_payload(annotations, "calibration")
    calibration = cast(dict[str, object], cast(dict[str, object], dataset["splits"])["calibration"])
    calibration["annotation_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(benchmark.CropBenchmarkError, match="invalid_annotations"):
        benchmark._validate_annotations(
            annotations,
            hashlib.sha256(annotations_path.read_bytes()).hexdigest(),
            hashlib.sha256(source_path.read_bytes()).hexdigest(),
            hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
            dataset,
        )


def test_patterns_are_deterministic_distinct_and_round_trip() -> None:
    for task in preparation.TASKS:
        patterns = [preparation.pattern_bits(task, label) for label in range(8)]
        assert len(set(patterns)) == 8
        assert all(len(pattern) == 64 for pattern in patterns)
        for label, pattern in enumerate(patterns):
            assert benchmark.classify(pattern, task) == label
    labels = preparation.assigned_labels("kit-buggy", "tiny")
    assert labels == {"face_visibility": 2, "plate_ocr": 3, "vehicle_detail": 1}
    assert len(preparation.combined_pattern(labels)) == 192


def test_cell_reader_recovers_an_exact_native_grid() -> None:
    labels = {
        "face_visibility": 2,
        "plate_ocr": 3,
        "vehicle_detail": 1,
    }
    bits = preparation.combined_pattern(labels)
    pixels = bytes(channel for bit in bits for channel in ((16,) * 3 if bit else (240,) * 3))
    decoded = benchmark.sample_cells(pixels, 24, 8, (0, 0, 24, 8))
    assert decoded == bits
    readable, specialists, tasks = benchmark._score_panel(decoded, labels)
    assert readable == 192
    assert specialists == 3
    assert all(tasks.values())


def test_rawvideo_movie_has_bounded_exact_payload() -> None:
    frames = (b"\x01\x02\x03", b"\x04\x05\x06", b"\x07\x08\x09")
    movie = preparation.rawvideo_movie(frames, 1, 1)
    assert movie[4:8] == b"ftyp"
    assert movie[24:28] == b"mdat"
    assert movie[28:37] == b"".join(frames)
    assert movie[41:45] == b"moov"
    with pytest.raises(preparation.CropPreparationError, match="clip_generation_failed"):
        preparation.rawvideo_movie((frames[0],), 1, 1)


def _fake_result(
    item_id: str, stratum: str, original: tuple[int, int], detector: tuple[int, int]
) -> dict[str, object]:
    def path(values: tuple[int, int]) -> dict[str, object]:
        return {
            "resolved_readable_cells": values[0],
            "resolved_specialists_correct": values[1],
            "absent_readable_cells": 96,
            "absent_specialists_correct": 0,
        }

    return {
        "exact_crop_byte_match": True,
        "item_id": item_id,
        "mapping_error_milli_pixels": 0,
        "scores": {"original": path(original), "detector": path(detector)},
        "stratum": stratum,
    }


def test_paired_gain_metrics_include_absent_control_once() -> None:
    items: list[dict[str, object]] = [
        {"item_id": f"item-{stratum}", "stratum": stratum} for stratum in preparation.STRATA
    ]
    results = [
        _fake_result(cast(str, item["item_id"]), cast(str, item["stratum"]), (192, 3), (96, 0))
        for item in items
    ]
    metrics, diagnostics = benchmark._metrics(
        items, results, [1_000_000, 2_000_000, 3_000_000], [30, 10, 20], 123
    )
    assert metrics["readable_detail_proxy_gain_basis_points"] == {
        "overall": 2500,
        "tiny": 2500,
        "small": 2500,
        "medium": 2500,
    }
    assert metrics["specialist_accuracy_gain_basis_points"] == {
        "overall": 5000,
        "tiny": 5000,
        "small": 5000,
        "medium": 5000,
    }
    assert metrics["evidence_bytes_per_crop"]["overall"] == 20
    assert metrics["crop_wall_ms"]["overall"] == 2
    overall = cast(dict[str, object], cast(dict[str, object], diagnostics["by_stratum"])["overall"])
    absent = cast(dict[str, object], overall["conditions"])["absent"]
    assert cast(dict[str, int], cast(dict[str, object], absent)["gain"]) == {
        "readable_detail_basis_points": 0,
        "specialist_accuracy_basis_points": 0,
    }


@pytest.mark.parametrize(
    "script",
    ["prepare_v02_crop_dataset.py", "run_v02_crop_benchmark.py"],
)
def test_crop_tools_redact_invalid_arguments(script: str) -> None:
    private_path = "/private/customer/face-and-plate.mov"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), private_path],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert result.stdout == '{"error": "invalid_arguments", "status": "error"}\n'
    assert private_path not in result.stdout + result.stderr


def test_local_generated_crop_clips_match_locks_when_available() -> None:
    dataset_root = ROOT / "artifacts" / "issue23" / "dataset-v4"
    if not dataset_root.is_dir():
        pytest.skip("generated crop clips are optional research prerequisites")
    annotations = _json(FIXTURE_ROOT / "annotations.json")
    clip = cast(list[dict[str, object]], annotations["clips"])[0]
    source, source_start = benchmark._movie_frames(
        dataset_root,
        clip["source_relative_path"],
        clip["source_byte_count"],
        clip["source_sha256"],
        preparation.SOURCE_WIDTH,
        preparation.SOURCE_HEIGHT,
        benchmark.MAX_SOURCE_CLIP_BYTES,
    )
    first = source[source_start : source_start + benchmark.SOURCE_FRAME_BYTES]
    first_item = cast(list[dict[str, object]], annotations["items"])[0]
    assert hashlib.sha256(first).hexdigest() == first_item["source_frame_sha256"]


def test_committed_crop_results_revalidate_when_present() -> None:
    raw_path = RESULT_ROOT / "raw-results.json"
    if not raw_path.is_file():
        pytest.skip("measured crop results are added after the harness lock commit")
    raw = _json(raw_path)
    outputs = cast(dict[str, object], raw["outputs"])
    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == (
        "3e36abc6ac437ca954898bdb37c8fe0a1cf1c4034a4ba509da16fbb39e43078b"
    )
    provenance = cast(dict[str, object], raw["provenance"])
    assert provenance["source_revision"] == "d9b4bdc305051a2e8cd91a17bc4b90115551ed03"
    test_result = cast(dict[str, object], raw["test"])
    aggregate = cast(dict[str, object], test_result["aggregate"])
    metrics = cast(dict[str, object], aggregate["metrics"])
    assert metrics["mapping_error_milli_pixels"] == {
        "overall": 0,
        "tiny": 0,
        "small": 0,
        "medium": 0,
    }
    assert metrics["exact_crop_byte_match_basis_points"] == {
        "overall": 10_000,
        "tiny": 10_000,
        "small": 10_000,
        "medium": 10_000,
    }
    assert metrics["readable_detail_proxy_gain_basis_points"] == {
        "overall": 673,
        "tiny": 1601,
        "small": 416,
        "medium": 0,
    }
    assert metrics["specialist_accuracy_gain_basis_points"] == {
        "overall": 93,
        "tiny": 278,
        "small": 0,
        "medium": 0,
    }
    repetitions = cast(list[dict[str, object]], test_result["repetitions"])
    diagnostics = cast(dict[str, object], repetitions[0]["diagnostics"])
    assert diagnostics["byte_ratio_milli"] == 56_250
    by_stratum = cast(dict[str, object], diagnostics["by_stratum"])
    for stratum in ("overall", "tiny", "small", "medium"):
        conditions = cast(
            dict[str, object], cast(dict[str, object], by_stratum[stratum])["conditions"]
        )
        absent = cast(dict[str, object], conditions["absent"])
        assert absent["gain"] == {
            "readable_detail_basis_points": 0,
            "specialist_accuracy_basis_points": 0,
        }
    policy, policy_sha256 = evaluator.load_policy(
        ROOT / "fixtures" / "v02-evaluation" / "policy-v0.2-gates-2.json"
    )
    dataset, dataset_sha256 = evaluator.load_manifest(
        FIXTURE_ROOT / "dataset-manifest.json", policy
    )
    receipt_path = RESULT_ROOT / cast(str, outputs["receipt"])
    receipt_bytes = receipt_path.read_bytes()
    receipt = cast(dict[str, object], json.loads(receipt_bytes))
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    assert receipt_sha256 == outputs["receipt_sha256"]
    validated = evaluator.validate_receipt(receipt, policy, policy_sha256, dataset, dataset_sha256)
    gate = evaluator.evaluate(
        policy,
        validated,
        baseline=None,
        baseline_receipt_sha256=None,
        receipt_sha256=receipt_sha256,
        as_of=date.fromisoformat(cast(str, receipt["evaluated_on"])),
    )
    gate_path = RESULT_ROOT / cast(str, outputs["gate"])
    gate_bytes = gate_path.read_bytes()
    assert hashlib.sha256(gate_bytes).hexdigest() == outputs["gate_sha256"]
    assert cast(dict[str, object], json.loads(gate_bytes)) == gate
    assert gate["status"] == "pass"
