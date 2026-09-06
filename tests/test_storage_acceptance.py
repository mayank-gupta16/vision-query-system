# SPDX-License-Identifier: Apache-2.0
"""Tests for the local EvidenceStore CPU-LITE acceptance harness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_storage_acceptance_emits_redacted_machine_readable_receipt(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_storage_acceptance.py",
            "--work-root",
            os.fspath(tmp_path),
            "--output",
            os.fspath(output),
            "--payload-bytes",
            "4096",
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
    assert receipt["schema"] == "visualworld.storage-acceptance-receipt"
    assert receipt["workload"]["payload_bytes"] == 4096
    assert all(receipt["checks"].values())
    assert all(receipt["resources"].values())
    rendered = output.read_text(encoding="utf-8") + completed.stdout + completed.stderr
    assert os.fspath(tmp_path) not in rendered
    assert hashlib_sha256(bytes(range(256)) * 16) not in rendered


def hashlib_sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()
