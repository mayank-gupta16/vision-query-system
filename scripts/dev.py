#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Small, explicit developer commands using the verified local toolchain."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from bootstrap import (
    ROOT,
    UV_VERSION,
    VERSIONS,
    download_record,
    platform_spec,
    python_path,
    sha256,
    tool_environment,
    verify,
)


def checked_environment(state: Path, version: str) -> dict[str, str]:
    verify(state / "uv", platform_spec()[2])
    receipt = json.loads((state / "receipt.json").read_text(encoding="utf-8"))
    if receipt["python_version"] != version:
        raise ValueError("Toolchain receipt is for a different Python version")
    if receipt["uv_version"] != UV_VERSION or receipt["uv_sha256"] != platform_spec()[2]:
        raise ValueError("Toolchain receipt has an unexpected uv identity")
    if receipt["python_archive"] != download_record(version)[1]:
        raise ValueError("Toolchain receipt does not match the approved Python archive")
    # uv's managed bin/python3 is a symlink; verify the actual executable.
    verify(python_path(state, version).resolve(), receipt["python_executable_sha256"])
    return tool_environment(state, version)


def execute(args: list[str], env: dict[str, str], cwd: Path = ROOT) -> None:
    print("+ " + " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, env=env, check=True, timeout=300)


def build(state: Path, version: str, env: dict[str, str]) -> None:
    uv = str(state / "uv")
    output_root = ROOT / "artifacts"
    output_root.mkdir(exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix="build-", dir=output_root))
    # uv 0.12.6 interprets spaces in an absolute constraint path as separators.
    # This reviewed repository-relative path is stable because builds run at ROOT.
    constraint = "build-constraints.txt"
    controls = [
        "--force-pep517",
        "--no-sources",
        "--build-constraint",
        constraint,
        "--require-hashes",
        "--out-dir",
        str(destination),
    ]
    execute([uv, "build", "--sdist", *controls], env)
    archives = list(destination.glob("*.tar.gz"))
    if len(archives) != 1:
        raise ValueError("Build must produce exactly one sdist")
    execute([uv, "build", str(archives[0]), "--wheel", *controls], env)
    wheels = list(destination.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("Build must produce exactly one wheel")
    execute(
        [
            str(python_path(state, version)),
            str(ROOT / "scripts/check_wheel.py"),
            "--uv",
            uv,
            "--wheel",
            str(wheels[0]),
        ],
        env,
    )
    # A successful build is insufficient: prove the backend hash gate is exercised.
    with tempfile.TemporaryDirectory(prefix="visualworld-bad-hash-") as temporary:
        bad = Path(temporary) / "constraints.txt"
        bad.write_text("uv-build==0.12.6 --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")
        result = subprocess.run(
            [
                uv,
                "build",
                "--sdist",
                "--force-pep517",
                "--no-sources",
                "--build-constraint",
                str(bad),
                "--require-hashes",
                "--out-dir",
                temporary,
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=300,
        )
        if result.returncode == 0 or "hash mismatch" not in result.stderr.lower():
            raise ValueError("Invalid backend hash was not explicitly rejected")
        print("Invalid backend hash rejected (including with the correct backend cached).")
    for artifact in sorted([*archives, *wheels]):
        print(f"{artifact.name}: {artifact.stat().st_size} bytes sha256:{sha256(artifact)}")
    print(f"Artifacts and runtime-only SBOM: {destination}")


def run_command(command: str, state: Path, version: str) -> None:
    env = checked_environment(state, version)
    uv = str(state / "uv")
    python = str(python_path(state, version))
    if command == "sync":
        execute([uv, "lock", "--check"], env)
        execute([uv, "sync", "--locked", "--all-groups"], env)
    elif command == "check":
        execute([uv, "lock", "--check", "--offline"], env)
        # No implicit dependency installation or network in the check command.
        prefix = [uv, "run", "--frozen", "--no-sync", "--offline"]
        for arguments in [
            ["ruff", "format", "--check", "src", "tests", "scripts", "workers"],
            ["ruff", "check", "src", "tests", "scripts", "workers"],
            ["mypy", "src", "tests", "scripts", "workers"],
            ["coverage", "run", "-m", "pytest"],
            ["coverage", "report"],
            ["python", "scripts/inspect_dependency_licenses.py", "--check"],
        ]:
            execute([*prefix, *arguments], env)
        execute([python, "scripts/validate_repository.py"], env)
    elif command == "build":
        build(state, version, env)
    elif command == "audit":
        with tempfile.TemporaryDirectory(prefix="visualworld-audit-") as temporary:
            requirements = str(Path(temporary) / "requirements.txt")
            execute(
                [
                    uv,
                    "export",
                    "--locked",
                    "--all-groups",
                    "--no-emit-project",
                    "--format",
                    "requirements-txt",
                    "--quiet",
                    "--output-file",
                    requirements,
                ],
                env,
            )
            execute(
                [
                    uv,
                    "run",
                    "--frozen",
                    "--no-sync",
                    "pip-audit",
                    "--require-hashes",
                    "--disable-pip",
                    "--no-deps",
                    "-r",
                    requirements,
                ],
                env,
            )
    else:
        raise ValueError("Unknown developer command")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sync", "check", "build", "audit"))
    parser.add_argument("--python", choices=VERSIONS, default=VERSIONS[0])
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    state = (args.state or ROOT / "artifacts" / "toolchain" / args.python).resolve()
    try:
        run_command(args.command, state, args.python)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Developer command failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
