# SPDX-License-Identifier: Apache-2.0
"""Tests for issue #13's machine-readable acceptance measurements."""

from __future__ import annotations

import os
import platform
from pathlib import Path

import pytest
import run_crop_acceptance as acceptance

from visualworld.geometry import Rgb24Crop


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("", None),
        ("Name:\tpython\n", None),
        ("VmHWM:\tunknown kB\n", None),
        ("VmHWM:\t0 kB\n", None),
        ("VmHWM:\t123 kB\n", 123 * 1024),
    ],
)
def test_peak_rss_requires_a_positive_observation(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected: int | None,
) -> None:
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: status)

    assert acceptance._peak_rss_bytes() == expected


@pytest.mark.parametrize(
    ("peak_rss", "expected_measured", "expected_bounded"),
    [
        (None, False, False),
        (1, True, True),
        (2 * 1024 * 1024 * 1024, True, True),
        (2 * 1024 * 1024 * 1024 + 1, True, False),
    ],
)
def test_rss_result_never_treats_unmeasured_as_bounded(
    peak_rss: int | None,
    expected_measured: bool,
    expected_bounded: bool,
) -> None:
    result = acceptance._rss_result(peak_rss)

    assert result["rss_measured"] is expected_measured
    assert result["bounded"] is expected_bounded
    assert result["process_peak_rss_bytes"] == peak_rss


def test_acceptance_status_fails_when_rss_was_not_measured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    monkeypatch.setattr(acceptance, "_memory_bytes", lambda: 16_000_000_000)
    monkeypatch.setattr(
        acceptance,
        "_fixture_checks",
        lambda: ({}, [], Rgb24Crop(1, 1, b"abc")),
    )
    monkeypatch.setattr(acceptance, "_destination_checks", lambda *_args: {"safe": True})
    monkeypatch.setattr(
        acceptance,
        "_copy_benchmark",
        lambda: {
            "exact_geometry": True,
            "timing_measured": True,
            "rss_measured": False,
            "bounded": False,
        },
    )

    assert acceptance._run(tmp_path)["status"] == "fail"
