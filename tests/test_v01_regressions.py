# SPDX-License-Identifier: Apache-2.0
"""Release-gate tests for v0.1 end-to-end and hostile-input regressions."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
import run_v01_regressions as regressions


def test_fixture_manifest_is_exact_rights_safe_and_privacy_safe() -> None:
    manifest = regressions.load_manifest()
    rights = cast(dict[str, object], manifest["rights"])
    privacy = cast(dict[str, object], manifest["privacy"])

    assert rights["license_expression"] == "Apache-2.0"
    assert rights["external_sources"] == []
    assert rights["redistribution_allowed"] is True
    assert rights["derivatives_allowed"] is True
    assert set(cast(list[str], rights["allowed_uses"])) == {
        "benchmarking",
        "modification",
        "redistribution",
        "testing",
    }
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


def test_manifest_validation_rejects_rights_or_case_drift() -> None:
    manifest = regressions.load_manifest()
    changed_rights = copy.deepcopy(manifest)
    cast(dict[str, object], changed_rights["rights"])["external_sources"] = ["unknown"]
    with pytest.raises(ValueError, match=r"invalid v0\.1 regression manifest"):
        regressions._validate_manifest(changed_rights)

    changed_cases = copy.deepcopy(manifest)
    cast(list[str], changed_cases["hostile_cases"]).pop()
    with pytest.raises(ValueError, match=r"invalid v0\.1 regression manifest"):
        regressions._validate_manifest(changed_cases)


def test_generated_fixture_is_bound_to_public_probe_golden() -> None:
    manifest = regressions.load_manifest()
    golden = cast(dict[str, object], manifest["golden"])
    source, frames, pixels = regressions.generated_fixture()

    assert source.source_id == golden["source_id"]
    assert frames[1].frame_id == golden["frame_id"]
    assert pixels == bytes(range(12))
    assert (
        hashlib.sha256(regressions.PROBE_GOLDEN_PATH.read_bytes()).hexdigest()
        == golden["probe_golden_sha256"]
    )


def test_v01_regression_harness_passes_and_emits_only_redacted_aggregates(
    tmp_path: Path,
) -> None:
    output = tmp_path / "receipt.json"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = "src"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_v01_regressions.py",
            "--work-root",
            os.fspath(tmp_path),
            "--output",
            os.fspath(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert completed.stderr == ""
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["schema"] == "visualworld.v01-regression-receipt"
    assert receipt["workload"] == {
        "candidate_frame_count": 2,
        "crop_rgb24_bytes": 6,
        "hostile_case_count": 8,
        "perception_dataset_used": False,
        "sample_count": 1,
        "scenario_count": 3,
        "synthetic_fixture_generated_at_test_time": True,
        "temporary_outputs_retained": False,
    }
    assert all(receipt["checks"].values())
    assert all(
        value
        for name, value in receipt["resources"].items()
        if name.endswith(("_measured", "_bounded"))
    )
    assert receipt["measurements"]
    expected_status = "pass" if receipt["profile"]["meets_requirements"] else "fail"
    expected_returncode = 0 if expected_status == "pass" else 1
    assert completed.returncode == expected_returncode
    assert receipt["status"] == expected_status
    assert json.loads(completed.stdout) == {"status": expected_status}

    rendered = output.read_text(encoding="utf-8") + completed.stdout + completed.stderr
    assert os.fspath(tmp_path) not in rendered
    assert "\x1b" not in rendered
    assert "example.invalid" not in rendered
    assert "touch" not in rendered
    assert repr(bytes(range(12))) not in rendered
    assert re.search(r"(?:src|frm|evi|run|del)_[0-9a-f]{64}", rendered) is None
