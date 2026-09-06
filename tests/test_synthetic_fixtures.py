# SPDX-License-Identifier: Apache-2.0
"""Contract tests for the generated synthetic-v1 media set."""

from __future__ import annotations

import copy
import hashlib
import json
import struct
import subprocess
import sys
from itertools import pairwise
from pathlib import Path
from typing import cast

import generate_synthetic_fixtures as fixtures
import pytest


def _fixture_records(manifest: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], manifest["fixtures"])


def _box_payload(content: bytes, kind: bytes) -> bytes:
    kind_offset = content.index(kind)
    box_offset = kind_offset - 4
    size = struct.unpack_from(">I", content, box_offset)[0]
    return content[kind_offset + 4 : box_offset + size]


def _time_deltas(content: bytes) -> list[int]:
    payload = _box_payload(content, b"stts")
    entry_count = struct.unpack_from(">I", payload, 4)[0]
    deltas: list[int] = []
    offset = 8
    for _ in range(entry_count):
        count, delta = struct.unpack_from(">II", payload, offset)
        deltas.extend([delta] * count)
        offset += 8
    return deltas


def test_manifest_is_exact_and_rights_safe() -> None:
    manifest = fixtures.load_manifest()
    assert manifest == fixtures.expected_manifest()
    assert manifest["schema"] == "visualworld.synthetic-fixture-manifest"
    assert manifest["schema_version"] == 1

    rights = cast(dict[str, object], manifest["rights"])
    assert rights["license_expression"] == "Apache-2.0"
    assert rights["external_sources"] == []
    assert rights["redistribution_allowed"] is True
    assert rights["derivatives_allowed"] is True
    assert set(cast(list[str], rights["allowed_uses"])) == {
        "benchmarking",
        "modification",
        "redistribution",
        "testing",
    }

    privacy = cast(dict[str, object], manifest["privacy"])
    assert privacy["classification"] == "public-synthetic-no-personal-data"
    assert privacy["consent_status"] == "not-applicable-no-personal-data"
    assert not any(
        cast(bool, privacy[field])
        for field in (
            "contains_faces",
            "contains_imported_assets",
            "contains_personal_data",
            "contains_plates",
            "contains_real_people",
        )
    )


def test_manifest_rejects_missing_or_unknown_facts() -> None:
    unknown = copy.deepcopy(fixtures.expected_manifest())
    unknown["unexpected"] = True
    with pytest.raises(ValueError, match="does not exactly match"):
        fixtures.validate_manifest(unknown)

    missing = copy.deepcopy(fixtures.expected_manifest())
    del missing["rights"]
    with pytest.raises(ValueError, match="does not exactly match"):
        fixtures.validate_manifest(missing)


def test_generation_is_byte_deterministic_and_matches_manifest(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    fixtures.generate(first)
    fixtures.generate(second)
    manifest = fixtures.load_manifest()

    for record in _fixture_records(manifest):
        filename = cast(str, record["filename"])
        first_bytes = (first / filename).read_bytes()
        assert first_bytes == (second / filename).read_bytes()
        assert len(first_bytes) == record["byte_count"]
        assert hashlib.sha256(first_bytes).hexdigest() == record["sha256"]


def test_movies_embed_exact_timing_pixels_and_rotation(tmp_path: Path) -> None:
    output = tmp_path / "fixtures"
    fixtures.generate(output)
    manifest = fixtures.load_manifest()

    for record in _fixture_records(manifest):
        content = (output / cast(str, record["filename"])).read_bytes()
        frames = cast(list[dict[str, object]], record["frames"])
        expected_durations = [int(cast(str, frame["duration"])) for frame in frames]
        assert _time_deltas(content) == expected_durations

        expected_pts: list[int] = []
        pts = 0
        for duration in expected_durations:
            expected_pts.append(pts)
            pts += duration
        assert [int(cast(str, frame["pts"])) for frame in frames] == expected_pts

        mdat = _box_payload(content, b"mdat")
        frame_size = fixtures.WIDTH * fixtures.HEIGHT * 3
        decoded_frames = [
            mdat[offset : offset + frame_size] for offset in range(0, len(mdat), frame_size)
        ]
        assert [hashlib.sha256(frame).hexdigest() for frame in decoded_frames] == [
            frame["rgb24_sha256"] for frame in frames
        ]

        tkhd = _box_payload(content, b"tkhd")
        matrix = struct.unpack_from(">9i", tkhd, 40)
        expected_matrix = (
            (0, -65_536, 0, 65_536, 0, 0, 0, 0, 1_073_741_824)
            if record["rotation_degrees"] == 90
            else (65_536, 0, 0, 0, 65_536, 0, 0, 0, 1_073_741_824)
        )
        assert matrix == expected_matrix

    by_id = {cast(str, item["fixture_id"]): item for item in _fixture_records(manifest)}
    cfr_frames = cast(list[dict[str, object]], by_id["cfr"]["frames"])
    vfr_frames = cast(list[dict[str, object]], by_id["vfr"]["frames"])
    cfr_pts = [int(cast(str, frame["pts"])) for frame in cfr_frames]
    vfr_pts = [int(cast(str, frame["pts"])) for frame in vfr_frames]
    assert [right - left for left, right in pairwise(cfr_pts)] == [200] * 3
    assert [right - left for left, right in pairwise(vfr_pts)] == [100, 250, 150]
    assert by_id["rotation-90"]["rotation_degrees"] == 90


def test_output_validation_fails_closed(tmp_path: Path) -> None:
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(ValueError, match="must be empty"):
        fixtures.generate(nonempty)

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-output"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        fixtures.generate(link)

    output = tmp_path / "generated"
    fixtures.generate(output)
    (output / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="missing or unexpected"):
        fixtures.scan_output(output)
    (output / "unexpected.txt").unlink()
    with (output / "cfr.mov").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="size mismatch"):
        fixtures.scan_output(output)


def test_default_output_is_ignored_and_media_is_not_tracked() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", str(fixtures.DEFAULT_OUTPUT / "cfr.mov")],
        cwd=fixtures.ROOT,
        check=False,
    )
    assert ignored.returncode == 0
    tracked = subprocess.run(
        ["git", "ls-files", "*.mov"],
        cwd=fixtures.ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert tracked.stdout == ""


def test_generation_fits_pr_ci_budget(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(fixtures.ROOT / "scripts" / "generate_synthetic_fixtures.py"),
            "--output",
            str(tmp_path / "cli-output"),
        ],
        cwd=fixtures.ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    receipt: object = json.loads(completed.stdout)
    assert isinstance(receipt, dict)
    assert receipt["status"] == "ok"
    assert receipt["elapsed_ms"] < 1_000
    assert receipt["peak_rss_bytes"] < 128 * 1024 * 1024
    assert receipt["total_bytes"] <= fixtures.MAX_TOTAL_BYTES
