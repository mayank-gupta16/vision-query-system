#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check release contents and the fresh installed runtime, without third-party tools."""

from __future__ import annotations

import argparse
import email.policy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]


def inspect_wheel(wheel: Path) -> tuple[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1 or len(names) != len(set(names)):
            raise ValueError("Ambiguous wheel metadata or duplicate entries")
        metadata = BytesParser(policy=email.policy.default).parsebytes(
            archive.read(metadata_names[0])
        )
        name, version = str(metadata["Name"]), str(metadata["Version"])
        if name != "visualworld-engine" or not version or version == "None":
            raise ValueError("Unexpected distribution identity")
        if metadata.get_all("Requires-Dist"):
            raise ValueError("Application runtime must have zero third-party dependencies")
        if metadata["License-Expression"] != "Apache-2.0":
            raise ValueError("Incorrect license expression")
        if metadata.get_all("License-File") != ["LICENSE"]:
            raise ValueError("Incorrect license-file metadata")
        info = metadata_names[0].rsplit("/", 1)[0]
        expected = {
            "visualworld/__init__.py",
            "visualworld/__main__.py",
            "visualworld/cli.py",
            "visualworld/coordinator.py",
            "visualworld/detection.py",
            "visualworld/evidence.py",
            "visualworld/experimental.py",
            "visualworld/frame_access.py",
            "visualworld/geometry.py",
            "visualworld/ingestion.py",
            "visualworld/media.py",
            "visualworld/original_frame_runtime.py",
            "visualworld/perception.py",
            "visualworld/perception_cli.py",
            "visualworld/perception_materialization.py",
            "visualworld/ports.py",
            "visualworld/sampling.py",
            "visualworld/storage.py",
            "visualworld/tracking.py",
            "visualworld/world_store.py",
            "visualworld/world_store_v2.py",
            "visualworld/py.typed",
            f"{info}/METADATA",
            f"{info}/WHEEL",
            f"{info}/RECORD",
            f"{info}/entry_points.txt",
            f"{info}/licenses/LICENSE",
        }
        allowed_directories = {"visualworld/", f"{info}/", f"{info}/licenses/"}
        actual_files = set(names) - allowed_directories
        if actual_files != expected:
            raise ValueError(f"Unexpected wheel contents: {sorted(actual_files ^ expected)}")
        if archive.read(f"{info}/licenses/LICENSE") != (ROOT / "LICENSE").read_bytes():
            raise ValueError("Wheel license differs from reviewed project license")
    return name, version


def check(wheel: Path, uv: Path) -> None:
    name, version = inspect_wheel(wheel)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    with tempfile.TemporaryDirectory(prefix="visualworld-wheel-") as temporary:
        directory = Path(temporary)
        runtime = directory / "runtime"
        subprocess.run(
            [str(uv), "venv", "--python", sys.executable, str(runtime)],
            cwd=directory,
            env=env,
            check=True,
            timeout=60,
        )
        python = runtime / "bin/python"
        subprocess.run(
            [
                str(uv),
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                "--offline",
                str(wheel),
            ],
            cwd=directory,
            env=env,
            check=True,
            timeout=60,
        )
        probe = (
            "import importlib.metadata as m,json,visualworld,visualworld.detection,"
            "visualworld.evidence,visualworld.experimental,visualworld.frame_access,"
            "visualworld.original_frame_runtime,visualworld.perception_cli,"
            "visualworld.perception_materialization,"
            "visualworld.tracking; "
            "print(json.dumps({'packages': sorted((d.metadata['Name'], d.version) "
            "for d in m.distributions()), 'version': visualworld.__version__, "
            "'file': visualworld.__file__}))"
        )
        result = subprocess.run(
            [str(python), "-I", "-c", probe],
            cwd=directory,
            env=env,
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        if data["packages"] != [[name, version]] or data["version"] != version:
            raise ValueError("Installed wheel metadata or runtime dependency mismatch")
        if not Path(data["file"]).resolve().is_relative_to(runtime.resolve()):
            raise ValueError("Smoke test imported from outside the fresh runtime")
        for prefix in [
            [str(python), "-I", "-m", "visualworld"],
            [str(runtime / "bin/visualworld")],
        ]:
            for option, expected in [
                ("--version", f"visualworld {version}"),
                ("--help", "usage: visualworld"),
            ]:
                result = subprocess.run(
                    [*prefix, option],
                    cwd=directory,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
                if expected not in result.stdout or result.stderr:
                    raise ValueError("Installed CLI smoke check failed")
            result = subprocess.run(
                [*prefix, "probe"],
                cwd=directory,
                env=env,
                text=True,
                capture_output=True,
                check=True,
                timeout=30,
            )
            try:
                loaded = json.loads(result.stdout)
            except (TypeError, ValueError) as error:
                raise ValueError("Installed CLI probe was not JSON") from error
            if not isinstance(loaded, dict):
                raise ValueError("Installed CLI probe was not an object")
            document = cast(dict[str, object], loaded)
            if (
                result.stderr
                or document.get("schema") != "visualworld.cli-result"
                or document.get("schema_version") != 1
                or document.get("command") != "probe"
                or document.get("status") != "ok"
            ):
                raise ValueError("Installed CLI probe smoke check failed")
            result = subprocess.run(
                [*prefix, "perception", "--help"],
                cwd=directory,
                env=env,
                text=True,
                capture_output=True,
                check=True,
                timeout=30,
            )
            if "usage: visualworld perception" not in result.stdout or result.stderr:
                raise ValueError("Installed perception CLI smoke check failed")

        console = runtime / "bin/visualworld"
        store = directory / "quickstart-store"
        output = directory / "quickstart-crop.rgb24"

        def run_json(arguments: list[str]) -> dict[str, object]:
            result = subprocess.run(
                [str(console), *arguments],
                cwd=directory,
                env=env,
                text=True,
                capture_output=True,
                check=True,
                timeout=30,
            )
            if result.stderr or str(directory) in result.stdout:
                raise ValueError("Installed CLI output was not redacted")
            try:
                loaded = json.loads(result.stdout)
            except (TypeError, ValueError) as error:
                raise ValueError("Installed CLI output was not JSON") from error
            if not isinstance(loaded, dict):
                raise ValueError("Installed CLI output was not an object")
            document = cast(dict[str, object], loaded)
            if (
                document.get("schema") != "visualworld.cli-result"
                or document.get("schema_version") != 1
                or document.get("status") != "ok"
            ):
                raise ValueError("Installed CLI vertical smoke check failed")
            return document

        ingested = run_json(["ingest", "--store", str(store)])
        ingestion_result = ingested.get("result")
        if not isinstance(ingestion_result, dict):
            raise ValueError("Installed CLI ingest result was invalid")
        run_id = ingestion_result.get("run_id")
        evidence_ids = ingestion_result.get("evidence_ids")
        if (
            not isinstance(run_id, str)
            or not isinstance(evidence_ids, list)
            or len(evidence_ids) != 1
            or not isinstance(evidence_ids[0], str)
        ):
            raise ValueError("Installed CLI ingest identifiers were invalid")
        inspected = run_json(["inspect-run", "--store", str(store), "--run-id", run_id])
        listed = run_json(["list-samples", "--store", str(store), "--run-id", run_id])
        shown = run_json(
            [
                "show-evidence",
                "--store",
                str(store),
                "--evidence-id",
                evidence_ids[0],
                "--output",
                str(output),
            ]
        )
        if (
            inspected.get("command") != "inspect-run"
            or listed.get("command") != "list-samples"
            or shown.get("command") != "show-evidence"
            or output.read_bytes() != bytes((3, 4, 5, 9, 10, 11))
        ):
            raise ValueError("Installed CLI exact-crop quickstart failed")
    ref = f"pkg:pypi/{name}@{version}"
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": ref,
                "name": name,
                "version": version,
                "purl": ref,
                "licenses": [{"license": {"id": "Apache-2.0"}}],
                "hashes": [
                    {"alg": "SHA-256", "content": hashlib.sha256(wheel.read_bytes()).hexdigest()}
                ],
            }
        },
        "components": [],
        "dependencies": [{"ref": ref, "dependsOn": []}],
    }
    wheel.with_suffix(".runtime.cdx.json").write_text(
        json.dumps(sbom, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "Fresh installed wheel: import, module, console, exact-crop CLI, license, "
        "perception help, zero runtime dependencies passed."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    args = parser.parse_args()
    check(args.wheel.resolve(), args.uv.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
