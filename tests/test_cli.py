# SPDX-License-Identifier: Apache-2.0
"""Golden, vertical-slice, cancellation, and privacy tests for issue #17."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import stat
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import pytest

import visualworld.cli as cli
from visualworld import __version__
from visualworld.coordinator import CoordinatorError, CoordinatorErrorCode, CoordinatorStage
from visualworld.geometry import CropError
from visualworld.ingestion import FrameRef
from visualworld.ports import MAX_PORT_BATCH_ITEMS, PortError, PortErrorCode, PortKind

GOLDENS = Path(__file__).with_name("goldens")


def _golden(name: str) -> str:
    return (GOLDENS / name).read_text(encoding="utf-8")


def _json_output(captured: pytest.CaptureFixture[str]) -> dict[str, Any]:
    streams = captured.readouterr()
    assert streams.err == ""
    assert streams.out.endswith("\n")
    assert "\n" not in streams.out[:-1]
    value = json.loads(streams.out)
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def test_distribution_version_matches_package() -> None:
    assert version("visualworld-engine") == __version__


def test_parser_is_explicit_and_side_effect_free(capsys: pytest.CaptureFixture[str]) -> None:
    parser = cli.build_parser()
    assert parser.prog == "visualworld"
    assert vars(parser.parse_args([])) == {"command": None}
    assert capsys.readouterr() == ("", "")


def test_no_arguments_shows_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == _golden("cli-help.txt")
    assert captured.err == ""


@pytest.mark.parametrize("arguments", [["--help"], ["probe", "--help"]])
def test_explicit_help(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        cli.main(arguments)
    assert result.value.code == 0
    captured = capsys.readouterr()
    assert "usage: visualworld" in captured.out
    assert captured.err == ""


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        cli.main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr() == (f"visualworld {__version__}\n", "")


@pytest.mark.parametrize(
    "arguments",
    [
        ["--unknown"],
        ["--ver"],
        ["video.mp4"],
        ["ingest"],
        ["probe", "--\x1b[31muntrusted"],
    ],
)
def test_usage_errors_are_stable_json_and_do_not_echo_input(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(arguments) == 2
    captured = capsys.readouterr()
    assert captured == ("", _golden("cli-error.json"))
    assert json.loads(captured.err) == {
        "command": "arguments",
        "error": {
            "code": "usage_error",
            "operation": "parse_arguments",
            "retryable": False,
        },
        "schema": "visualworld.cli-result",
        "schema_version": 1,
        "status": "error",
    }


def test_probe_has_canonical_machine_readable_golden(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["probe"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == _golden("cli-probe.json")
    assert captured.out == cli.canonical_json(cli.probe_result()) + "\n"
    result = json.loads(captured.out)
    assert result["command"] == "probe"
    assert result["status"] == "ok"
    assert result["result"]["fixture"] == "deterministic-rgb24-v1"
    assert len(result["result"]["frames"]) == 2
    assert "pixels" not in captured.out


def test_vertical_quickstart_retrieves_exact_original_pixel_crop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "private-store"

    assert cli.main(["ingest", "--store", str(store)]) == 0
    ingested = _json_output(capsys)
    run_id = str(ingested["result"]["run_id"])
    evidence_id = str(ingested["result"]["evidence_ids"][0])
    assert ingested["result"]["disposition"] == "committed"
    assert str(tmp_path) not in json.dumps(ingested)

    assert cli.main(["inspect-run", "--store", str(store), "--run-id", run_id]) == 0
    inspected = _json_output(capsys)
    assert inspected["result"]["manifest"]["run_id"] == run_id
    assert inspected["result"]["manifest"]["state"] == "committed"

    assert (
        cli.main(
            [
                "inspect-run",
                "--store",
                str(store),
                "--run-id",
                str(ingested["result"]["source_id"]),
            ]
        )
        == 1
    )
    wrong_record = capsys.readouterr()
    assert wrong_record.out == ""
    assert json.loads(wrong_record.err)["error"]["code"] == "invalid_request"

    assert cli.main(["list-samples", "--store", str(store), "--run-id", run_id]) == 0
    listed = _json_output(capsys)
    assert listed["result"]["run_id"] == run_id
    assert len(listed["result"]["samples"]) == 1
    assert listed["result"]["samples"][0]["evidence"][0]["evidence_id"] == evidence_id

    destination = tmp_path / "crop.rgb24"
    assert (
        cli.main(
            [
                "show-evidence",
                "--store",
                str(store),
                "--evidence-id",
                evidence_id,
                "--output",
                str(destination),
            ]
        )
        == 0
    )
    shown = _json_output(capsys)
    expected = bytes((3, 4, 5, 9, 10, 11))
    assert destination.read_bytes() == expected
    assert stat.S_IMODE(destination.stat().st_mode) == 0o400
    assert shown["result"]["export"] == {
        "bytes": 6,
        "height": 2,
        "media_type": "application/vnd.visualworld.rgb24",
        "sha256": hashlib.sha256(expected).hexdigest(),
        "width": 1,
    }
    rendered = json.dumps(shown)
    assert str(destination) not in rendered
    assert repr(expected) not in rendered

    assert (
        cli.main(
            [
                "show-evidence",
                "--store",
                str(store),
                "--evidence-id",
                evidence_id,
                "--output",
                str(destination),
            ]
        )
        == 1
    )
    overwrite = capsys.readouterr()
    assert overwrite.out == ""
    assert str(destination) not in overwrite.err
    assert json.loads(overwrite.err)["error"]["code"] == "invalid_request"
    assert destination.read_bytes() == expected

    assert cli.main(["ingest", "--store", str(store)]) == 0
    retried = _json_output(capsys)
    assert retried["result"]["disposition"] == "already_committed"
    assert retried["result"]["run_id"] == run_id

    inside_store = store / "unsafe-export.rgb24"
    assert (
        cli.main(
            [
                "show-evidence",
                "--store",
                str(store),
                "--evidence-id",
                evidence_id,
                "--output",
                str(inside_store),
            ]
        )
        == 1
    )
    rejected = capsys.readouterr()
    assert rejected.out == ""
    assert str(inside_store) not in rejected.err
    assert json.loads(rejected.err)["error"]["code"] == "invalid_request"
    assert not inside_store.exists()


def test_list_samples_is_scoped_to_the_requested_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "private-store"
    assert cli.main(["ingest", "--store", str(store)]) == 0
    first = _json_output(capsys)
    assert cli.main(["ingest", "--store", str(store), "--box", "0", "0", "2", "2"]) == 0
    second = _json_output(capsys)
    assert first["result"]["run_id"] != second["result"]["run_id"]

    assert (
        cli.main(
            [
                "list-samples",
                "--store",
                str(store),
                "--run-id",
                str(first["result"]["run_id"]),
            ]
        )
        == 0
    )
    listed = _json_output(capsys)
    evidence_ids = [
        evidence["evidence_id"]
        for sample in listed["result"]["samples"]
        for evidence in sample["evidence"]
    ]
    assert evidence_ids == first["result"]["evidence_ids"]
    assert second["result"]["evidence_ids"][0] not in evidence_ids


def test_operational_errors_redact_paths_and_untrusted_identifiers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private_text = "private-\x1b[31m-location"
    missing = tmp_path / private_text
    hostile_run = "run_" + "f" * 63 + "\x1b"

    assert (
        cli.main(
            [
                "inspect-run",
                "--store",
                str(missing),
                "--run-id",
                hostile_run,
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(missing) not in captured.err
    assert hostile_run not in captured.err
    assert "\x1b" not in captured.err
    assert json.loads(captured.err)["error"]["code"] == "not_found"


def test_cli_helpers_reject_invalid_paths_shapes_and_unbounded_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(cli._CliError, match="invalid_request"):
        cli._argument(argparse.Namespace(), "missing")
    with pytest.raises(cli._CliError, match="invalid_request"):
        cli._store_path("relative", must_exist=False)
    with pytest.raises(cli._CliError, match="invalid_request"):
        cli._output_path("relative")

    regular_file = tmp_path / "regular-file"
    regular_file.write_text("not a store", encoding="utf-8")
    with pytest.raises(cli._CliError, match="invalid_request"):
        cli._store_path(str(regular_file), must_exist=True)
    linked = tmp_path / "linked-store"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(cli._CliError, match="invalid_request"):
        cli._store_path(str(linked), must_exist=True)

    inaccessible = tmp_path / "inaccessible"
    original_lstat = Path.lstat

    def fail_selected(path: Path) -> os.stat_result:
        if path == inaccessible:
            raise PermissionError
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_selected)
    with pytest.raises(cli._CliError, match="operation_failed"):
        cli._store_path(str(inaccessible), must_exist=True)

    with pytest.raises(cli._CliError, match="invalid_request"):
        cli._run_ingest(argparse.Namespace(store=str(tmp_path / "store"), box=()))
    page = cast(tuple[FrameRef, ...], (None,) * MAX_PORT_BATCH_ITEMS)
    with pytest.raises(cli._CliError, match="limit_exceeded"):
        cli._complete_page(page, lambda: True, "list_samples")


@pytest.mark.parametrize(
    ("error", "exit_code", "code", "operation", "retryable"),
    [
        (
            CoordinatorError(
                CoordinatorErrorCode.OPERATION_FAILED,
                CoordinatorStage.PROBE,
                retryable=True,
            ),
            1,
            "operation_failed",
            "probe",
            True,
        ),
        (
            PortError(
                PortErrorCode.NOT_FOUND,
                PortKind.WORLD_STORE,
                "get",
            ),
            1,
            "not_found",
            "get",
            False,
        ),
        (
            PortError(
                PortErrorCode.CANCELLED,
                PortKind.VIDEO_SOURCE,
                "probe",
            ),
            130,
            "cancelled",
            "probe",
            False,
        ),
        (CropError("invalid_crop"), 1, "invalid_request", "probe", False),
        (RuntimeError("private implementation detail"), 1, "operation_failed", "probe", False),
    ],
)
def test_runtime_failures_map_to_stable_redacted_errors(
    error: BaseException,
    exit_code: int,
    code: str,
    operation: str,
    retryable: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_: object) -> dict[str, object]:
        raise error

    monkeypatch.setattr(cli, "_run_probe", fail)
    assert cli.main(["probe"]) == exit_code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private implementation detail" not in captured.err
    assert json.loads(captured.err) == {
        "command": "probe",
        "error": {"code": code, "operation": operation, "retryable": retryable},
        "schema": "visualworld.cli-result",
        "schema_version": 1,
        "status": "error",
    }


def test_invalid_manual_box_is_a_structured_coordinator_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cli.main(
            [
                "ingest",
                "--store",
                str(tmp_path / "store"),
                "--box",
                "0",
                "0",
                "0",
                "1",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == {
        "code": "invalid_request",
        "operation": "crop",
        "retryable": False,
    }


def test_cancellation_has_a_stable_golden(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def cancel(_: object) -> dict[str, object]:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run_probe", cancel)

    assert cli.main(["probe"]) == 130
    captured = capsys.readouterr()
    assert captured == ("", _golden("cli-cancel.json"))
    assert json.loads(captured.err) == {
        "command": "probe",
        "error": {
            "code": "cancelled",
            "operation": "probe",
            "retryable": False,
        },
        "schema": "visualworld.cli-result",
        "schema_version": 1,
        "status": "error",
    }


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
        (["--unknown"], 2, "", '"code":"usage_error"'),
        (["probe"], 0, '"command":"probe"', ""),
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
