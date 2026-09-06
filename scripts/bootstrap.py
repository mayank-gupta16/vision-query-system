#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Acquire the reviewed developer toolchain; no shell installer or global writes.

Run with a trusted host Python 3.9+; application commands use the acquired Python.
The host TLS/root-certificate configuration is part of the bootstrap trust base.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ("3.13.15", "3.14.7")
UV_VERSION = "0.12.6"
PLATFORMS = {
    ("Linux", "x86_64"): (
        "x86_64-unknown-linux-gnu",
        "8681d8921e7d520fb368991dcf5f9c1905b80f5bf2a265a0ed085c8d8e342477",
        "d381f11517c66523211b0876552ff7dea5c1b4b0f13800571b35225761302fba",
        "linux",
        "x86_64",
        "gnu",
        "linux-x86_64-gnu",
        {
            "3.13.15": "8af9a8214c71b2dd698005e39fab87aad02a994330508857da4e6d1ba7e6ddb6",
            "3.14.7": "a0f39e822fd3b96a2605f30f954acf9a2cdf91faa1385a13e24bc407a41e05ea",
        },
    ),
    ("Darwin", "arm64"): (
        "aarch64-apple-darwin",
        "14b459d51ea2e71eeba28c45a268c922bdf8607fc6455e3f40b4e082895d160d",
        "e8929237934c8679686428f5a7736c7ae7a5fe7a33b0504d1b03446cdbc43c94",
        "darwin",
        "aarch64",
        "none",
        "macos-aarch64-none",
        {
            "3.13.15": "149038dd0c194c25d4616d7e42a35f67f2edee96412788f74115819b6a4c8548",
            "3.14.7": "17ecb3d29c49765856370cbb47d948a24ec518be40f607362c1bbb3ebbc5c442",
        },
    ),
}


def platform_spec() -> tuple[str, str, str, str, str, str, str, dict[str, str]]:
    try:
        return PLATFORMS[(platform.system(), platform.machine())]
    except KeyError as error:
        raise ValueError("Supported hosts: Linux x86_64 (glibc), macOS arm64") from error


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, expected: str) -> None:
    if path.is_symlink() or not path.is_file() or sha256(path) != expected:
        raise ValueError(f"Missing, symlinked, or checksum-mismatched input: {path.name}")


def tool_environment(state: Path, version: str) -> dict[str, str]:
    # Do not inherit resolver overrides, alternate registries or Python injection.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("UV_", "PIP_", "PYTHON"))
        and key not in {"VIRTUAL_ENV", "CONDA_PREFIX"}
    }
    env.update(
        {
            "UV_PYTHON_INSTALL_DIR": str(state / "python"),
            "UV_CACHE_DIR": str(state / "cache"),
            "UV_PROJECT_ENVIRONMENT": str(state / "dev"),
            "UV_PYTHON": str(python_path(state, version)),
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_NO_PROGRESS": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return env


def python_path(state: Path, version: str) -> Path:
    return state / "python" / f"cpython-{version}-{platform_spec()[6]}" / "bin" / "python3"


def install_uv(state: Path) -> Path:
    triple, archive_hash, executable_hash, *_ = platform_spec()
    executable = state / "uv"
    if executable.exists() or executable.is_symlink():
        verify(executable, executable_hash)
        return executable
    url = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{triple}.tar.gz"
    # Fixed reviewed URL, bounded download, and only one explicitly named regular member.
    with tempfile.TemporaryDirectory(prefix="download-", dir=state) as temporary:
        archive = Path(temporary) / "uv.tar.gz"
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("xb") as output:
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 128 * 1024 * 1024:
                    raise ValueError("uv download exceeds 128 MiB bootstrap limit")
                output.write(chunk)
        verify(archive, archive_hash)
        with tarfile.open(archive, "r:gz") as bundle:
            member = bundle.getmember(f"uv-{triple}/uv")
            if not member.isfile() or member.size > 128 * 1024 * 1024:
                raise ValueError("Unexpected uv executable archive member")
            stream = bundle.extractfile(member)
            if stream is None:
                raise ValueError("uv executable missing")
            candidate = Path(temporary) / "uv"
            with stream, candidate.open("xb") as output:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    output.write(chunk)
            verify(candidate, executable_hash)
            candidate.chmod(0o755)
            # Exclusive creation avoids replacing a user's existing executable.
            with executable.open("xb") as output, candidate.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(chunk)
            executable.chmod(0o755)
    return executable


def download_record(version: str) -> tuple[str, dict[str, object]]:
    triple, _, _, operating_system, arch, libc, _, hashes = platform_spec()
    major, minor, patch = map(int, version.split("."))
    key = f"cpython-{version}-{operating_system}-{arch}-{libc}"
    record: dict[str, object] = {
        "name": "cpython",
        "arch": {"family": arch, "variant": None},
        "os": operating_system,
        "libc": libc,
        "major": major,
        "minor": minor,
        "patch": patch,
        "prerelease": "",
        "variant": None,
        "build": "20260825",
        "url": "https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"20260825/cpython-{version}%2B20260825-{triple}-install_only_stripped.tar.gz",
        "sha256": hashes[version],
    }
    return key, record


def bootstrap(state: Path, version: str) -> None:
    state.mkdir(parents=True, exist_ok=True)
    uv = install_uv(state)
    interpreter = python_path(state, version)
    if interpreter.exists():
        # Never silently bless a pre-existing interpreter with a new receipt.
        previous = json.loads((state / "receipt.json").read_text(encoding="utf-8"))
        if previous["python_version"] != version:
            raise ValueError("Existing interpreter has a different receipt version")
        verify(interpreter.resolve(), previous["python_executable_sha256"])
    key, record = download_record(version)
    metadata = state / "python-downloads.json"
    metadata.write_text(json.dumps({key: record}, indent=2) + "\n", encoding="utf-8")
    env = tool_environment(state, version)
    env.pop("UV_PYTHON_DOWNLOADS")
    env.pop("UV_PYTHON")
    subprocess.run(
        [
            str(uv),
            "python",
            "install",
            "--no-config",
            "--no-bin",
            "--python-downloads-json-url",
            metadata.as_uri(),
            key,
        ],
        env=env,
        check=True,
        timeout=300,
    )
    identity = subprocess.run(
        [
            str(interpreter),
            "-I",
            "-c",
            "import platform,sysconfig; print(platform.python_version()); "
            "print(sysconfig.get_config_var('Py_GIL_DISABLED') or 0)",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.splitlines()
    if identity != [version, "0"]:
        raise ValueError("Unexpected interpreter version or free-threaded build")
    receipt = {
        "uv_version": UV_VERSION,
        "uv_sha256": sha256(uv),
        "python_version": version,
        "python_archive": record,
        "python_executable_sha256": sha256(interpreter),
    }
    (state / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"Verified developer toolchain: {state}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", choices=VERSIONS, default=VERSIONS[0])
    parser.add_argument(
        "--state", type=Path, help="Private local tool directory (default artifacts/)"
    )
    args = parser.parse_args()
    state = (args.state or ROOT / "artifacts" / "toolchain" / args.python).resolve()
    try:
        bootstrap(state, args.python)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Bootstrap failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
