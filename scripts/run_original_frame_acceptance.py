#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run issue #87 original-frame acceptance on the supported Linux host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, cast

import generate_synthetic_fixtures as fixtures

from visualworld.geometry import extract_rgb24_crop, source_geometry
from visualworld.ingestion import FrameRef, Source
from visualworld.media import LocalVideoSource, MediaRuntime
from visualworld.original_frame_runtime import (
    IsolatedOriginalFrameReader,
    OriginalFrameLimits,
    OriginalFrameRuntime,
)
from visualworld.ports import PerceptionResultState, PortError, PortErrorCode, PortKind

CROP_MANIFEST_PATH = fixtures.ROOT / "fixtures" / "synthetic-v1" / "crops.json"
_RECEIPT_SCHEMA = "visualworld.original-frame-acceptance-receipt"


class AcceptanceError(RuntimeError):
    """A static acceptance failure that never includes paths or pixel values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"{code} at original-frame acceptance")


def _fail(code: str) -> NoReturn:
    raise AcceptanceError(code) from None


def _supported_host() -> bool:
    libc, version = platform.libc_ver()
    try:
        parts = tuple(int(part) for part in version.split("."))
    except ValueError:
        return False
    return (
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and libc == "glibc"
        and parts >= (2, 28)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fixture_records(manifest: dict[str, object]) -> list[dict[str, object]]:
    value = manifest.get("fixtures")
    if not isinstance(value, list) or not all(type(item) is dict for item in value):
        _fail("invalid_fixture_manifest")
    return cast(list[dict[str, object]], value)


def _crop_records() -> dict[str, list[dict[str, object]]]:
    try:
        raw: object = json.loads(CROP_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("invalid_crop_manifest")
    if type(raw) is not dict or set(raw) != {
        "crop_format",
        "crops",
        "schema",
        "schema_version",
        "source_fixture_manifest_sha256",
    }:
        _fail("invalid_crop_manifest")
    manifest = cast(dict[str, object], raw)
    crops = manifest["crops"]
    expected_ids = {spec.fixture_id for spec in fixtures.SPECS}
    if (
        manifest["schema"] != "visualworld.synthetic-crop-goldens"
        or manifest["schema_version"] != 1
        or manifest["crop_format"] != "packed_rgb24_encoded_source"
        or manifest["source_fixture_manifest_sha256"] != _sha256(fixtures.MANIFEST_PATH)
        or type(crops) is not dict
        or set(crops) != expected_ids
    ):
        _fail("invalid_crop_manifest")
    result = cast(dict[str, list[dict[str, object]]], crops)
    for records in result.values():
        if type(records) is not list or len(records) != 4:
            _fail("invalid_crop_manifest")
        for record in records:
            if type(record) is not dict or set(record) != {"box_xyxy", "rgb24_sha256"}:
                _fail("invalid_crop_manifest")
            box = record["box_xyxy"]
            digest = record["rgb24_sha256"]
            if (
                type(box) is not list
                or len(box) != 4
                or any(type(value) is not int for value in box)
                or type(digest) is not str
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                _fail("invalid_crop_manifest")
    return result


def _moving_box(record: dict[str, object]) -> tuple[int, int, int, int]:
    region = record.get("moving_region")
    if type(region) is not dict or set(region) != {"height", "rgb", "width", "x", "y"}:
        _fail("invalid_fixture_manifest")
    values = cast(dict[str, object], region)
    x = values["x"]
    y = values["y"]
    width = values["width"]
    height = values["height"]
    if not all(type(value) is int for value in (x, y, width, height)):
        _fail("invalid_fixture_manifest")
    typed_x, typed_y, typed_width, typed_height = cast(
        tuple[int, int, int, int], (x, y, width, height)
    )
    return (
        typed_x,
        typed_y,
        typed_x + typed_width,
        typed_y + typed_height,
    )


def _expect_static_error(
    operation: Callable[[], object],
    expected: PortErrorCode,
    forbidden_pixels: tuple[bytes, ...],
) -> bool:
    try:
        operation()
    except PortError as error:
        rendered = str(error) + repr(error)
        return (
            error.code is expected
            and error.port is PortKind.ORIGINAL_FRAME_READER
            and error.operation == "read"
            and error.__cause__ is None
            and str(error) == f"{expected.value} at original_frame_reader.read"
            and all(repr(content) not in rendered for content in forbidden_pixels)
        )
    return False


def _canonical_receipt_bytes(value: dict[str, object]) -> bytes:
    def reject_binary(item: object) -> None:
        if isinstance(item, (bytes, bytearray, memoryview)):
            _fail("receipt_contains_binary_data")
        if type(item) is dict:
            for key, nested in cast(dict[object, object], item).items():
                if type(key) is not str:
                    _fail("invalid_receipt")
                reject_binary(nested)
        elif type(item) is list:
            for nested in cast(list[object], item):
                reject_binary(nested)

    reject_binary(value)
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError):
        _fail("invalid_receipt")


def _write_receipt(path: Path, value: dict[str, object]) -> None:
    payload = _canonical_receipt_bytes(value)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
    except OSError:
        _fail("receipt_write_failed")


def _resolve_inputs(
    media_root: Path,
    media_worker: Path,
    overlay_root: Path,
    overlay_worker: Path,
    work_root: Path,
    output: Path,
) -> tuple[Path, Path, Path, Path, Path, Path]:
    try:
        selected = (
            media_root.resolve(strict=True),
            media_worker.resolve(strict=True),
            overlay_root.resolve(strict=True),
            overlay_worker.resolve(strict=True),
            work_root.resolve(strict=True),
        )
    except OSError:
        _fail("invalid_input_path")
    if not all(path.is_dir() for path in (selected[0], selected[2], selected[4])):
        _fail("invalid_input_path")
    if not all(path.is_file() for path in (selected[1], selected[3])):
        _fail("invalid_input_path")
    selected_output = Path(os.path.abspath(output))
    if (
        selected_output.exists()
        or selected_output.is_symlink()
        or not selected_output.parent.is_dir()
    ):
        _fail("invalid_output_path")
    return (*selected, selected_output)


def _run(
    media_root: Path,
    media_worker: Path,
    overlay_root: Path,
    overlay_worker: Path,
    work_root: Path,
) -> dict[str, object]:
    if not _supported_host():
        _fail("unsupported_platform")
    manifest = fixtures.load_manifest()
    crops = _crop_records()
    expected_by_id = {cast(str, item["fixture_id"]): item for item in _fixture_records(manifest)}
    media_runtime = MediaRuntime(media_root, media_worker)
    runtime = OriginalFrameRuntime(media_runtime, overlay_root, overlay_worker)
    fixture_results: dict[str, object] = {}
    cfr_context: tuple[str, Source, tuple[FrameRef, ...], tuple[bytes, ...]] | None = None
    vfr_frames: tuple[FrameRef, ...] | None = None
    all_checks: list[bool] = []

    with tempfile.TemporaryDirectory(
        prefix="visualworld-original-frame-", dir=work_root
    ) as temporary:
        source_root = Path(temporary) / "sources"
        fixtures.generate(source_root)
        for spec in fixtures.SPECS:
            expected = expected_by_id[spec.fixture_id]
            expected_frames = cast(list[dict[str, object]], expected["frames"])
            adapter = LocalVideoSource(source_root, spec.filename, media_runtime)
            source = adapter.probe()
            frames = adapter.read_frames(stream_index=0)
            if len(frames) != len(expected_frames):
                _fail("fixture_frame_count_mismatch")
            request = tuple(reversed(frames))
            reader = IsolatedOriginalFrameReader(source_root, spec.filename, runtime)
            read_result = reader.read(source, request)
            originals = read_result.frames
            full_hashes = [item.sha256 for item in originals]
            expected_hashes = [
                cast(str, expected_frames[int(frame.decode_index)]["rgb24_sha256"])
                for frame in request
            ]
            crop_hashes: list[str] = []
            expected_crop_hashes: list[str] = []
            exact_crops = len(originals) == len(request)
            for original, frame in zip(originals, request, strict=True):
                index = int(frame.decode_index)
                box = _moving_box(expected_frames[index])
                golden = crops[spec.fixture_id][index]
                crop = extract_rgb24_crop(
                    original.pixels,
                    original.width,
                    original.height,
                    source_geometry(original.width, original.height, box),
                )
                crop_hashes.append(crop.sha256)
                expected_crop_hashes.append(cast(str, golden["rgb24_sha256"]))
                exact_crops = exact_crops and golden["box_xyxy"] == list(box)
            checks = {
                "complete": read_result.state is PerceptionResultState.COMPLETE,
                "source_fingerprint": source.fingerprint.digest == expected["sha256"]
                and source.fingerprint.bytes == str(expected["byte_count"]),
                "encoded_dimensions": source.streams[0].to_mapping()["width"]
                == cast(dict[str, int], expected["encoded_dimensions"])["width"]
                and source.streams[0].to_mapping()["height"]
                == cast(dict[str, int], expected["encoded_dimensions"])["height"],
                "rotation": source.streams[0].rotation_degrees == expected["rotation_degrees"],
                "time_base": source.streams[0].time_base.to_mapping() == expected["time_base"],
                "decode_order": [frame.decode_index for frame in frames]
                == [str(index) for index in range(len(expected_frames))],
                "pts": [frame.pts.value for frame in frames]
                == [cast(str, item["pts"]) for item in expected_frames],
                "durations": [
                    None if frame.duration is None else frame.duration.value for frame in frames
                ]
                == [cast(str, item["duration"]) for item in expected_frames],
                "media_probe_rgb24_hashes": list(adapter.details.pixel_hashes)
                == [cast(str, item["rgb24_sha256"]) for item in expected_frames],
                "exact_frame_ref_request_order": tuple(item.frame for item in originals) == request,
                "exact_source_binding": all(item.source == source for item in originals),
                "exact_full_rgb24_hashes": full_hashes == expected_hashes
                and all(
                    hashlib.sha256(item.pixels).hexdigest() == item.sha256 for item in originals
                ),
                "exact_moving_region_crop_hashes": exact_crops
                and crop_hashes == expected_crop_hashes,
            }
            all_checks.extend(checks.values())
            fixture_results[spec.fixture_id] = {
                "checks": checks,
                "requested_decode_indexes": [frame.decode_index for frame in request],
                "returned_decode_indexes": [item.frame.decode_index for item in originals],
                "full_rgb24_sha256": full_hashes,
                "moving_region_crop_sha256": crop_hashes,
            }
            if spec.fixture_id == "cfr":
                cfr_context = (
                    spec.filename,
                    source,
                    frames,
                    tuple(item.pixels for item in originals),
                )
            elif spec.fixture_id == "vfr":
                vfr_frames = frames
            if originals:
                del original
            del originals, read_result

        if cfr_context is None or vfr_frames is None:
            _fail("fixture_context_missing")
        cfr_filename, cfr_source, cfr_frames, cfr_pixels = cfr_context
        ordinary_reader = IsolatedOriginalFrameReader(source_root, cfr_filename, runtime)
        cancelled = threading.Event()
        cancelled.set()
        failure_checks = {
            "duplicate_request": _expect_static_error(
                lambda: ordinary_reader.read(cfr_source, (cfr_frames[0], cfr_frames[0])),
                PortErrorCode.CONFLICT,
                cfr_pixels,
            ),
            "foreign_source_frame": _expect_static_error(
                lambda: ordinary_reader.read(cfr_source, vfr_frames[:1]),
                PortErrorCode.CONFLICT,
                cfr_pixels,
            ),
            "byte_limit": _expect_static_error(
                lambda: ordinary_reader.read(
                    cfr_source,
                    cfr_frames[:1],
                    max_total_bytes=fixtures.WIDTH * fixtures.HEIGHT * 3 - 1,
                ),
                PortErrorCode.LIMIT_EXCEEDED,
                cfr_pixels,
            ),
            "cancellation": _expect_static_error(
                lambda: IsolatedOriginalFrameReader(
                    source_root,
                    cfr_filename,
                    runtime,
                    cancelled=cancelled,
                ).read(cfr_source, cfr_frames[:1]),
                PortErrorCode.CANCELLED,
                cfr_pixels,
            ),
            "timeout": _expect_static_error(
                lambda: IsolatedOriginalFrameReader(
                    source_root,
                    cfr_filename,
                    runtime,
                    limits=OriginalFrameLimits(wall_timeout_ms=1),
                ).read(cfr_source, cfr_frames[:1]),
                PortErrorCode.TIMEOUT,
                cfr_pixels,
            ),
        }
        mutation_name = "mutation-check.mov"
        shutil.copyfile(source_root / cfr_filename, source_root / mutation_name)
        mutation_adapter = LocalVideoSource(source_root, mutation_name, media_runtime)
        mutation_source = mutation_adapter.probe()
        mutation_frames = mutation_adapter.read_frames(stream_index=0)
        with (source_root / mutation_name).open("ab") as stream:
            stream.write(b"mutation")
        failure_checks["source_mutation"] = _expect_static_error(
            lambda: IsolatedOriginalFrameReader(source_root, mutation_name, runtime).read(
                mutation_source, mutation_frames[:1]
            ),
            PortErrorCode.CONFLICT,
            cfr_pixels,
        )
        all_checks.extend(failure_checks.values())

    receipt: dict[str, object] = {
        "schema": _RECEIPT_SCHEMA,
        "schema_version": 1,
        "status": "pass" if all(all_checks) else "fail",
        "host": {
            "machine": platform.machine(),
            "operating_system": platform.system(),
            "native_linux_x86_64": True,
        },
        "fixture_set": "synthetic-v1",
        "fixture_manifest_sha256": _sha256(fixtures.MANIFEST_PATH),
        "crop_manifest_sha256": _sha256(CROP_MANIFEST_PATH),
        "media_worker_sha256": _sha256(media_worker),
        "overlay_worker_sha256": _sha256(overlay_worker),
        "successful_fixtures": fixture_results,
        "bounded_static_failure_cases": failure_checks,
        "isolation": {
            "linux_x86_64_only": True,
            "sealed_input_snapshot_required": True,
            "sealed_output_memfd_required": True,
            "whole_cgroup_cancel_and_timeout_required": True,
        },
    }
    _canonical_receipt_bytes(receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-runtime", type=Path, required=True)
    parser.add_argument("--media-worker", type=Path, required=True)
    parser.add_argument("--overlay-root", type=Path, required=True)
    parser.add_argument("--overlay-worker", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if not _supported_host():
        parser.exit(
            2,
            "Original-frame acceptance requires supported Linux x86_64 GNU libc; "
            "no native proof was run.\n",
        )
    try:
        inputs = _resolve_inputs(
            arguments.media_runtime,
            arguments.media_worker,
            arguments.overlay_root,
            arguments.overlay_worker,
            arguments.work_root,
            arguments.output,
        )
        result = _run(*inputs[:-1])
        _write_receipt(inputs[-1], result)
    except AcceptanceError as error:
        parser.exit(1, f"Original-frame acceptance failed: {error.code}.\n")
    except PortError as error:
        parser.exit(1, f"Original-frame acceptance failed: {error.code.value}.\n")
    except (KeyError, OSError, TypeError, ValueError):
        parser.exit(1, "Original-frame acceptance failed: operation_failed.\n")
    print(json.dumps({"schema": _RECEIPT_SCHEMA, "status": result["status"]}, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
