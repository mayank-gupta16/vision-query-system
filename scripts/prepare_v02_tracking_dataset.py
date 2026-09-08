#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate the locked, rights-safe v0.2 short-term tracking clips."""

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
import prepare_v02_sampling_dataset as sampling_prep

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "fixtures" / "v02-tracking-research"
SOURCE_MANIFEST = FIXTURE_ROOT / "source-manifest.json"
ANNOTATION_LOCK = FIXTURE_ROOT / "annotations.json"
DATASET_LOCK = FIXTURE_ROOT / "dataset-manifest.json"
DETECTION_ANNOTATIONS = ROOT / "fixtures" / "v02-detection-research" / "annotations.json"
DETECTION_SOURCE_MANIFEST = ROOT / "fixtures" / "v02-detection-research" / "source-manifest.json"
WIDTH = 640
HEIGHT = 360
FRAME_COUNT = 60
FRAME_DURATION_MS = 200
TARGET_WIDTH = 170
DENSE_TARGET_WIDTH = 120
DENSE_MIN_VISIBILITY_MILLIONTHS = 250_000
OCCLUSION_MASK_RGB = (74, 78, 82)
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
_IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
STRATA = ("overall", "camera_motion", "cuts", "occlusion", "dense_crossing")
SCENARIOS = ("camera_motion", "cuts", "dense_crossing", "occlusion", "stationary_camera")


class TrackingPreparationError(RuntimeError):
    """A stable tracking-dataset preparation failure."""


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise TrackingPreparationError("invalid_arguments")

    def _print_message(self, message: str, file: object | None = None) -> None:
        if message:
            _write_text(message, sys.stderr if file is None else cast(TextIO, file))


def _fail(code: str) -> NoReturn:
    raise TrackingPreparationError(code)


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


def _write_text(value: str, stream: TextIO) -> bool:
    try:
        stream.write(value)
        stream.flush()
    except (AttributeError, OSError, UnicodeError, ValueError):
        _silence_stream(stream)
        return False
    return True


def _emit(value: object, stream: TextIO) -> bool:
    try:
        raw = json.dumps(value, allow_nan=False, sort_keys=True) + "\n"
    except (TypeError, UnicodeError, ValueError):
        _silence_stream(stream)
        return False
    return _write_text(raw, stream)


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
        raise TrackingPreparationError("invalid_json") from error


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_json(path: Path, code: str) -> tuple[dict[str, object], str]:
    try:
        raw = detection_prep._regular_file(path, maximum=MAX_JSON_BYTES)
        value = json.loads(
            raw,
            object_pairs_hook=detection_prep._unique_object,
            parse_constant=detection_prep._reject_constant,
        )
    except (OSError, ValueError, detection_prep.PreparationError) as error:
        raise TrackingPreparationError(code) from error
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


def _expected_derivation() -> dict[str, object]:
    return {
        "background": "visualworld-generated-road-and-hard-cut-v2",
        "cut_before_frame_indices": [20, 40],
        "dense_layering": (
            "three disjoint vertical lanes; taller occurrence painted first within each "
            "crossing pair"
        ),
        "dense_min_visibility_millionths": DENSE_MIN_VISIBILITY_MILLIONTHS,
        "dense_object_target_width": DENSE_TARGET_WIDTH,
        "duration_ms": 12000,
        "frame_count": FRAME_COUNT,
        "interpolation": "visualworld-fixed-point-bilinear-v1",
        "object_target_width": TARGET_WIDTH,
        "occlusion_layout": (
            "three disjoint x lanes with deterministic plus-or-minus four pixel drift"
        ),
        "occlusion_mask_color_rgb": list(OCCLUSION_MASK_RGB),
        "occlusion_mask_frames": {"a": [20], "b": [20, 21, 22], "c": [20, 21, 22, 23, 24]},
        "occlusion_partial_mask_geometry": (
            "full box height and horizontal half-open interval "
            "[x0+floor(width/3), x0+2*floor(width/3))"
        ),
        "occlusion_partial_mask_frames": {"a": [19, 21], "b": [19, 23], "c": [19, 25]},
        "occlusion_visibility_millionths": (
            "floor(unmasked box pixels times 1000000 divided by box pixels)"
        ),
        "sample_fps": 5,
        "scenario_clip_counts": {
            "calibration": {
                "camera_motion": 1,
                "cuts": 1,
                "dense_crossing": 1,
                "occlusion": 1,
                "stationary_camera": 1,
            },
            "test": {
                "camera_motion": 2,
                "cuts": 2,
                "dense_crossing": 2,
                "occlusion": 2,
                "stationary_camera": 2,
            },
        },
        "time_base": {"denominator": 1000, "numerator": 1},
        "timing": "60 constant-duration 200 ms frames; fixed 5 FPS selected policy",
    }


def _movie(frames: tuple[bytes, ...], durations: tuple[int, ...]) -> bytes:
    frame_size = WIDTH * HEIGHT * 3
    if len(frames) != len(durations) or any(len(frame) != frame_size for frame in frames):
        _fail("clip_generation_failed")
    ftyp = sampling_prep._box(b"ftyp", b"qt  " + struct.pack(">I", 0) + b"qt  ")
    mdat = sampling_prep._box(b"mdat", b"".join(frames))
    compressor = b"VisualWorld RGB"
    compressor_name = bytes((len(compressor),)) + compressor + bytes(31 - len(compressor))
    sample_entry = sampling_prep._box(
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
    stsd = sampling_prep._full_box(b"stsd", 0, 0, struct.pack(">I", 1) + sample_entry)
    entries = sampling_prep._time_entries(durations)
    stts = sampling_prep._full_box(
        b"stts",
        0,
        0,
        struct.pack(">I", len(entries))
        + b"".join(struct.pack(">II", count, delta) for count, delta in entries),
    )
    stsc = sampling_prep._full_box(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, len(frames), 1))
    stsz = sampling_prep._full_box(b"stsz", 0, 0, struct.pack(">II", frame_size, len(frames)))
    stco = sampling_prep._full_box(b"stco", 0, 0, struct.pack(">II", 1, len(ftyp) + 8))
    stbl = sampling_prep._box(b"stbl", stsd + stts + stsc + stsz + stco)
    vmhd = sampling_prep._full_box(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0))
    url = sampling_prep._full_box(b"url ", 0, 1, b"")
    dref = sampling_prep._full_box(b"dref", 0, 0, struct.pack(">I", 1) + url)
    minf = sampling_prep._box(b"minf", vmhd + sampling_prep._box(b"dinf", dref) + stbl)
    duration = sum(durations)
    mdhd = sampling_prep._full_box(
        b"mdhd",
        0,
        0,
        struct.pack(">IIIIHH", 0, 0, 1000, duration, 0x55C4, 0),
    )
    hdlr = sampling_prep._full_box(
        b"hdlr", 0, 0, struct.pack(">I4sIII", 0, b"vide", 0, 0, 0) + b"Video\0"
    )
    mdia = sampling_prep._box(b"mdia", mdhd + hdlr + minf)
    tkhd = sampling_prep._full_box(
        b"tkhd",
        0,
        7,
        struct.pack(">IIIII", 0, 0, 1, 0, duration)
        + bytes(8)
        + struct.pack(">hhhh", 0, 0, 0, 0)
        + sampling_prep._matrix()
        + struct.pack(">II", WIDTH << 16, HEIGHT << 16),
    )
    mvhd = sampling_prep._full_box(
        b"mvhd",
        0,
        0,
        struct.pack(">IIII", 0, 0, 1000, duration)
        + struct.pack(">IHHII", 0x00010000, 0x0100, 0, 0, 0)
        + sampling_prep._matrix()
        + bytes(24)
        + struct.pack(">I", 2),
    )
    movie = (
        ftyp + mdat + sampling_prep._box(b"moov", mvhd + sampling_prep._box(b"trak", tkhd + mdia))
    )
    if len(movie) > 48 * 1024 * 1024:
        _fail("clip_generation_failed")
    return movie


def _validate_detection_annotations(value: dict[str, object]) -> dict[str, dict[str, object]]:
    items = value.get("items")
    if (
        set(value) != {"items", "schema", "schema_version", "source_manifest_sha256"}
        or value.get("schema") != "visualworld.v02-detection-annotation-lock"
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or not isinstance(items, list)
    ):
        _fail("invalid_manifest")
    selected: dict[str, dict[str, object]] = {}
    fields = {
        "frame_sha256",
        "height",
        "item_id",
        "object_box",
        "relative_path",
        "source_id",
        "split",
        "stratum",
        "width",
    }
    for raw_item in items:
        if not isinstance(raw_item, dict) or set(raw_item) != fields:
            _fail("invalid_manifest")
        item = cast(dict[str, object], raw_item)
        if item["stratum"] != "easy":
            continue
        source_id = _text(item["source_id"], "invalid_manifest", 64)
        split = _text(item["split"], "invalid_manifest", 16)
        if (
            _IDENTIFIER.fullmatch(source_id) is None
            or split not in {"calibration", "test"}
            or item["item_id"] != f"{split}-{source_id}-easy"
            or item["relative_path"] != f"{split}/{item['item_id']}.ppm"
            or _integer(item["width"], "invalid_manifest", 640, 640) != 640
            or _integer(item["height"], "invalid_manifest", 360, 360) != 360
        ):
            _fail("invalid_manifest")
        try:
            detection_prep._box(item["object_box"], 640, 360)
        except detection_prep.PreparationError as error:
            raise TrackingPreparationError("invalid_manifest") from error
        if source_id in selected:
            _fail("invalid_manifest")
        selected[source_id] = item
    if len(selected) != 10:
        _fail("invalid_manifest")
    return selected


def _validate_source_manifest(
    value: dict[str, object], detection_annotations: dict[str, object]
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    if set(value) != {
        "canvas",
        "clips",
        "derivation",
        "license_expression",
        "privacy",
        "schema",
        "schema_version",
        "source_detection_annotation_sha256",
        "source_detection_manifest_sha256",
        "sources",
        "terms_url",
    }:
        _fail("invalid_manifest")
    if (
        value["schema"] != "visualworld.v02-tracking-source-manifest"
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["license_expression"] != "CC0-1.0"
        or value["terms_url"] != "https://creativecommons.org/publicdomain/zero/1.0/legalcode"
        or _canonical(value["canvas"])
        != _canonical({"height": HEIGHT, "pixel_format": "rgb24", "width": WIDTH})
        or _canonical(value["derivation"]) != _canonical(_expected_derivation())
        or _canonical(value["privacy"])
        != _canonical(
            {
                "contains_faces": False,
                "contains_personal_data": False,
                "contains_plates": False,
                "contains_real_people": False,
                "source_boundary": ("only plate-sanitized issue-21 RGB derivatives are accepted"),
            }
        )
        or value["source_detection_annotation_sha256"]
        != "6c35c9d961fa274d4cdf3682d927ac9c22cb8587832dd099a3f014662363bbe8"
        or value["source_detection_manifest_sha256"]
        != "d85631980f31fb2bec6b5a9ec098b35c336365ed263efed5cd17e01d1fa12f66"
    ):
        _fail("invalid_manifest")
    clips = value["clips"]
    sources = value["sources"]
    if not isinstance(clips, list) or len(clips) != 15 or not isinstance(sources, list):
        _fail("invalid_manifest")
    upstream = _validate_detection_annotations(detection_annotations)
    if len(sources) != 10:
        _fail("invalid_manifest")
    source_catalog: dict[str, dict[str, object]] = {}
    source_fields = {"frame_sha256", "object_box", "relative_path", "source_id", "split"}
    for raw_source in sources:
        if not isinstance(raw_source, dict) or set(raw_source) != source_fields:
            _fail("invalid_manifest")
        source = cast(dict[str, object], raw_source)
        source_id = _text(source["source_id"], "invalid_manifest", 64)
        split = _text(source["split"], "invalid_manifest", 16)
        relative = PurePosixPath(_text(source["relative_path"], "invalid_manifest", 192))
        if (
            source_id in source_catalog
            or split not in {"calibration", "test"}
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or _canonical(upstream.get(source_id))
            != _canonical(
                {
                    "frame_sha256": source["frame_sha256"],
                    "height": 360,
                    "item_id": f"{split}-{source_id}-easy",
                    "object_box": source["object_box"],
                    "relative_path": source["relative_path"],
                    "source_id": source_id,
                    "split": split,
                    "stratum": "easy",
                    "width": 640,
                }
            )
        ):
            _fail("invalid_manifest")
        source_catalog[source_id] = source
    seen_clips: set[str] = set()
    validated: list[dict[str, object]] = []
    scenario_counts: dict[str, dict[str, int]] = {
        split: {scenario: 0 for scenario in SCENARIOS} for split in ("calibration", "test")
    }
    for index, raw_clip in enumerate(clips):
        if not isinstance(raw_clip, dict) or set(raw_clip) != {
            "background_seed",
            "clip_id",
            "scenario",
            "source_ids",
            "split",
            "trajectory_variant",
        }:
            _fail("invalid_manifest")
        clip = cast(dict[str, object], raw_clip)
        clip_id = _text(clip["clip_id"], "invalid_manifest", 96)
        split = _text(clip["split"], "invalid_manifest", 16)
        scenario = _text(clip["scenario"], "invalid_manifest", 32)
        source_ids = clip["source_ids"]
        if (
            _IDENTIFIER.fullmatch(clip_id) is None
            or clip_id in seen_clips
            or split not in {"calibration", "test"}
            or not clip_id.startswith(f"{split}-")
            or scenario not in scenario_counts.get(split, {})
            or not isinstance(source_ids, list)
            or len(source_ids) != 2
            or any(type(source_id) is not str for source_id in source_ids)
            or len(set(cast(list[str], source_ids))) != 2
            or _integer(clip["trajectory_variant"], "invalid_manifest", index, index) != index
        ):
            _fail("invalid_manifest")
        _integer(clip["background_seed"], "invalid_manifest", 0, 255)
        for source_id in cast(list[str], source_ids):
            if source_id not in source_catalog or source_catalog[source_id]["split"] != split:
                _fail("invalid_manifest")
        seen_clips.add(clip_id)
        scenario_counts[split][scenario] += 1
        validated.append(clip)
    if scenario_counts != _expected_derivation()["scenario_clip_counts"] or {
        source["split"] for source in source_catalog.values()
    } != {"calibration", "test"}:
        _fail("invalid_manifest")
    return validated, source_catalog


def _mirror(pixels: bytes, width: int, height: int) -> bytes:
    output = bytearray(len(pixels))
    for y in range(height):
        for x in range(width):
            source = (y * width + x) * 3
            target = (y * width + (width - x - 1)) * 3
            output[target : target + 3] = pixels[source : source + 3]
    return bytes(output)


def _background(seed: int, segment: int) -> bytes:
    output = bytearray(WIDTH * HEIGHT * 3)
    horizon = 150 + seed % 17
    for y in range(HEIGHT):
        if y < horizon:
            color = (80 + seed % 41 + y // 18, 126 + seed % 37, 174 + y // 23)
        else:
            shade = 58 + (y - horizon) // 5
            color = (shade, min(255, shade + 3), min(255, shade + 7))
        if segment == 1:
            color = cast(tuple[int, int, int], tuple(255 - channel for channel in color))
        elif segment == 2:
            color = (color[2], min(255, color[0] + 35), color[1])
        output[y * WIDTH * 3 : (y + 1) * WIDTH * 3] = bytes(color) * WIDTH
    for y in range(horizon + 24, HEIGHT, 42):
        left = WIDTH // 2 - max(2, (y - horizon) // 10)
        right = WIDTH // 2 + max(2, (y - horizon) // 10)
        detection_prep.fill_box(
            output,
            WIDTH,
            HEIGHT,
            (left, y, right, min(HEIGHT, y + 3)),
            (224, 211, 142),
        )
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


def _paste_object(canvas: bytearray, chip: tuple[int, int, bytes], box: list[int]) -> None:
    width, height, pixels = chip
    if width != box[2] - box[0] or height != box[3] - box[1]:
        _fail("clip_generation_failed")
    for y in range(height):
        source = y * width * 3
        target = ((box[1] + y) * WIDTH + box[0]) * 3
        canvas[target : target + width * 3] = pixels[source : source + width * 3]


def _camera_shift(frame_index: int, variant: int) -> int:
    pattern = (-8, 5, -6, 9, -9, 6, -4, 8)
    pan = ((frame_index % 20) - 10) * 3
    return pan + pattern[(frame_index + variant) % len(pattern)]


def _left(scenario: str, role: str, frame_index: int, variant: int, object_width: int) -> int:
    role_index = ord(role) - ord("a")
    offset = (variant % 5 - 2) * 2
    if scenario == "stationary_camera":
        bases = (20, 235, 450)
        value = bases[role_index] + ((frame_index + role_index * 3) % 11) - 5
    elif scenario == "camera_motion":
        bases = (35, 235, 435)
        value = bases[role_index] + _camera_shift(frame_index, variant)
    elif scenario == "cuts":
        step = frame_index % 20
        bases = (25, 235, 445)
        velocities = (3, -3, 2)
        value = bases[role_index] + velocities[role_index] * step
    elif scenario == "occlusion":
        bases = (15, 235, 455)
        value = bases[role_index] + (frame_index + role_index * 3 + variant) % 9 - 4
    else:
        progress = frame_index if frame_index < 30 else 59 - frame_index
        travel = WIDTH - 16 - object_width
        value = (
            8 + travel * progress // 29
            if role_index % 2 == 0
            else WIDTH - object_width - 8 - travel * progress // 29
        )
    return max(0, min(WIDTH - object_width, value + offset))


def _box_for(
    scenario: str,
    role: str,
    frame_index: int,
    variant: int,
    chip_width: int,
    chip_height: int,
) -> list[int]:
    bottoms = (
        {"a": 115, "b": 115, "c": 235, "d": 235, "e": 355, "f": 355}
        if scenario == "dense_crossing"
        else {"a": 344, "b": 314, "c": 284, "d": 354, "e": 324, "f": 294}
    )
    bottom = bottoms[role]
    top = max(0, bottom - chip_height)
    left = _left(scenario, role, frame_index, variant, chip_width)
    return [left, top, left + chip_width, bottom]


def _roles(scenario: str) -> tuple[str, ...]:
    return ("a", "b", "c", "d", "e", "f") if scenario == "dense_crossing" else ("a", "b", "c")


def _track_id(clip_id: str, scenario: str, frame_index: int, role: str) -> str:
    if scenario == "cuts":
        return f"{clip_id}-segment-{frame_index // 20}-{role}"
    return f"{clip_id}-{role}"


def _track_descriptors(clip: dict[str, object]) -> list[dict[str, object]]:
    clip_id = cast(str, clip["clip_id"])
    scenario = cast(str, clip["scenario"])
    source_ids = cast(list[str], clip["source_ids"])
    strata = ["overall"] if scenario == "stationary_camera" else ["overall", scenario]
    frames_and_roles = (
        [(segment * 20, role) for segment in range(3) for role in _roles(scenario)]
        if scenario == "cuts"
        else [(0, role) for role in _roles(scenario)]
    )
    return [
        {
            "occurrence_transform": "horizontal-mirror" if role in {"c", "d"} else "original",
            "source_id": source_ids[(ord(role) - ord("a")) % 2],
            "strata": strata,
            "track_id": _track_id(clip_id, scenario, start, role),
        }
        for start, role in frames_and_roles
    ]


def _visibility_millionths(box: list[int], occluders: list[list[int]]) -> int:
    width = box[2] - box[0]
    height = box[3] - box[1]
    covered = bytearray(width * height)
    for occluder in occluders:
        left = max(box[0], occluder[0])
        top = max(box[1], occluder[1])
        right = min(box[2], occluder[2])
        bottom = min(box[3], occluder[3])
        if left >= right or top >= bottom:
            continue
        for y in range(top, bottom):
            start = (y - box[1]) * width + left - box[0]
            covered[start : start + right - left] = b"\x01" * (right - left)
    visible = len(covered) - sum(covered)
    return visible * 1_000_000 // len(covered)


def _partial_occlusion_cover(box: list[int]) -> tuple[int, int, int, int]:
    third = max(1, (box[2] - box[0]) // 3)
    return (box[0] + third, box[1], min(box[2], box[0] + 2 * third), box[3])


def _render_frame(
    *,
    backgrounds: tuple[bytes, bytes, bytes],
    chips: tuple[
        tuple[int, int, bytes],
        tuple[int, int, bytes],
        tuple[int, int, bytes],
        tuple[int, int, bytes],
    ],
    clip_id: str,
    frame_index: int,
    scenario: str,
    variant: int,
) -> tuple[bytes, list[dict[str, object]]]:
    segment = frame_index // 20 if scenario == "cuts" else 0
    shift = _camera_shift(frame_index, variant) if scenario == "camera_motion" else 0
    canvas = _shift_rows(backgrounds[segment], shift)
    objects: list[dict[str, object]] = []
    boxes: dict[str, list[int]] = {}
    missing = {
        "a": {20},
        "b": {20, 21, 22},
        "c": {20, 21, 22, 23, 24},
    }
    partial = {"a": {19, 21}, "b": {19, 23}, "c": {19, 25}}
    chip_indices = {"a": 0, "b": 1, "c": 2, "d": 3, "e": 0, "f": 1}
    roles = _roles(scenario)
    draw_roles = (
        tuple(
            role
            for pair in (("a", "b"), ("c", "d"), ("e", "f"))
            for role in sorted(pair, key=lambda item: -chips[chip_indices[item]][1])
        )
        if scenario == "dense_crossing"
        else roles
    )
    for role in draw_roles:
        chip = chips[chip_indices[role]]
        box = _box_for(scenario, role, frame_index, variant, chip[0], chip[1])
        boxes[role] = box
        _paste_object(canvas, chip, box)
    for role_index, role in enumerate(draw_roles):
        box = boxes[role]
        fully_occluded = scenario == "occlusion" and frame_index in missing[role]
        if not fully_occluded:
            is_partial = scenario == "occlusion" and frame_index in partial[role]
            visibility = (
                _visibility_millionths(box, [boxes[item] for item in draw_roles[role_index + 1 :]])
                if scenario == "dense_crossing"
                else _visibility_millionths(box, [list(_partial_occlusion_cover(box))])
                if is_partial
                else 1_000_000
            )
            if scenario == "dense_crossing" and visibility < DENSE_MIN_VISIBILITY_MILLIONTHS:
                _fail("dense_visibility_too_low")
            if visibility > 0:
                objects.append(
                    {
                        "box": box,
                        "track_id": _track_id(clip_id, scenario, frame_index, role),
                        "visibility_millionths": visibility,
                    }
                )
    if scenario == "occlusion":
        for role in _roles(scenario):
            box = boxes[role]
            if frame_index in missing[role]:
                cover = (box[0], box[1], box[2], box[3])
            elif frame_index in partial[role]:
                cover = _partial_occlusion_cover(box)
            else:
                continue
            detection_prep.fill_box(canvas, WIDTH, HEIGHT, cover, OCCLUSION_MASK_RGB)
    return bytes(canvas), objects


def _dataset_manifest(
    source_sha256: str,
    annotation_clips: list[dict[str, object]],
    tracks: list[dict[str, object]],
) -> dict[str, object]:
    splits: dict[str, object] = {}
    for split in ("calibration", "test"):
        selected_clips = [clip for clip in annotation_clips if clip["split"] == split]
        selected_tracks = [track for track in tracks if track["split"] == split]
        payload = _canonical({"clips": selected_clips, "tracks": selected_tracks})
        ids = [cast(str, track["track_id"]) for track in selected_tracks]
        splits[split] = {
            "annotation_sha256": _sha256(payload),
            "item_count": len(selected_tracks),
            "item_ids_sha256": _sha256(("\n".join(ids) + "\n").encode("ascii")),
            "stratum_item_counts": {
                stratum: sum(
                    stratum in cast(list[str], track["strata"]) for track in selected_tracks
                )
                for stratum in STRATA
            },
        }
    return {
        "acquisition": {
            "method": (
                "deterministic 5 FPS rawvideo clips from ten locked plate-sanitized CC0 "
                "issue-21 derivatives; source identities, transforms, tracks, cut, frames, "
                "and pixels are bound by source-manifest.json and annotations.json"
            ),
            "owner": "Wikimedia Commons source artists and VisualWorld derivative generator",
            "revision": "v02-tracking-cc0-derived-3",
            "sha256": source_sha256,
            "url": (
                "https://github.com/mayank-gupta16/vision-query-system/tree/main/"
                "fixtures/v02-tracking-research"
            ),
        },
        "experiment": "tracking",
        "manifest_id": "tracking-cc0-derived-3",
        "privacy": {
            "classification": "licensed-no-personal-data",
            "consent_status": "not-applicable-no-personal-data",
            "contains_faces": False,
            "contains_personal_data": False,
            "contains_plates": False,
            "contains_real_people": False,
            "sensitive_derived_outputs_retained": False,
        },
        "rights": {
            "allowed_uses": ["benchmarking", "evaluation", "modification", "redistribution"],
            "commercial_use_allowed": True,
            "derivatives_allowed": True,
            "license_expression": "CC0-1.0",
            "redistribution_allowed": True,
        },
        "schema": "visualworld.v02-evaluation-dataset-manifest",
        "schema_version": 1,
        "splits": splits,
        "strata": list(STRATA),
    }


def prepare(input_root: Path, output_root: Path) -> dict[str, object]:
    manifest, manifest_sha256 = _load_json(SOURCE_MANIFEST, "invalid_manifest")
    detection_annotations, detection_annotation_sha256 = _load_json(
        DETECTION_ANNOTATIONS, "invalid_manifest"
    )
    _, detection_source_sha256 = _load_json(DETECTION_SOURCE_MANIFEST, "invalid_manifest")
    clips, source_catalog = _validate_source_manifest(manifest, detection_annotations)
    if (
        manifest["source_detection_annotation_sha256"] != detection_annotation_sha256
        or manifest["source_detection_manifest_sha256"] != detection_source_sha256
        or detection_annotations["source_manifest_sha256"] != detection_source_sha256
    ):
        _fail("invalid_manifest")
    output_descriptor = detection_prep._create_private_directory(output_root)
    annotation_clips: list[dict[str, object]] = []
    tracks: list[dict[str, object]] = []
    durations = (FRAME_DURATION_MS,) * FRAME_COUNT
    try:
        for clip in clips:
            prepared_chips: list[tuple[int, int, bytes]] = []
            for source_id in cast(list[str], clip["source_ids"]):
                source = source_catalog[source_id]
                try:
                    raw = detection_prep._regular_file(
                        input_root / cast(str, source["relative_path"]), maximum=MAX_INPUT_BYTES
                    )
                    if _sha256(raw) != source["frame_sha256"]:
                        _fail("source_digest_mismatch")
                    source_width, source_height, pixels = detection_prep.parse_ppm(raw)
                    left, top, right, bottom = cast(list[int], source["object_box"])
                    chip_width, chip_height, chip = detection_prep.crop_rgb24(
                        pixels, source_width, source_height, (left, top, right, bottom)
                    )
                    target_height = max(1, chip_height * TARGET_WIDTH // chip_width)
                    prepared_chips.append(
                        (
                            TARGET_WIDTH,
                            target_height,
                            detection_prep.resize_rgb24(
                                chip, chip_width, chip_height, TARGET_WIDTH, target_height
                            ),
                        )
                    )
                except detection_prep.PreparationError as error:
                    raise TrackingPreparationError("invalid_source") from error
            first, second = prepared_chips
            first_mirror = (first[0], first[1], _mirror(first[2], first[0], first[1]))
            second_mirror = (
                second[0],
                second[1],
                _mirror(second[2], second[0], second[1]),
            )
            chip_tuple = (first, second, first_mirror, second_mirror)
            dense_chip_tuple = tuple(
                (
                    DENSE_TARGET_WIDTH,
                    max(1, chip[1] * DENSE_TARGET_WIDTH // chip[0]),
                    detection_prep.resize_rgb24(
                        chip[2],
                        chip[0],
                        chip[1],
                        DENSE_TARGET_WIDTH,
                        max(1, chip[1] * DENSE_TARGET_WIDTH // chip[0]),
                    ),
                )
                for chip in chip_tuple
            )
            seed = cast(int, clip["background_seed"])
            backgrounds = (
                _background(seed, 0),
                _background(seed, 1),
                _background(seed, 2),
            )
            frames: list[bytes] = []
            frame_records: list[dict[str, object]] = []
            scenario = cast(str, clip["scenario"])
            strata = ["overall"] if scenario == "stationary_camera" else ["overall", scenario]
            for frame_index in range(FRAME_COUNT):
                frame, objects = _render_frame(
                    backgrounds=backgrounds,
                    chips=(
                        cast(
                            tuple[
                                tuple[int, int, bytes],
                                tuple[int, int, bytes],
                                tuple[int, int, bytes],
                                tuple[int, int, bytes],
                            ],
                            dense_chip_tuple,
                        )
                        if scenario == "dense_crossing"
                        else chip_tuple
                    ),
                    clip_id=cast(str, clip["clip_id"]),
                    frame_index=frame_index,
                    scenario=scenario,
                    variant=cast(int, clip["trajectory_variant"]),
                )
                frames.append(frame)
                frame_records.append(
                    {
                        "duration_ms": FRAME_DURATION_MS,
                        "frame_index": frame_index,
                        "objects": objects,
                        "pts_ms": frame_index * FRAME_DURATION_MS,
                        "rgb24_sha256": _sha256(frame),
                        "strata": strata,
                    }
                )
            movie = _movie(tuple(frames), durations)
            relative_path = f"{clip['split']}/{clip['clip_id']}.mov"
            detection_prep._write_new(output_descriptor, relative_path, movie)
            annotation_clips.append(
                {
                    "byte_count": len(movie),
                    "clip_id": clip["clip_id"],
                    "cut_before_frame_indices": [20, 40] if scenario == "cuts" else [],
                    "duration_ms": FRAME_COUNT * FRAME_DURATION_MS,
                    "frame_count": FRAME_COUNT,
                    "frames": frame_records,
                    "relative_path": relative_path,
                    "sha256": _sha256(movie),
                    "primary_stratum": scenario,
                    "source_ids": clip["source_ids"],
                    "split": clip["split"],
                }
            )
            tracks.extend(
                {**track, "clip_id": clip["clip_id"], "split": clip["split"]}
                for track in _track_descriptors(clip)
            )
        lock: dict[str, object] = {
            "clips": annotation_clips,
            "schema": "visualworld.v02-tracking-annotation-lock",
            "schema_version": 1,
            "source_manifest_sha256": manifest_sha256,
            "tracks": tracks,
        }
        dataset = _dataset_manifest(manifest_sha256, annotation_clips, tracks)
        for name, value in (
            ("annotations.generated.json", lock),
            ("dataset-manifest.generated.json", dataset),
        ):
            raw = (
                json.dumps(value, allow_nan=False, indent=2, sort_keys=True).encode("ascii") + b"\n"
            )
            detection_prep._write_new(output_descriptor, name, raw)
    finally:
        detection_prep._close_once(output_descriptor)
    for path, actual, code in (
        (ANNOTATION_LOCK, lock, "annotation_lock_mismatch"),
        (DATASET_LOCK, dataset, "dataset_lock_mismatch"),
    ):
        if path.exists():
            expected, _ = _load_json(path, code)
            if _canonical(expected) != _canonical(actual):
                _fail(code)
    return lock


def main() -> int:
    parser = _StableArgumentParser(description=__doc__)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    try:
        arguments = parser.parse_args()
        lock = prepare(arguments.input_root.resolve(), Path(os.path.abspath(arguments.output)))
    except (OSError, TrackingPreparationError, detection_prep.PreparationError) as error:
        code = str(error) if isinstance(error, TrackingPreparationError) else "filesystem_failed"
        _emit({"error": code, "status": "error"}, sys.stdout)
        return 1
    return (
        0
        if _emit(
            {
                "clip_count": len(cast(list[object], lock["clips"])),
                "status": "pass",
                "track_count": len(cast(list[object], lock["tracks"])),
            },
            sys.stdout,
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
