#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run issue #17's deterministic CLI vertical-slice acceptance on CPU-LITE."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
_EXPECTED_CROP = bytes((3, 4, 5, 9, 10, 11))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _memory_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def _child_peak_rss_bytes() -> int:
    try:
        value = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
        return value if platform.system() == "Darwin" else value * 1024
    except (OSError, ValueError):
        return 0


def _disk_bytes(root: Path) -> int:
    total = 0
    for directory, _, names in os.walk(root):
        for name in names:
            metadata = (Path(directory) / name).stat(follow_symlinks=False)
            if metadata.st_mode & 0o170000 == 0o100000:
                total += metadata.st_size
    return total


def _invoke(
    arguments: list[str],
    *,
    environment: dict[str, str],
    expected_exit: int,
) -> tuple[dict[str, object], dict[str, int], str]:
    started = time.perf_counter_ns()
    completed = subprocess.run(
        [sys.executable, "-m", "visualworld", *arguments],
        cwd=ROOT,
        env=environment,
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
    )
    wall_ns = max(1, time.perf_counter_ns() - started)
    selected = completed.stdout if expected_exit == 0 else completed.stderr
    other = completed.stderr if expected_exit == 0 else completed.stdout
    if completed.returncode != expected_exit or other or selected.count("\n") != 1:
        raise ValueError("CLI command contract failed")
    try:
        document = json.loads(selected)
    except (TypeError, ValueError) as error:
        raise ValueError("CLI command output was not JSON") from error
    if not isinstance(document, dict):
        raise ValueError("CLI command output was not an object")
    return (
        cast(dict[str, object], document),
        {
            "stdout_bytes": len(completed.stdout.encode("utf-8")),
            "stderr_bytes": len(completed.stderr.encode("utf-8")),
            "wall_ns": wall_ns,
        },
        completed.stdout + completed.stderr,
    )


def _result(document: dict[str, object]) -> dict[str, object]:
    value = document.get("result")
    if not isinstance(value, dict):
        raise ValueError("CLI result payload was invalid")
    return cast(dict[str, object], value)


def _run(work_root: Path) -> dict[str, object]:
    cpu_count = os.cpu_count() or 0
    memory_bytes = _memory_bytes()
    profile_ok = (
        platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and cpu_count >= 4
        and memory_bytes >= 16_000_000_000
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = "src"
    measurements: dict[str, dict[str, int]] = {}
    rendered: list[str] = []

    with tempfile.TemporaryDirectory(prefix="visualworld-cli-", dir=work_root) as temporary:
        private_root = Path(temporary)
        store = private_root / "store"
        output = private_root / "crop.rgb24"

        probe, measurements["probe"], text = _invoke(
            ["probe"], environment=environment, expected_exit=0
        )
        rendered.append(text)
        ingest, measurements["ingest"], text = _invoke(
            ["ingest", "--store", os.fspath(store)],
            environment=environment,
            expected_exit=0,
        )
        rendered.append(text)
        ingest_result = _result(ingest)
        run_id = ingest_result.get("run_id")
        evidence_ids = ingest_result.get("evidence_ids")
        if (
            not isinstance(run_id, str)
            or not isinstance(evidence_ids, list)
            or len(evidence_ids) != 1
            or not isinstance(evidence_ids[0], str)
        ):
            raise ValueError("CLI ingest identifiers were invalid")
        inspect_run, measurements["inspect_run"], text = _invoke(
            ["inspect-run", "--store", os.fspath(store), "--run-id", run_id],
            environment=environment,
            expected_exit=0,
        )
        rendered.append(text)
        list_samples, measurements["list_samples"], text = _invoke(
            ["list-samples", "--store", os.fspath(store), "--run-id", run_id],
            environment=environment,
            expected_exit=0,
        )
        rendered.append(text)
        show_evidence, measurements["show_evidence"], text = _invoke(
            [
                "show-evidence",
                "--store",
                os.fspath(store),
                "--evidence-id",
                evidence_ids[0],
                "--output",
                os.fspath(output),
            ],
            environment=environment,
            expected_exit=0,
        )
        rendered.append(text)
        usage_error, measurements["usage_error"], text = _invoke(
            ["probe", "--\x1b[31muntrusted"],
            environment=environment,
            expected_exit=2,
        )
        rendered.append(text)

        crop = output.read_bytes()
        store_disk_bytes = _disk_bytes(store)
        paths_redacted = os.fspath(private_root) not in "".join(rendered)

    documents = (probe, ingest, inspect_run, list_samples, show_evidence)
    success_contract = all(
        document.get("schema") == "visualworld.cli-result"
        and document.get("schema_version") == 1
        and document.get("status") == "ok"
        for document in documents
    )
    error = usage_error.get("error")
    error_contract = (
        usage_error.get("schema") == "visualworld.cli-result"
        and usage_error.get("schema_version") == 1
        and usage_error.get("status") == "error"
        and isinstance(error, dict)
        and error.get("code") == "usage_error"
        and "\x1b" not in "".join(rendered)
    )
    checks = {
        "canonical_success_contract": success_contract,
        "exact_original_pixel_crop": crop == _EXPECTED_CROP,
        "machine_readable_error_contract": error_contract,
        "paths_and_terminal_control_redacted": paths_redacted,
        "vertical_commands_completed": tuple(document.get("command") for document in documents)
        == ("probe", "ingest", "inspect-run", "list-samples", "show-evidence"),
    }
    peak_rss = _child_peak_rss_bytes()
    total_wall_ns = sum(item["wall_ns"] for item in measurements.values())
    resource_checks = {
        "disk_bounded": store_disk_bytes <= 16 * 1024 * 1024,
        "per_command_wall_bounded": all(
            0 < item["wall_ns"] <= 10_000_000_000 for item in measurements.values()
        ),
        "rss_bounded": 0 < peak_rss <= 512 * 1024 * 1024,
        "timing_measured": total_wall_ns > 0,
    }
    passed = profile_ok and all(checks.values()) and all(resource_checks.values())
    return {
        "schema": "visualworld.cli-acceptance-receipt",
        "schema_version": 1,
        "status": "pass" if passed else "fail",
        "implementation": {
            "cli_module_sha256": _sha256(ROOT / "src/visualworld/cli.py"),
            "executable_implementation": sys.implementation.name,
            "harness_sha256": _sha256(Path(__file__)),
            "python_version": platform.python_version(),
            "world_store_module_sha256": _sha256(ROOT / "src/visualworld/world_store.py"),
        },
        "profile": {
            "cpu_count": cpu_count,
            "gpu_required": False,
            "machine": platform.machine(),
            "meets_requirements": profile_ok,
            "memory_bytes": memory_bytes,
            "name": "CPU-LITE",
            "os": platform.platform(),
            "required_memory_bytes": 16_000_000_000,
            "required_vcpu": 4,
        },
        "workload": {
            "command_count": len(measurements),
            "crop_rgb24_bytes": len(_EXPECTED_CROP),
            "deterministic_synthetic_input": True,
            "sample_count": 1,
        },
        "checks": checks,
        "resources": {
            **resource_checks,
            "child_process_peak_rss_bytes": peak_rss,
            "peak_rss_limit_bytes": 512 * 1024 * 1024,
            "store_logical_bytes": store_disk_bytes,
            "store_logical_limit_bytes": 16 * 1024 * 1024,
            "total_command_wall_ns": total_wall_ns,
        },
        "measurements": measurements,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = _run(arguments.work_root.resolve(strict=True))
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": result["status"]}, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
