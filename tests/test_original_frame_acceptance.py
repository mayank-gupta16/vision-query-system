# SPDX-License-Identifier: Apache-2.0
"""Mac-safe helper tests for issue #87's Linux-native acceptance runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import generate_synthetic_fixtures as fixtures
import pytest
import run_original_frame_acceptance as acceptance

from visualworld.ports import PortError, PortErrorCode, PortKind


def test_crop_manifest_is_exactly_bound_to_synthetic_fixture_facts() -> None:
    manifest = fixtures.load_manifest()
    crops = acceptance._crop_records()
    fixtures_by_id = {item["fixture_id"]: item for item in acceptance._fixture_records(manifest)}

    assert set(crops) == {spec.fixture_id for spec in fixtures.SPECS}
    for fixture_id, records in crops.items():
        expected_frames = cast(list[dict[str, object]], fixtures_by_id[fixture_id]["frames"])
        for expected_frame, crop in zip(expected_frames, records, strict=True):
            assert crop["box_xyxy"] == list(acceptance._moving_box(expected_frame))


def test_receipt_encoding_is_canonical_and_rejects_binary_pixels() -> None:
    receipt: dict[str, object] = {
        "status": "pass",
        "nested": {"digest": "ab" * 32},
        "checks": [True],
    }

    payload = acceptance._canonical_receipt_bytes(receipt)

    assert payload.endswith(b"\n")
    assert (
        payload
        == (
            json.dumps(
                receipt, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode()
    )
    with pytest.raises(acceptance.AcceptanceError) as raised:
        acceptance._canonical_receipt_bytes({"pixels": b"private-rgb24"})
    assert raised.value.code == "receipt_contains_binary_data"
    assert "private-rgb24" not in str(raised.value)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            PortError(
                PortErrorCode.LIMIT_EXCEEDED,
                PortKind.ORIGINAL_FRAME_READER,
                "read",
            ),
            True,
        ),
        (
            PortError(PortErrorCode.CONFLICT, PortKind.ORIGINAL_FRAME_READER, "read"),
            False,
        ),
        (PortError(PortErrorCode.LIMIT_EXCEEDED, PortKind.VIDEO_SOURCE, "read"), False),
    ],
)
def test_static_error_check_requires_exact_redacted_contract(
    error: PortError, expected: bool
) -> None:
    pixels = b"private-rgb24"

    def fail() -> object:
        raise error

    assert (
        acceptance._expect_static_error(fail, PortErrorCode.LIMIT_EXCEEDED, (pixels,)) is expected
    )


def test_main_rejects_macos_before_touching_runtime_or_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "must-not-exist.json"
    monkeypatch.setattr(acceptance, "_supported_host", lambda: False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_original_frame_acceptance.py",
            "--media-runtime",
            "/missing/media",
            "--media-worker",
            "/missing/media-worker",
            "--overlay-root",
            "/missing/overlay",
            "--overlay-worker",
            "/missing/overlay-worker",
            "--work-root",
            "/missing/work",
            "--output",
            str(output),
        ],
    )

    with pytest.raises(SystemExit) as raised:
        acceptance.main()

    assert raised.value.code == 2
    assert not output.exists()
    error = capsys.readouterr().err
    assert error == (
        "Original-frame acceptance requires supported Linux x86_64 GNU libc; "
        "no native proof was run.\n"
    )
    assert "/missing" not in error
