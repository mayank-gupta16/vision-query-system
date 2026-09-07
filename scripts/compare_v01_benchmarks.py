#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compare two compatible v0.1 CPU-LITE benchmark receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import NoReturn, cast

import run_v01_benchmark as benchmark

_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_INTEGER = (1 << 63) - 1
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PYTHON_VERSION = re.compile(r"3\.(?:13|14)\.[0-9]+\Z")
_SQLITE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_CHECK_NAMES = {
    "artifact_integrity_valid",
    "crop_bytes_retrievable",
    "evidence_index_matches",
    "exact_candidate_count",
    "exact_near_five_fps_selection",
    "exact_sample_count",
    "exact_source_pts",
    "fixture_rights_and_privacy_proven",
    "frame_index_matches",
    "golden_matches",
    "manifest_reopened",
    "temporary_outputs_removed",
    "world_counts_match",
}
_IMPLEMENTATION_NAMES = {
    "benchmark_harness_sha256",
    "executable_implementation",
    "geometry_module_sha256",
    "ingestion_module_sha256",
    "python_version",
    "sampling_module_sha256",
    "sqlite_version",
    "storage_module_sha256",
    "world_store_module_sha256",
}
_MEASUREMENT_NAMES = {
    "artifact_promotion",
    "artifact_stage",
    "crop_and_hash",
    "finalize_transaction",
    "indexed_queries",
    "input_generation",
    "intent_transaction",
    "metadata_transactions",
    "prepare_transaction",
    "record_construction",
    "reopen_and_verify",
    "sampling",
    "store_initialization",
}
_METRIC_NAMES = {
    "cpu_utilization_milli_percent",
    "frames_per_second_milli",
    "process_cpu_ns",
    "real_time_factor_milli",
    "total_wall_ns",
}
_PROFILE_NAMES = {
    "cpu_count",
    "gpu_required",
    "machine",
    "meets_requirements",
    "memory_bytes",
    "name",
    "os",
    "required_memory_bytes",
    "required_vcpu",
}
_PROVENANCE_NAMES = {
    "fixture_manifest_sha256",
    "generated_input",
    "license_expression",
    "perception_accuracy_claimed",
    "privacy_classification",
    "source_revision",
}
_RESOURCE_NAMES = {
    "artifact_store_allocated_bytes",
    "artifact_store_file_count",
    "artifact_store_logical_bytes",
    "combined_store_allocated_bytes",
    "combined_store_file_count",
    "combined_store_logical_bytes",
    "disk_measured",
    "index_measured",
    "metadata_index_allocated_bytes",
    "metadata_index_file_count",
    "metadata_index_logical_bytes",
    "peak_rss_limit_bytes",
    "process_peak_rss_bytes",
    "rss_bounded",
    "rss_measured",
    "timing_measured",
}
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


def _exact_mapping(value: object, names: set[str]) -> dict[str, object]:
    selected = _mapping(value)
    if set(selected) != names:
        _fail("invalid_receipt")
    return selected


def _integer(mapping: dict[str, object], name: str, *, positive: bool = True) -> int:
    value = mapping.get(name)
    minimum = 1 if positive else 0
    if type(value) is not int or value < minimum or value > _MAX_INTEGER:
        _fail("invalid_receipt")
    return value


def _text(mapping: dict[str, object], name: str) -> str:
    value = mapping.get(name)
    if type(value) is not str or not value or len(value) > 512:
        _fail("invalid_receipt")
    return value


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


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    selected: dict[str, object] = {}
    for name, value in pairs:
        if name in selected:
            _fail("invalid_receipt")
        selected[name] = value
    return selected


def _load(path: Path) -> dict[str, object]:
    descriptor = -1
    try:
        if not hasattr(os, "O_NOFOLLOW"):
            _fail("invalid_receipt")
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_RECEIPT_BYTES:
            _fail("invalid_receipt")
        chunks: list[bytes] = []
        remaining = metadata.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        final_metadata = os.fstat(descriptor)
        if (
            len(raw) != metadata.st_size
            or final_metadata.st_dev != metadata.st_dev
            or final_metadata.st_ino != metadata.st_ino
            or final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
            or final_metadata.st_ctime_ns != metadata.st_ctime_ns
        ):
            _fail("invalid_receipt")
        loaded = json.loads(raw, object_pairs_hook=_unique_object)
    except ComparisonError:
        raise
    except (OSError, TypeError, ValueError):
        _fail("invalid_receipt")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _receipt(loaded)


def _positive_metric(receipt: dict[str, object], path: tuple[str, str]) -> int:
    section = _mapping(receipt.get(path[0]))
    return _integer(section, path[1])


def _manifest() -> dict[str, object]:
    try:
        return benchmark.load_manifest()
    except (OSError, TypeError, ValueError):
        _fail("invalid_policy")


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
    manifest = _manifest()
    configuration = _mapping(receipt["configuration"])
    workload = _mapping(receipt["workload"])
    if _canonical(configuration) != _canonical(manifest["configuration"]) or _canonical(
        workload
    ) != _canonical(manifest["workload"]):
        _fail("invalid_receipt")

    provenance = _exact_mapping(receipt["provenance"], _PROVENANCE_NAMES)
    revision = provenance.get("source_revision")
    fixture_digest = provenance.get("fixture_manifest_sha256")
    if (
        type(revision) is not str
        or not _REVISION.fullmatch(revision)
        or type(fixture_digest) is not str
        or not _DIGEST.fullmatch(fixture_digest)
        or provenance.get("generated_input") is not True
        or provenance.get("license_expression") != "Apache-2.0"
        or provenance.get("perception_accuracy_claimed") is not False
        or provenance.get("privacy_classification") != "public-synthetic-no-personal-data"
    ):
        _fail("invalid_receipt")

    implementation = _exact_mapping(receipt["implementation"], _IMPLEMENTATION_NAMES)
    harness_digest = implementation.get("benchmark_harness_sha256")
    digest_names = (
        "benchmark_harness_sha256",
        "geometry_module_sha256",
        "ingestion_module_sha256",
        "sampling_module_sha256",
        "storage_module_sha256",
        "world_store_module_sha256",
    )
    if (
        type(harness_digest) is not str
        or not _DIGEST.fullmatch(harness_digest)
        or harness_digest != benchmark._sha256(benchmark.ROOT / "scripts" / "run_v01_benchmark.py")
        or any(
            type(implementation[name]) is not str
            or not _DIGEST.fullmatch(cast(str, implementation[name]))
            for name in digest_names
        )
        or implementation.get("executable_implementation") != "cpython"
        or type(implementation.get("python_version")) is not str
        or not _PYTHON_VERSION.fullmatch(cast(str, implementation["python_version"]))
        or type(implementation.get("sqlite_version")) is not str
        or not _SQLITE_VERSION.fullmatch(cast(str, implementation["sqlite_version"]))
    ):
        _fail("invalid_receipt")

    profile = _exact_mapping(receipt["profile"], _PROFILE_NAMES)
    cpu_count = _integer(profile, "cpu_count")
    memory_bytes = _integer(profile, "memory_bytes")
    machine = _text(profile, "machine")
    operating_system = _text(profile, "os")
    expected_profile = (
        operating_system.startswith("Linux-")
        and machine == "x86_64"
        and cpu_count >= 4
        and memory_bytes >= 16_000_000_000
    )
    if (
        type(profile.get("meets_requirements")) is not bool
        or profile.get("name") != "CPU-LITE"
        or profile.get("gpu_required") is not False
        or profile.get("required_memory_bytes") != 16_000_000_000
        or type(profile.get("required_memory_bytes")) is not int
        or profile.get("required_vcpu") != 4
        or type(profile.get("required_vcpu")) is not int
        or profile["meets_requirements"] is not expected_profile
    ):
        _fail("invalid_receipt")

    warmup = _mapping(receipt["warmup"])
    configuration_warmup = {
        "crop_count": configuration["warmup_crop_count"],
        "excluded_from_total": True,
        "sampling_candidate_count": configuration["warmup_sampling_candidates"],
    }
    if _canonical(warmup) != _canonical(configuration_warmup):
        _fail("invalid_receipt")

    measurements = _exact_mapping(receipt["measurements"], _MEASUREMENT_NAMES)
    for name in _MEASUREMENT_NAMES:
        extra = {"transaction_count"} if name == "metadata_transactions" else set()
        if name == "sampling":
            extra = {"page_count"}
        measurement = _exact_mapping(measurements[name], {"cpu_ns", "wall_ns", *extra})
        stage_cpu_ns = _integer(measurement, "cpu_ns")
        stage_wall_ns = _integer(measurement, "wall_ns")
        if stage_cpu_ns > stage_wall_ns * cpu_count:
            _fail("invalid_receipt")
    metadata_measurement = _mapping(measurements["metadata_transactions"])
    sampling_measurement = _mapping(measurements["sampling"])
    candidate_count = cast(int, workload["candidate_frame_count"])
    sample_count = cast(int, workload["sample_count"])
    batch_size = cast(int, configuration["metadata_batch_size"])
    page_size = cast(int, configuration["sample_page_candidates"])
    expected_transactions = 2 * ((sample_count + batch_size - 1) // batch_size)
    expected_pages = 1 + max(0, (candidate_count - page_size + page_size - 2) // (page_size - 1))
    if (
        _integer(metadata_measurement, "transaction_count") != expected_transactions
        or _integer(sampling_measurement, "page_count") != expected_pages
    ):
        _fail("invalid_receipt")

    metrics = _exact_mapping(receipt["metrics"], _METRIC_NAMES)
    for name in _METRIC_NAMES:
        _integer(metrics, name)
    total_wall_ns = _integer(metrics, "total_wall_ns")
    process_cpu_ns = _integer(metrics, "process_cpu_ns")
    source_seconds = cast(int, workload["source_seconds"])
    timed_measurements = _MEASUREMENT_NAMES - {"input_generation"}
    if (
        metrics["cpu_utilization_milli_percent"] != process_cpu_ns * 100_000 // total_wall_ns
        or metrics["frames_per_second_milli"] != sample_count * 1_000_000_000_000 // total_wall_ns
        or metrics["real_time_factor_milli"] != source_seconds * 1_000_000_000_000 // total_wall_ns
        or process_cpu_ns > total_wall_ns * cpu_count
        or sum(_integer(_mapping(measurements[name]), "wall_ns") for name in timed_measurements)
        > total_wall_ns
        or sum(_integer(_mapping(measurements[name]), "cpu_ns") for name in timed_measurements)
        > process_cpu_ns
    ):
        _fail("invalid_receipt")

    resources = _exact_mapping(receipt["resources"], _RESOURCE_NAMES)
    resource_checks = (
        "disk_measured",
        "index_measured",
        "rss_bounded",
        "rss_measured",
        "timing_measured",
    )
    resource_values = {
        name: _integer(resources, name) for name in _RESOURCE_NAMES - set(resource_checks)
    }
    if resources.get("peak_rss_limit_bytes") != benchmark._PEAK_RSS_LIMIT_BYTES or any(
        type(resources.get(name)) is not bool for name in resource_checks
    ):
        _fail("invalid_receipt")
    peak_rss = _positive_metric(receipt, _METRIC_PATHS["process_peak_rss_bytes"])
    timing_measured = all(
        _integer(_mapping(measurements[name]), "cpu_ns") > 0
        and _integer(_mapping(measurements[name]), "wall_ns") > 0
        for name in _MEASUREMENT_NAMES
    )
    if (
        resources["disk_measured"]
        is not (
            resource_values["combined_store_file_count"] > 0
            and resource_values["combined_store_logical_bytes"] > 0
            and resource_values["combined_store_allocated_bytes"] > 0
        )
        or resources["index_measured"]
        is not (
            resource_values["metadata_index_file_count"] > 0
            and resource_values["metadata_index_logical_bytes"] > 0
        )
        or resources["rss_measured"] is not (peak_rss > 0)
        or resources["rss_bounded"] is not (peak_rss <= benchmark._PEAK_RSS_LIMIT_BYTES)
        or resources["timing_measured"] is not timing_measured
        or resource_values["artifact_store_file_count"] != 1
        or resource_values["artifact_store_logical_bytes"] != 2_764_800
        or resource_values["combined_store_logical_bytes"]
        != resource_values["artifact_store_logical_bytes"]
        + resource_values["metadata_index_logical_bytes"]
        or resource_values["combined_store_allocated_bytes"]
        != resource_values["artifact_store_allocated_bytes"]
        + resource_values["metadata_index_allocated_bytes"]
        or resource_values["combined_store_file_count"]
        != resource_values["artifact_store_file_count"]
        + resource_values["metadata_index_file_count"]
        + 1
    ):
        _fail("invalid_receipt")

    checks = _exact_mapping(receipt["checks"], _CHECK_NAMES)
    if any(type(result) is not bool for result in checks.values()):
        _fail("invalid_receipt")
    expected_status = (
        "pass"
        if profile["meets_requirements"] is True
        and all(checks.values())
        and all(resources[name] is True for name in resource_checks)
        else "fail"
    )
    if receipt["status"] != expected_status:
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
                "cpu_count",
                "gpu_required",
                "machine",
                "memory_bytes",
                "name",
                "os",
                "required_memory_bytes",
                "required_vcpu",
            )
        },
        "runtime": {
            name: implementation.get(name)
            for name in (
                "benchmark_harness_sha256",
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
        difference = candidate - baseline
    elif direction == "higher_is_better":
        difference = baseline - candidate
    else:
        _fail("invalid_policy")
    numerator = difference * 10_000
    if numerator <= 0:
        return -((-numerator) // baseline)
    return (numerator + baseline - 1) // baseline


def compare(
    baseline: dict[str, object],
    candidate: dict[str, object],
) -> dict[str, object]:
    selected_baseline = _receipt(baseline)
    selected_candidate = _receipt(candidate)
    manifest = _manifest()
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
