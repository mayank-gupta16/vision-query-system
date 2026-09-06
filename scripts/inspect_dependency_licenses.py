#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Inspect wheel evidence and check a reviewed development-only license inventory.

This offline checker never installs/imports third-party packages. It binds the
reviewed wheel hashes to uv.lock and compares installed metadata/license payloads.
uv's hash-verified clean sync, not this metadata check, verifies package code.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import platform
import re
import sys
import sysconfig
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LICENSE_NAME = re.compile(
    r"^(?:licen[cs]e|copying|notice|authors)(?:[.\-_]|$)|[.]licen[cs]e(?:[.]|$)", re.I
)


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_member(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError(f"Unsafe evidence path: {name!r}")


def inspect_wheel(data: bytes, expected_sha256: str) -> dict[str, Any]:
    """Read only bounded metadata/license members of an already downloaded wheel."""
    if sha256(data) != expected_sha256:
        raise ValueError("Wheel SHA-256 does not match approved artifact")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate wheel members")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError("Wheel must contain exactly one METADATA file")
        selected = [
            name
            for name in names
            if LICENSE_NAME.search(PurePosixPath(name).name)
            or name.endswith("/vendor.txt")
            or name.endswith(".ABOUT")
            or name == metadata_names[0]
        ]
        if sum(archive.getinfo(name).file_size for name in selected) > 8 * 1024 * 1024:
            raise ValueError("Wheel evidence exceeds 8 MiB inspection limit")
        for name in selected:
            safe_member(name)
        metadata_bytes = archive.read(metadata_names[0])
        metadata = BytesParser().parsebytes(metadata_bytes)
        return {
            "name": canonical_name(str(metadata["Name"])),
            "version": str(metadata["Version"]),
            "metadata": {"path": metadata_names[0], "sha256": sha256(metadata_bytes)},
            "license_expression": metadata.get("License-Expression"),
            "legacy_license": metadata.get("License"),
            "declared_license_files": metadata.get_all("License-File", []),
            "evidence_files": [
                {"path": name, "sha256": sha256(archive.read(name))}
                for name in sorted(selected)
                if name != metadata_names[0] and not name.endswith("/")
            ],
        }


def check_lock(lock: dict[str, Any], inventory: dict[str, Any]) -> None:
    """Reject graph, registry, artifact, or license-review drift."""
    for item in lock["package"]:
        if "registry" not in item["source"] and not (
            item["name"] == "visualworld-engine" and item["source"] == {"editable": "."}
        ):
            raise ValueError(f"Unapproved non-registry dependency: {item['name']}")
    packages = {item["name"]: item for item in lock["package"] if "registry" in item["source"]}
    if len(packages) != sum("registry" in item["source"] for item in lock["package"]):
        raise ValueError("Duplicate locked third-party package")
    graph = json.dumps(list(packages.values()), sort_keys=True, separators=(",", ":")).encode()
    if sha256(graph) != inventory["locked_packages_sha256"]:
        raise ValueError("Locked third-party graph/artifact records changed since license review")
    records = {item["name"]: item for item in inventory["packages"]}
    if len(records) != len(inventory["packages"]) or set(records) != set(packages):
        raise ValueError("Inventory does not match complete third-party lock graph")
    for name, package in packages.items():
        record = records[name]
        if package["source"] != {"registry": "https://pypi.org/simple"}:
            raise ValueError(f"Unapproved package registry: {name}")
        if record["version"] != package["version"] or not record.get("reviewed_license"):
            raise ValueError(f"Unreviewed dependency version/license: {name}")
        approved = {wheel["url"]: wheel["hash"] for wheel in package["wheels"]}
        if not record["wheels"]:
            raise ValueError(f"No reviewed wheel: {name}")
        for wheel in record["wheels"]:
            if approved.get(wheel["url"]) != f"sha256:{wheel['sha256']}":
                raise ValueError(f"Reviewed artifact absent from lock: {name}")
            for evidence in [wheel["metadata"], *wheel["evidence_files"]]:
                safe_member(evidence["path"])
                if not re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"]):
                    raise ValueError(f"Invalid evidence hash: {name}")


def current_lane() -> str:
    runtime = f"{sys.version_info.major}.{sys.version_info.minor}"
    lane = f"{sys.platform}-{platform.machine()}-cpython-{runtime}"
    if sys.implementation.name != "cpython" or sysconfig.get_config_var("Py_GIL_DISABLED"):
        raise ValueError("Only ordinary CPython lanes are approved")
    return lane


def check_environment(inventory: dict[str, Any], lane: str) -> int:
    """Check actual all-groups environment without importing inspected packages."""
    if lane not in inventory["lanes"]:
        raise ValueError(f"Unreviewed platform/runtime lane: {lane}")
    expected = {
        record["name"]: record
        for record in inventory["packages"]
        if any(lane in wheel["lanes"] for wheel in record["wheels"])
    }
    installed = {}
    for distribution in importlib.metadata.distributions():
        name = canonical_name(distribution.metadata["Name"])
        if name == "visualworld-engine":
            continue
        if name in installed:
            raise ValueError(f"Duplicate installed distribution: {name}")
        installed[name] = distribution
    if set(installed) != set(expected):
        missing, extra = set(expected) - set(installed), set(installed) - set(expected)
        raise ValueError(
            f"Installed graph differs: missing={sorted(missing)}, extra={sorted(extra)}"
        )
    prefix = Path(sys.prefix).resolve()
    for name, distribution in installed.items():
        record = expected[name]
        if distribution.version != record["version"]:
            raise ValueError(f"Installed version differs: {name}")
        candidates = [wheel for wheel in record["wheels"] if lane in wheel["lanes"]]
        matches = False
        for wheel in candidates:
            valid = True
            for evidence in [wheel["metadata"], *wheel["evidence_files"]]:
                safe_member(evidence["path"])
                path = Path(str(distribution.locate_file(evidence["path"]))).resolve()
                if not path.is_relative_to(prefix):
                    raise ValueError(f"Evidence escaped active environment: {name}")
                if not path.is_file() or sha256(path.read_bytes()) != evidence["sha256"]:
                    valid = False
                    break
            if valid:
                matches = True
                break
        if not matches:
            raise ValueError(f"Installed metadata/license evidence differs: {name}")
    return len(installed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check lock and current environment")
    parser.add_argument("--lock", type=Path, default=ROOT / "uv.lock")
    parser.add_argument(
        "--inventory", type=Path, default=ROOT / "docs/legal/development-tool-inventory.json"
    )
    parser.add_argument("--inspect-wheel", type=Path, help="Print evidence for a downloaded wheel")
    parser.add_argument("--sha256", help="Required approved wheel digest for inspection")
    args = parser.parse_args(argv)
    try:
        if args.inspect_wheel is not None:
            if args.check or args.sha256 is None:
                parser.error("--inspect-wheel requires --sha256 and cannot use --check")
            print(json.dumps(inspect_wheel(args.inspect_wheel.read_bytes(), args.sha256), indent=2))
        elif args.check:
            inventory = json.loads(args.inventory.read_text())
            check_lock(tomllib.loads(args.lock.read_text()), inventory)
            count = check_environment(inventory, current_lane())
            print(f"Development license evidence verified: {count} installed third-party packages")
        else:
            parser.error("Choose --check or --inspect-wheel")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"Dependency inventory check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
