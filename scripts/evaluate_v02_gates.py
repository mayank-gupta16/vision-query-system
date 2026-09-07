#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Validate and evaluate a v0.2 research receipt against the frozen gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from datetime import date
from pathlib import Path
from typing import NoReturn, cast
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "fixtures" / "v02-evaluation" / "policy.json"
PINNED_POLICY_SHA256 = "e745f520485e0de3f2ad9c312b3f09d6ffda361e134e717187f3d3839100d6eb"
_MAX_JSON_BYTES = 1024 * 1024
_MAX_INTEGER = (1 << 63) - 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_IMMUTABLE_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+@/-]{0,255}\Z")
_FOLLOW_UP = re.compile(
    r"https://github\.com/mayank-gupta16/vision-query-system/issues/[1-9][0-9]*\Z"
)
_EXPERIMENT_ISSUES = {"crop_value": 23, "detection": 21, "sampling": 22, "tracking": 24}
_METRIC_FIELDS = {
    "aggregation",
    "calculation",
    "direction",
    "kind",
    "maximum",
    "minimum",
    "regression_budget_basis_points",
    "source",
    "strata",
    "unit",
    "unknown",
}
_UNITS = {
    "basis_points",
    "bytes",
    "count",
    "milliseconds",
    "milli_frames_per_second",
    "milli_pixels",
    "milli_ratio",
    "signed_basis_points",
}
_ARTIFACT_FORMATS = {
    "configuration-json",
    "native-library",
    "onnx",
    "openvino-ir",
    "python-source",
    "safetensors",
}


class EvaluationError(RuntimeError):
    """A stable, non-sensitive evaluation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise EvaluationError(code) from None


def _mapping(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        _fail(code)
    return cast(dict[str, object], value)


def _exact(value: object, fields: set[str], code: str) -> dict[str, object]:
    selected = _mapping(value, code)
    if set(selected) != fields:
        _fail(code)
    return selected


def _integer(
    value: object,
    code: str,
    *,
    minimum: int = 0,
    maximum: int = _MAX_INTEGER,
) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        _fail(code)
    return value


def _text(value: object, code: str, *, maximum: int = 2048) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or not value.isascii()
        or any(ord(character) < 0x20 for character in value)
    ):
        _fail(code)
    return value


def _name(value: object, code: str) -> str:
    selected = _text(value, code, maximum=128)
    if not _NAME.fullmatch(selected):
        _fail(code)
    return selected


def _sha256_text(value: object, code: str) -> str:
    selected = _text(value, code, maximum=64)
    if not _DIGEST.fullmatch(selected):
        _fail(code)
    return selected


def _immutable_revision(value: object, code: str) -> str:
    selected = _text(value, code, maximum=256)
    if _IMMUTABLE_REVISION.fullmatch(selected) is None or selected.casefold() in {
        "head",
        "latest",
        "main",
        "master",
        "stable",
        "trunk",
    }:
        _fail(code)
    return selected


def _https_url(value: object, code: str) -> str:
    selected = _text(value, code)
    try:
        parsed = urlsplit(selected)
        port = parsed.port
    except ValueError:
        _fail(code)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        _fail(code)
    return selected


def _day(value: object, code: str) -> date:
    selected = _text(value, code, maximum=10)
    try:
        parsed = date.fromisoformat(selected)
    except ValueError:
        _fail(code)
    if parsed.isoformat() != selected:
        _fail(code)
    return parsed


def _string_list(
    value: object,
    code: str,
    *,
    minimum: int = 1,
    maximum: int = 128,
) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        _fail(code)
    selected = [_text(item, code, maximum=256) for item in value]
    if len(set(selected)) != len(selected):
        _fail(code)
    return selected


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    selected: dict[str, object] = {}
    for key, value in pairs:
        if key in selected:
            raise ValueError("duplicate key")
        selected[key] = value
    return selected


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite number")


def _read_json(path: Path, code: str) -> tuple[object, str]:
    descriptor = -1
    try:
        if not all(hasattr(os, flag) for flag in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK")):
            _fail(code)
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= _MAX_JSON_BYTES:
            _fail(code)
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            _fail(code)
        loaded = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except EvaluationError:
        raise
    except (OSError, TypeError, UnicodeError, ValueError):
        _fail(code)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return loaded, hashlib.sha256(raw).hexdigest()


def _canonical(value: object, code: str = "invalid_receipt") -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, UnicodeError, ValueError):
        _fail(code)


def _thresholds(
    value: object,
    strata: list[str],
    unit: str,
    code: str,
) -> dict[str, int] | None:
    if value is None:
        return None
    selected = _exact(value, set(strata), code)
    maximum = 10_000 if unit in {"basis_points", "signed_basis_points"} else _MAX_INTEGER
    return {key: _integer(selected[key], code, maximum=maximum) for key in strata}


def _metric_definition(value: object, required_strata: list[str]) -> dict[str, object]:
    code = "invalid_policy"
    metric = _exact(value, _METRIC_FIELDS, code)
    strata = _string_list(metric["strata"], code)
    if not set(strata) <= set(required_strata):
        _fail(code)
    unit = _text(metric["unit"], code, maximum=64)
    if unit not in _UNITS:
        _fail(code)
    minimum = _thresholds(metric["minimum"], strata, unit, code)
    maximum = _thresholds(metric["maximum"], strata, unit, code)
    kind = metric["kind"]
    direction = metric["direction"]
    aggregation = metric["aggregation"]
    source = metric["source"]
    unknown = metric["unknown"]
    budget = metric["regression_budget_basis_points"]
    calculation = _name(metric["calculation"], code)
    if (
        kind not in {"diagnostic", "gate"}
        or direction not in {"higher_is_better", "lower_is_better"}
        or aggregation not in {"adverse_mean", "maximum", "median"}
        or source not in {"reported", "run_status"}
        or unknown not in {"fail", "report"}
    ):
        _fail(code)
    if kind == "diagnostic":
        if minimum is not None or maximum is not None or budget is not None or unknown != "report":
            _fail(code)
    else:
        if (minimum is None) == (maximum is None) or unknown != "fail":
            _fail(code)
        if minimum is not None and direction != "higher_is_better":
            _fail(code)
        if maximum is not None and direction != "lower_is_better":
            _fail(code)
        if budget is not None:
            _integer(budget, code, maximum=10_000)
        elif maximum is None or any(limit != 0 for limit in maximum.values()):
            _fail(code)
    if source == "run_status":
        if (
            calculation != "visualworld.repetition-failure-rate.v1"
            or aggregation != "adverse_mean"
            or direction != "lower_is_better"
            or strata != ["overall"]
            or maximum != {"overall": 0}
        ):
            _fail(code)
    elif calculation == "visualworld.repetition-failure-rate.v1":
        _fail(code)
    metric["strata"] = strata
    metric["minimum"] = minimum
    metric["maximum"] = maximum
    return metric


def validate_policy(value: object) -> dict[str, object]:
    code = "invalid_policy"
    policy = _exact(
        value,
        {
            "allowed_candidate_license_expressions",
            "allowed_dataset_license_expressions",
            "experiments",
            "frozen_on",
            "policy_version",
            "profile",
            "protocol",
            "schema",
            "schema_version",
            "waiver_policy",
        },
        code,
    )
    if (
        policy["schema"] != "visualworld.v02-evaluation-policy"
        or _integer(policy["schema_version"], code, minimum=1, maximum=1) != 1
        or policy["policy_version"] != "v0.2-gates-1"
        or _day(policy["frozen_on"], code) != date(2026, 9, 7)
    ):
        _fail(code)
    candidate_licenses = _string_list(policy["allowed_candidate_license_expressions"], code)
    dataset_licenses = _string_list(policy["allowed_dataset_license_expressions"], code)
    if candidate_licenses != sorted(candidate_licenses) or dataset_licenses != sorted(
        dataset_licenses
    ):
        _fail(code)

    profile = _exact(
        policy["profile"],
        {
            "architecture",
            "gpu_required",
            "maximum_peak_rss_bytes",
            "minimum_memory_bytes",
            "name",
            "operating_system",
            "vcpu",
        },
        code,
    )
    if (
        profile["architecture"] != "x86_64"
        or profile["gpu_required"] is not False
        or _integer(
            profile["maximum_peak_rss_bytes"],
            code,
            minimum=2 * 1024**3,
            maximum=2 * 1024**3,
        )
        != 2 * 1024**3
        or _integer(
            profile["minimum_memory_bytes"],
            code,
            minimum=16_000_000_000,
            maximum=16_000_000_000,
        )
        != 16_000_000_000
        or profile["name"] != "CPU-LITE"
        or profile["operating_system"] != "Linux"
        or _integer(profile["vcpu"], code, minimum=4, maximum=4) != 4
    ):
        _fail(code)

    protocol = _exact(
        policy["protocol"],
        {
            "aggregate",
            "dispersion",
            "gate_split",
            "measured_repetitions",
            "report_each_repetition",
            "seed_set",
            "selection_split",
            "warmup_runs",
        },
        code,
    )
    seeds = protocol["seed_set"]
    if not isinstance(seeds, list) or any(type(seed) is not int for seed in seeds):
        _fail(code)
    if (
        protocol["aggregate"] != "metric_defined"
        or protocol["dispersion"] != "median_absolute_deviation"
        or protocol["gate_split"] != "test"
        or protocol["selection_split"] != "calibration"
        or protocol["report_each_repetition"] is not True
        or _integer(protocol["warmup_runs"], code, minimum=1, maximum=1) != 1
        or _integer(protocol["measured_repetitions"], code, minimum=5, maximum=5) != 5
        or len(seeds) != 5
        or len(set(cast(list[int], seeds))) != 5
        or any(seed <= 0 or seed > _MAX_INTEGER for seed in cast(list[int], seeds))
    ):
        _fail(code)

    waiver = _exact(
        policy["waiver_policy"],
        {"maximum_duration_days", "owner", "required_compensating_controls"},
        code,
    )
    if (
        waiver["owner"] != "mayank-gupta16"
        or _integer(waiver["maximum_duration_days"], code, minimum=30, maximum=30) != 30
        or _integer(waiver["required_compensating_controls"], code, minimum=1, maximum=1) != 1
    ):
        _fail(code)

    experiments = _exact(policy["experiments"], set(_EXPERIMENT_ISSUES), code)
    for experiment_name, issue in _EXPERIMENT_ISSUES.items():
        experiment = _exact(
            experiments[experiment_name], {"issue", "metrics", "required_strata"}, code
        )
        if experiment["issue"] != issue or type(experiment["issue"]) is not int:
            _fail(code)
        required_strata = _string_list(experiment["required_strata"], code)
        if required_strata[0] != "overall":
            _fail(code)
        metrics = _mapping(experiment["metrics"], code)
        if not metrics or not all(_NAME.fullmatch(name) for name in metrics):
            _fail(code)
        experiment["required_strata"] = required_strata
        experiment["metrics"] = {
            name: _metric_definition(metrics[name], required_strata) for name in sorted(metrics)
        }
        used_gate_strata = {
            stratum
            for definition in cast(dict[str, dict[str, object]], experiment["metrics"]).values()
            if definition["kind"] == "gate"
            for stratum in cast(list[str], definition["strata"])
        }
        if used_gate_strata != set(required_strata):
            _fail(code)
    return policy


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict[str, object], str]:
    loaded, digest = _read_json(path, "invalid_policy")
    policy = validate_policy(loaded)
    if digest != PINNED_POLICY_SHA256:
        _fail("invalid_policy")
    return policy, digest


def validate_manifest(value: object, policy: dict[str, object]) -> dict[str, object]:
    code = "invalid_manifest"
    manifest = _exact(
        value,
        {
            "acquisition",
            "experiment",
            "manifest_id",
            "privacy",
            "rights",
            "schema",
            "schema_version",
            "splits",
            "strata",
        },
        code,
    )
    experiment_name = _name(manifest["experiment"], code)
    experiments = _mapping(policy["experiments"], code)
    if (
        manifest["schema"] != "visualworld.v02-evaluation-dataset-manifest"
        or _integer(manifest["schema_version"], code, minimum=1, maximum=1) != 1
        or experiment_name not in experiments
    ):
        _fail(code)
    experiment = _mapping(experiments[experiment_name], code)
    required_strata = cast(list[str], experiment["required_strata"])
    if not _NAME.fullmatch(_text(manifest["manifest_id"], code, maximum=128)):
        _fail(code)

    acquisition = _exact(
        manifest["acquisition"],
        {"method", "owner", "revision", "sha256", "url"},
        code,
    )
    _text(acquisition["method"], code)
    _text(acquisition["owner"], code, maximum=256)
    _immutable_revision(acquisition["revision"], code)
    _sha256_text(acquisition["sha256"], code)
    _https_url(acquisition["url"], code)

    rights = _exact(
        manifest["rights"],
        {
            "allowed_uses",
            "commercial_use_allowed",
            "derivatives_allowed",
            "license_expression",
            "redistribution_allowed",
        },
        code,
    )
    allowed_uses = _string_list(rights["allowed_uses"], code)
    allowed_dataset_licenses = cast(list[str], policy["allowed_dataset_license_expressions"])
    if (
        not {"benchmarking", "evaluation"} <= set(allowed_uses)
        or rights["license_expression"] not in allowed_dataset_licenses
        or rights["commercial_use_allowed"] is not True
        or not set(allowed_uses) <= {"benchmarking", "evaluation", "modification", "redistribution"}
        or rights["derivatives_allowed"] is not True
        or rights["redistribution_allowed"] is not True
        or "modification" not in allowed_uses
        or "redistribution" not in allowed_uses
    ):
        _fail(code)

    privacy = _exact(
        manifest["privacy"],
        {
            "classification",
            "consent_status",
            "contains_faces",
            "contains_personal_data",
            "contains_plates",
            "contains_real_people",
            "sensitive_derived_outputs_retained",
        },
        code,
    )
    if (
        privacy["classification"]
        not in {"licensed-no-personal-data", "public-synthetic-no-personal-data"}
        or privacy["consent_status"] != "not-applicable-no-personal-data"
        or any(
            privacy[field] is not False
            for field in (
                "contains_faces",
                "contains_personal_data",
                "contains_plates",
                "contains_real_people",
                "sensitive_derived_outputs_retained",
            )
        )
    ):
        _fail(code)

    splits = _exact(manifest["splits"], {"calibration", "test"}, code)
    annotation_ids: list[str] = []
    split_ids: list[str] = []
    for split_name in ("calibration", "test"):
        split = _exact(
            splits[split_name],
            {"annotation_sha256", "item_count", "item_ids_sha256", "stratum_item_counts"},
            code,
        )
        item_count = _integer(split["item_count"], code, minimum=1)
        annotation_ids.append(_sha256_text(split["annotation_sha256"], code))
        split_ids.append(_sha256_text(split["item_ids_sha256"], code))
        stratum_counts = _exact(split["stratum_item_counts"], set(required_strata), code)
        counts = {
            stratum: _integer(stratum_counts[stratum], code, minimum=1, maximum=item_count)
            for stratum in required_strata
        }
        if counts["overall"] != item_count:
            _fail(code)
    if len(set(annotation_ids)) != 2 or len(set(split_ids)) != 2:
        _fail(code)

    strata = _string_list(manifest["strata"], code)
    if strata != required_strata:
        _fail(code)
    manifest["experiment"] = experiment_name
    manifest["strata"] = strata
    return manifest


def load_manifest(path: Path, policy: dict[str, object]) -> tuple[dict[str, object], str]:
    loaded, digest = _read_json(path, "invalid_manifest")
    return validate_manifest(loaded, policy), digest


def _metric_value(value: object, unit: str, code: str) -> int | str:
    if value == "UNKNOWN":
        return "UNKNOWN"
    minimum = -10_000 if unit == "signed_basis_points" else 0
    maximum = 10_000 if unit in {"basis_points", "signed_basis_points"} else _MAX_INTEGER
    return _integer(value, code, minimum=minimum, maximum=maximum)


def _metric_map(
    value: object,
    definitions: dict[str, dict[str, object]],
    code: str,
) -> dict[str, dict[str, int | str]]:
    selected = _exact(value, set(definitions), code)
    result: dict[str, dict[str, int | str]] = {}
    for name, definition in definitions.items():
        strata = cast(list[str], definition["strata"])
        values = _exact(selected[name], set(strata), code)
        unit = cast(str, definition["unit"])
        result[name] = {stratum: _metric_value(values[stratum], unit, code) for stratum in strata}
    return result


def _validate_candidate(
    value: object,
    policy: dict[str, object],
    evaluated_on: date,
    code: str,
) -> dict[str, object]:
    candidate = _exact(value, {"artifacts", "configuration_sha256", "name"}, code)
    _text(candidate["name"], code, maximum=256)
    _sha256_text(candidate["configuration_sha256"], code)
    artifacts = candidate["artifacts"]
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 32:
        _fail(code)
    selected_artifacts: list[dict[str, object]] = []
    names: list[str] = []
    allowed_licenses = cast(list[str], policy["allowed_candidate_license_expressions"])
    fields = {
        "commercial_use_allowed",
        "executable_serialization",
        "format",
        "isolation",
        "kind",
        "license_expression",
        "name",
        "redistribution_allowed",
        "review_status",
        "reviewed_on",
        "revision",
        "sha256",
        "source_url",
        "terms_url",
        "trust_remote_code",
        "use",
    }
    for value_item in artifacts:
        artifact = _exact(value_item, fields, code)
        artifact_name = _name(artifact["name"], code)
        names.append(artifact_name)
        if artifact["kind"] not in {"code", "configuration", "runtime", "weights"}:
            _fail(code)
        if artifact["format"] not in _ARTIFACT_FORMATS:
            _fail(code)
        if artifact["isolation"] not in {
            "in-process-reviewed",
            "isolated-worker",
            "none-generated-fixture",
        }:
            _fail(code)
        if (
            artifact["license_expression"] not in allowed_licenses
            or artifact["commercial_use_allowed"] is not True
            or artifact["redistribution_allowed"] is not True
            or artifact["trust_remote_code"] is not False
            or artifact["executable_serialization"] is not False
            or artifact["review_status"] != "approved-for-evaluation"
        ):
            _fail(code)
        _immutable_revision(artifact["revision"], code)
        _sha256_text(artifact["sha256"], code)
        _text(artifact["use"], code, maximum=512)
        _https_url(artifact["source_url"], code)
        _https_url(artifact["terms_url"], code)
        if _day(artifact["reviewed_on"], code) > evaluated_on:
            _fail(code)
        selected_artifacts.append(artifact)
    if len(set(names)) != len(names):
        _fail(code)
    candidate["artifacts"] = selected_artifacts
    return candidate


def _validate_implementation_component(
    value: object,
    policy: dict[str, object],
    code: str,
) -> dict[str, object]:
    component = _exact(
        value,
        {"license_expression", "name", "revision", "sha256", "source_url"},
        code,
    )
    _name(component["name"], code)
    _immutable_revision(component["revision"], code)
    _sha256_text(component["sha256"], code)
    _https_url(component["source_url"], code)
    if component["license_expression"] not in cast(
        list[str], policy["allowed_candidate_license_expressions"]
    ):
        _fail(code)
    return component


def _aggregate_values(
    values: list[int | str],
    definition: dict[str, object],
) -> tuple[int | str, int | str]:
    if any(value == "UNKNOWN" for value in values):
        return "UNKNOWN", "UNKNOWN"
    integers = cast(list[int], values)
    if definition["aggregation"] == "median":
        aggregate = sorted(integers)[len(integers) // 2]
    elif definition["aggregation"] == "maximum":
        aggregate = max(integers)
    elif definition["aggregation"] == "adverse_mean":
        if any(value < 0 for value in integers):
            _fail("invalid_receipt")
        total = sum(integers)
        if definition["direction"] == "lower_is_better":
            aggregate = (total + len(integers) - 1) // len(integers)
        else:
            aggregate = total // len(integers)
    else:
        _fail("invalid_policy")
    center = sorted(integers)[len(integers) // 2]
    dispersion = sorted(abs(value - center) for value in integers)[len(integers) // 2]
    return aggregate, dispersion


def _expected_aggregate(
    repetitions: list[dict[str, object]],
    definitions: dict[str, dict[str, object]],
) -> dict[str, object]:
    metrics: dict[str, dict[str, int | str]] = {}
    dispersion: dict[str, dict[str, int | str]] = {}
    for name, definition in definitions.items():
        metrics[name] = {}
        dispersion[name] = {}
        for stratum in cast(list[str], definition["strata"]):
            values = [
                cast(dict[str, dict[str, int | str]], repetition["metrics"])[name][stratum]
                for repetition in repetitions
            ]
            aggregate, spread = _aggregate_values(values, definition)
            metrics[name][stratum] = aggregate
            dispersion[name][stratum] = spread
    return {"dispersion": dispersion, "metrics": metrics}


def validate_receipt(
    value: object,
    policy: dict[str, object],
    policy_sha256: str,
    manifest: dict[str, object],
    manifest_sha256: str,
    *,
    code: str = "invalid_receipt",
) -> dict[str, object]:
    receipt = _exact(
        value,
        {
            "aggregate",
            "candidate",
            "dataset_manifest_sha256",
            "evaluated_on",
            "evaluation_phase",
            "experiment",
            "implementation",
            "policy_sha256",
            "policy_version",
            "profile",
            "repetitions",
            "schema",
            "schema_version",
            "split",
            "waiver",
        },
        code,
    )
    if (
        receipt["schema"] != "visualworld.v02-evaluation-receipt"
        or _integer(receipt["schema_version"], code, minimum=1, maximum=1) != 1
        or receipt["policy_version"] != policy["policy_version"]
        or receipt["policy_sha256"] != policy_sha256
        or receipt["dataset_manifest_sha256"] != manifest_sha256
        or receipt["experiment"] != manifest["experiment"]
        or receipt["evaluation_phase"] not in {"regression", "selection"}
        or receipt["split"] != _mapping(policy["protocol"], code)["gate_split"]
    ):
        _fail(code)
    evaluated_on = _day(receipt["evaluated_on"], code)
    experiment_name = cast(str, receipt["experiment"])
    experiment = _mapping(_mapping(policy["experiments"], code)[experiment_name], code)
    definitions = cast(dict[str, dict[str, object]], experiment["metrics"])
    manifest_splits = _mapping(manifest["splits"], code)
    test_item_count = cast(int, _mapping(manifest_splits["test"], code)["item_count"])

    receipt["candidate"] = _validate_candidate(receipt["candidate"], policy, evaluated_on, code)
    implementation = _exact(
        receipt["implementation"],
        {
            "evaluation_harness",
            "executable_implementation",
            "metric_implementation",
            "python_version",
            "source_revision",
        },
        code,
    )
    implementation["evaluation_harness"] = _validate_implementation_component(
        implementation["evaluation_harness"], policy, code
    )
    implementation["metric_implementation"] = _validate_implementation_component(
        implementation["metric_implementation"], policy, code
    )
    revision = _text(implementation["source_revision"], code, maximum=40)
    python_version = _text(implementation["python_version"], code, maximum=32)
    if (
        implementation["executable_implementation"] != "cpython"
        or not _REVISION.fullmatch(revision)
        or re.fullmatch(r"3\.(?:13|14)\.[0-9]+", python_version) is None
    ):
        _fail(code)

    profile_policy = _mapping(policy["profile"], code)
    profile = _exact(
        receipt["profile"],
        {
            "architecture",
            "cpu_model",
            "gpu_present",
            "kernel_release",
            "memory_bytes",
            "name",
            "operating_system",
            "vcpu",
        },
        code,
    )
    if (
        profile["architecture"] != profile_policy["architecture"]
        or profile["gpu_present"] is not False
        or profile["name"] != profile_policy["name"]
        or profile["operating_system"] != profile_policy["operating_system"]
        or profile["vcpu"] != profile_policy["vcpu"]
        or type(profile["vcpu"]) is not int
        or _integer(profile["memory_bytes"], code, minimum=1)
        < cast(int, profile_policy["minimum_memory_bytes"])
    ):
        _fail(code)
    _text(profile["cpu_model"], code, maximum=256)
    _text(profile["kernel_release"], code, maximum=256)

    protocol = _mapping(policy["protocol"], code)
    seeds = cast(list[int], protocol["seed_set"])
    repetitions_value = receipt["repetitions"]
    if not isinstance(repetitions_value, list) or len(repetitions_value) != len(seeds):
        _fail(code)
    repetitions: list[dict[str, object]] = []
    for index, repetition_value in enumerate(repetitions_value):
        repetition = _exact(
            repetition_value,
            {
                "failed_item_count",
                "failure_code",
                "metrics",
                "processed_item_count",
                "seed",
                "status",
            },
            code,
        )
        if repetition["seed"] != seeds[index] or repetition["status"] not in {"failed", "pass"}:
            _fail(code)
        failure_code = repetition["failure_code"]
        failed_items = _integer(repetition["failed_item_count"], code, maximum=test_item_count)
        processed_items = _integer(
            repetition["processed_item_count"], code, maximum=test_item_count
        )
        if failed_items + processed_items != test_item_count:
            _fail(code)
        if repetition["status"] == "pass":
            if failure_code is not None or failed_items != 0 or processed_items != test_item_count:
                _fail(code)
        elif (
            type(failure_code) is not str
            or _FAILURE_CODE.fullmatch(failure_code) is None
            or failed_items == 0
        ):
            _fail(code)
        metric_values = _metric_map(repetition["metrics"], definitions, code)
        for name, definition in definitions.items():
            if definition["source"] == "run_status":
                expected = 0 if repetition["status"] == "pass" else 10_000
                if metric_values[name] != {"overall": expected}:
                    _fail(code)
        repetition["metrics"] = metric_values
        repetitions.append(repetition)
    expected_aggregate = _expected_aggregate(repetitions, definitions)
    aggregate = _exact(receipt["aggregate"], {"dispersion", "metrics"}, code)
    aggregate["metrics"] = _metric_map(aggregate["metrics"], definitions, code)
    aggregate["dispersion"] = _metric_map(aggregate["dispersion"], definitions, code)
    if _canonical(aggregate, code) != _canonical(expected_aggregate, code):
        _fail(code)

    if receipt["waiver"] is not None:
        _mapping(receipt["waiver"], code)
    receipt["repetitions"] = repetitions
    receipt["aggregate"] = aggregate
    return receipt


def load_receipt(
    path: Path,
    policy: dict[str, object],
    policy_sha256: str,
    manifest: dict[str, object],
    manifest_sha256: str,
    *,
    code: str = "invalid_receipt",
) -> tuple[dict[str, object], str]:
    loaded, digest = _read_json(path, code)
    receipt = validate_receipt(
        loaded,
        policy,
        policy_sha256,
        manifest,
        manifest_sha256,
        code=code,
    )
    return receipt, digest


def _absolute_evaluations(
    receipt: dict[str, object],
    definitions: dict[str, dict[str, object]],
) -> tuple[dict[str, object], set[str]]:
    aggregate = _mapping(receipt["aggregate"], "invalid_receipt")
    metrics = cast(dict[str, dict[str, int | str]], aggregate["metrics"])
    evaluations: dict[str, object] = {}
    failures: set[str] = set()
    for name, definition in definitions.items():
        minimum = cast(dict[str, int] | None, definition["minimum"])
        maximum = cast(dict[str, int] | None, definition["maximum"])
        metric_evaluations: dict[str, object] = {}
        for stratum in cast(list[str], definition["strata"]):
            value = metrics[name][stratum]
            passed: bool | None
            if definition["kind"] == "diagnostic":
                passed = None
            elif value == "UNKNOWN":
                passed = False
            else:
                numeric = cast(int, value)
                passed = (minimum is None or numeric >= minimum[stratum]) and (
                    maximum is None or numeric <= maximum[stratum]
                )
            status = "report" if passed is None else ("pass" if passed else "fail")
            metric_evaluations[stratum] = {
                "kind": definition["kind"],
                "maximum": None if maximum is None else maximum[stratum],
                "minimum": None if minimum is None else minimum[stratum],
                "status": status,
                "value": value,
            }
            if passed is False:
                failures.add(f"absolute:{name}:{stratum}")
        evaluations[name] = metric_evaluations
    return evaluations, failures


def _regression_basis_points(baseline: int, candidate: int, direction: str) -> int:
    if baseline < 0 or candidate < 0:
        _fail("invalid_receipt")
    if baseline == 0:
        if candidate == 0:
            return 0
        return 10_001 if direction == "lower_is_better" else -10_001
    difference = candidate - baseline if direction == "lower_is_better" else baseline - candidate
    numerator = difference * 10_000
    if numerator <= 0:
        return -((-numerator) // baseline)
    return (numerator + baseline - 1) // baseline


def _baseline_compatible(
    baseline: dict[str, object],
    candidate: dict[str, object],
) -> bool:
    baseline_implementation = _mapping(baseline["implementation"], "invalid_baseline")
    candidate_implementation = _mapping(candidate["implementation"], "invalid_receipt")
    return (
        baseline["experiment"] == candidate["experiment"]
        and baseline["policy_sha256"] == candidate["policy_sha256"]
        and baseline["dataset_manifest_sha256"] == candidate["dataset_manifest_sha256"]
        and baseline["profile"] == candidate["profile"]
        and baseline_implementation["evaluation_harness"]
        == candidate_implementation["evaluation_harness"]
        and baseline_implementation["metric_implementation"]
        == candidate_implementation["metric_implementation"]
        and baseline_implementation["executable_implementation"]
        == candidate_implementation["executable_implementation"]
        and baseline_implementation["python_version"] == candidate_implementation["python_version"]
    )


def _regression_evaluations(
    baseline: dict[str, object],
    candidate: dict[str, object],
    definitions: dict[str, dict[str, object]],
) -> tuple[dict[str, object], set[str]]:
    baseline_metrics = cast(
        dict[str, dict[str, int | str]],
        _mapping(baseline["aggregate"], "invalid_baseline")["metrics"],
    )
    candidate_metrics = cast(
        dict[str, dict[str, int | str]],
        _mapping(candidate["aggregate"], "invalid_receipt")["metrics"],
    )
    evaluations: dict[str, object] = {}
    failures: set[str] = set()
    for name, definition in definitions.items():
        budget = definition["regression_budget_basis_points"]
        if definition["kind"] != "gate" or budget is None:
            continue
        metric_evaluations: dict[str, object] = {}
        for stratum in cast(list[str], definition["strata"]):
            baseline_value = baseline_metrics[name][stratum]
            candidate_value = candidate_metrics[name][stratum]
            if baseline_value == "UNKNOWN" or candidate_value == "UNKNOWN":
                _fail("invalid_baseline" if baseline_value == "UNKNOWN" else "invalid_receipt")
            regression = _regression_basis_points(
                cast(int, baseline_value),
                cast(int, candidate_value),
                cast(str, definition["direction"]),
            )
            passed = regression <= cast(int, budget)
            metric_evaluations[stratum] = {
                "baseline": baseline_value,
                "budget_basis_points": budget,
                "candidate": candidate_value,
                "regression_basis_points": regression,
                "status": "pass" if passed else "fail",
            }
            if not passed:
                failures.add(f"regression:{name}:{stratum}")
        evaluations[name] = metric_evaluations
    return evaluations, failures


def _validated_waiver(
    value: object,
    policy: dict[str, object],
    evaluated_on: date,
    as_of: date,
    failures: set[str],
) -> dict[str, object] | None:
    if value is None:
        return None
    code = "invalid_waiver"
    if not failures:
        _fail(code)
    waiver = _exact(
        value,
        {
            "compensating_controls",
            "expires_on",
            "follow_up_issue",
            "issued_on",
            "owner",
            "review_trigger",
            "risk",
            "scope",
        },
        code,
    )
    waiver_policy = _mapping(policy["waiver_policy"], code)
    issued_on = _day(waiver["issued_on"], code)
    expires_on = _day(waiver["expires_on"], code)
    controls = _string_list(waiver["compensating_controls"], code)
    scope = _string_list(waiver["scope"], code)
    follow_up = _text(waiver["follow_up_issue"], code)
    if (
        waiver["owner"] != waiver_policy["owner"]
        or len(controls) < cast(int, waiver_policy["required_compensating_controls"])
        or sorted(scope) != sorted(failures)
        or _FOLLOW_UP.fullmatch(follow_up) is None
        or issued_on < _day(policy["frozen_on"], code)
        or issued_on > evaluated_on
        or evaluated_on > as_of
        or expires_on < as_of
        or (expires_on - issued_on).days > cast(int, waiver_policy["maximum_duration_days"])
    ):
        _fail(code)
    _text(waiver["risk"], code)
    _text(waiver["review_trigger"], code)
    return {
        "compensating_controls": controls,
        "expires_on": expires_on.isoformat(),
        "follow_up_issue": follow_up,
        "issued_on": issued_on.isoformat(),
        "owner": waiver["owner"],
        "review_trigger": waiver["review_trigger"],
        "risk": waiver["risk"],
        "scope": sorted(scope),
    }


def evaluate(
    policy: dict[str, object],
    receipt: dict[str, object],
    *,
    baseline: dict[str, object] | None,
    baseline_receipt_sha256: str | None,
    receipt_sha256: str,
    as_of: date,
) -> dict[str, object]:
    _sha256_text(receipt_sha256, "invalid_receipt")
    evaluated_on = _day(receipt["evaluated_on"], "invalid_receipt")
    frozen_on = _day(policy["frozen_on"], "invalid_policy")
    if evaluated_on < frozen_on or evaluated_on > as_of:
        _fail("invalid_receipt")
    experiment_name = cast(str, receipt["experiment"])
    experiment = _mapping(
        _mapping(policy["experiments"], "invalid_policy")[experiment_name],
        "invalid_policy",
    )
    definitions = cast(dict[str, dict[str, object]], experiment["metrics"])
    absolute, absolute_failures = _absolute_evaluations(receipt, definitions)
    phase = receipt["evaluation_phase"]
    regression: dict[str, object] = {}
    regression_failures: set[str] = set()
    compatible: bool | None = None
    if phase == "selection":
        if baseline is not None or baseline_receipt_sha256 is not None:
            _fail("unexpected_baseline")
    else:
        if baseline is None or baseline_receipt_sha256 is None:
            _fail("baseline_required")
        _sha256_text(baseline_receipt_sha256, "invalid_baseline")
        baseline_evaluated_on = _day(baseline["evaluated_on"], "invalid_baseline")
        if baseline_evaluated_on < frozen_on or baseline_evaluated_on > evaluated_on:
            _fail("invalid_baseline")
        baseline_absolute, baseline_failures = _absolute_evaluations(baseline, definitions)
        if (
            baseline_failures
            or baseline["waiver"] is not None
            or baseline["evaluation_phase"] != "selection"
        ):
            _fail("invalid_baseline")
        del baseline_absolute
        compatible = _baseline_compatible(baseline, receipt)
        if not compatible:
            _fail("incompatible_baseline")
        regression, regression_failures = _regression_evaluations(baseline, receipt, definitions)
    failures = absolute_failures | regression_failures
    waiver = _validated_waiver(receipt["waiver"], policy, evaluated_on, as_of, failures)
    candidate = _mapping(receipt["candidate"], "invalid_receipt")
    baseline_candidate: dict[str, object] | None = None
    if baseline is not None:
        baseline_details = _mapping(baseline["candidate"], "invalid_baseline")
        baseline_candidate = {
            "configuration_sha256": baseline_details["configuration_sha256"],
            "evaluated_on": baseline["evaluated_on"],
            "name": baseline_details["name"],
        }
    if not failures:
        status = "pass"
    elif waiver is not None:
        status = "waived"
    else:
        status = "fail"
    return {
        "absolute": absolute,
        "baseline_candidate": baseline_candidate,
        "baseline_receipt_sha256": baseline_receipt_sha256,
        "candidate": {
            "configuration_sha256": candidate["configuration_sha256"],
            "name": candidate["name"],
        },
        "checks": {
            "absolute_gates_pass": not absolute_failures,
            "baseline_compatible": compatible,
            "regression_gates_pass": not regression_failures,
            "waiver_valid": waiver is not None,
        },
        "evaluated_on": receipt["evaluated_on"],
        "evaluation_phase": phase,
        "experiment": experiment_name,
        "failures": sorted(failures),
        "dataset_manifest_sha256": receipt["dataset_manifest_sha256"],
        "implementation": receipt["implementation"],
        "policy_sha256": receipt["policy_sha256"],
        "policy_version": receipt["policy_version"],
        "profile": receipt["profile"],
        "receipt_sha256": receipt_sha256,
        "regression": regression,
        "schema": "visualworld.v02-evaluation-result",
        "schema_version": 1,
        "split": receipt["split"],
        "status": status,
        "waiver": waiver,
    }


def evaluate_paths(
    *,
    policy_path: Path,
    manifest_path: Path,
    receipt_path: Path,
    baseline_path: Path | None,
    as_of: date,
) -> dict[str, object]:
    policy, policy_sha256 = load_policy(policy_path)
    manifest, manifest_sha256 = load_manifest(manifest_path, policy)
    receipt, receipt_sha256 = load_receipt(
        receipt_path,
        policy,
        policy_sha256,
        manifest,
        manifest_sha256,
    )
    baseline: dict[str, object] | None = None
    baseline_receipt_sha256: str | None = None
    if baseline_path is not None:
        try:
            baseline, baseline_receipt_sha256 = load_receipt(
                baseline_path,
                policy,
                policy_sha256,
                manifest,
                manifest_sha256,
                code="invalid_baseline",
            )
        except EvaluationError:
            _fail("invalid_baseline")
    return evaluate(
        policy,
        receipt,
        baseline=baseline,
        baseline_receipt_sha256=baseline_receipt_sha256,
        receipt_sha256=receipt_sha256,
        as_of=as_of,
    )


def _write_new(path: Path, value: object) -> None:
    descriptor = -1
    try:
        if not hasattr(os, "O_NOFOLLOW"):
            _fail("invalid_output")
        data = json.dumps(value, allow_nan=False, indent=2, sort_keys=True).encode("ascii") + b"\n"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                _fail("invalid_output")
            offset += written
        os.fsync(descriptor)
    except EvaluationError:
        raise
    except OSError:
        _fail("invalid_output")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--as-of", type=str)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        as_of = date.today() if arguments.as_of is None else _day(arguments.as_of, "invalid_date")
        result = evaluate_paths(
            policy_path=arguments.policy,
            manifest_path=arguments.manifest,
            receipt_path=arguments.receipt,
            baseline_path=arguments.baseline,
            as_of=as_of,
        )
        _write_new(arguments.output, result)
    except EvaluationError as error:
        print(json.dumps({"code": error.code, "status": "error"}, sort_keys=True))
        return 2
    print(json.dumps({"status": result["status"]}, sort_keys=True))
    return 0 if result["status"] in {"pass", "waived"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
