#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate the locked, rights-safe v0.2 sampling research clips."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import NoReturn, TextIO, cast

import prepare_v02_detection_dataset as detection_prep

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "fixtures" / "v02-sampling-research"
SOURCE_MANIFEST = FIXTURE_ROOT / "source-manifest.json"
ANNOTATION_LOCK = FIXTURE_ROOT / "annotations.json"
DETECTION_ANNOTATIONS = ROOT / "fixtures" / "v02-detection-research" / "annotations.json"
DETECTION_SOURCE_MANIFEST = ROOT / "fixtures" / "v02-detection-research" / "source-manifest.json"
WIDTH = 384
HEIGHT = 216
TIME_SCALE = 1000
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
_IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class SamplingPreparationError(RuntimeError):
    """A stable sampling-dataset preparation failure."""


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise SamplingPreparationError("invalid_arguments")


def _fail(code: str) -> NoReturn:
    raise SamplingPreparationError(code)


def _silence_stream(stream: TextIO) -> None:
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return
    try:
        null_descriptor = os.open(os.devnull, os.O_WRONLY | os.O_CLOEXEC)
    except OSError:
        return
    if null_descriptor == descriptor:
        return
    try:
        os.dup2(null_descriptor, descriptor)
    except OSError:
        pass
    finally:
        with suppress(OSError):
            os.close(null_descriptor)


def _emit(value: object, stream: TextIO) -> bool:
    try:
        stream.write(json.dumps(value, allow_nan=False, sort_keys=True) + "\n")
        stream.flush()
    except (AttributeError, OSError, UnicodeError, ValueError):
        _silence_stream(stream)
        return False
    return True


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _same_json(value: object, expected: object) -> bool:
    try:
        return _canonical(value) == _canonical(expected)
    except (TypeError, UnicodeError, ValueError):
        return False


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_json(path: Path, code: str) -> tuple[dict[str, object], str]:
    try:
        raw = detection_prep._regular_file(path, maximum=1024 * 1024)
        value = json.loads(
            raw,
            object_pairs_hook=detection_prep._unique_object,
            parse_constant=detection_prep._reject_constant,
        )
    except (OSError, ValueError, detection_prep.PreparationError) as error:
        raise SamplingPreparationError(code) from error
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        _fail(code)
    return cast(dict[str, object], value), _sha256(raw)


def _integer(value: object, code: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(code)
    return value


def _text(value: object, code: str, maximum: int = 256) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        _fail(code)
    return value


def _durations() -> tuple[int, ...]:
    durations = [67 if index % 3 != 2 else 66 for index in range(86)]
    durations[70] += 266
    if sum(durations) != 6000:
        _fail("invalid_timing")
    return tuple(durations)


def _box(kind: bytes, payload: bytes) -> bytes:
    if len(kind) != 4 or len(payload) + 8 > 0xFFFFFFFF:
        _fail("clip_generation_failed")
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def _full_box(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return _box(kind, bytes((version,)) + flags.to_bytes(3, "big") + payload)


def _matrix() -> bytes:
    return struct.pack(">9i", 65_536, 0, 0, 0, 65_536, 0, 0, 0, 1_073_741_824)


def _time_entries(durations: tuple[int, ...]) -> list[tuple[int, int]]:
    entries: list[tuple[int, int]] = []
    for duration in durations:
        if entries and entries[-1][1] == duration:
            count, delta = entries[-1]
            entries[-1] = (count + 1, delta)
        else:
            entries.append((1, duration))
    return entries


def _movie(frames: tuple[bytes, ...], durations: tuple[int, ...]) -> bytes:
    frame_size = WIDTH * HEIGHT * 3
    if len(frames) != len(durations) or any(len(frame) != frame_size for frame in frames):
        _fail("clip_generation_failed")
    ftyp = _box(b"ftyp", b"qt  " + struct.pack(">I", 0) + b"qt  ")
    mdat = _box(b"mdat", b"".join(frames))
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
    entries = _time_entries(durations)
    stts = _full_box(
        b"stts",
        0,
        0,
        struct.pack(">I", len(entries))
        + b"".join(struct.pack(">II", count, delta) for count, delta in entries),
    )
    stsc = _full_box(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, len(frames), 1))
    stsz = _full_box(b"stsz", 0, 0, struct.pack(">II", frame_size, len(frames)))
    stco = _full_box(b"stco", 0, 0, struct.pack(">II", 1, len(ftyp) + 8))
    stbl = _box(b"stbl", stsd + stts + stsc + stsz + stco)
    vmhd = _full_box(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0))
    url = _full_box(b"url ", 0, 1, b"")
    dref = _full_box(b"dref", 0, 0, struct.pack(">I", 1) + url)
    minf = _box(b"minf", vmhd + _box(b"dinf", dref) + stbl)
    duration = sum(durations)
    mdhd = _full_box(
        b"mdhd",
        0,
        0,
        struct.pack(">IIIIHH", 0, 0, TIME_SCALE, duration, 0x55C4, 0),
    )
    hdlr = _full_box(b"hdlr", 0, 0, struct.pack(">I4sIII", 0, b"vide", 0, 0, 0) + b"Video\0")
    mdia = _box(b"mdia", mdhd + hdlr + minf)
    tkhd = _full_box(
        b"tkhd",
        0,
        7,
        struct.pack(">IIIII", 0, 0, 1, 0, duration)
        + bytes(8)
        + struct.pack(">hhhh", 0, 0, 0, 0)
        + _matrix()
        + struct.pack(">II", WIDTH << 16, HEIGHT << 16),
    )
    mvhd = _full_box(
        b"mvhd",
        0,
        0,
        struct.pack(">IIII", 0, 0, TIME_SCALE, duration)
        + struct.pack(">IHHII", 0x00010000, 0x0100, 0, 0, 0)
        + _matrix()
        + bytes(24)
        + struct.pack(">I", 2),
    )
    movie = ftyp + mdat + _box(b"moov", mvhd + _box(b"trak", tkhd + mdia))
    if len(movie) > MAX_OUTPUT_BYTES:
        _fail("clip_generation_failed")
    return movie


def _base_background(seed: int, after_cut: bool) -> bytes:
    output = bytearray(WIDTH * HEIGHT * 3)
    horizon = 94 + seed % 9
    offset = 37 if after_cut else 0
    for y in range(HEIGHT):
        if y < horizon:
            color = (
                84 + (seed + offset) % 37 + y // 11,
                132 + (seed + offset) % 31 + y // 14,
                178 + (seed + offset) % 29 + y // 19,
            )
        else:
            shade = 63 + (y - horizon) // 5 + (offset // 6)
            color = (shade, min(255, shade + 2), min(255, shade + 5))
        output[y * WIDTH * 3 : (y + 1) * WIDTH * 3] = bytes(color) * WIDTH
    for y in range(horizon + 18, HEIGHT, 28):
        left = WIDTH // 2 - max(2, (y - horizon) // 12)
        right = WIDTH // 2 + max(2, (y - horizon) // 12)
        detection_prep.fill_box(output, WIDTH, HEIGHT, (left, y, right, y + 2), (224, 211, 142))
    return bytes(output)


def _shift_rows(pixels: bytes, shift: int) -> bytearray:
    normalized = shift % WIDTH
    if normalized == 0:
        return bytearray(pixels)
    output = bytearray(len(pixels))
    offset = normalized * 3
    row_bytes = WIDTH * 3
    for y in range(HEIGHT):
        start = y * row_bytes
        row = pixels[start : start + row_bytes]
        output[start : start + row_bytes] = row[-offset:] + row[:-offset]
    return output


def _paste(
    canvas: bytearray,
    chip: bytes,
    chip_width: int,
    chip_height: int,
    left: int,
    top: int,
) -> None:
    for y in range(chip_height):
        source_start = y * chip_width * 3
        target_start = ((top + y) * WIDTH + left) * 3
        canvas[target_start : target_start + chip_width * 3] = chip[
            source_start : source_start + chip_width * 3
        ]


def _event_at(
    events: list[dict[str, object]], phase_ms: int, pts_ms: int
) -> tuple[dict[str, object], int, int] | None:
    for event in events:
        start = cast(int, event["event_offset_ms"]) + phase_ms
        end = start + cast(int, event["duration_ms"])
        if start <= pts_ms < end:
            return event, start, end
    return None


def _frame(
    *,
    base_before: bytes,
    base_after: bytes,
    resized: dict[str, tuple[int, int, bytes]],
    events: list[dict[str, object]],
    phase_ms: int,
    pts_ms: int,
    cut_ms: int,
) -> tuple[bytes, str | None, list[int] | None]:
    active = _event_at(events, phase_ms, pts_ms)
    camera_shift = 0
    if active is not None and active[0]["stratum"] == "camera_motion":
        camera_shift = (pts_ms - active[1]) // 34
    canvas = _shift_rows(base_after if pts_ms >= cut_ms else base_before, camera_shift)
    if active is None:
        return bytes(canvas), None, None
    event, start, end = active
    stratum = cast(str, event["stratum"])
    chip_width, chip_height, chip = resized[stratum]
    progress_numerator = pts_ms - start
    progress_denominator = max(1, end - start - 1)
    start_x = cast(int, event["start_x"])
    end_x = cast(int, event["end_x"])
    left = start_x + (end_x - start_x) * progress_numerator // progress_denominator
    left = max(0, min(WIDTH - chip_width, left))
    top = min(HEIGHT - chip_height, cast(int, event["top"]))
    _paste(canvas, chip, chip_width, chip_height, left, top)
    box = [left, top, left + chip_width, top + chip_height]
    if stratum == "camera_motion":
        occlusion_start = start + cast(int, event["occlusion_start_offset_ms"])
        occlusion_end = start + cast(int, event["occlusion_end_offset_ms"])
        if occlusion_start <= pts_ms < occlusion_end:
            third = max(1, chip_width // 3)
            detection_prep.fill_box(
                canvas,
                WIDTH,
                HEIGHT,
                (left + third, top, min(left + 2 * third, left + chip_width), top + chip_height),
                (91, 93, 96),
            )
    return bytes(canvas), stratum, box


def _validate_source_manifest(
    manifest: dict[str, object], detection_annotations: dict[str, object]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if set(manifest) != {
        "canvas",
        "clips",
        "derivation",
        "license_expression",
        "privacy",
        "schema",
        "schema_version",
        "source_detection_annotation_sha256",
        "source_detection_manifest_sha256",
        "terms_url",
    }:
        _fail("invalid_manifest")
    if (
        manifest["schema"] != "visualworld.v02-sampling-source-manifest"
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["license_expression"] != "CC0-1.0"
        or manifest["terms_url"] != "https://creativecommons.org/publicdomain/zero/1.0/legalcode"
    ):
        _fail("invalid_manifest")
    canvas = manifest["canvas"]
    if not isinstance(canvas, dict) or not _same_json(
        canvas, {"height": HEIGHT, "pixel_format": "rgb24", "width": WIDTH}
    ):
        _fail("invalid_manifest")
    clips = manifest["clips"]
    derivation = manifest["derivation"]
    if not isinstance(clips, list) or len(clips) != 10 or not isinstance(derivation, dict):
        _fail("invalid_manifest")
    expected_events: list[dict[str, object]] = [
        {
            "duration_ms": 900,
            "end_x": 270,
            "event_offset_ms": 300,
            "start_x": 10,
            "stratum": "small_fast",
            "target_width": 144,
            "top": 136,
        },
        {
            "duration_ms": 1400,
            "end_x": 115,
            "event_offset_ms": 1450,
            "occlusion_end_offset_ms": 850,
            "occlusion_start_offset_ms": 550,
            "start_x": 90,
            "stratum": "camera_motion",
            "target_width": 180,
            "top": 100,
        },
        {
            "duration_ms": 880,
            "end_x": 105,
            "event_offset_ms": 3020,
            "start_x": 92,
            "stratum": "cut_adjacent",
            "target_width": 200,
            "top": 92,
        },
        {
            "duration_ms": 1500,
            "end_x": 80,
            "event_offset_ms": 4200,
            "start_x": 210,
            "stratum": "vfr_gap",
            "target_width": 150,
            "top": 112,
        },
    ]
    if not _same_json(
        derivation,
        {
            "background": "visualworld-generated-moving-road-v1",
            "clip_duration_ms": 6000,
            "cut_base_ms": 3000,
            "events": expected_events,
            "frame_count": 86,
            "interpolation": "visualworld-fixed-point-bilinear-v1",
            "time_base": {"denominator": 1000, "numerator": 1},
            "timing": "67,67,66 ms cadence with frame 70 extended by 266 ms; total 6000 ms",
            "vfr_gap_frame_index": 70,
        },
    ):
        _fail("invalid_manifest")
    if not _same_json(
        manifest["privacy"],
        {
            "contains_faces": False,
            "contains_personal_data": False,
            "contains_plates": False,
            "contains_real_people": False,
            "source_boundary": "only plate-sanitized issue-21 RGB derivatives are accepted",
        },
    ):
        _fail("invalid_manifest")
    for field in (
        "source_detection_annotation_sha256",
        "source_detection_manifest_sha256",
    ):
        digest = _text(manifest[field], "invalid_manifest", 64)
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            _fail("invalid_manifest")
    items_value = detection_annotations.get("items")
    if (
        set(detection_annotations)
        != {"items", "schema", "schema_version", "source_manifest_sha256"}
        or detection_annotations.get("schema") != "visualworld.v02-detection-annotation-lock"
        or type(detection_annotations.get("schema_version")) is not int
        or detection_annotations.get("schema_version") != 1
        or not isinstance(items_value, list)
    ):
        _fail("invalid_manifest")
    detection_source_digest = _text(
        detection_annotations.get("source_manifest_sha256"), "invalid_manifest", 64
    )
    if re.fullmatch(r"[0-9a-f]{64}", detection_source_digest) is None:
        _fail("invalid_manifest")
    events = expected_events
    detection_items: dict[tuple[str, str], dict[str, object]] = {}
    for raw_item in items_value:
        if not isinstance(raw_item, dict) or set(raw_item) != {
            "frame_sha256",
            "height",
            "item_id",
            "object_box",
            "relative_path",
            "source_id",
            "split",
            "stratum",
            "width",
        }:
            _fail("invalid_manifest")
        item = cast(dict[str, object], raw_item)
        source_id = _text(item["source_id"], "invalid_manifest")
        detection_split = _text(item["split"], "invalid_manifest")
        stratum = _text(item["stratum"], "invalid_manifest")
        item_id = _text(item["item_id"], "invalid_manifest")
        relative_path = _text(item["relative_path"], "invalid_manifest")
        digest = _text(item["frame_sha256"], "invalid_manifest", 64)
        if (
            _IDENTIFIER.fullmatch(source_id) is None
            or detection_split not in {"calibration", "test"}
            or stratum not in {"easy", "small_distant"}
            or item_id != f"{detection_split}-{source_id}-{stratum}"
            or relative_path != f"{detection_split}/{item_id}.ppm"
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or _integer(item["width"], "invalid_manifest", 640, 640) != 640
            or _integer(item["height"], "invalid_manifest", 360, 360) != 360
        ):
            _fail("invalid_manifest")
        try:
            detection_prep._box(item["object_box"], 640, 360)
        except detection_prep.PreparationError as error:
            raise SamplingPreparationError("invalid_manifest") from error
        key = (source_id, stratum)
        if key in detection_items:
            _fail("invalid_manifest")
        detection_items[key] = item
    easy_items = {
        source_id: item
        for (source_id, stratum), item in detection_items.items()
        if stratum == "easy"
    }
    if len(detection_items) != 20 or len(easy_items) != 10:
        _fail("invalid_manifest")
    seen_clips: set[str] = set()
    seen_sources: set[str] = set()
    for raw_clip in clips:
        if not isinstance(raw_clip, dict) or set(raw_clip) != {
            "background_seed",
            "clip_id",
            "input_frame_sha256",
            "input_object_box",
            "input_relative_path",
            "phase_ms",
            "source_id",
            "split",
        }:
            _fail("invalid_manifest")
        clip = cast(dict[str, object], raw_clip)
        clip_id = _text(clip["clip_id"], "invalid_manifest")
        source_id = _text(clip["source_id"], "invalid_manifest")
        input_relative = PurePosixPath(_text(clip["input_relative_path"], "invalid_manifest"))
        clip_split = _text(clip["split"], "invalid_manifest")
        if (
            _IDENTIFIER.fullmatch(clip_id) is None
            or _IDENTIFIER.fullmatch(source_id) is None
            or input_relative.is_absolute()
            or any(part in {"", ".", ".."} for part in input_relative.parts)
            or clip_id in seen_clips
            or source_id in seen_sources
            or clip_split not in {"calibration", "test"}
            or not clip_id.startswith(f"{clip_split}-")
            or not _same_json(
                easy_items.get(source_id),
                {
                    "frame_sha256": clip["input_frame_sha256"],
                    "height": 360,
                    "item_id": f"{clip_split}-{source_id}-easy",
                    "object_box": clip["input_object_box"],
                    "relative_path": clip["input_relative_path"],
                    "source_id": source_id,
                    "split": clip_split,
                    "stratum": "easy",
                    "width": 640,
                },
            )
        ):
            _fail("invalid_manifest")
        _integer(clip["background_seed"], "invalid_manifest", 0, 255)
        _integer(clip["phase_ms"], "invalid_manifest", 0, 100)
        input_digest = _text(clip["input_frame_sha256"], "invalid_manifest", 64)
        if re.fullmatch(r"[0-9a-f]{64}", input_digest) is None:
            _fail("invalid_manifest")
        try:
            detection_prep._box(clip["input_object_box"], 640, 360)
        except detection_prep.PreparationError as error:
            raise SamplingPreparationError("invalid_manifest") from error
        seen_clips.add(clip_id)
        seen_sources.add(source_id)
    if sum(clip["split"] == "calibration" for clip in clips) != 4:
        _fail("invalid_manifest")
    return cast(list[dict[str, object]], clips), events


def prepare(input_root: Path, output_root: Path) -> dict[str, object]:
    manifest, manifest_sha256 = _load_json(SOURCE_MANIFEST, "invalid_manifest")
    detection_annotations, detection_annotation_sha256 = _load_json(
        DETECTION_ANNOTATIONS, "invalid_manifest"
    )
    _, detection_source_sha256 = _load_json(DETECTION_SOURCE_MANIFEST, "invalid_manifest")
    if (
        manifest.get("source_detection_annotation_sha256") != detection_annotation_sha256
        or manifest.get("source_detection_manifest_sha256") != detection_source_sha256
    ):
        _fail("invalid_manifest")
    clips, events = _validate_source_manifest(manifest, detection_annotations)
    durations = _durations()
    output_descriptor = detection_prep._create_private_directory(output_root)
    annotation_clips: list[dict[str, object]] = []
    annotation_events: list[dict[str, object]] = []
    try:
        for clip in clips:
            relative_path = cast(str, clip["input_relative_path"])
            try:
                ppm = detection_prep._regular_file(
                    input_root / relative_path, maximum=MAX_INPUT_BYTES
                )
                if _sha256(ppm) != clip["input_frame_sha256"]:
                    _fail("source_digest_mismatch")
                source_width, source_height, pixels = detection_prep.parse_ppm(ppm)
                box = cast(list[int], clip["input_object_box"])
                chip_width, chip_height, chip = detection_prep.crop_rgb24(
                    pixels,
                    source_width,
                    source_height,
                    (box[0], box[1], box[2], box[3]),
                )
            except detection_prep.PreparationError as error:
                raise SamplingPreparationError("invalid_source") from error
            resized: dict[str, tuple[int, int, bytes]] = {}
            for event in events:
                target_width = cast(int, event["target_width"])
                target_height = max(1, chip_height * target_width // chip_width)
                resized[cast(str, event["stratum"])] = (
                    target_width,
                    target_height,
                    detection_prep.resize_rgb24(
                        chip, chip_width, chip_height, target_width, target_height
                    ),
                )
            phase_ms = cast(int, clip["phase_ms"])
            cut_ms = cast(int, cast(dict[str, object], manifest["derivation"])["cut_base_ms"])
            cut_ms += phase_ms
            base_before = _base_background(cast(int, clip["background_seed"]), False)
            base_after = _base_background(cast(int, clip["background_seed"]), True)
            frames: list[bytes] = []
            frame_records: list[dict[str, object]] = []
            pts_ms = 0
            for frame_index, duration_ms in enumerate(durations):
                frame, stratum, object_box = _frame(
                    base_before=base_before,
                    base_after=base_after,
                    resized=resized,
                    events=events,
                    phase_ms=phase_ms,
                    pts_ms=pts_ms,
                    cut_ms=cut_ms,
                )
                frames.append(frame)
                event_id = (
                    None if stratum is None else f"{clip['clip_id']}-{stratum.replace('_', '-')}"
                )
                frame_records.append(
                    {
                        "duration_ms": duration_ms,
                        "event_id": event_id,
                        "frame_index": frame_index,
                        "object_box": object_box,
                        "pts_ms": pts_ms,
                        "rgb24_sha256": _sha256(frame),
                        "stratum": stratum,
                    }
                )
                pts_ms += duration_ms
            movie = _movie(tuple(frames), durations)
            output_name = f"{clip['split']}/{clip['clip_id']}.mov"
            detection_prep._write_new(output_descriptor, output_name, movie)
            annotation_clips.append(
                {
                    "byte_count": len(movie),
                    "clip_id": clip["clip_id"],
                    "cut_ms": cut_ms,
                    "duration_ms": sum(durations),
                    "frame_count": len(frames),
                    "frames": frame_records,
                    "relative_path": output_name,
                    "sha256": _sha256(movie),
                    "source_id": clip["source_id"],
                    "split": clip["split"],
                }
            )
            for event in events:
                start_ms = cast(int, event["event_offset_ms"]) + phase_ms
                stratum = cast(str, event["stratum"])
                annotation_events.append(
                    {
                        "clip_id": clip["clip_id"],
                        "end_ms": start_ms + cast(int, event["duration_ms"]),
                        "event_id": f"{clip['clip_id']}-{stratum.replace('_', '-')}",
                        "source_id": clip["source_id"],
                        "split": clip["split"],
                        "start_ms": start_ms,
                        "strata": ["overall", stratum],
                    }
                )
        lock: dict[str, object] = {
            "clips": annotation_clips,
            "events": annotation_events,
            "schema": "visualworld.v02-sampling-annotation-lock",
            "schema_version": 1,
            "source_manifest_sha256": manifest_sha256,
        }
        serialized = (
            json.dumps(lock, allow_nan=False, indent=2, sort_keys=True).encode("ascii") + b"\n"
        )
        detection_prep._write_new(output_descriptor, "annotations.generated.json", serialized)
    finally:
        detection_prep._close_once(output_descriptor)
    if ANNOTATION_LOCK.exists():
        expected, _ = _load_json(ANNOTATION_LOCK, "invalid_annotation_lock")
        if _canonical(expected) != _canonical(lock):
            _fail("annotation_lock_mismatch")
    return lock


def main() -> int:
    parser = _StableArgumentParser(description=__doc__)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    try:
        arguments = parser.parse_args()
        lock = prepare(arguments.input_root.resolve(), Path(os.path.abspath(arguments.output)))
    except (OSError, SamplingPreparationError, detection_prep.PreparationError) as error:
        code = str(error) if isinstance(error, SamplingPreparationError) else "filesystem_failed"
        _emit({"error": code, "status": "error"}, sys.stdout)
        return 1
    return (
        0
        if _emit(
            {
                "clip_count": len(cast(list[object], lock["clips"])),
                "event_count": len(cast(list[object], lock["events"])),
                "status": "pass",
            },
            sys.stdout,
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
