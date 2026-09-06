# SPDX-License-Identifier: Apache-2.0
"""Offline regression tests for development-artifact license evidence checks."""

from __future__ import annotations

import copy
import importlib.metadata
import io
import json
import sys
import sysconfig
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import inspect_dependency_licenses as inventory
import pytest


def make_wheel(extra: dict[str, bytes] | None = None) -> bytes:
    stream = io.BytesIO()
    files = {
        "example-1.0.dist-info/METADATA": b"Name: example\nVersion: 1.0\nLicense-Expression: MIT\n",
        "example-1.0.dist-info/licenses/mit.LICENSE": b"license evidence",
        "example/module.py": b"raise RuntimeError('must never execute')",
    }
    files.update(extra or {})
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


def test_inspects_hash_verified_metadata_and_suffix_license_without_execution() -> None:
    wheel = make_wheel()
    record = inventory.inspect_wheel(wheel, inventory.sha256(wheel))
    assert record["name"] == "example"
    assert record["license_expression"] == "MIT"
    assert record["evidence_files"] == [
        {
            "path": "example-1.0.dist-info/licenses/mit.LICENSE",
            "sha256": inventory.sha256(b"license evidence"),
        }
    ]


def test_rejects_wrong_wheel_digest() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        inventory.inspect_wheel(make_wheel(), "0" * 64)


@pytest.mark.parametrize("path", ["../LICENSE", "/LICENSE", "example/../../NOTICE", "..\\LICENSE"])
def test_rejects_unsafe_evidence_paths(path: str) -> None:
    with pytest.raises(ValueError, match="Unsafe"):
        inventory.safe_member(path)


def test_rejects_duplicate_members() -> None:
    wheel = make_wheel()
    stream = io.BytesIO(wheel)
    with zipfile.ZipFile(stream, "a") as archive, pytest.warns(UserWarning):
        archive.writestr("example-1.0.dist-info/METADATA", b"duplicate")
    data = stream.getvalue()
    with pytest.raises(ValueError, match="Duplicate"):
        inventory.inspect_wheel(data, inventory.sha256(data))


def test_rejects_multiple_metadata_files() -> None:
    data = make_wheel({"other-1.0.dist-info/METADATA": b"Name: other\nVersion: 1.0"})
    with pytest.raises(ValueError, match="exactly one"):
        inventory.inspect_wheel(data, inventory.sha256(data))


def test_rejects_excessive_license_payload() -> None:
    data = make_wheel({"example/LICENSE": b"x" * (8 * 1024 * 1024)})
    with pytest.raises(ValueError, match="inspection limit"):
        inventory.inspect_wheel(data, inventory.sha256(data))


def reviewed_data() -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(__file__).resolve().parents[1]
    lock = tomllib.loads((root / "uv.lock").read_text())
    evidence = json.loads((root / "docs/legal/development-tool-inventory.json").read_text())
    return lock, evidence


def test_complete_actual_lock_matches_reviewed_inventory() -> None:
    lock, evidence = reviewed_data()
    inventory.check_lock(lock, evidence)
    assert len(evidence["packages"]) == 40
    assert sum(len(package["wheels"]) for package in evidence["packages"]) == 60


@pytest.mark.parametrize("change", ["version", "wheel_hash", "source"])
def test_rejects_lock_drift(change: str) -> None:
    lock, evidence = reviewed_data()
    package = lock["package"][0]
    if change == "version":
        package["version"] = "999.0"
    elif change == "wheel_hash":
        package["wheels"][0]["hash"] = "sha256:" + "0" * 64
    else:
        package["source"] = {"url": "https://unapproved.example/tool.whl"}
    with pytest.raises(ValueError):
        inventory.check_lock(lock, evidence)


def test_rejects_additional_direct_url_package() -> None:
    lock, evidence = reviewed_data()
    lock["package"].append({"name": "intruder", "source": {"url": "https://example.com/a.whl"}})
    with pytest.raises(ValueError, match="non-registry"):
        inventory.check_lock(lock, evidence)


def test_rejects_duplicate_locked_package() -> None:
    lock, evidence = reviewed_data()
    lock["package"].append(copy.deepcopy(lock["package"][0]))
    with pytest.raises(ValueError, match="Duplicate locked"):
        inventory.check_lock(lock, evidence)


def test_rejects_free_threaded_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sysconfig, "get_config_var", lambda _name: 1)
    with pytest.raises(ValueError, match="ordinary CPython"):
        inventory.current_lane()


@pytest.mark.parametrize("change", ["missing_package", "duplicate", "license", "artifact", "path"])
def test_rejects_incomplete_or_unsafe_review(change: str) -> None:
    lock, evidence = reviewed_data()
    package = evidence["packages"][0]
    if change == "missing_package":
        evidence["packages"].pop()
    elif change == "duplicate":
        evidence["packages"].append(copy.deepcopy(package))
    elif change == "license":
        package["reviewed_license"] = ""
    elif change == "artifact":
        package["wheels"][0]["sha256"] = "0" * 64
    else:
        package["wheels"][0]["metadata"]["path"] = "../METADATA"
    with pytest.raises(ValueError):
        inventory.check_lock(lock, evidence)


class FakeDistribution:
    def __init__(self, root: Path, version: str = "1.0") -> None:
        self.root = root
        self.version = version
        self.metadata = {"Name": "example"}

    def locate_file(self, path: str) -> Path:
        return self.root / path


def environment_fixture(tmp_path: Path) -> dict[str, Any]:
    metadata = tmp_path / "example-1.0.dist-info/METADATA"
    metadata.parent.mkdir()
    metadata.write_bytes(b"metadata")
    license_file = tmp_path / "example-1.0.dist-info/LICENSE"
    license_file.write_bytes(b"license")
    return {
        "lanes": ["test"],
        "packages": [
            {
                "name": "example",
                "version": "1.0",
                "wheels": [
                    {
                        "lanes": ["test"],
                        "metadata": {
                            "path": "example-1.0.dist-info/METADATA",
                            "sha256": inventory.sha256(b"metadata"),
                        },
                        "evidence_files": [
                            {
                                "path": "example-1.0.dist-info/LICENSE",
                                "sha256": inventory.sha256(b"license"),
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_actual_environment_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = environment_fixture(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: [FakeDistribution(tmp_path)])
    assert inventory.check_environment(evidence, "test") == 1
    (tmp_path / "example-1.0.dist-info/LICENSE").write_bytes(b"changed")
    with pytest.raises(ValueError, match="evidence differs"):
        inventory.check_environment(evidence, "test")


def test_rejects_unreviewed_lane() -> None:
    with pytest.raises(ValueError, match="Unreviewed platform"):
        inventory.check_environment({"lanes": []}, "unapproved")


def test_rejects_environment_graph_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = environment_fixture(tmp_path)
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: [])
    with pytest.raises(ValueError, match="Installed graph differs"):
        inventory.check_environment(evidence, "test")


def test_rejects_evidence_outside_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = environment_fixture(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "other-environment"))
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: [FakeDistribution(tmp_path)])
    with pytest.raises(ValueError, match="escaped active environment"):
        inventory.check_environment(evidence, "test")


def test_cli_inspection_and_invalid_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "example.whl"
    path.write_bytes(make_wheel())
    assert (
        inventory.main(
            ["--inspect-wheel", str(path), "--sha256", inventory.sha256(path.read_bytes())]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["name"] == "example"
    assert inventory.main(["--inspect-wheel", str(path), "--sha256", "0" * 64]) == 1
    assert "SHA-256" in capsys.readouterr().err
