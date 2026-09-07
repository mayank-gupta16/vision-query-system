# SPDX-License-Identifier: Apache-2.0
"""Contracts for the locked detector/runtime research harness."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import cast

import evaluate_v02_gates as evaluator
import prepare_v02_detection_dataset as preparation
import pytest
import run_v02_detection_benchmark as benchmark

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "fixtures" / "v02-detection-research"
RESULT_ROOT = FIXTURE_ROOT / "results"


def _json(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def test_dataset_manifest_binds_source_annotations_splits_and_gate_policy() -> None:
    source_path = FIXTURE_ROOT / "source-manifest.json"
    annotations_path = FIXTURE_ROOT / "annotations.json"
    dataset_path = FIXTURE_ROOT / "dataset-manifest.json"
    source = _json(source_path)
    annotations = _json(annotations_path)
    dataset = _json(dataset_path)
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()

    assert annotations["source_manifest_sha256"] == source_sha256
    assert cast(dict[str, object], dataset["acquisition"])["sha256"] == source_sha256
    policy, _ = evaluator.load_policy(ROOT / "fixtures" / "v02-evaluation" / "policy.json")
    validated, digest = evaluator.load_manifest(dataset_path, policy)
    assert validated == dataset
    assert digest == hashlib.sha256(dataset_path.read_bytes()).hexdigest()

    source_records = cast(list[dict[str, object]], source["sources"])
    assert len(source_records) == 10
    assert len({record["source_id"] for record in source_records}) == 10
    assert {record["split"] for record in source_records} == {"calibration", "test"}
    assert all(
        cast(str, record["download_url"]).startswith("https://thumb.wikimedia.org/")
        for record in source_records
    )
    assert all("?" not in cast(str, record["download_url"]) for record in source_records)

    items = cast(list[dict[str, object]], annotations["items"])
    assert len(items) == 20
    assert len({item["item_id"] for item in items}) == 20
    split_sources: dict[str, set[object]] = {}
    for split in ("calibration", "test"):
        split_items = [item for item in items if item["split"] == split]
        split_sources[split] = {item["source_id"] for item in split_items}
        split_contract = cast(dict[str, object], cast(dict[str, object], dataset["splits"])[split])
        assert split_contract["item_count"] == len(split_items)
        assert (
            split_contract["annotation_sha256"]
            == hashlib.sha256(_canonical(split_items)).hexdigest()
        )
        item_ids = ("\n".join(cast(str, item["item_id"]) for item in split_items) + "\n").encode()
        assert split_contract["item_ids_sha256"] == hashlib.sha256(item_ids).hexdigest()
        counts = cast(dict[str, int], split_contract["stratum_item_counts"])
        assert counts == {
            "easy": len([item for item in split_items if item["stratum"] == "easy"]),
            "overall": len(split_items),
            "small_distant": len(
                [item for item in split_items if item["stratum"] == "small_distant"]
            ),
        }
    assert split_sources["calibration"].isdisjoint(split_sources["test"])
    benchmark._validate_dataset_locks(annotations, dataset)


def test_source_manifest_is_cc0_privacy_sanitized_and_bounded() -> None:
    manifest = _json(FIXTURE_ROOT / "source-manifest.json")
    assert manifest["license_expression"] == "CC0-1.0"
    assert manifest["terms_url"] == "https://creativecommons.org/publicdomain/zero/1.0/legalcode"
    for source in cast(list[dict[str, object]], manifest["sources"]):
        width = cast(int, source["download_width"])
        height = cast(int, source["download_height"])
        preparation._box(source["object_box"], width, height)
        assert len(cast(str, source["download_sha256"])) == 64
        assert len(cast(str, source["file_sha1"])) == 40
        assert cast(str, source["privacy_review"])
        for redaction in cast(list[dict[str, object]], source["redactions"]):
            assert redaction["kind"] in {"background-plate-risk", "plate"}
            preparation._box(redaction["box"], width, height)


def test_candidate_list_is_a_pinned_same_family_tradeoff() -> None:
    manifest = _json(FIXTURE_ROOT / "candidates.json")
    candidates = cast(list[dict[str, object]], manifest["candidates"])
    assert [candidate["input_width"] for candidate in candidates] == [256, 384, 512]
    assert [candidate["omz_reported_ap_50_95_millionths"] for candidate in candidates] == [
        254000,
        322000,
        363000,
    ]
    assert {candidate["omz_revision"] for candidate in candidates} == {
        "86ba23e80b27eb9149da911e5c023b108cb06e80"
    }
    for candidate in candidates:
        for key in ("model_xml", "model_bin"):
            artifact = cast(dict[str, object], candidate[key])
            assert cast(int, artifact["size"]) > 0
            assert benchmark._DIGEST.fullmatch(cast(str, artifact["sha256"]))
            assert len(cast(str, artifact["download_sha384"])) == 96
            assert cast(str, artifact["url"]).startswith("https://storage.openvinotoolkit.org/")
    runtime = cast(dict[str, dict[str, object]], manifest["runtime"])
    assert {
        component["license_expression"]
        for component in runtime.values()
        if isinstance(component, dict)
    } <= {
        "Apache-2.0",
        "BSD-3-Clause",
    }
    validated, validated_runtime, confidence_floor, grid = benchmark._validate_candidate_manifest(
        manifest
    )
    assert validated == candidates
    assert validated_runtime == runtime
    assert confidence_floor == 10_000
    assert grid == list(range(50_000, 1_000_000, 50_000))


def test_research_json_loaders_reject_duplicate_and_nonfinite_values(tmp_path: Path) -> None:
    for name, raw in {
        "duplicate.json": b'{"schema":1,"schema":2}',
        "nonfinite.json": b'{"value":NaN}',
    }.items():
        path = tmp_path / name
        path.write_bytes(raw)
        with pytest.raises(preparation.PreparationError, match="invalid_manifest"):
            preparation._load_json(path)
        with pytest.raises(benchmark.BenchmarkError, match="invalid_input"):
            benchmark._load_json(path)


def test_candidate_manifest_and_worker_result_fail_closed() -> None:
    candidate_manifest = _json(FIXTURE_ROOT / "candidates.json")
    runtime = cast(dict[str, object], candidate_manifest["runtime"])
    runtime["openvino"] = None
    with pytest.raises(benchmark.BenchmarkError, match="invalid_candidate_manifest"):
        benchmark._validate_candidate_manifest(candidate_manifest)

    annotations_path = FIXTURE_ROOT / "annotations.json"
    annotations = _json(annotations_path)
    items = [
        item
        for item in cast(list[dict[str, object]], annotations["items"])
        if item["split"] == "test"
    ]
    timings = {cast(str, item["item_id"]): 1 for item in items}
    predictions = {
        cast(str, item["item_id"]): [
            {"box_milli_pixels": [0, 0, 1000, 1000], "confidence_millionths": 500_000}
        ]
        for item in items
    }
    result: dict[str, object] = {
        "annotation_lock_sha256": hashlib.sha256(annotations_path.read_bytes()).hexdigest(),
        "cold_start_ns": 1,
        "cpu_ns": 0,
        "item_wall_ns": timings,
        "measurement_wall_ns": 1,
        "openvino_version": "2026.3.1-22476-759c5a6ab8c-releases/2026/3",
        "peak_rss_bytes": 1,
        "predictions": predictions,
        "process_wall_ns": 1,
        "processed_item_count": len(items),
        "python_version": "3.13.15",
        "seed": 1729,
        "split": "test",
        "warm_start_ns": 1,
    }
    first_id = cast(str, items[0]["item_id"])
    first_prediction = predictions[first_id][0]
    first_prediction["box_milli_pixels"] = [0, 0, 640_001, 1000]
    with pytest.raises(benchmark.BenchmarkError, match="invalid_worker_result"):
        benchmark._validate_worker_result(
            result,
            items,
            split="test",
            seed=1729,
            annotation_sha256=hashlib.sha256(annotations_path.read_bytes()).hexdigest(),
        )


def test_published_machine_results_reproduce_frozen_gate_outputs() -> None:
    raw_path = RESULT_ROOT / "raw-results.json"
    raw_bytes = raw_path.read_bytes()
    raw = cast(dict[str, object], json.loads(raw_bytes))
    assert hashlib.sha256(raw_bytes).hexdigest() == (
        "df3cdbf68e5bd40c861b2f374d65882a749f273bbb2848e36a283d3373cc4a2e"
    )
    assert b"/root" not in raw_bytes
    provenance = cast(dict[str, object], raw["provenance"])
    assert provenance["source_revision"] == "225ddf9d32a7acad9c6b8ade8a965cd1dace0bbd"
    assert (
        provenance["evaluation_harness_sha256"]
        == hashlib.sha256(
            (ROOT / "scripts" / "run_v02_detection_benchmark.py").read_bytes()
        ).hexdigest()
    )

    policy, policy_sha256 = evaluator.load_policy(
        ROOT / "fixtures" / "v02-evaluation" / "policy.json"
    )
    dataset, dataset_sha256 = evaluator.load_manifest(
        FIXTURE_ROOT / "dataset-manifest.json", policy
    )
    outputs = cast(dict[str, dict[str, object]], raw["outputs"])
    expected_status = {
        "vehicle-detection-0200": "fail",
        "vehicle-detection-0201": "pass",
        "vehicle-detection-0202": "pass",
    }
    for short_name, status in expected_status.items():
        candidate = f"{short_name}-fp32-openvino-2026.3.1"
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


def test_rgb24_primitives_are_deterministic_and_exact() -> None:
    pixels = bytes((1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12))
    assert preparation.resize_rgb24(pixels, 2, 2, 2, 2) == pixels
    mutable = bytearray(pixels)
    preparation.fill_box(mutable, 2, 2, (1, 0, 2, 1), (20, 21, 22))
    assert mutable == bytes((1, 2, 3, 20, 21, 22, 7, 8, 9, 10, 11, 12))
    assert preparation.crop_rgb24(mutable, 2, 2, (1, 0, 2, 2)) == (
        1,
        2,
        bytes((20, 21, 22, 10, 11, 12)),
    )
    first = preparation.compose_item(
        "fixture",
        "small_distant",
        2,
        2,
        pixels,
        64,
        36,
        {
            "easy_max_height": 24,
            "easy_max_width": 46,
            "small_distant_max_height": 6,
            "small_distant_max_width": 9,
        },
    )
    assert first == preparation.compose_item(
        "fixture",
        "small_distant",
        2,
        2,
        pixels,
        64,
        36,
        {
            "easy_max_height": 24,
            "easy_max_width": 46,
            "small_distant_max_height": 6,
            "small_distant_max_width": 9,
        },
    )
    parsed = preparation.parse_ppm(first[0])
    assert parsed[:2] == (64, 36)


def _metric_fixture() -> tuple[list[dict[str, object]], dict[str, object]]:
    items: list[dict[str, object]] = [
        {"item_id": "easy", "object_box": [0, 0, 10, 10], "stratum": "easy"},
        {
            "item_id": "small",
            "object_box": [0, 0, 10, 10],
            "stratum": "small_distant",
        },
    ]
    repetition: dict[str, object] = {
        "predictions": {
            "easy": [
                {
                    "box_milli_pixels": [20_000, 20_000, 30_000, 30_000],
                    "confidence_millionths": 900_000,
                },
                {
                    "box_milli_pixels": [0, 0, 10_000, 10_000],
                    "confidence_millionths": 800_000,
                },
            ],
            "small": [
                {
                    "box_milli_pixels": [0, 0, 10_000, 10_000],
                    "confidence_millionths": 700_000,
                },
                {
                    "box_milli_pixels": [20_000, 20_000, 30_000, 30_000],
                    "confidence_millionths": 200_000,
                },
            ],
        }
    }
    return items, repetition


def test_detection_metrics_count_false_positives_and_ranked_ap() -> None:
    items, repetition = _metric_fixture()
    metrics = benchmark.accuracy_metrics(repetition, items, 500_000)
    assert metrics == {
        "map50_basis_points": {"easy": 5000, "overall": 6666, "small_distant": 10000},
        "precision_basis_points": {"easy": 5000, "overall": 6666, "small_distant": 10000},
        "recall_basis_points": {"easy": 10000, "overall": 10000, "small_distant": 10000},
    }
    assert benchmark.iou_basis_points((0, 0, 10, 10), (5, 0, 15, 10)) == 3333


def test_threshold_selection_and_receipt_aggregation_are_adverse() -> None:
    items, repetition = _metric_fixture()
    calibration = cast(dict[str, object], json.loads(json.dumps(repetition)))
    calibration_predictions = cast(dict[str, list[dict[str, object]]], calibration["predictions"])
    calibration_predictions["easy"][0]["confidence_millionths"] = 100_000
    selected, summaries = benchmark.choose_threshold(
        [calibration] * 5,
        items,
        [200_000, 700_000],
    )
    assert selected == 700_000
    assert [summary["passes_calibration_floors"] for summary in summaries] == [False, True]

    repetitions: list[dict[str, object]] = [
        {
            "metrics": {
                "peak_rss_bytes": {"overall": value},
                "warm_start_wall_ms": {"overall": index},
            }
        }
        for index, value in enumerate((10, 12, 11, 15, 13), start=1)
    ]
    assert benchmark._aggregate(repetitions) == {
        "dispersion": {
            "peak_rss_bytes": {"overall": 1},
            "warm_start_wall_ms": {"overall": 1},
        },
        "metrics": {
            "peak_rss_bytes": {"overall": 15},
            "warm_start_wall_ms": {"overall": 3},
        },
    }
