#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate the tiny, deterministic, rights-safe VisualWorld media fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "fixtures" / "synthetic-v1" / "manifest.json"
DEFAULT_OUTPUT = ROOT / "artifacts" / "fixtures" / "synthetic-v1"
WIDTH = 16
HEIGHT = 12
TIME_SCALE = 1_000
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024
GENERATOR_VERSION = 1


@dataclass(frozen=True)
class FixtureSpec:
    fixture_id: str
    filename: str
    durations: tuple[int, ...]
    rotation_degrees: int
    pattern_seed: int


SPECS = (
    FixtureSpec("cfr", "cfr.mov", (200, 200, 200, 200), 0, 1),
    FixtureSpec("vfr", "vfr.mov", (100, 250, 150, 300), 0, 2),
    FixtureSpec("rotation-90", "rotation-90.mov", (200, 200, 200, 200), 90, 3),
)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _box(kind: bytes, payload: bytes) -> bytes:
    if len(kind) != 4:
        raise ValueError("MOV box kinds must contain four bytes")
    size = len(payload) + 8
    if size > 0xFFFFFFFF:
        raise ValueError("MOV box exceeds the supported 32-bit size")
    return struct.pack(">I4s", size, kind) + payload


def _full_box(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return _box(kind, bytes((version,)) + flags.to_bytes(3, "big") + payload)


def _matrix(rotation_degrees: int) -> bytes:
    values = {
        0: (65_536, 0, 0, 0, 65_536, 0, 0, 0, 1_073_741_824),
        90: (0, -65_536, 0, 65_536, 0, 0, 0, 0, 1_073_741_824),
    }
    try:
        return struct.pack(">9i", *values[rotation_degrees])
    except KeyError as error:
        raise ValueError("Only 0 and 90 degree fixture rotations are supported") from error


def _region(spec: FixtureSpec, frame_index: int) -> tuple[int, int, int, int, tuple[int, int, int]]:
    region_width = 3
    region_height = 3
    x = (frame_index * 3 + spec.pattern_seed) % (WIDTH - region_width + 1)
    y = (frame_index * 2 + spec.pattern_seed) % (HEIGHT - region_height + 1)
    color = (240, 32 + spec.pattern_seed * 16, 16 + frame_index * 32)
    return x, y, region_width, region_height, color


def _frame(spec: FixtureSpec, frame_index: int) -> bytes:
    region_x, region_y, region_width, region_height, region_color = _region(spec, frame_index)
    content = bytearray()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if region_x <= x < region_x + region_width and region_y <= y < region_y + region_height:
                content.extend(region_color)
            else:
                content.extend(
                    (
                        (x * 17 + spec.pattern_seed * 31) % 256,
                        (y * 23 + spec.pattern_seed * 19) % 256,
                        ((x // 4 + y // 3 + frame_index + spec.pattern_seed) % 2) * 96,
                    )
                )
    return bytes(content)


def _compressed_time_to_sample(durations: tuple[int, ...]) -> list[tuple[int, int]]:
    entries: list[tuple[int, int]] = []
    for duration in durations:
        if entries and entries[-1][1] == duration:
            count, delta = entries[-1]
            entries[-1] = (count + 1, delta)
        else:
            entries.append((1, duration))
    return entries


def _sample_table(spec: FixtureSpec, frame_size: int, chunk_offset: int) -> bytes:
    compressor = b"VisualWorld RGB"
    compressor_name = bytes((len(compressor),)) + compressor + bytes(31 - len(compressor))
    sample_entry = _box(
        b"raw ",
        bytes(6)
        + struct.pack(">H", 1)
        + struct.pack(">HHIII", 0, 0, 0, 0, 0)
        + struct.pack(">HH", WIDTH, HEIGHT)
        + struct.pack(">II", 0x00480000, 0x00480000)
        + struct.pack(">I", 0)
        + struct.pack(">H", 1)
        + compressor_name
        + struct.pack(">Hh", 24, -1),
    )
    stsd = _full_box(b"stsd", 0, 0, struct.pack(">I", 1) + sample_entry)
    time_entries = _compressed_time_to_sample(spec.durations)
    stts = _full_box(
        b"stts",
        0,
        0,
        struct.pack(">I", len(time_entries))
        + b"".join(struct.pack(">II", count, delta) for count, delta in time_entries),
    )
    stsc = _full_box(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, len(spec.durations), 1))
    stsz = _full_box(b"stsz", 0, 0, struct.pack(">II", frame_size, len(spec.durations)))
    stco = _full_box(b"stco", 0, 0, struct.pack(">II", 1, chunk_offset))
    return _box(b"stbl", stsd + stts + stsc + stsz + stco)


def _movie(spec: FixtureSpec) -> tuple[bytes, tuple[bytes, ...]]:
    frames = tuple(_frame(spec, index) for index in range(len(spec.durations)))
    frame_size = WIDTH * HEIGHT * 3
    if any(len(frame) != frame_size for frame in frames):
        raise ValueError("Generated RGB frame has an unexpected size")

    ftyp = _box(b"ftyp", b"qt  " + struct.pack(">I", 0) + b"qt  ")
    mdat = _box(b"mdat", b"".join(frames))
    chunk_offset = len(ftyp) + 8
    duration = sum(spec.durations)

    mvhd = _full_box(
        b"mvhd",
        0,
        0,
        struct.pack(">IIII", 0, 0, TIME_SCALE, duration)
        + struct.pack(">IHHII", 0x00010000, 0x0100, 0, 0, 0)
        + _matrix(0)
        + bytes(24)
        + struct.pack(">I", 2),
    )
    tkhd = _full_box(
        b"tkhd",
        0,
        7,
        struct.pack(">IIIII", 0, 0, 1, 0, duration)
        + bytes(8)
        + struct.pack(">hhhh", 0, 0, 0, 0)
        + _matrix(spec.rotation_degrees)
        + struct.pack(">II", WIDTH << 16, HEIGHT << 16),
    )
    mdhd = _full_box(
        b"mdhd",
        0,
        0,
        struct.pack(">IIIIHH", 0, 0, TIME_SCALE, duration, 0x55C4, 0),
    )
    hdlr = _full_box(b"hdlr", 0, 0, struct.pack(">I4sIII", 0, b"vide", 0, 0, 0) + b"Video\0")
    vmhd = _full_box(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0))
    url = _full_box(b"url ", 0, 1, b"")
    dref = _full_box(b"dref", 0, 0, struct.pack(">I", 1) + url)
    dinf = _box(b"dinf", dref)
    stbl = _sample_table(spec, frame_size, chunk_offset)
    minf = _box(b"minf", vmhd + dinf + stbl)
    mdia = _box(b"mdia", mdhd + hdlr + minf)
    trak = _box(b"trak", tkhd + mdia)
    moov = _box(b"moov", mvhd + trak)
    return ftyp + mdat + moov, frames


def _frame_record(spec: FixtureSpec, index: int, pts: int, frame: bytes) -> dict[str, object]:
    x, y, width, height, color = _region(spec, index)
    return {
        "duration": str(spec.durations[index]),
        "moving_region": {
            "height": height,
            "rgb": list(color),
            "width": width,
            "x": x,
            "y": y,
        },
        "pts": str(pts),
        "rgb24_sha256": _sha256_bytes(frame),
    }


def expected_manifest() -> dict[str, object]:
    fixtures: list[dict[str, object]] = []
    for spec in SPECS:
        movie, frames = _movie(spec)
        pts = 0
        frame_records: list[dict[str, object]] = []
        for index, frame in enumerate(frames):
            frame_records.append(_frame_record(spec, index, pts, frame))
            pts += spec.durations[index]
        display_width = HEIGHT if spec.rotation_degrees == 90 else WIDTH
        display_height = WIDTH if spec.rotation_degrees == 90 else HEIGHT
        fixtures.append(
            {
                "byte_count": len(movie),
                "codec": "rawvideo",
                "container": "quicktime",
                "display_dimensions": {"height": display_height, "width": display_width},
                "encoded_dimensions": {"height": HEIGHT, "width": WIDTH},
                "filename": spec.filename,
                "fixture_id": spec.fixture_id,
                "frame_count": len(frames),
                "frames": frame_records,
                "pixel_format": "rgb24",
                "rotation_degrees": spec.rotation_degrees,
                "sha256": _sha256_bytes(movie),
                "time_base": {"denominator": str(TIME_SCALE), "numerator": "1"},
            }
        )
    return {
        "annotations": {
            "provenance": "Exact facts computed from the reviewed generator specification",
            "reviewed_on": "2026-09-06",
        },
        "fixture_set": {"id": "synthetic-v1", "version": 1},
        "fixtures": fixtures,
        "generator": {
            "path": "scripts/generate_synthetic_fixtures.py",
            "sha256": _sha256_file(Path(__file__).resolve()),
            "version": GENERATOR_VERSION,
        },
        "privacy": {
            "consent_status": "not-applicable-no-personal-data",
            "contains_faces": False,
            "contains_imported_assets": False,
            "contains_personal_data": False,
            "contains_plates": False,
            "contains_real_people": False,
            "classification": "public-synthetic-no-personal-data",
        },
        "rights": {
            "acquisition_method": "Generated locally from reviewed repository source",
            "allowed_uses": ["testing", "benchmarking", "redistribution", "modification"],
            "derivatives_allowed": True,
            "external_sources": [],
            "license_expression": "Apache-2.0",
            "owner": "VisualWorld contributors",
            "permission_basis": "Original project-generated synthetic work",
            "redistribution_allowed": True,
            "retention_requirement": "none",
            "source": "Algorithmic geometric RGB patterns; no imported media or fonts",
        },
        "schema": "visualworld.synthetic-fixture-manifest",
        "schema_version": 1,
    }


def validate_manifest(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("Fixture manifest must be a JSON object with string keys")
    manifest = cast(dict[str, object], value)
    if manifest != expected_manifest():
        raise ValueError("Fixture manifest does not exactly match the reviewed generator facts")
    return manifest


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to read fixture manifest: {path}") from error
    return validate_manifest(raw)


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _prepare_output(path: Path) -> Path:
    output = _absolute_without_resolving(path)
    if output.is_symlink():
        raise ValueError("Fixture output path must not be a symlink")
    if output.exists():
        if not output.is_dir():
            raise ValueError("Fixture output path must be a directory")
        if any(output.iterdir()):
            raise ValueError("Fixture output directory must be empty")
    else:
        output.mkdir(parents=True)
    return output


def _write_exclusive(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _fixture_records(manifest: dict[str, object]) -> list[dict[str, object]]:
    value = manifest["fixtures"]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("Validated fixture list has an unexpected shape")
    return cast(list[dict[str, object]], value)


def scan_output(output: Path, manifest: dict[str, object] | None = None) -> None:
    reviewed = manifest or load_manifest()
    fixtures = _fixture_records(reviewed)
    expected_names = {cast(str, fixture["filename"]) for fixture in fixtures}
    actual_names = {path.name for path in output.iterdir()}
    if actual_names != expected_names:
        raise ValueError("Generated fixture output contains missing or unexpected files")
    total = 0
    for fixture in fixtures:
        path = output / cast(str, fixture["filename"])
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Generated fixture is not a regular file: {path.name}")
        byte_count = path.stat().st_size
        total += byte_count
        if byte_count > MAX_FILE_BYTES or byte_count != fixture["byte_count"]:
            raise ValueError(f"Generated fixture size mismatch: {path.name}")
        if _sha256_file(path) != fixture["sha256"]:
            raise ValueError(f"Generated fixture checksum mismatch: {path.name}")
    if total > MAX_TOTAL_BYTES:
        raise ValueError("Generated fixture set exceeds its reviewed total byte limit")


def _peak_rss_bytes() -> int:
    maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(maximum if sys.platform == "darwin" else maximum * 1024)


def generate(output_path: Path) -> dict[str, object]:
    manifest = load_manifest()
    output = _prepare_output(output_path)
    started = time.perf_counter_ns()
    for spec in SPECS:
        movie, _ = _movie(spec)
        _write_exclusive(output / spec.filename, movie)
    scan_output(output, manifest)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "files": len(SPECS),
        "fixture_set": "synthetic-v1",
        "peak_rss_bytes": _peak_rss_bytes(),
        "status": "ok",
        "total_bytes": sum((output / spec.filename).stat().st_size for spec in SPECS),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        receipt = generate(args.output)
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.exit(1, f"Fixture generation failed: {error}\n")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
