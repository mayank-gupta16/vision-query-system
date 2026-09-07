# SPDX-License-Identifier: Apache-2.0
"""Release-gate tests for the v0.1 CPU-LITE benchmark and comparator."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import cast

import compare_v01_benchmarks as comparator
import pytest
import run_v01_benchmark as benchmark

_REVISION = "0" * 40
_BASELINE_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "benchmarks" / "v0.1-cpu-lite-baseline.json"
)


@pytest.fixture(scope="module")
def benchmark_receipt(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, object]:
    root = tmp_path_factory.mktemp("v01-benchmark")
    output = root / "receipt.json"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = "src"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_v01_benchmark.py",
            "--work-root",
            os.fspath(root),
            "--output",
            os.fspath(output),
            "--revision",
            _REVISION,
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert completed.stderr == ""
    receipt = cast(dict[str, object], json.loads(output.read_text(encoding="utf-8")))
    profile = cast(dict[str, object], receipt["profile"])
    expected_status = "pass" if profile["meets_requirements"] else "fail"
    assert completed.returncode == (0 if expected_status == "pass" else 1)
    assert json.loads(completed.stdout) == {"status": expected_status}

    rendered = output.read_text(encoding="utf-8") + completed.stdout
    assert os.fspath(root) not in rendered
    assert "\x1b" not in rendered
    assert repr(bytes(range(256))) not in rendered
    assert re.search(r"(?:src|frm|evi|run)_[0-9a-f]{64}", rendered) is None
    return receipt


def test_fixture_manifest_is_exact_rights_safe_and_privacy_safe() -> None:
    manifest = benchmark.load_manifest()
    rights = cast(dict[str, object], manifest["rights"])
    privacy = cast(dict[str, object], manifest["privacy"])
    workload = cast(dict[str, object], manifest["workload"])

    assert rights["license_expression"] == "Apache-2.0"
    assert rights["external_sources"] == []
    assert rights["redistribution_allowed"] is True
    assert rights["derivatives_allowed"] is True
    assert privacy["classification"] == "public-synthetic-no-personal-data"
    assert privacy["consent_status"] == "not-applicable-no-personal-data"
    assert privacy["temporary_outputs_retained"] is False
    assert not any(
        cast(bool, privacy[name])
        for name in (
            "contains_faces",
            "contains_imported_assets",
            "contains_personal_data",
            "contains_plates",
            "contains_real_people",
        )
    )
    assert workload["source_seconds"] == 60
    assert workload["source_dimensions"] == [1920, 1080]
    assert workload["candidate_frame_count"] == 1800
    assert workload["sample_count"] == 300
    assert workload["materialized_encoded_media"] is False
    assert workload["perception_dataset_used"] is False


def test_manifest_validation_rejects_workload_rights_or_policy_drift() -> None:
    manifest = benchmark.load_manifest()
    changed_workload = copy.deepcopy(manifest)
    cast(dict[str, object], changed_workload["workload"])["source_seconds"] = 59
    with pytest.raises(ValueError, match=r"invalid v0\.1 benchmark manifest"):
        benchmark._validate_manifest(changed_workload)

    changed_rights = copy.deepcopy(manifest)
    cast(dict[str, object], changed_rights["rights"])["external_sources"] = ["unknown"]
    with pytest.raises(ValueError, match=r"invalid v0\.1 benchmark manifest"):
        benchmark._validate_manifest(changed_rights)

    changed_policy = copy.deepcopy(manifest)
    cast(dict[str, object], changed_policy["comparator"])["max_regression_basis_points"] = 2001
    with pytest.raises(ValueError, match=r"invalid v0\.1 benchmark manifest"):
        benchmark._validate_manifest(changed_policy)


def test_benchmark_receipt_covers_the_pinned_application_path(
    benchmark_receipt: dict[str, object],
) -> None:
    receipt = benchmark_receipt
    workload = cast(dict[str, object], receipt["workload"])
    configuration = cast(dict[str, object], receipt["configuration"])
    checks = cast(dict[str, object], receipt["checks"])
    resources = cast(dict[str, object], receipt["resources"])
    metrics = cast(dict[str, object], receipt["metrics"])
    measurements = cast(dict[str, object], receipt["measurements"])
    provenance = cast(dict[str, object], receipt["provenance"])

    assert receipt["schema"] == "visualworld.v01-benchmark-receipt"
    assert receipt["schema_version"] == 1
    assert workload == benchmark.load_manifest()["workload"]
    assert configuration == benchmark.load_manifest()["configuration"]
    assert all(checks.values())
    assert resources["timing_measured"] is True
    assert resources["rss_measured"] is True
    assert resources["rss_bounded"] is True
    assert resources["disk_measured"] is True
    assert resources["index_measured"] is True
    assert 0 < cast(int, resources["process_peak_rss_bytes"]) <= 2 * 1024**3
    assert cast(int, resources["combined_store_logical_bytes"]) > 0
    assert cast(int, resources["metadata_index_logical_bytes"]) > 0
    assert cast(int, resources["artifact_store_logical_bytes"]) == 2_764_800
    assert all(type(value) is int and value > 0 for value in metrics.values())
    assert cast(dict[str, object], measurements["sampling"])["page_count"] == 29
    assert cast(dict[str, object], measurements["metadata_transactions"])["transaction_count"] == 10
    assert all(
        cast(int, cast(dict[str, object], value)["wall_ns"]) > 0
        and cast(int, cast(dict[str, object], value)["cpu_ns"]) > 0
        for value in measurements.values()
    )
    assert provenance["source_revision"] == _REVISION
    assert provenance["generated_input"] is True
    assert provenance["perception_accuracy_claimed"] is False


def _passing_receipt(receipt: dict[str, object]) -> dict[str, object]:
    selected = copy.deepcopy(receipt)
    selected["status"] = "pass"
    cast(dict[str, object], selected["profile"])["meets_requirements"] = True
    resources = cast(dict[str, object], selected["resources"])
    resources["rss_bounded"] = True
    resources["process_peak_rss_bytes"] = min(
        cast(int, resources["process_peak_rss_bytes"]),
        benchmark._PEAK_RSS_LIMIT_BYTES,
    )
    return selected


def test_comparator_accepts_identical_compatible_receipts(
    benchmark_receipt: dict[str, object],
) -> None:
    baseline = _passing_receipt(benchmark_receipt)
    result = comparator.compare(baseline, copy.deepcopy(baseline))
    comparisons = cast(dict[str, dict[str, object]], result["comparisons"])

    assert result["status"] == "pass"
    assert all(cast(dict[str, object], result["checks"]).values())
    assert set(comparisons) == set(comparator._METRIC_PATHS)
    assert all(item["regression_basis_points"] == 0 for item in comparisons.values())
    assert all(item["within_budget"] is True for item in comparisons.values())


def test_checked_in_baseline_is_valid_and_self_comparable() -> None:
    baseline = comparator._load(_BASELINE_PATH)
    result = comparator.compare(baseline, copy.deepcopy(baseline))

    assert baseline["status"] == "pass"
    assert result["status"] == "pass"


def test_comparator_rejects_regression_incompatibility_and_invalid_metrics(
    benchmark_receipt: dict[str, object],
) -> None:
    baseline = _passing_receipt(benchmark_receipt)
    slower = copy.deepcopy(baseline)
    slower_metrics = cast(dict[str, object], slower["metrics"])
    baseline_wall = cast(int, cast(dict[str, object], baseline["metrics"])["total_wall_ns"])
    slower_metrics["total_wall_ns"] = baseline_wall * 121 // 100 + 1
    regression = comparator.compare(baseline, slower)
    regression_checks = cast(dict[str, object], regression["checks"])
    wall_comparison = cast(dict[str, object], regression["comparisons"])["total_wall_ns"]

    assert regression["status"] == "fail"
    assert regression_checks["compatible"] is True
    assert regression_checks["metrics_within_budget"] is False
    assert cast(int, cast(dict[str, object], wall_comparison)["regression_basis_points"]) > 2000
    assert cast(dict[str, object], wall_comparison)["within_budget"] is False

    incompatible = copy.deepcopy(baseline)
    cast(dict[str, object], incompatible["workload"])["source_seconds"] = 61
    incompatible_result = comparator.compare(baseline, incompatible)
    assert incompatible_result["status"] == "fail"
    assert cast(dict[str, object], incompatible_result["checks"])["compatible"] is False
    assert incompatible_result["comparisons"] == {}

    changed_harness = copy.deepcopy(baseline)
    cast(dict[str, object], changed_harness["implementation"])["benchmark_harness_sha256"] = (
        "f" * 64
    )
    changed_harness_result = comparator.compare(baseline, changed_harness)
    assert changed_harness_result["status"] == "fail"
    assert cast(dict[str, object], changed_harness_result["checks"])["compatible"] is False

    invalid = copy.deepcopy(baseline)
    cast(dict[str, object], invalid["metrics"])["total_wall_ns"] = 0
    with pytest.raises(comparator.ComparisonError, match="invalid_receipt"):
        comparator.compare(baseline, invalid)

    inconsistent = copy.deepcopy(baseline)
    cast(dict[str, object], inconsistent["checks"])["golden_matches"] = False
    with pytest.raises(comparator.ComparisonError, match="invalid_receipt"):
        comparator.compare(baseline, inconsistent)


def test_comparator_loader_rejects_symlinked_receipt(
    tmp_path: Path,
    benchmark_receipt: dict[str, object],
) -> None:
    target = tmp_path / "receipt.json"
    target.write_text(json.dumps(benchmark_receipt), encoding="utf-8")
    link = tmp_path / "receipt-link.json"
    link.symlink_to(target)

    with pytest.raises(comparator.ComparisonError, match="invalid_receipt"):
        comparator._load(link)
