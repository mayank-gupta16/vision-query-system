# SPDX-License-Identifier: Apache-2.0
"""Offline policy tests; downloads and child processes are always replaced."""

import hashlib
import io
import json
import platform
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import NoReturn

import bootstrap
import check_wheel
import dev
import pytest


@pytest.fixture(autouse=True)
def forbid_external_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Tooling unit tests must not download or launch processes")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


@pytest.mark.parametrize("condition", ["valid", "mismatch", "missing", "symlink"])
def test_verify_rejects_untrusted_files(tmp_path: Path, condition: str) -> None:
    candidate = tmp_path / "candidate"
    content = b"reviewed bytes"
    expected = hashlib.sha256(content).hexdigest()
    if condition == "symlink":
        target = tmp_path / "target"
        target.write_bytes(content)
        candidate.symlink_to(target)
    elif condition != "missing":
        candidate.write_bytes(content if condition == "valid" else b"changed")
    if condition == "valid":
        bootstrap.verify(candidate, expected)
    else:
        with pytest.raises(ValueError, match="checksum-mismatched"):
            bootstrap.verify(candidate, expected)


def test_unsupported_host_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    with pytest.raises(ValueError, match="Supported hosts"):
        bootstrap.platform_spec()


@pytest.mark.parametrize("host", [("Linux", "x86_64"), ("Darwin", "arm64")])
@pytest.mark.parametrize("version", ["3.13.15", "3.14.7"])
def test_download_records_are_fixed(
    monkeypatch: pytest.MonkeyPatch, host: tuple[str, str], version: str
) -> None:
    monkeypatch.setattr(platform, "system", lambda: host[0])
    monkeypatch.setattr(platform, "machine", lambda: host[1])
    spec = bootstrap.PLATFORMS[host]
    key, record = bootstrap.download_record(version)
    assert key == f"cpython-{version}-{spec[3]}-{spec[4]}-{spec[5]}"
    assert record["sha256"] == spec[7][version]
    assert record["url"] == (
        "https://github.com/astral-sh/python-build-standalone/releases/download/20260825/"
        f"cpython-{version}%2B20260825-{spec[0]}-install_only_stripped.tar.gz"
    )
    assert record["build"] == "20260825"
    assert record["variant"] is None
    assert record["prerelease"] == ""


def test_environment_removes_resolver_and_python_injection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    injected = {
        "UV_INDEX_URL": "https://untrusted.invalid",
        "UV_PYTHON": "/untrusted/python",
        "PIP_EXTRA_INDEX_URL": "https://untrusted.invalid",
        "PYTHONPATH": "/untrusted/modules",
        "PYTHONHOME": "/untrusted/home",
        "VIRTUAL_ENV": "/untrusted/env",
        "CONDA_PREFIX": "/untrusted/conda",
        "VISUALWORLD_TEST_MARKER": "preserved",
    }
    for key, value in injected.items():
        monkeypatch.setenv(key, value)
    env = bootstrap.tool_environment(tmp_path, "3.13.15")
    assert all(
        key not in env for key in injected if key not in {"UV_PYTHON", "VISUALWORLD_TEST_MARKER"}
    )
    assert env["UV_PYTHON"] == str(bootstrap.python_path(tmp_path, "3.13.15"))
    assert env["UV_CACHE_DIR"] == str(tmp_path / "cache")
    assert env["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / "dev")
    assert env["UV_PYTHON_DOWNLOADS"] == "never"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["VISUALWORLD_TEST_MARKER"] == "preserved"


def fake_spec(
    archive: bytes = b"", executable: bytes = b"reviewed uv"
) -> tuple[str, str, str, str, str, str, str, dict[str, str]]:
    original = bootstrap.PLATFORMS[("Linux", "x86_64")]
    return (
        original[0],
        hashlib.sha256(archive).hexdigest(),
        hashlib.sha256(executable).hexdigest(),
        *original[3:],
    )


def test_existing_uv_is_verified_without_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "uv"
    executable.write_bytes(b"reviewed uv")
    monkeypatch.setattr(bootstrap, "platform_spec", fake_spec)
    assert bootstrap.install_uv(tmp_path) == executable
    executable.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum-mismatched"):
        bootstrap.install_uv(tmp_path)


@pytest.mark.parametrize("symlink", [False, True])
def test_uv_archive_selects_only_regular_reviewed_member(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, symlink: bool
) -> None:
    executable = b"reviewed uv"
    stream = io.BytesIO()
    member_name = f"uv-{fake_spec()[0]}/uv"
    with tarfile.open(fileobj=stream, mode="w:gz") as bundle:
        unrelated = tarfile.TarInfo("../unrelated")
        unrelated.size = 1
        bundle.addfile(unrelated, io.BytesIO(b"x"))
        member = tarfile.TarInfo(member_name)
        if symlink:
            member.type = tarfile.SYMTYPE
            member.linkname = "../unrelated"
            bundle.addfile(member)
        else:
            member.size = len(executable)
            bundle.addfile(member, io.BytesIO(executable))
    archive = stream.getvalue()
    spec = fake_spec(archive, executable)
    monkeypatch.setattr(bootstrap, "platform_spec", lambda: spec)
    requested: list[str] = []

    def download(url: str, timeout: int) -> io.BytesIO:
        assert timeout == 60
        requested.append(url)
        return io.BytesIO(archive)

    monkeypatch.setattr(urllib.request, "urlopen", download)
    if symlink:
        with pytest.raises(ValueError, match="Unexpected uv executable archive member"):
            bootstrap.install_uv(tmp_path)
        assert not (tmp_path / "uv").exists()
    else:
        result = bootstrap.install_uv(tmp_path)
        assert result.read_bytes() == executable
        assert result.stat().st_mode & 0o111
    assert requested == [
        f"https://github.com/astral-sh/uv/releases/download/0.12.6/uv-{spec[0]}.tar.gz"
    ]
    assert not (tmp_path.parent / "unrelated").exists()


@pytest.fixture
def verified_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(bootstrap, "platform_spec", fake_spec)
    monkeypatch.setattr(dev, "platform_spec", fake_spec)
    (tmp_path / "uv").write_bytes(b"reviewed uv")
    interpreter = bootstrap.python_path(tmp_path, "3.13.15")
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"reviewed python")
    receipt = {
        "uv_version": bootstrap.UV_VERSION,
        "uv_sha256": fake_spec()[2],
        "python_version": "3.13.15",
        "python_archive": bootstrap.download_record("3.13.15")[1],
        "python_executable_sha256": bootstrap.sha256(interpreter),
    }
    (tmp_path / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    return tmp_path


def test_checked_environment_accepts_reviewed_receipt(verified_state: Path) -> None:
    env = dev.checked_environment(verified_state, "3.13.15")
    assert env["UV_PROJECT_ENVIRONMENT"] == str(verified_state / "dev")


@pytest.mark.parametrize(
    "field",
    ["uv_version", "uv_sha256", "python_version", "python_archive", "python_executable_sha256"],
)
def test_checked_environment_rejects_tampered_receipt(verified_state: Path, field: str) -> None:
    path = verified_state / "receipt.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt[field] = "not the reviewed value"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError):
        dev.checked_environment(verified_state, "3.13.15")


@pytest.mark.parametrize("condition", ["missing_receipt", "changed_interpreter"])
def test_bootstrap_does_not_bless_existing_unverified_python(
    verified_state: Path, condition: str
) -> None:
    if condition == "missing_receipt":
        (verified_state / "receipt.json").unlink()
    else:
        bootstrap.python_path(verified_state, "3.13.15").write_bytes(b"changed")
    with pytest.raises((ValueError, FileNotFoundError)):
        bootstrap.bootstrap(verified_state, "3.13.15")


@pytest.mark.parametrize("command", ["sync", "check", "audit"])
def test_developer_commands_preserve_lock_and_network_boundaries(
    monkeypatch: pytest.MonkeyPatch, verified_state: Path, command: str
) -> None:
    calls: list[list[str]] = []

    def record(args: list[str], env: dict[str, str], cwd: Path = bootstrap.ROOT) -> None:
        assert env["UV_PYTHON_DOWNLOADS"] == "never"
        calls.append(args)

    monkeypatch.setattr(dev, "execute", record)
    dev.run_command(command, verified_state, "3.13.15")
    uv = str(verified_state / "uv")
    if command == "sync":
        assert calls == [[uv, "lock", "--check"], [uv, "sync", "--locked", "--all-groups"]]
    elif command == "check":
        assert calls[0] == [uv, "lock", "--check", "--offline"]
        assert all(
            call[:5] == [uv, "run", "--frozen", "--no-sync", "--offline"] for call in calls[1:-1]
        )
        assert [call[5] for call in calls[1:-1]] == [
            "ruff",
            "ruff",
            "mypy",
            "coverage",
            "coverage",
            "python",
        ]
        assert calls[-1][-1] == "scripts/validate_repository.py"
    else:
        assert len(calls) == 2
        assert calls[0][:7] == [
            uv,
            "export",
            "--locked",
            "--all-groups",
            "--no-emit-project",
            "--format",
            "requirements-txt",
        ]
        assert calls[1][:9] == [
            uv,
            "run",
            "--frozen",
            "--no-sync",
            "pip-audit",
            "--require-hashes",
            "--disable-pip",
            "--no-deps",
            "-r",
        ]
        assert calls[0][-1] == calls[1][-1]


@pytest.mark.parametrize(
    "returncode,stderr", [(0, ""), (2, "network failure"), (2, "Hash mismatch")]
)
def test_build_enforces_sdist_wheel_and_explicit_negative_hash_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int, stderr: str
) -> None:
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    positive: list[list[str]] = []

    def execute(args: list[str], env: dict[str, str], cwd: Path = tmp_path) -> None:
        positive.append(args)
        if "build" in args:
            output = Path(args[args.index("--out-dir") + 1])
            suffix = ".tar.gz" if "--sdist" in args else ".whl"
            (output / f"visualworld_engine-0.1.0a0{suffix}").write_bytes(b"fake artifact")

    def negative(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert {"--sdist", "--force-pep517", "--no-sources", "--require-hashes"} <= set(args)
        constraint = Path(args[args.index("--build-constraint") + 1])
        assert (
            constraint.read_text(encoding="utf-8")
            == "uv-build==0.12.6 --hash=sha256:" + "0" * 64 + "\n"
        )
        return subprocess.CompletedProcess(args, returncode, "", stderr)

    monkeypatch.setattr(dev, "execute", execute)
    monkeypatch.setattr(subprocess, "run", negative)
    if returncode == 2 and stderr == "Hash mismatch":
        dev.build(tmp_path / "tools", "3.13.15", {})
    else:
        with pytest.raises(ValueError, match="not explicitly rejected"):
            dev.build(tmp_path / "tools", "3.13.15", {})
    assert len(positive) == 3
    assert "--sdist" in positive[0]
    assert positive[1][2].endswith(".tar.gz")
    assert "--wheel" in positive[1]
    for call in positive[:2]:
        assert {"--force-pep517", "--no-sources", "--require-hashes"} <= set(call)
        assert call[call.index("--build-constraint") + 1] == "build-constraints.txt"
    assert positive[2][1] == str(tmp_path / "scripts/check_wheel.py")


def make_wheel(tmp_path: Path, defect: str = "none") -> Path:
    info = "visualworld_engine-0.1.0a0.dist-info"
    metadata = (
        "Metadata-Version: 2.4\nName: visualworld-engine\nVersion: 0.1.0a0\n"
        "Requires-Python: >=3.13,<3.15\nLicense-Expression: Apache-2.0\nLicense-File: LICENSE\n"
    )
    replacements = {
        "identity": ("Name: visualworld-engine", "Name: unrelated"),
        "license_expression": ("License-Expression: Apache-2.0", "License-Expression: MIT"),
        "license_metadata": ("License-File: LICENSE", "License-File: OTHER"),
    }
    if defect in replacements:
        metadata = metadata.replace(*replacements[defect])
    if defect == "dependency":
        metadata += "Requires-Dist: unexpected-package\n"
    contents = {
        "visualworld/__init__.py": b"",
        "visualworld/__main__.py": b"",
        "visualworld/cli.py": b"",
        "visualworld/py.typed": b"",
        f"{info}/METADATA": metadata.encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\n",
        f"{info}/RECORD": b"",
        f"{info}/entry_points.txt": b"[console_scripts]\nvisualworld = visualworld.cli:main\n",
        f"{info}/licenses/LICENSE": (check_wheel.ROOT / "LICENSE").read_bytes(),
    }
    if defect == "unexpected_file":
        contents["unreviewed.py"] = b""
    elif defect == "license_text":
        contents[f"{info}/licenses/LICENSE"] = b"not the reviewed license"
    wheel = tmp_path / "visualworld_engine-0.1.0a0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    return wheel


def test_wheel_accepts_exact_scaffold_contents(tmp_path: Path) -> None:
    assert check_wheel.inspect_wheel(make_wheel(tmp_path)) == ("visualworld-engine", "0.1.0a0")


@pytest.mark.parametrize(
    "defect",
    [
        "identity",
        "dependency",
        "license_expression",
        "license_metadata",
        "unexpected_file",
        "license_text",
    ],
)
def test_wheel_rejects_unapproved_metadata_and_contents(tmp_path: Path, defect: str) -> None:
    with pytest.raises(ValueError):
        check_wheel.inspect_wheel(make_wheel(tmp_path, defect))


def test_wheel_smoke_uses_fresh_offline_runtime_and_records_only_shipped_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wheel = make_wheel(tmp_path)
    calls: list[list[str]] = []

    def execute(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        directory = Path(str(kwargs["cwd"]))
        assert directory != check_wheel.ROOT
        assert kwargs["check"] is True
        assert kwargs["timeout"] in {30, 60}
        if "-c" in args:
            output = json.dumps(
                {
                    "packages": [["visualworld-engine", "0.1.0a0"]],
                    "version": "0.1.0a0",
                    "file": str(directory / "runtime/lib/visualworld/__init__.py"),
                }
            )
        elif args[-1] == "--version":
            output = "visualworld 0.1.0a0\n"
        elif args[-1] == "--help":
            output = "usage: visualworld\n"
        else:
            output = ""
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(subprocess, "run", execute)
    check_wheel.check(wheel, tmp_path / "uv")
    assert len(calls) == 7
    assert calls[0][1] == "venv"
    assert {"--no-deps", "--offline"} <= set(calls[1])
    assert calls[1][-1] == str(wheel)
    assert "-I" in calls[2]
    sbom = json.loads(wheel.with_suffix(".runtime.cdx.json").read_text(encoding="utf-8"))
    assert sbom["components"] == []
    assert sbom["metadata"]["component"]["name"] == "visualworld-engine"
    assert sbom["dependencies"][0]["dependsOn"] == []
