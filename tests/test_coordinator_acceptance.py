# SPDX-License-Identifier: Apache-2.0
"""Tests for issue #16's CPU-LITE coordinator acceptance receipt."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def test_coordinator_acceptance_emits_redacted_machine_receipt(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_coordinator_acceptance.py",
            "--work-root",
            os.fspath(tmp_path),
            "--output",
            os.fspath(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert completed.returncode in {0, 1}
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["schema"] == "visualworld.coordinator-acceptance-receipt"
    assert receipt["workload"]["sample_count"] == 1
    assert all(receipt["checks"].values())
    assert all(
        value
        for name, value in receipt["resources"].items()
        if name.endswith(("_measured", "_bounded"))
    )
    assert receipt["measurements"]
    rendered = output.read_text(encoding="utf-8") + completed.stdout + completed.stderr
    assert os.fspath(tmp_path) not in rendered
    assert re.search(r"(?:src|frm|evi|run)_[0-9a-f]{64}", rendered) is None
