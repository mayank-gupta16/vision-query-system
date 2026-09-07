#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compare two compatible v0.1 CPU-LITE benchmark receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from typing import NoReturn, cast

import run_v01_benchmark as benchmark

_MAX_RECEIPT_BYTES = 1024 * 1024
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_METRIC_PATHS = {
    "combined_store_logical_bytes": ("resources", "combined_store_logical_bytes"),
    "frames_per_second_milli": ("metrics", "frames_per_second_milli"),
    "process_cpu_ns": ("metrics", "process_cpu_ns"),
    "process_peak_rss_bytes": ("resources", "process_peak_rss_bytes"),
    "real_time_factor_milli": ("metrics", "real_time_factor_milli"),
    "total_wall_ns": ("metrics", "total_wall_ns"),
}


class ComparisonError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise ComparisonError(code) from None


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        _fail("invalid_receipt")
    return cast(dict[str, object], value)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError):
        _fail("invalid_receipt")


def _load(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_RECEIPT_BYTES:
            _fail("invalid_receipt")
        raw = path.read_bytes()
        if len(raw) != metadata.st_size:
            _fail("invalid_receipt")
        loaded = json.loads(raw)
    except ComparisonError:
        raise
    except (OSError, TypeError, ValueError):
        _fail("invalid_receipt")
    return _receipt(loaded)


def _positive_metric(receipt: dict[str, object], path: tuple[str, str]) -> int:
    section = _mapping(receipt.get(path[0]))
    value = section.get(path[1])
    if type(value) is not int or value <= 0:
        _fail("invalid_receipt")
    return value


def _receipt(value: object) -> dict[str, object]:
    receipt = _mapping(value)
    required = {
        "checks",
        "configuration",
        "implementation",
        "measurements",
        "metrics",
        "profile",
        "provenance",
        "resources",
        "schema",
        "schema_version",
        "status",
        "warmup",
        "workload",
    }
    if (
        set(receipt) != required
        or receipt["schema"] != "visualworld.v01-benchmark-receipt"
        or receipt["schema_version"] != 1
        or receipt["status"] not in {"pass", "fail"}
    ):
        _fail("invalid_receipt")
    provenance = _mapping(receipt["provenance"])
    revision = provenance.get("source_revision")
    if type(revision) is not str or not _REVISION.fullmatch(revision):
        _fail("invalid_receipt")
    for path in _METRIC_PATHS.values():
        _positive_metric(receipt, path)
    profile = _mapping(receipt["profile"])
    if (
        type(profile.get("meets_requirements")) is not bool
        or profile.get("name") != "CPU-LITE"
        or profile.get("gpu_required") is not False
    ):
        _fail("invalid_receipt")
    resources = _mapping(receipt["resources"])
    if (
        resources.get("peak_rss_limit_bytes") != benchmark._PEAK_RSS_LIMIT_BYTES
        or type(resources.get("rss_bounded")) is not bool
    ):
        _fail("invalid_receipt")
    return receipt


def _compatibility(receipt: dict[str, object]) -> dict[str, object]:
    profile = _mapping(receipt["profile"])
    implementation = _mapping(receipt["implementation"])
    provenance = _mapping(receipt["provenance"])
    return {
        "configuration": receipt["configuration"],
        "fixture_manifest_sha256": provenance.get("fixture_manifest_sha256"),
        "profile": {
            name: profile.get(name)
            for name in (
                "gpu_required",
                "machine",
                "name",
                "required_memory_bytes",
                "required_vcpu",
            )
        },
        "runtime": {
            name: implementation.get(name)
            for name in (
                "executable_implementation",
                "python_version",
                "sqlite_version",
            )
        },
        "workload": receipt["workload"],
    }


def _regression_basis_points(baseline: int, candidate: int, direction: str) -> int:
    if baseline <= 0 or candidate <= 0:
        _fail("invalid_receipt")
    if direction == "lower_is_better":
        return (candidate - baseline) * 10_000 // baseline
    if direction == "higher_is_better":
        return (baseline - candidate) * 10_000 // baseline
    _fail("invalid_policy")


def compare(
    baseline: dict[str, object],
    candidate: dict[str, object],
) -> dict[str, object]:
    selected_baseline = _receipt(baseline)
    selected_candidate = _receipt(candidate)
    manifest = benchmark.load_manifest()
    policy = _mapping(manifest["comparator"])
    directions = _mapping(policy["metrics"])
    maximum = policy["max_regression_basis_points"]
    if type(maximum) is not int or maximum < 0 or set(directions) != set(_METRIC_PATHS):
        _fail("invalid_policy")
    expected_manifest_sha256 = benchmark._sha256(benchmark.MANIFEST_PATH)
    baseline_compatibility = _compatibility(selected_baseline)
    candidate_compatibility = _compatibility(selected_candidate)
    compatible = (
        baseline_compatibility == candidate_compatibility
        and baseline_compatibility["fixture_manifest_sha256"] == expected_manifest_sha256
    )
    comparisons: dict[str, object] = {}
    metrics_within_budget = compatible
    if compatible:
        for name, path in _METRIC_PATHS.items():
            baseline_value = _positive_metric(selected_baseline, path)
            candidate_value = _positive_metric(selected_candidate, path)
            direction = directions[name]
            if type(direction) is not str:
                _fail("invalid_policy")
            regression = _regression_basis_points(
                baseline_value,
                candidate_value,
                direction,
            )
            within_budget = regression <= maximum
            metrics_within_budget = metrics_within_budget and within_budget
            comparisons[name] = {
                "baseline": baseline_value,
                "candidate": candidate_value,
                "direction": direction,
                "regression_basis_points": regression,
                "within_budget": within_budget,
            }
    baseline_profile = _mapping(selected_baseline["profile"])
    candidate_profile = _mapping(selected_candidate["profile"])
    candidate_resources = _mapping(selected_candidate["resources"])
    checks = {
        "compatible": compatible,
        "baseline_passed": selected_baseline["status"] == "pass"
        and baseline_profile["meets_requirements"] is True,
        "candidate_passed": selected_candidate["status"] == "pass"
        and candidate_profile["meets_requirements"] is True,
        "candidate_rss_within_release_limit": (
            candidate_resources["rss_bounded"] is True
            and _positive_metric(
                selected_candidate,
                _METRIC_PATHS["process_peak_rss_bytes"],
            )
            <= benchmark._PEAK_RSS_LIMIT_BYTES
        ),
        "metrics_within_budget": metrics_within_budget,
    }
    baseline_revision = _mapping(selected_baseline["provenance"])["source_revision"]
    candidate_revision = _mapping(selected_candidate["provenance"])["source_revision"]
    passed = all(checks.values())
    return {
        "schema": "visualworld.v01-benchmark-comparison",
        "schema_version": 1,
        "status": "pass" if passed else "fail",
        "revisions": {
            "baseline": baseline_revision,
            "candidate": candidate_revision,
        },
        "compatibility_sha256": hashlib.sha256(_canonical(baseline_compatibility)).hexdigest(),
        "policy": policy,
        "checks": checks,
        "comparisons": comparisons,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = compare(_load(arguments.baseline), _load(arguments.candidate))
        arguments.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except ComparisonError as error:
        print(
            json.dumps({"error": {"code": error.code}, "status": "error"}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except OSError:
        print(
            json.dumps({"error": {"code": "output_failed"}, "status": "error"}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"status": result["status"]}, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
