# SPDX-License-Identifier: Apache-2.0
"""Exercise the scaffold CLI without network, models, or media inputs."""

import runpy
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from visualworld import __version__
from visualworld.cli import build_parser, main


def test_distribution_version_matches_package() -> None:
    assert version("visualworld-engine") == __version__


def test_parser_is_explicit_and_side_effect_free(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    assert parser.prog == "visualworld"
    assert vars(parser.parse_args([])) == {}
    assert capsys.readouterr() == ("", "")


def test_no_arguments_shows_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    captured = capsys.readouterr()
    assert "usage: visualworld" in captured.out
    assert "--version" in captured.out
    assert "not implemented" in " ".join(captured.out.split())
    assert captured.err == ""


@pytest.mark.parametrize("option", ["-h", "--help"])
def test_explicit_help(option: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        main([option])
    assert result.value.code == 0
    captured = capsys.readouterr()
    assert "usage: visualworld" in captured.out
    assert captured.err == ""


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr() == (f"visualworld {__version__}\n", "")


@pytest.mark.parametrize("argument", ["--unknown", "--ver", "ingest", "video.mp4"])
def test_unsupported_arguments_fail(argument: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        main([argument])
    assert result.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unrecognized arguments" in captured.err


def test_module_entry_point(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["visualworld"])
    with pytest.raises(SystemExit) as result:
        runpy.run_module("visualworld", run_name="__main__")
    assert result.value.code == 0
    assert "usage: visualworld" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("arguments", "returncode", "expected_stdout", "expected_stderr"),
    [
        ([], 0, "usage: visualworld", ""),
        (["--help"], 0, "usage: visualworld", ""),
        (["--version"], 0, f"visualworld {__version__}\n", ""),
        (["--unknown"], 2, "", "unrecognized arguments"),
    ],
)
@pytest.mark.parametrize("invocation", ["module", "console"])
def test_installed_entry_points_from_outside_checkout(
    invocation: str,
    arguments: list[str],
    returncode: int,
    expected_stdout: str,
    expected_stderr: str,
    tmp_path: Path,
) -> None:
    command = (
        [sys.executable, "-m", "visualworld"]
        if invocation == "module"
        else [str(Path(sys.executable).with_name("visualworld"))]
    )
    result = subprocess.run(
        [*command, *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == returncode
    if expected_stdout:
        assert expected_stdout in result.stdout
    else:
        assert result.stdout == ""
    if expected_stderr:
        assert expected_stderr in result.stderr
    else:
        assert result.stderr == ""
