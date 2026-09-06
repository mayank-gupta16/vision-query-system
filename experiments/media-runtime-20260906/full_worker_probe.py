# SPDX-License-Identifier: Apache-2.0
"""Replay one real decode inside the measured systemd and namespace boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from inspect_records import inspect
from isolation_probe import SYSTEMD_PROPERTIES, namespace_argv

SOURCE_DESCRIPTOR = 3
SOURCE_DESCRIPTOR_NAME = "visualworld-source"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _summary(stderr: bytes) -> dict[str, Any] | None:
    for line in stderr.decode("utf-8", errors="replace").splitlines():
        if line.startswith("{"):
            value = json.loads(line)
            if isinstance(value, dict):
                return value
    return None


def _run(args: argparse.Namespace) -> dict[str, Any]:
    runtime = args.runtime.resolve(strict=True)
    source = args.source.resolve(strict=True)
    records = args.records.resolve()
    if ":" in str(source) or "\n" in str(source):
        raise ValueError("source path cannot be represented by systemd OpenFile")

    program = ["/runtime/python/bin/python3.13", "/runtime/worker_probe.py"]
    namespace = namespace_argv(runtime, program)
    open_file = f"OpenFile={source}:{SOURCE_DESCRIPTOR_NAME}:read-only"
    unit = f"visualworld-full-worker-probe-{args.unit_suffix}"
    systemd_argv = [
        "/usr/bin/systemd-run",
        "--quiet",
        "--pipe",
        "--wait",
        "--collect",
        f"--unit={unit}",
        *(f"--property={item}" for item in SYSTEMD_PROPERTIES),
        f"--property={open_file}",
        *namespace,
    ]

    started = time.monotonic()
    completed = subprocess.run(
        systemd_argv,
        check=False,
        capture_output=True,
        timeout=25,
    )
    wall_seconds = time.monotonic() - started
    records.parent.mkdir(parents=True, exist_ok=True)
    records.write_bytes(completed.stdout)

    inspected: dict[str, Any] | None = None
    if completed.returncode == 0:
        inspected = inspect(records)
    worker_summary = _summary(completed.stderr)
    status = (
        "pass"
        if completed.returncode == 0
        and inspected is not None
        and inspected["status"] == "ok"
        and worker_summary is not None
        and worker_summary.get("status") == "ok"
        else "fail"
    )
    return {
        "schema_version": 1,
        "status": status,
        "source": {
            "path": str(source),
            "bytes": source.stat().st_size,
            "sha256": _sha256(source),
        },
        "descriptor_mapping": {
            "fd": SOURCE_DESCRIPTOR,
            "name": SOURCE_DESCRIPTOR_NAME,
            "access": "read-only",
            "systemd_property": open_file,
            "bubblewrap_handling": "inherited descriptor 3",
        },
        "systemd_properties": [*SYSTEMD_PROPERTIES, open_file],
        "systemd_run_argv": systemd_argv,
        "namespace_argv": namespace,
        "returncode": completed.returncode,
        "wall_seconds": wall_seconds,
        "stdout_bytes": len(completed.stdout),
        "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "stderr_bytes": len(completed.stderr),
        "stderr": completed.stderr.decode("utf-8", errors="replace").strip(),
        "worker_summary": worker_summary,
        "records": inspected,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--unit-suffix", default="manual")
    args = parser.parse_args()
    result = _run(args)
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
