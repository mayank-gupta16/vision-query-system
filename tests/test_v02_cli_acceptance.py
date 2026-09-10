# SPDX-License-Identifier: Apache-2.0
"""Fresh-process deterministic acceptance for the issue-77 CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_v02_cli_acceptance_is_redacted_and_explicitly_non_native(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = "src"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_v02_cli_acceptance.py",
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

    assert completed.returncode == 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["schema"] == "visualworld.perception-cli-acceptance-receipt"
    assert receipt["schema_version"] == 1
    assert receipt["status"] == "pass"
    assert receipt["native_linux_runtime_executed"] is False
    assert receipt["workload"] == {
        "command_count": 23,
        "deterministic_adapter_seam": True,
        "network_allowed": False,
        "runtime_failure_boundary_seam": True,
    }
    assert all(receipt["checks"].values())
    assert all(receipt["resources"].values())
    rendered = output.read_text(encoding="utf-8") + completed.stdout + completed.stderr
    assert os.fspath(tmp_path) not in rendered
