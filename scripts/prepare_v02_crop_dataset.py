#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build the locked, privacy-safe v0.2 original-crop research clips."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
from pathlib import Path, PurePosixPath
from typing import NoReturn, TextIO, cast

import prepare_v02_detection_dataset as detection_prep

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "fixtures" / "v02-crop-research"
SOURCE_MANIFEST = FIXTURE_ROOT / "source-manifest.json"
CANDIDATES = FIXTURE_ROOT / "candidates.json"
ANNOTATION_LOCK = FIXTURE_ROOT / "annotations.json"
DETECTION_ANNOTATIONS = ROOT / "fixtures" / "v02-detection-research" / "annotations.json"
DETECTION_SOURCE_MANIFEST = ROOT / "fixtures" / "v02-detection-research" / "source-manifest.json"
SOURCE_WIDTH = 3840
SOURCE_HEIGHT = 2160
DETECTOR_WIDTH = 384
DETECTOR_HEIGHT = 384
FRAME_DURATION_MS = 1000
STRATA = ("tiny", "small", "medium")
TASKS = ("face_visibility", "plate_ocr", "vehicle_detail")
GRID_WIDTH = 24
GRID_HEIGHT = 8
_MAX_JSON_BYTES = 1024 * 1024
_MAX_INPUT_BYTES = 4 * 1024 * 1024
_IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class CropPreparationError(RuntimeError):
    """A stable crop research dataset preparation failure."""


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise CropPreparationError("invalid_arguments")

    def _print_message(self, message: str, file: object | None = None) -> None:
        if message:
            _write_text(message, sys.stderr if file is None else cast(TextIO, file))


def _fail(code: str) -> NoReturn:
    raise CropPreparationError(code)


def _write_text(value: str, stream: TextIO) -> bool:
    try:
        stream.write(value)
        stream.flush()
    except (AttributeError, OSError, UnicodeError, ValueError):
        return False
    return True


def _emit(value: object, stream: TextIO) -> bool:
    try:
        serialized = json.dumps(value, allow_nan=False, sort_keys=True) + "\n"
    except (TypeError, UnicodeError, ValueError):
        return False
    return _write_text(serialized, stream)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, UnicodeError, ValueError) as error:
        raise CropPreparationError("invalid_manifest") from error


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_json(path: Path, code: str) -> tuple[dict[str, object], str]:
    try:
        raw = detection_prep._regular_file(path, maximum=_MAX_JSON_BYTES)
        value = json.loads(raw)
    except (OSError, UnicodeError, ValueError, detection_prep.PreparationError) as error:
        raise CropPreparationError(code) from error
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        _fail(code)
    return cast(dict[str, object], value), _sha256(raw)


def _mapping(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        _fail(code)
    return cast(dict[str, object], value)


def _text(value: object, code: str, maximum: int = 256) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or not value.isascii()
        or any(ord(character) < 0x20 for character in value)
    ):
        _fail(code)
    return value


def _integer(value: object, code: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(code)
    return value


def _digest(value: object, code: str) -> str:
    selected = _text(value, code, 64)
    if re.fullmatch(r"[0-9a-f]{64}", selected) is None:
        _fail(code)
    return selected


def _same_json(first: object, second: object) -> bool:
    return _canonical(first) == _canonical(second)


def pattern_bits(task: str, label: int) -> tuple[int, ...]:
    """Return one frozen 8x8 pseudo-glyph from its public task and label."""

    if task not in TASKS or type(label) is not int or not 0 <= label < 8:
        _fail("invalid_pattern")
    digest = hashlib.sha256(f"visualworld-v02-crop:{task}:{label}".encode("ascii")).digest()
    return tuple((digest[index // 8] >> (7 - index % 8)) & 1 for index in range(64))


def assigned_labels(source_id: str, stratum: str) -> dict[str, int]:
    """Assign deterministic, non-identifying labels to one generated frame."""

    if _IDENTIFIER.fullmatch(source_id) is None or stratum not in STRATA:
        _fail("invalid_pattern")
    return {
        task: hashlib.sha256(f"{source_id}:{stratum}:{task}".encode("ascii")).digest()[0] % 8
        for task in TASKS
    }


def combined_pattern(labels: dict[str, int]) -> tuple[int, ...]:
    if set(labels) != set(TASKS):
        _fail("invalid_pattern")
    rows: list[int] = []
    task_patterns = {task: pattern_bits(task, labels[task]) for task in TASKS}
    for row in range(GRID_HEIGHT):
        for task in TASKS:
            start = row * 8
            rows.extend(task_patterns[task][start : start + 8])
    return tuple(rows)


def _source_box(detector_box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    left, top, right, bottom = detector_box
    if any(value < 0 for value in detector_box) or not (
        left < right <= DETECTOR_WIDTH and top < bottom <= DETECTOR_HEIGHT
    ):
        _fail("invalid_manifest")
    numerators = (
        left * SOURCE_WIDTH,
        top * SOURCE_HEIGHT,
        right * SOURCE_WIDTH,
        bottom * SOURCE_HEIGHT,
    )
    denominators = (DETECTOR_WIDTH, DETECTOR_HEIGHT, DETECTOR_WIDTH, DETECTOR_HEIGHT)
    if any(
        numerator % denominator
        for numerator, denominator in zip(numerators, denominators, strict=True)
    ):
        _fail("invalid_manifest")
    return cast(
        tuple[int, int, int, int],
        tuple(n // d for n, d in zip(numerators, denominators, strict=True)),
    )


def _background(seed: int) -> bytearray:
    output = bytearray(SOURCE_WIDTH * SOURCE_HEIGHT * 3)
    horizon = 810 + seed % 91
    for y in range(SOURCE_HEIGHT):
        if y < horizon:
            color = (86 + seed % 23 + y // 180, 137 + seed % 17, 184 + seed % 29)
        else:
            shade = min(164, 58 + (y - horizon) // 22 + seed % 13)
            color = (shade, min(255, shade + 2), min(255, shade + 5))
        start = y * SOURCE_WIDTH * 3
        output[start : start + SOURCE_WIDTH * 3] = bytes(color) * SOURCE_WIDTH
    for y in range(horizon + 180, SOURCE_HEIGHT, 240):
        half = max(12, (y - horizon) // 16)
        detection_prep.fill_box(
            output,
            SOURCE_WIDTH,
            SOURCE_HEIGHT,
            (SOURCE_WIDTH // 2 - half, y, SOURCE_WIDTH // 2 + half, min(SOURCE_HEIGHT, y + 12)),
            (224, 211, 142),
        )
    return output


def _paste(
    canvas: bytearray,
    chip: bytes,
    chip_width: int,
    chip_height: int,
    box: tuple[int, int, int, int],
) -> None:
    left, top, right, bottom = box
    if (right - left, bottom - top) != (chip_width, chip_height):
        _fail("clip_generation_failed")
    for row in range(chip_height):
        source_start = row * chip_width * 3
        target_start = ((top + row) * SOURCE_WIDTH + left) * 3
        canvas[target_start : target_start + chip_width * 3] = chip[
            source_start : source_start + chip_width * 3
        ]


def render_panel(
    canvas: bytearray,
    box: tuple[int, int, int, int],
    bits: tuple[int, ...],
    *,
    detail_present: bool,
) -> None:
    """Render a frozen binary panel or a source-detail-absent gray control."""

    if len(bits) != GRID_WIDTH * GRID_HEIGHT:
        _fail("invalid_pattern")
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    for y in range(top, bottom):
        row = min(GRID_HEIGHT - 1, (y - top) * GRID_HEIGHT // height)
        for x in range(left, right):
            column = min(GRID_WIDTH - 1, (x - left) * GRID_WIDTH // width)
            value = (16 if bits[row * GRID_WIDTH + column] else 240) if detail_present else 127
            offset = (y * SOURCE_WIDTH + x) * 3
            canvas[offset : offset + 3] = bytes((value, value, value))


def _box_atom(kind: bytes, payload: bytes) -> bytes:
    if len(kind) != 4 or len(payload) + 8 > 0xFFFFFFFF:
        _fail("clip_generation_failed")
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def _full_box(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return _box_atom(kind, bytes((version,)) + flags.to_bytes(3, "big") + payload)


def _matrix() -> bytes:
    return struct.pack(">9i", 65_536, 0, 0, 0, 65_536, 0, 0, 0, 1_073_741_824)


def rawvideo_movie(frames: tuple[bytes, ...], width: int, height: int) -> bytes:
    """Wrap three packed RGB24 frames in a deterministic raw QuickTime movie."""

    frame_size = width * height * 3
    if (
        type(width) is not int
        or type(height) is not int
        or not 1 <= width <= 4096
        or not 1 <= height <= 4096
        or len(frames) != len(STRATA)
        or any(type(frame) is not bytes or len(frame) != frame_size for frame in frames)
    ):
        _fail("clip_generation_failed")
    duration = len(frames) * FRAME_DURATION_MS
    ftyp = _box_atom(b"ftyp", b"qt  " + struct.pack(">I", 0) + b"qt  ")
    mdat = _box_atom(b"mdat", b"".join(frames))
    compressor = b"VisualWorld RGB"
    compressor_name = bytes((len(compressor),)) + compressor + bytes(31 - len(compressor))
    sample_entry = _box_atom(
        b"raw ",
        bytes(6)
        + struct.pack(">H", 1)
        + struct.pack(">HHIII", 0, 0, 0, 0, 0)
        + struct.pack(">HH", width, height)
        + struct.pack(">II", 0x00480000, 0x00480000)
        + struct.pack(">I", 0)
        + struct.pack(">H", 1)
        + compressor_name
        + struct.pack(">Hh", 24, -1),
    )
    stsd = _full_box(b"stsd", 0, 0, struct.pack(">I", 1) + sample_entry)
    stts = _full_box(b"stts", 0, 0, struct.pack(">III", 1, len(frames), FRAME_DURATION_MS))
    stsc = _full_box(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, len(frames), 1))
    stsz = _full_box(b"stsz", 0, 0, struct.pack(">II", frame_size, len(frames)))
    stco = _full_box(b"stco", 0, 0, struct.pack(">II", 1, len(ftyp) + 8))
    stbl = _box_atom(b"stbl", stsd + stts + stsc + stsz + stco)
    vmhd = _full_box(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0))
    url = _full_box(b"url ", 0, 1, b"")
    dref = _full_box(b"dref", 0, 0, struct.pack(">I", 1) + url)
    minf = _box_atom(b"minf", vmhd + _box_atom(b"dinf", dref) + stbl)
    mdhd = _full_box(b"mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, 1000, duration, 0x55C4, 0))
    hdlr = _full_box(b"hdlr", 0, 0, struct.pack(">I4sIII", 0, b"vide", 0, 0, 0) + b"Video\0")
    mdia = _box_atom(b"mdia", mdhd + hdlr + minf)
    tkhd = _full_box(
        b"tkhd",
        0,
        7,
        struct.pack(">IIIII", 0, 0, 1, 0, duration)
        + bytes(8)
        + struct.pack(">hhhh", 0, 0, 0, 0)
        + _matrix()
        + struct.pack(">II", width << 16, height << 16),
    )
    mvhd = _full_box(
        b"mvhd",
        0,
        0,
        struct.pack(">IIII", 0, 0, 1000, duration)
        + struct.pack(">IHHII", 0x00010000, 0x0100, 0, 0, 0)
        + _matrix()
        + bytes(24)
        + struct.pack(">I", 2),
    )
    return ftyp + mdat + _box_atom(b"moov", mvhd + _box_atom(b"trak", tkhd + mdia))


def _validate_candidates(value: dict[str, object]) -> None:
    if set(value) != {
        "baseline",
        "candidate",
        "decision",
        "detector",
        "metric",
        "schema",
        "schema_version",
        "specialist",
    }:
        _fail("invalid_candidate_manifest")
    if value["schema"] != "visualworld.v02-crop-candidates" or value["schema_version"] != 1:
        _fail("invalid_candidate_manifest")
    detector = _mapping(value["detector"], "invalid_candidate_manifest")
    specialist = _mapping(value["specialist"], "invalid_candidate_manifest")
    if (
        detector.get("candidate_manifest_sha256")
        != "38afb812c6681700c15b30874e91ac649f1dde521354a5ce5c27d1b4e7bafcee"
        or detector.get("name") != "vehicle-detection-0201-fp32-openvino-2026.3.1"
        or _integer(
            detector.get("confidence_millionths"),
            "invalid_candidate_manifest",
            950000,
            950000,
        )
        != 950000
        or _integer(
            detector.get("width"),
            "invalid_candidate_manifest",
            DETECTOR_WIDTH,
            DETECTOR_WIDTH,
        )
        != DETECTOR_WIDTH
        or _integer(
            detector.get("height"),
            "invalid_candidate_manifest",
            DETECTOR_HEIGHT,
            DETECTOR_HEIGHT,
        )
        != DETECTOR_HEIGHT
        or specialist.get("tasks") != list(TASKS)
        or _integer(
            specialist.get("cell_grid_width"),
            "invalid_candidate_manifest",
            GRID_WIDTH,
            GRID_WIDTH,
        )
        != GRID_WIDTH
        or _integer(
            specialist.get("cell_grid_height"),
            "invalid_candidate_manifest",
            GRID_HEIGHT,
            GRID_HEIGHT,
        )
        != GRID_HEIGHT
        or _integer(specialist.get("labels_per_task"), "invalid_candidate_manifest", 8, 8) != 8
        or _integer(specialist.get("threshold"), "invalid_candidate_manifest", 127, 127) != 127
    ):
        _fail("invalid_candidate_manifest")


def _validate_source_manifest(
    value: dict[str, object],
    detection_annotations: dict[str, object],
    detection_annotation_sha256: str,
    detection_source_sha256: str,
) -> tuple[list[dict[str, object]], dict[str, dict[str, tuple[int, int, int, int]]]]:
    expected_fields = {
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
    }
    if (
        set(value) != expected_fields
        or value["schema"] != "visualworld.v02-crop-source-manifest"
        or value["schema_version"] != 1
    ):
        _fail("invalid_source_manifest")
    if (
        value["license_expression"] != "CC0-1.0"
        or value["terms_url"] != "https://creativecommons.org/publicdomain/zero/1.0/legalcode"
        or value["source_detection_annotation_sha256"] != detection_annotation_sha256
        or value["source_detection_manifest_sha256"] != detection_source_sha256
        or detection_annotations.get("source_manifest_sha256") != detection_source_sha256
    ):
        _fail("source_manifest_mismatch")
    expected_canvas = {
        "detector_height": DETECTOR_HEIGHT,
        "detector_pixel_format": "rgb24",
        "detector_width": DETECTOR_WIDTH,
        "source_height": SOURCE_HEIGHT,
        "source_pixel_format": "rgb24",
        "source_width": SOURCE_WIDTH,
    }
    if not _same_json(value["canvas"], expected_canvas):
        _fail("invalid_source_manifest")
    privacy = _mapping(value["privacy"], "invalid_source_manifest")
    if privacy != {
        "contains_faces": False,
        "contains_personal_data": False,
        "contains_plates": False,
        "contains_real_people": False,
        "generated_detail_is_identifying": False,
        "sensitive_derived_outputs_retained": False,
        "source_boundary": "only plate-sanitized issue-21 RGB derivatives are accepted",
    }:
        _fail("invalid_source_manifest")
    derivation = _mapping(value["derivation"], "invalid_source_manifest")
    strata_value = _mapping(derivation.get("strata"), "invalid_source_manifest")
    if (
        derivation.get("clip_duration_ms") != 3000
        or derivation.get("frame_duration_ms") != FRAME_DURATION_MS
        or derivation.get("interpolation") != "visualworld-fixed-point-bilinear-v1"
        or derivation.get("detail_controls") != ["source_resolvable", "source_absent"]
        or set(strata_value) != set(STRATA)
    ):
        _fail("invalid_source_manifest")
    strata: dict[str, dict[str, tuple[int, int, int, int]]] = {}
    for stratum in STRATA:
        raw = _mapping(strata_value[stratum], "invalid_source_manifest")
        if set(raw) != {"detector_box", "left_panel_box", "right_panel_box"}:
            _fail("invalid_source_manifest")
        parsed = {
            name: detection_prep._box(raw[name], DETECTOR_WIDTH, DETECTOR_HEIGHT)
            for name in ("detector_box", "left_panel_box", "right_panel_box")
        }
        detector_box = parsed["detector_box"]
        for panel in (parsed["left_panel_box"], parsed["right_panel_box"]):
            if not (
                detector_box[0] <= panel[0] < panel[2] <= detector_box[2]
                and detector_box[1] <= panel[1] < panel[3] <= detector_box[3]
            ):
                _fail("invalid_source_manifest")
            _source_box(panel)
        _source_box(detector_box)
        strata[stratum] = parsed
    easy_items = {
        cast(str, item["source_id"]): item
        for item in cast(list[dict[str, object]], detection_annotations.get("items"))
        if item.get("stratum") == "easy"
    }
    clips_value = value["clips"]
    if not isinstance(clips_value, list) or len(clips_value) != 10 or len(easy_items) != 10:
        _fail("invalid_source_manifest")
    clips: list[dict[str, object]] = []
    seen_sources: set[str] = set()
    for raw_clip in clips_value:
        clip = _mapping(raw_clip, "invalid_source_manifest")
        if set(clip) != {
            "background_seed",
            "clip_id",
            "input_frame_sha256",
            "input_object_box",
            "input_relative_path",
            "source_id",
            "split",
        }:
            _fail("invalid_source_manifest")
        source_id = _text(clip["source_id"], "invalid_source_manifest", 64)
        split = _text(clip["split"], "invalid_source_manifest", 16)
        clip_id = _text(clip["clip_id"], "invalid_source_manifest", 96)
        relative = PurePosixPath(_text(clip["input_relative_path"], "invalid_source_manifest", 256))
        expected = easy_items.get(source_id)
        if (
            source_id in seen_sources
            or split not in {"calibration", "test"}
            or clip_id != f"{split}-{source_id}"
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or expected is None
            or expected.get("split") != split
            or expected.get("relative_path") != clip["input_relative_path"]
            or expected.get("frame_sha256") != clip["input_frame_sha256"]
            or not _same_json(expected.get("object_box"), clip["input_object_box"])
        ):
            _fail("source_manifest_mismatch")
        _digest(clip["input_frame_sha256"], "invalid_source_manifest")
        detection_prep._box(clip["input_object_box"], 640, 360)
        _integer(clip["background_seed"], "invalid_source_manifest", 0, 255)
        seen_sources.add(source_id)
        clips.append(clip)
    if sum(clip["split"] == "calibration" for clip in clips) != 4:
        _fail("invalid_source_manifest")
    return clips, strata


def _frame(
    base: bytes,
    chip: bytes,
    chip_width: int,
    chip_height: int,
    source_box: tuple[int, int, int, int],
    resolved_panel: tuple[int, int, int, int],
    absent_panel: tuple[int, int, int, int],
    bits: tuple[int, ...],
) -> bytes:
    canvas = bytearray(base)
    target_width = source_box[2] - source_box[0]
    target_height = source_box[3] - source_box[1]
    resized = detection_prep.resize_rgb24(
        chip, chip_width, chip_height, target_width, target_height
    )
    _paste(canvas, resized, target_width, target_height, source_box)
    render_panel(canvas, resolved_panel, bits, detail_present=True)
    render_panel(canvas, absent_panel, bits, detail_present=False)
    return bytes(canvas)


def prepare(input_root: Path, output_root: Path) -> dict[str, object]:
    source, source_sha256 = _load_json(SOURCE_MANIFEST, "invalid_source_manifest")
    candidates, candidates_sha256 = _load_json(CANDIDATES, "invalid_candidate_manifest")
    detection_annotations, detection_annotation_sha256 = _load_json(
        DETECTION_ANNOTATIONS, "invalid_source_manifest"
    )
    _, detection_source_sha256 = _load_json(DETECTION_SOURCE_MANIFEST, "invalid_source_manifest")
    _validate_candidates(candidates)
    clips, strata = _validate_source_manifest(
        source,
        detection_annotations,
        detection_annotation_sha256,
        detection_source_sha256,
    )
    output_descriptor = detection_prep._create_private_directory(output_root)
    annotation_clips: list[dict[str, object]] = []
    annotation_items: list[dict[str, object]] = []
    try:
        for clip in clips:
            relative = PurePosixPath(cast(str, clip["input_relative_path"]))
            try:
                ppm = detection_prep._regular_file(
                    input_root.joinpath(*relative.parts), maximum=_MAX_INPUT_BYTES
                )
                if _sha256(ppm) != clip["input_frame_sha256"]:
                    _fail("source_digest_mismatch")
                width, height, pixels = detection_prep.parse_ppm(ppm)
                if (width, height) != (640, 360):
                    _fail("invalid_source")
                input_box = detection_prep._box(clip["input_object_box"], width, height)
                chip_width, chip_height, chip = detection_prep.crop_rgb24(
                    pixels, width, height, input_box
                )
            except detection_prep.PreparationError as error:
                raise CropPreparationError("invalid_source") from error
            base = bytes(_background(cast(int, clip["background_seed"])))
            source_frames: list[bytes] = []
            detector_frames: list[bytes] = []
            frame_records: list[dict[str, object]] = []
            for frame_index, stratum in enumerate(STRATA):
                geometry = strata[stratum]
                detector_box = geometry["detector_box"]
                resolved_detector_panel = geometry["left_panel_box"]
                absent_detector_panel = geometry["right_panel_box"]
                source_box = _source_box(detector_box)
                resolved_source_panel = _source_box(resolved_detector_panel)
                absent_source_panel = _source_box(absent_detector_panel)
                labels = assigned_labels(cast(str, clip["source_id"]), stratum)
                bits = combined_pattern(labels)
                source_frame = _frame(
                    base,
                    chip,
                    chip_width,
                    chip_height,
                    source_box,
                    resolved_source_panel,
                    absent_source_panel,
                    bits,
                )
                detector_frame = detection_prep.resize_rgb24(
                    source_frame,
                    SOURCE_WIDTH,
                    SOURCE_HEIGHT,
                    DETECTOR_WIDTH,
                    DETECTOR_HEIGHT,
                )
                _, _, original_crop = detection_prep.crop_rgb24(
                    source_frame, SOURCE_WIDTH, SOURCE_HEIGHT, source_box
                )
                _, _, detector_crop = detection_prep.crop_rgb24(
                    detector_frame, DETECTOR_WIDTH, DETECTOR_HEIGHT, detector_box
                )
                source_frames.append(source_frame)
                detector_frames.append(detector_frame)
                item_id = f"{clip['clip_id']}-{stratum}"
                frame_records.append(
                    {
                        "absent_detector_panel_box": list(absent_detector_panel),
                        "absent_source_panel_box": list(absent_source_panel),
                        "detector_box": list(detector_box),
                        "detector_crop_sha256": _sha256(detector_crop),
                        "detector_frame_sha256": _sha256(detector_frame),
                        "clip_id": clip["clip_id"],
                        "frame_index": frame_index,
                        "item_id": item_id,
                        "labels": labels,
                        "original_crop_bytes": len(original_crop),
                        "original_crop_sha256": _sha256(original_crop),
                        "resolved_detector_panel_box": list(resolved_detector_panel),
                        "resolved_source_panel_box": list(resolved_source_panel),
                        "source_box": list(source_box),
                        "source_frame_sha256": _sha256(source_frame),
                        "source_id": clip["source_id"],
                        "split": clip["split"],
                        "stratum": stratum,
                    }
                )
                annotation_items.append(frame_records[-1])
            source_movie = rawvideo_movie(tuple(source_frames), SOURCE_WIDTH, SOURCE_HEIGHT)
            detector_movie = rawvideo_movie(tuple(detector_frames), DETECTOR_WIDTH, DETECTOR_HEIGHT)
            source_name = f"{clip['split']}/{clip['clip_id']}-source.mov"
            detector_name = f"{clip['split']}/{clip['clip_id']}-detector.mov"
            detection_prep._write_new(output_descriptor, source_name, source_movie)
            detection_prep._write_new(output_descriptor, detector_name, detector_movie)
            annotation_clips.append(
                {
                    "clip_id": clip["clip_id"],
                    "detector_byte_count": len(detector_movie),
                    "detector_relative_path": detector_name,
                    "detector_sha256": _sha256(detector_movie),
                    "source_byte_count": len(source_movie),
                    "source_id": clip["source_id"],
                    "source_relative_path": source_name,
                    "source_sha256": _sha256(source_movie),
                    "split": clip["split"],
                }
            )
        lock: dict[str, object] = {
            "candidate_manifest_sha256": candidates_sha256,
            "clips": annotation_clips,
            "items": annotation_items,
            "schema": "visualworld.v02-crop-annotation-lock",
            "schema_version": 1,
            "source_manifest_sha256": source_sha256,
        }
        serialized = (
            json.dumps(lock, allow_nan=False, indent=2, sort_keys=True).encode("ascii") + b"\n"
        )
        detection_prep._write_new(output_descriptor, "annotations.generated.json", serialized)
    finally:
        detection_prep._close_once(output_descriptor)
    if ANNOTATION_LOCK.exists():
        expected, _ = _load_json(ANNOTATION_LOCK, "invalid_annotation_lock")
        if not _same_json(lock, expected):
            _fail("annotation_lock_mismatch")
    return lock


def main() -> int:
    parser = _StableArgumentParser(description=__doc__)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    try:
        arguments = parser.parse_args()
        lock = prepare(arguments.input_root.resolve(), Path(os.path.abspath(arguments.output)))
    except (OSError, CropPreparationError, detection_prep.PreparationError) as error:
        code = str(error) if isinstance(error, CropPreparationError) else "filesystem_failed"
        _emit({"error": code, "status": "error"}, sys.stdout)
        return 1
    return (
        0
        if _emit(
            {
                "clip_count": len(cast(list[object], lock["clips"])),
                "item_count": len(cast(list[object], lock["items"])),
                "status": "pass",
            },
            sys.stdout,
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
