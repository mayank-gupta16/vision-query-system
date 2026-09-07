# SPDX-License-Identifier: Apache-2.0
"""Contract and hostile-input tests for the frozen v0.2 evaluation gates."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import cast

import evaluate_v02_gates as evaluator
import pytest

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "fixtures" / "v02-evaluation" / "policy.json"
MANIFEST_PATH = ROOT / "fixtures" / "v02-evaluation" / "detection-manifest.json"
RECEIPT_PATH = ROOT / "fixtures" / "v02-evaluation" / "detection-receipt.json"


def _json(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


@pytest.fixture
def context() -> tuple[dict[str, object], str, dict[str, object], str]:
    policy, policy_sha256 = evaluator.load_policy(POLICY_PATH)
    manifest, manifest_sha256 = evaluator.load_manifest(MANIFEST_PATH, policy)
    return policy, policy_sha256, manifest, manifest_sha256


def _validated(
    receipt: dict[str, object],
    context: tuple[dict[str, object], str, dict[str, object], str],
    *,
    code: str = "invalid_receipt",
) -> dict[str, object]:
    policy, policy_sha256, manifest, manifest_sha256 = context
    return evaluator.validate_receipt(
        receipt,
        policy,
        policy_sha256,
        manifest,
        manifest_sha256,
        code=code,
    )


def _set_metric(
    receipt: dict[str, object],
    name: str,
    stratum: str,
    value: int | str,
    *,
    dispersion: int | str = 0,
) -> None:
    repetitions = cast(list[dict[str, object]], receipt["repetitions"])
    for repetition in repetitions:
        metrics = cast(dict[str, dict[str, int | str]], repetition["metrics"])
        metrics[name][stratum] = value
    aggregate = cast(dict[str, dict[str, dict[str, int | str]]], receipt["aggregate"])
    aggregate["metrics"][name][stratum] = value
    aggregate["dispersion"][name][stratum] = dispersion


def _passing_receipt(
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> dict[str, object]:
    return _validated(_json(RECEIPT_PATH), context)


def _receipt_sha256(receipt: dict[str, object]) -> str:
    serialized = json.dumps(receipt, allow_nan=False, indent=2, sort_keys=True).encode("ascii")
    return hashlib.sha256(serialized + b"\n").hexdigest()


def _evaluate(
    policy: dict[str, object],
    receipt: dict[str, object],
    *,
    baseline: dict[str, object] | None,
    as_of: date,
) -> dict[str, object]:
    return evaluator.evaluate(
        policy,
        receipt,
        baseline=baseline,
        baseline_receipt_sha256=None if baseline is None else _receipt_sha256(baseline),
        receipt_sha256=_receipt_sha256(receipt),
        as_of=as_of,
    )


def test_checked_in_policy_freezes_protocol_thresholds_and_diagnostics(
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    policy, policy_sha256, _, _ = context
    protocol = cast(dict[str, object], policy["protocol"])
    experiments = cast(dict[str, dict[str, object]], policy["experiments"])

    assert policy_sha256 == "e745f520485e0de3f2ad9c312b3f09d6ffda361e134e717187f3d3839100d6eb"
    assert protocol == {
        "aggregate": "metric_defined",
        "dispersion": "median_absolute_deviation",
        "gate_split": "test",
        "measured_repetitions": 5,
        "report_each_repetition": True,
        "seed_set": [1729, 3253, 5081, 7919, 104729],
        "selection_split": "calibration",
        "warmup_runs": 1,
    }
    assert {name: experiment["issue"] for name, experiment in experiments.items()} == {
        "crop_value": 23,
        "detection": 21,
        "sampling": 22,
        "tracking": 24,
    }
    detection = cast(dict[str, dict[str, object]], experiments["detection"]["metrics"])
    assert detection["recall_basis_points"]["minimum"] == {
        "easy": 8500,
        "overall": 7000,
        "small_distant": 5000,
    }
    assert detection["frames_per_second_milli"]["minimum"] == {"overall": 5000}
    assert detection["peak_rss_bytes"]["maximum"] == {"overall": 2 * 1024**3}
    assert detection["peak_rss_bytes"]["aggregation"] == "maximum"
    crop = cast(dict[str, dict[str, object]], experiments["crop_value"]["metrics"])
    assert crop["specialist_accuracy_gain_basis_points"]["kind"] == "diagnostic"
    assert crop["specialist_accuracy_gain_basis_points"]["unknown"] == "report"


def test_manifest_is_split_locked_rights_safe_and_privacy_safe(
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    _, _, manifest, manifest_sha256 = context
    rights = cast(dict[str, object], manifest["rights"])
    privacy = cast(dict[str, object], manifest["privacy"])
    splits = cast(dict[str, dict[str, object]], manifest["splits"])

    assert manifest_sha256 == "0eb04c84521fba7439189683a433f68525d22c7cc7bcf7cccc43265fbdf89b60"
    assert manifest["strata"] == ["overall", "easy", "small_distant"]
    assert rights["license_expression"] == "Apache-2.0"
    assert rights["commercial_use_allowed"] is True
    assert {"benchmarking", "evaluation"} <= set(cast(list[str], rights["allowed_uses"]))
    assert privacy["classification"] == "public-synthetic-no-personal-data"
    assert not any(
        cast(bool, privacy[name])
        for name in (
            "contains_faces",
            "contains_personal_data",
            "contains_plates",
            "contains_real_people",
            "sensitive_derived_outputs_retained",
        )
    )
    assert splits["calibration"]["item_ids_sha256"] != splits["test"]["item_ids_sha256"]


def test_checked_in_selection_receipt_passes_exact_gates(
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    policy, _, _, _ = context
    receipt = _passing_receipt(context)
    result = _evaluate(policy, receipt, baseline=None, as_of=date(2026, 9, 7))

    assert result["status"] == "pass"
    assert result["failures"] == []
    assert result["regression"] == {}
    assert result["waiver"] is None
    assert result["candidate"] == {
        "configuration_sha256": "7" * 64,
        "name": "deterministic detection contract fixture",
    }
    assert result["dataset_manifest_sha256"] == context[3]
    assert result["baseline_candidate"] is None
    assert result["baseline_receipt_sha256"] is None
    assert result["receipt_sha256"] == hashlib.sha256(RECEIPT_PATH.read_bytes()).hexdigest()
    assert cast(dict[str, object], result["checks"]) == {
        "absolute_gates_pass": True,
        "baseline_compatible": None,
        "regression_gates_pass": True,
        "waiver_valid": False,
    }


def test_result_digest_distinguishes_receipts_with_the_same_decisions(
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    first = _passing_receipt(context)
    changed = _json(RECEIPT_PATH)
    first_repetition = cast(list[dict[str, object]], changed["repetitions"])[0]
    first_metrics = cast(dict[str, dict[str, int]], first_repetition["metrics"])
    first_metrics["frames_per_second_milli"]["overall"] = 6001
    second = _validated(changed, context)

    first_result = _evaluate(context[0], first, baseline=None, as_of=date(2026, 9, 7))
    second_result = _evaluate(context[0], second, baseline=None, as_of=date(2026, 9, 7))
    assert first_result["receipt_sha256"] != second_result["receipt_sha256"]
    first_result.pop("receipt_sha256")
    second_result.pop("receipt_sha256")
    assert first_result == second_result


def test_absolute_boundaries_and_unknown_fail_closed(
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    policy, _, _, _ = context
    boundary = _json(RECEIPT_PATH)
    _set_metric(boundary, "frames_per_second_milli", "overall", 5000)
    validated_boundary = _validated(boundary, context)
    assert (
        _evaluate(policy, validated_boundary, baseline=None, as_of=date(2026, 9, 7))["status"]
        == "pass"
    )

    below = _json(RECEIPT_PATH)
    _set_metric(below, "frames_per_second_milli", "overall", 4999)
    below_result = _evaluate(
        policy,
        _validated(below, context),
        baseline=None,
        as_of=date(2026, 9, 7),
    )
    assert below_result["status"] == "fail"
    assert below_result["failures"] == ["absolute:frames_per_second_milli:overall"]

    unknown = _json(RECEIPT_PATH)
    _set_metric(unknown, "recall_basis_points", "small_distant", "UNKNOWN", dispersion="UNKNOWN")
    unknown_result = _evaluate(
        policy,
        _validated(unknown, context),
        baseline=None,
        as_of=date(2026, 9, 7),
    )
    assert unknown_result["status"] == "fail"
    assert unknown_result["failures"] == ["absolute:recall_basis_points:small_distant"]


def test_repetition_aggregate_and_run_failure_are_recomputed(
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    tampered = _json(RECEIPT_PATH)
    aggregate = cast(dict[str, dict[str, dict[str, int]]], tampered["aggregate"])
    aggregate["metrics"]["precision_basis_points"]["overall"] = 7600
    with pytest.raises(evaluator.EvaluationError, match="invalid_receipt"):
        _validated(tampered, context)

    failed = _json(RECEIPT_PATH)
    repetitions = cast(list[dict[str, object]], failed["repetitions"])
    repetitions[0]["status"] = "failed"
    repetitions[0]["failure_code"] = "runtime_error"
    repetitions[0]["failed_item_count"] = 5
    repetitions[0]["processed_item_count"] = 0
    failure_metrics = cast(dict[str, dict[str, int]], repetitions[0]["metrics"])
    failure_metrics["failure_rate_basis_points"]["overall"] = 10_000
    failed_aggregate = cast(dict[str, dict[str, dict[str, int]]], failed["aggregate"])
    failed_aggregate["metrics"]["failure_rate_basis_points"]["overall"] = 2000
    failed_aggregate["dispersion"]["failure_rate_basis_points"]["overall"] = 0
    policy = context[0]
    result = _evaluate(
        policy,
        _validated(failed, context),
        baseline=None,
        as_of=date(2026, 9, 7),
    )
    assert result["status"] == "fail"
    assert result["failures"] == ["absolute:failure_rate_basis_points:overall"]

    inconsistent = _json(RECEIPT_PATH)
    inconsistent_repetition = cast(list[dict[str, object]], inconsistent["repetitions"])[0]
    inconsistent_repetition["status"] = "failed"
    inconsistent_repetition["failure_code"] = "runtime_error"
    inconsistent_repetition["failed_item_count"] = 5
    inconsistent_repetition["processed_item_count"] = 0
    with pytest.raises(evaluator.EvaluationError, match="invalid_receipt"):
        _validated(inconsistent, context)


def test_peak_rss_uses_worst_repetition_instead_of_median(
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    raw = _json(RECEIPT_PATH)
    peak = 2 * 1024**3 + 1
    first = cast(list[dict[str, object]], raw["repetitions"])[0]
    cast(
        dict[str, object],
        cast(dict[str, object], first["metrics"])["peak_rss_bytes"],
    )["overall"] = peak
    aggregate = cast(dict[str, dict[str, dict[str, int]]], raw["aggregate"])
    aggregate["metrics"]["peak_rss_bytes"]["overall"] = peak
    aggregate["dispersion"]["peak_rss_bytes"]["overall"] = 0
    result = _evaluate(
        context[0],
        _validated(raw, context),
        baseline=None,
        as_of=date(2026, 9, 7),
    )
    assert result["status"] == "fail"
    assert result["failures"] == ["absolute:peak_rss_bytes:overall"]


def test_regression_comparison_uses_adverse_rounding_and_compatibility(
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    policy = context[0]
    baseline = _passing_receipt(context)
    candidate = _json(RECEIPT_PATH)
    candidate["evaluation_phase"] = "regression"
    _set_metric(candidate, "frames_per_second_milli", "overall", 5399)
    candidate_validated = _validated(candidate, context)
    result = _evaluate(
        policy,
        candidate_validated,
        baseline=baseline,
        as_of=date(2026, 9, 7),
    )
    assert result["status"] == "fail"
    assert result["failures"] == ["regression:frames_per_second_milli:overall"]
    comparison = cast(dict[str, dict[str, dict[str, object]]], result["regression"])
    assert comparison["frames_per_second_milli"]["overall"]["regression_basis_points"] == 1002

    assert evaluator._regression_basis_points(100, 105, "lower_is_better") == 500
    assert evaluator._regression_basis_points(100, 106, "lower_is_better") == 600
    assert evaluator._regression_basis_points(100, 95, "higher_is_better") == 500
    assert evaluator._regression_basis_points(100, 94, "higher_is_better") == 600

    incompatible = copy.deepcopy(candidate_validated)
    cast(dict[str, object], incompatible["profile"])["cpu_model"] = "different CPU"
    with pytest.raises(evaluator.EvaluationError, match="incompatible_baseline"):
        _evaluate(
            policy,
            incompatible,
            baseline=baseline,
            as_of=date(2026, 9, 7),
        )

    incompatible_runtime = copy.deepcopy(candidate_validated)
    cast(dict[str, object], incompatible_runtime["implementation"])["python_version"] = "3.14.7"
    with pytest.raises(evaluator.EvaluationError, match="incompatible_baseline"):
        _evaluate(
            policy,
            incompatible_runtime,
            baseline=baseline,
            as_of=date(2026, 9, 7),
        )


def test_selection_and_regression_require_the_correct_baseline_mode(
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    policy = context[0]
    selection = _passing_receipt(context)
    with pytest.raises(evaluator.EvaluationError, match="unexpected_baseline"):
        _evaluate(
            policy,
            selection,
            baseline=selection,
            as_of=date(2026, 9, 7),
        )

    regression = copy.deepcopy(selection)
    regression["evaluation_phase"] = "regression"
    with pytest.raises(evaluator.EvaluationError, match="baseline_required"):
        _evaluate(
            policy,
            regression,
            baseline=None,
            as_of=date(2026, 9, 7),
        )

    with pytest.raises(evaluator.EvaluationError, match="invalid_baseline"):
        _evaluate(
            policy,
            regression,
            baseline=regression,
            as_of=date(2026, 9, 7),
        )


def test_waiver_requires_exact_scope_owner_controls_follow_up_and_freshness(
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    policy = context[0]
    raw = _json(RECEIPT_PATH)
    _set_metric(raw, "frames_per_second_milli", "overall", 4000)
    raw["waiver"] = {
        "compensating_controls": ["Do not select this candidate for the shipped adapter."],
        "expires_on": "2026-10-07",
        "follow_up_issue": "https://github.com/mayank-gupta16/vision-query-system/issues/21",
        "issued_on": "2026-09-07",
        "owner": "mayank-gupta16",
        "review_trigger": "Re-evaluate before any adapter selection or release.",
        "risk": "The candidate cannot sustain the required sampled-frame rate.",
        "scope": ["absolute:frames_per_second_milli:overall"],
    }
    validated = _validated(raw, context)
    waived = _evaluate(
        policy,
        validated,
        baseline=None,
        as_of=date(2026, 9, 8),
    )
    assert waived["status"] == "waived"
    assert cast(dict[str, object], waived["checks"])["waiver_valid"] is True

    for key, value, as_of in (
        ("owner", "someone-else", date(2026, 9, 8)),
        ("scope", ["absolute:recall_basis_points:overall"], date(2026, 9, 8)),
        ("follow_up_issue", "https://example.invalid/21", date(2026, 9, 8)),
        ("expires_on", "2026-10-07", date(2026, 10, 8)),
    ):
        changed = copy.deepcopy(validated)
        cast(dict[str, object], changed["waiver"])[key] = value
        with pytest.raises(evaluator.EvaluationError, match="invalid_waiver"):
            _evaluate(policy, changed, baseline=None, as_of=as_of)


def test_receipt_and_baseline_dates_are_ordered_from_policy_freeze(
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    policy = context[0]
    future = _passing_receipt(context)
    future["evaluated_on"] = "2026-09-08"
    with pytest.raises(evaluator.EvaluationError, match="invalid_receipt"):
        _evaluate(policy, future, baseline=None, as_of=date(2026, 9, 7))

    candidate = _passing_receipt(context)
    candidate["evaluation_phase"] = "regression"
    later_baseline = _passing_receipt(context)
    later_baseline["evaluated_on"] = "2026-09-08"
    with pytest.raises(evaluator.EvaluationError, match="invalid_baseline"):
        _evaluate(
            policy,
            candidate,
            baseline=later_baseline,
            as_of=date(2026, 9, 8),
        )


def test_loaded_policy_digest_prevents_same_version_substitution(tmp_path: Path) -> None:
    changed = _json(POLICY_PATH)
    detection = cast(
        dict[str, dict[str, object]],
        cast(dict[str, dict[str, object]], changed["experiments"])["detection"]["metrics"],
    )
    cast(dict[str, int], detection["frames_per_second_milli"]["minimum"])["overall"] = 1
    path = tmp_path / "substituted-policy.json"
    path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(evaluator.EvaluationError, match="invalid_policy"):
        evaluator.load_policy(path)


@pytest.mark.parametrize("schema_version", [True, 1.0])
@pytest.mark.parametrize("family", ["policy", "manifest", "receipt"])
def test_schema_versions_reject_boolean_and_float_values(
    family: str,
    schema_version: object,
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    paths = {"manifest": MANIFEST_PATH, "policy": POLICY_PATH, "receipt": RECEIPT_PATH}
    value = _json(paths[family])
    value["schema_version"] = schema_version

    with pytest.raises(evaluator.EvaluationError, match=f"invalid_{family}"):
        if family == "policy":
            evaluator.validate_policy(value)
        elif family == "manifest":
            evaluator.validate_manifest(value, context[0])
        else:
            _validated(value, context)


@pytest.mark.parametrize(
    ("family", "mutate"),
    [
        ("policy", lambda value: value.pop("protocol")),
        (
            "policy",
            lambda value: cast(dict[str, object], value["profile"]).__setitem__("vcpu", True),
        ),
        (
            "policy",
            lambda value: cast(dict[str, object], value["profile"]).__setitem__(
                "maximum_peak_rss_bytes", float(2 * 1024**3)
            ),
        ),
        (
            "manifest",
            lambda value: cast(dict[str, object], value["privacy"]).__setitem__(
                "contains_personal_data", True
            ),
        ),
        (
            "manifest",
            lambda value: cast(dict[str, object], value["rights"]).__setitem__(
                "license_expression", "CC-BY-NC-4.0"
            ),
        ),
        (
            "manifest",
            lambda value: cast(dict[str, object], value["rights"]).__setitem__(
                "redistribution_allowed", False
            ),
        ),
        ("receipt", lambda value: value.__setitem__("policy_sha256", "0" * 64)),
        (
            "receipt",
            lambda value: cast(dict[str, object], value["profile"]).__setitem__(
                "memory_bytes", True
            ),
        ),
        (
            "receipt",
            lambda value: cast(
                list[dict[str, object]], cast(dict[str, object], value["candidate"])["artifacts"]
            )[0].__setitem__("trust_remote_code", True),
        ),
    ],
)
def test_malformed_policy_manifest_and_receipt_families_are_rejected(
    family: str,
    mutate: Callable[[dict[str, object]], object],
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    if family == "policy":
        value = _json(POLICY_PATH)
        mutate(value)
        with pytest.raises(evaluator.EvaluationError, match="invalid_policy"):
            evaluator.validate_policy(value)
    elif family == "manifest":
        value = _json(MANIFEST_PATH)
        mutate(value)
        with pytest.raises(evaluator.EvaluationError, match="invalid_manifest"):
            evaluator.validate_manifest(value, context[0])
    else:
        value = _json(RECEIPT_PATH)
        mutate(value)
        with pytest.raises(evaluator.EvaluationError, match="invalid_receipt"):
            _validated(value, context)


def test_unknown_fields_missing_metrics_strata_and_impossible_totals_are_rejected(
    context: tuple[dict[str, object], str, dict[str, object], str],
) -> None:
    policy_extra = _json(POLICY_PATH)
    policy_extra["unreviewed"] = True
    with pytest.raises(evaluator.EvaluationError, match="invalid_policy"):
        evaluator.validate_policy(policy_extra)

    manifest_total = _json(MANIFEST_PATH)
    test_split = cast(dict[str, object], cast(dict[str, object], manifest_total["splits"])["test"])
    cast(dict[str, object], test_split["stratum_item_counts"])["easy"] = 6
    with pytest.raises(evaluator.EvaluationError, match="invalid_manifest"):
        evaluator.validate_manifest(manifest_total, context[0])

    manifest_duplicate_annotations = _json(MANIFEST_PATH)
    duplicate_splits = cast(dict[str, dict[str, object]], manifest_duplicate_annotations["splits"])
    duplicate_splits["test"]["annotation_sha256"] = duplicate_splits["calibration"][
        "annotation_sha256"
    ]
    with pytest.raises(evaluator.EvaluationError, match="invalid_manifest"):
        evaluator.validate_manifest(manifest_duplicate_annotations, context[0])

    malformed: list[dict[str, object]] = []
    extra = _json(RECEIPT_PATH)
    extra["unreviewed"] = True
    malformed.append(extra)

    wrong_split = _json(RECEIPT_PATH)
    wrong_split["split"] = "calibration"
    malformed.append(wrong_split)

    impossible_total = _json(RECEIPT_PATH)
    cast(list[dict[str, object]], impossible_total["repetitions"])[0]["processed_item_count"] = 6
    malformed.append(impossible_total)

    omitted_total = _json(RECEIPT_PATH)
    omitted_repetition = cast(list[dict[str, object]], omitted_total["repetitions"])[0]
    omitted_repetition["status"] = "failed"
    omitted_repetition["failure_code"] = "runtime_error"
    omitted_repetition["failed_item_count"] = 1
    omitted_repetition["processed_item_count"] = 3
    malformed.append(omitted_total)

    missing_metric = _json(RECEIPT_PATH)
    first_metrics = cast(
        dict[str, object],
        cast(list[dict[str, object]], missing_metric["repetitions"])[0]["metrics"],
    )
    first_metrics.pop("recall_basis_points")
    malformed.append(missing_metric)

    missing_stratum = _json(RECEIPT_PATH)
    first_repetition = cast(list[dict[str, object]], missing_stratum["repetitions"])[0]
    recall = cast(
        dict[str, object],
        cast(dict[str, object], first_repetition["metrics"])["recall_basis_points"],
    )
    recall.pop("small_distant")
    malformed.append(missing_stratum)

    boolean_metric = _json(RECEIPT_PATH)
    first_boolean = cast(list[dict[str, object]], boolean_metric["repetitions"])[0]
    cast(
        dict[str, object],
        cast(dict[str, object], first_boolean["metrics"])["frames_per_second_milli"],
    )["overall"] = True
    malformed.append(boolean_metric)

    for receipt in malformed:
        with pytest.raises(evaluator.EvaluationError, match="invalid_receipt"):
            _validated(receipt, context)


def test_hardened_loader_rejects_duplicate_nonfinite_link_fifo_and_oversize(
    tmp_path: Path,
) -> None:
    cases = {
        "duplicate.json": b'{"schema":1,"schema":2}',
        "nonfinite.json": b'{"value":NaN}',
        "oversize.json": b" " * (evaluator._MAX_JSON_BYTES + 1),
    }
    for name, content in cases.items():
        path = tmp_path / name
        path.write_bytes(content)
        with pytest.raises(evaluator.EvaluationError, match="invalid_policy"):
            evaluator._read_json(path, "invalid_policy")

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(evaluator.EvaluationError, match="invalid_policy"):
        evaluator._read_json(link, "invalid_policy")

    fifo = tmp_path / "receipt.fifo"
    os.mkfifo(fifo)
    with pytest.raises(evaluator.EvaluationError, match="invalid_receipt"):
        evaluator._read_json(fifo, "invalid_receipt")


def test_cli_emits_only_status_and_refuses_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    command = [
        sys.executable,
        os.fspath(ROOT / "scripts" / "evaluate_v02_gates.py"),
        "--manifest",
        os.fspath(MANIFEST_PATH),
        "--receipt",
        os.fspath(RECEIPT_PATH),
        "--as-of",
        "2026-09-07",
        "--output",
        os.fspath(output),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {"status": "pass"}
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "pass"
    assert result["receipt_sha256"] == hashlib.sha256(RECEIPT_PATH.read_bytes()).hexdigest()
    assert result["baseline_receipt_sha256"] is None
    assert os.fspath(tmp_path) not in completed.stdout

    second = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)
    assert second.returncode == 2
    assert second.stderr == ""
    assert json.loads(second.stdout) == {"code": "invalid_output", "status": "error"}
