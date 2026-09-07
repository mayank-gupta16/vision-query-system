#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build the locked, privacy-sanitized v0.2 detector research frames."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import NoReturn, TextIO, cast

ROOT = Path(__file__).resolve().parents[1]
SOURCE_MANIFEST = ROOT / "fixtures" / "v02-detection-research" / "source-manifest.json"
ANNOTATION_LOCK = ROOT / "fixtures" / "v02-detection-research" / "annotations.json"
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_DECODER_DIAGNOSTIC_BYTES = 64 * 1024


class PreparationError(RuntimeError):
    """A stable dataset preparation failure."""


def _silence_stream(stream: TextIO) -> None:
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return
    try:
        null_descriptor = os.open(os.devnull, os.O_WRONLY | os.O_CLOEXEC)
        try:
            os.dup2(null_descriptor, descriptor)
        finally:
            os.close(null_descriptor)
    except OSError:
        pass


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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    selected: dict[str, object] = {}
    for key, value in pairs:
        if key in selected:
            raise ValueError("duplicate key")
        selected[key] = value
    return selected


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite number")


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(type(key) is str for key in value):
        raise PreparationError("invalid_manifest")
    return cast(dict[str, object], value)


def _integer(value: object, *, minimum: int = 0, maximum: int = (1 << 63) - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PreparationError("invalid_manifest")
    return value


def _text(value: object, *, maximum: int = 2048) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or not value.isascii()
        or any(ord(character) < 0x20 for character in value)
    ):
        raise PreparationError("invalid_manifest")
    return value


def _digest(value: object, length: int) -> str:
    selected = _text(value, maximum=length)
    if len(selected) != length or any(
        character not in "0123456789abcdef" for character in selected
    ):
        raise PreparationError("invalid_manifest")
    return selected


def _load_json(path: Path) -> tuple[dict[str, object], str]:
    try:
        raw = _regular_file(path, maximum=_MAX_MANIFEST_BYTES)
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if not isinstance(value, dict) or not all(type(key) is str for key in value):
            raise ValueError
    except (OSError, PreparationError, UnicodeError, ValueError) as error:
        raise PreparationError("invalid_manifest") from error
    return cast(dict[str, object], value), _sha256_bytes(raw)


def _regular_file(path: Path, *, maximum: int) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
            raise PreparationError("invalid_source")
        raw = b""
        while len(raw) < metadata.st_size:
            chunk = os.read(descriptor, min(64 * 1024, metadata.st_size - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) != metadata.st_size:
            raise PreparationError("invalid_source")
        return raw
    except OSError as error:
        raise PreparationError("invalid_source") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _create_private_directory(path: Path) -> None:
    """Create an absolute directory without following any path-component links."""

    if not path.is_absolute() or path == Path("/"):
        raise PreparationError("output_failed")
    descriptor = os.open("/", os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError as error:
                    raise PreparationError("output_failed") from error
                if final:
                    return
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise PreparationError("output_exists" if final else "output_failed") from error
            else:
                if final:
                    os.close(child)
                    raise PreparationError("output_exists")
            os.close(descriptor)
            descriptor = child
        raise PreparationError("output_failed")
    except FileNotFoundError as error:
        raise PreparationError("output_failed") from error
    finally:
        os.close(descriptor)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired as error:
        raise PreparationError("decoder_failed") from error


def _run_bounded(
    command: list[str],
    *,
    timeout_seconds: int,
    maximum_output_bytes: int,
    code: str,
    environment: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    try:
        process = subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise PreparationError(code) from error
    if process.stdout is None or process.stderr is None:
        _terminate(process)
        raise PreparationError(code)
    selector = selectors.DefaultSelector()
    streams = {
        process.stdout.fileno(): bytearray(),
        process.stderr.fileno(): bytearray(),
    }
    for descriptor in streams:
        os.set_blocking(descriptor, False)
        selector.register(descriptor, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    failed = False
    try:
        while selector.get_map() or process.poll() is None:
            if time.monotonic() >= deadline:
                failed = True
                break
            for key, _ in selector.select(0.02):
                descriptor = cast(int, key.fileobj)
                try:
                    chunk = os.read(descriptor, 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(descriptor)
                    continue
                streams[descriptor].extend(chunk)
                if sum(len(buffer) for buffer in streams.values()) > maximum_output_bytes:
                    failed = True
                    break
            if failed:
                break
        if failed:
            _terminate(process)
            raise PreparationError(code)
        try:
            returncode = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _terminate(process)
            raise PreparationError(code) from None
        return (
            returncode,
            bytes(streams[process.stdout.fileno()]),
            bytes(streams[process.stderr.fileno()]),
        )
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def parse_ppm(raw: bytes) -> tuple[int, int, bytes]:
    """Parse the narrow P6 form emitted by the reviewed decoder command."""

    first, separator, rest = raw.partition(b"\n")
    dimensions, separator_two, body = rest.partition(b"\n")
    maximum, separator_three, pixels = body.partition(b"\n")
    if (
        first != b"P6"
        or not separator
        or not separator_two
        or not separator_three
        or maximum != b"255"
    ):
        raise PreparationError("invalid_decoder_output")
    try:
        width_text, height_text = dimensions.split(b" ")
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError) as error:
        raise PreparationError("invalid_decoder_output") from error
    if not 1 <= width <= 4096 or not 1 <= height <= 4096 or len(pixels) != width * height * 3:
        raise PreparationError("invalid_decoder_output")
    return width, height, pixels


def _box(value: object, width: int, height: int) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(coordinate) is not int for coordinate in value)
    ):
        raise PreparationError("invalid_manifest")
    left, top, right, bottom = cast(list[int], value)
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise PreparationError("invalid_manifest")
    return left, top, right, bottom


def fill_box(
    pixels: bytearray,
    width: int,
    height: int,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
) -> None:
    left, top, right, bottom = _box(list(box), width, height)
    row = bytes(color) * (right - left)
    for y in range(top, bottom):
        start = (y * width + left) * 3
        pixels[start : start + len(row)] = row


def crop_rgb24(
    pixels: bytes | bytearray,
    width: int,
    height: int,
    box: tuple[int, int, int, int],
) -> tuple[int, int, bytes]:
    left, top, right, bottom = _box(list(box), width, height)
    crop_width, crop_height = right - left, bottom - top
    output = bytearray(crop_width * crop_height * 3)
    for output_y, source_y in enumerate(range(top, bottom)):
        source_start = (source_y * width + left) * 3
        output_start = output_y * crop_width * 3
        output[output_start : output_start + crop_width * 3] = pixels[
            source_start : source_start + crop_width * 3
        ]
    return crop_width, crop_height, bytes(output)


def resize_rgb24(
    pixels: bytes,
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> bytes:
    """Resize RGB24 with deterministic fixed-point bilinear interpolation."""

    if min(source_width, source_height, target_width, target_height) < 1:
        raise PreparationError("invalid_dimensions")
    output = bytearray(target_width * target_height * 3)
    scale = 1 << 16
    for target_y in range(target_height):
        source_y_fixed = ((2 * target_y + 1) * source_height * scale) // (
            2 * target_height
        ) - scale // 2
        y0 = max(0, min(source_height - 1, source_y_fixed // scale))
        y1 = min(source_height - 1, y0 + 1)
        fy = max(0, min(scale, source_y_fixed - y0 * scale))
        for target_x in range(target_width):
            source_x_fixed = ((2 * target_x + 1) * source_width * scale) // (
                2 * target_width
            ) - scale // 2
            x0 = max(0, min(source_width - 1, source_x_fixed // scale))
            x1 = min(source_width - 1, x0 + 1)
            fx = max(0, min(scale, source_x_fixed - x0 * scale))
            output_offset = (target_y * target_width + target_x) * 3
            for channel in range(3):
                top_value = (
                    pixels[(y0 * source_width + x0) * 3 + channel] * (scale - fx)
                    + pixels[(y0 * source_width + x1) * 3 + channel] * fx
                )
                bottom_value = (
                    pixels[(y1 * source_width + x0) * 3 + channel] * (scale - fx)
                    + pixels[(y1 * source_width + x1) * 3 + channel] * fx
                )
                value = top_value * (scale - fy) + bottom_value * fy
                output[output_offset + channel] = (value + (1 << 31)) >> 32
    return bytes(output)


def generated_background(width: int, height: int) -> bytearray:
    """Return a person-, text-, and plate-free synthetic road scene."""

    horizon = height // 2
    output = bytearray(width * height * 3)
    for y in range(height):
        if y < horizon:
            color = (112 + y // 9, 158 + y // 12, 205 + y // 18)
        else:
            shade = 80 + (y - horizon) // 8
            color = (shade, shade, min(255, shade + 3))
        row = bytes(color) * width
        output[y * width * 3 : (y + 1) * width * 3] = row
    for y in range(horizon, height):
        half_width = max(2, (y - horizon) // 30)
        if (y // 18) % 2 == 0:
            fill_box(
                output,
                width,
                height,
                (width // 2 - half_width, y, width // 2 + half_width, y + 1),
                (230, 220, 150),
            )
    return output


def _fit(source_width: int, source_height: int, max_width: int, max_height: int) -> tuple[int, int]:
    numerator = min(max_width * source_height, max_height * source_width)
    if numerator == max_width * source_height:
        width = max_width
        height = max(1, source_height * max_width // source_width)
    else:
        height = max_height
        width = max(1, source_width * max_height // source_height)
    return width, height


def compose_item(
    source_id: str,
    stratum: str,
    chip_width: int,
    chip_height: int,
    chip_pixels: bytes,
    canvas_width: int,
    canvas_height: int,
    limits: dict[str, int],
) -> tuple[bytes, list[int]]:
    if stratum == "easy":
        max_width, max_height = limits["easy_max_width"], limits["easy_max_height"]
    elif stratum == "small_distant":
        max_width = limits["small_distant_max_width"]
        max_height = limits["small_distant_max_height"]
    else:
        raise PreparationError("invalid_stratum")
    target_width, target_height = _fit(chip_width, chip_height, max_width, max_height)
    resized = resize_rgb24(chip_pixels, chip_width, chip_height, target_width, target_height)
    seed = hashlib.sha256(f"{source_id}:{stratum}".encode("ascii")).digest()
    if stratum == "easy":
        left = (canvas_width - target_width) // 2 + (seed[0] % 21) - 10
        top = canvas_height - target_height - 12
    else:
        left = 70 + seed[0] * (canvas_width - target_width - 140) // 255
        top = canvas_height // 2 + 8 + seed[1] % 30
    left = max(0, min(canvas_width - target_width, left))
    top = max(0, min(canvas_height - target_height, top))
    canvas = generated_background(canvas_width, canvas_height)
    for y in range(target_height):
        source_start = y * target_width * 3
        target_start = ((top + y) * canvas_width + left) * 3
        canvas[target_start : target_start + target_width * 3] = resized[
            source_start : source_start + target_width * 3
        ]
    header = f"P6\n{canvas_width} {canvas_height}\n255\n".encode("ascii")
    return header + canvas, [left, top, left + target_width, top + target_height]


def _decoder_command(source: Path, root: Path) -> list[str]:
    return [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--dir",
        "/home",
        "--dir",
        "/home/worker",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/home/worker",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/lib",
        "/lib",
        "--ro-bind",
        "/lib64",
        "/lib64",
        "--ro-bind",
        "/etc/ld.so.cache",
        "/etc/ld.so.cache",
        "--ro-bind",
        os.fspath(source),
        "/input.jpg",
        "--bind",
        os.fspath(root),
        "/output",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "/usr/bin/ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "/input.jpg",
        "-frames:v",
        "1",
        "-pix_fmt",
        "rgb24",
        "-f",
        "image2",
        "/output/decoded.ppm",
    ]


def _decode(source: Path, expected_width: int, expected_height: int) -> tuple[int, int, bytes]:
    with tempfile.TemporaryDirectory(prefix="visualworld-v02-detection-") as temporary:
        root = Path(temporary)
        command = _decoder_command(source, root)
        returncode, stdout, stderr = _run_bounded(
            command,
            timeout_seconds=30,
            maximum_output_bytes=_MAX_DECODER_DIAGNOSTIC_BYTES,
            code="decoder_failed",
        )
        if returncode or stdout or stderr:
            raise PreparationError("decoder_failed")
        width, height, pixels = parse_ppm(
            _regular_file(root / "decoded.ppm", maximum=64 * 1024 * 1024)
        )
    if (width, height) != (expected_width, expected_height):
        raise PreparationError("source_dimensions_mismatch")
    return width, height, pixels


def _write_new(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        written = 0
        while written < len(raw):
            written += os.write(descriptor, raw[written:])
        os.fsync(descriptor)
    except OSError as error:
        raise PreparationError("output_failed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def prepare(source_root: Path, output_root: Path) -> dict[str, object]:
    manifest, manifest_sha256 = _load_json(SOURCE_MANIFEST)
    if (
        set(manifest)
        != {
            "canvas",
            "decoder",
            "derivation",
            "license_expression",
            "reviewed_on",
            "schema",
            "schema_version",
            "sources",
            "terms_url",
        }
        or manifest.get("schema") != "visualworld.v02-detection-source-manifest"
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("license_expression") != "CC0-1.0"
        or manifest.get("terms_url")
        != "https://creativecommons.org/publicdomain/zero/1.0/legalcode"
    ):
        raise PreparationError("invalid_manifest")
    _text(manifest.get("reviewed_on"), maximum=10)
    canvas = _mapping(manifest.get("canvas"))
    if canvas != {"height": 360, "pixel_format": "rgb24", "width": 640}:
        raise PreparationError("invalid_manifest")
    canvas_width = _integer(canvas.get("width"), minimum=1, maximum=4096)
    canvas_height = _integer(canvas.get("height"), minimum=1, maximum=4096)
    derivation = _mapping(manifest.get("derivation"))
    if set(derivation) != {
        "background",
        "easy_max_height",
        "easy_max_width",
        "interpolation",
        "plate_fill_rgb",
        "small_distant_max_height",
        "small_distant_max_width",
    } or (
        derivation.get("background") != "visualworld-generated-road-v1"
        or derivation.get("interpolation") != "visualworld-fixed-point-bilinear-v1"
    ):
        raise PreparationError("invalid_manifest")
    limits = {
        name: _integer(derivation.get(name), minimum=1, maximum=640)
        for name in (
            "easy_max_height",
            "easy_max_width",
            "small_distant_max_height",
            "small_distant_max_width",
        )
    }
    fill_value = derivation.get("plate_fill_rgb")
    if not isinstance(fill_value, list) or len(fill_value) != 3:
        raise PreparationError("invalid_manifest")
    fill = (
        _integer(fill_value[0], maximum=255),
        _integer(fill_value[1], maximum=255),
        _integer(fill_value[2], maximum=255),
    )
    decoder = _mapping(manifest.get("decoder"))
    if (
        set(decoder)
        != {
            "binary_sha256",
            "bubblewrap_binary_sha256",
            "bubblewrap_package_version",
            "command_surface",
            "ffmpeg_package_version",
            "isolation",
            "redistributed",
        }
        or decoder.get("redistributed") is not False
    ):
        raise PreparationError("invalid_manifest")
    if _sha256_bytes(_regular_file(Path("/usr/bin/ffmpeg"), maximum=2 * 1024 * 1024)) != _digest(
        decoder.get("binary_sha256"), 64
    ) or _sha256_bytes(_regular_file(Path("/usr/bin/bwrap"), maximum=2 * 1024 * 1024)) != _digest(
        decoder.get("bubblewrap_binary_sha256"), 64
    ):
        raise PreparationError("decoder_digest_mismatch")
    sources_value = manifest.get("sources")
    if not isinstance(sources_value, list) or len(sources_value) != 10:
        raise PreparationError("invalid_manifest")
    _create_private_directory(output_root)

    annotations: list[dict[str, object]] = []
    seen: set[str] = set()
    split_counts = {"calibration": 0, "test": 0}
    source_fields = {
        "artist",
        "download_height",
        "download_name",
        "download_sha256",
        "download_url",
        "download_width",
        "file_revision_timestamp",
        "file_sha1",
        "object_box",
        "page_id",
        "page_revision",
        "page_url",
        "privacy_review",
        "redactions",
        "source_id",
        "split",
        "title",
    }
    for source_value_raw in sources_value:
        source_value = _mapping(source_value_raw)
        if set(source_value) != source_fields:
            raise PreparationError("invalid_manifest")
        source_id = _text(source_value.get("source_id"), maximum=64)
        if (
            source_id in seen
            or not source_id.strip("-_").replace("-", "").replace("_", "").isalnum()
        ):
            raise PreparationError("invalid_manifest")
        seen.add(source_id)
        split = _text(source_value.get("split"), maximum=16)
        if split not in split_counts:
            raise PreparationError("invalid_manifest")
        split_counts[split] += 1
        download_name = _text(source_value.get("download_name"), maximum=128)
        if download_name != f"{source_id}.jpg":
            raise PreparationError("invalid_manifest")
        source_path = source_root / download_name
        source_raw = _regular_file(source_path, maximum=_MAX_SOURCE_BYTES)
        if _sha256_bytes(source_raw) != _digest(source_value.get("download_sha256"), 64):
            raise PreparationError("source_digest_mismatch")
        width = _integer(source_value.get("download_width"), minimum=1, maximum=4096)
        height = _integer(source_value.get("download_height"), minimum=1, maximum=4096)
        if (
            not _text(source_value.get("download_url")).startswith("https://thumb.wikimedia.org/")
            or "?" in cast(str, source_value["download_url"])
            or "#" in cast(str, source_value["download_url"])
            or not _text(source_value.get("page_url")).startswith(
                "https://commons.wikimedia.org/wiki/File:"
            )
        ):
            raise PreparationError("invalid_manifest")
        _digest(source_value.get("file_sha1"), 40)
        _integer(source_value.get("page_id"), minimum=1)
        _integer(source_value.get("page_revision"), minimum=1)
        _text(source_value.get("file_revision_timestamp"), maximum=32)
        _text(source_value.get("artist"), maximum=256)
        _text(source_value.get("title"), maximum=512)
        _text(source_value.get("privacy_review"), maximum=1024)
        _box(source_value.get("object_box"), width, height)
        redactions_value = source_value.get("redactions")
        if not isinstance(redactions_value, list) or len(redactions_value) > 16:
            raise PreparationError("invalid_manifest")
        for redaction_raw in redactions_value:
            redaction = _mapping(redaction_raw)
            if set(redaction) != {"box", "kind"} or redaction.get("kind") not in {
                "background-plate-risk",
                "plate",
            }:
                raise PreparationError("invalid_manifest")
            _box(redaction.get("box"), width, height)
        width, height, pixels = _decode(
            source_path.resolve(),
            width,
            height,
        )
        sanitized = bytearray(pixels)
        for redaction_raw in redactions_value:
            redaction = _mapping(redaction_raw)
            fill_box(
                sanitized,
                width,
                height,
                _box(redaction.get("box"), width, height),
                fill,
            )
        chip_width, chip_height, chip = crop_rgb24(
            sanitized,
            width,
            height,
            _box(source_value.get("object_box"), width, height),
        )
        for stratum in ("easy", "small_distant"):
            item_id = f"{split}-{source_id}-{stratum}"
            frame, object_box = compose_item(
                source_id,
                stratum,
                chip_width,
                chip_height,
                chip,
                canvas_width,
                canvas_height,
                limits,
            )
            relative_path = f"{split}/{item_id}.ppm"
            _write_new(output_root / relative_path, frame)
            annotations.append(
                {
                    "frame_sha256": _sha256_bytes(frame),
                    "height": canvas_height,
                    "item_id": item_id,
                    "object_box": object_box,
                    "relative_path": relative_path,
                    "source_id": source_id,
                    "split": split,
                    "stratum": stratum,
                    "width": canvas_width,
                }
            )
    if split_counts != {"calibration": 4, "test": 6}:
        raise PreparationError("invalid_manifest")
    lock: dict[str, object] = {
        "items": annotations,
        "schema": "visualworld.v02-detection-annotation-lock",
        "schema_version": 1,
        "source_manifest_sha256": manifest_sha256,
    }
    serialized = json.dumps(lock, allow_nan=False, indent=2, sort_keys=True).encode("ascii") + b"\n"
    _write_new(output_root / "annotations.generated.json", serialized)
    if ANNOTATION_LOCK.exists():
        expected, _ = _load_json(ANNOTATION_LOCK)
        if _canonical(expected) != _canonical(lock):
            raise PreparationError("annotation_lock_mismatch")
    return lock


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        lock = prepare(arguments.source_root.resolve(), Path(os.path.abspath(arguments.output)))
    except (OSError, PreparationError) as error:
        code = str(error) if isinstance(error, PreparationError) else "filesystem_failed"
        _emit({"error": code, "status": "error"}, sys.stdout)
        return 1
    items = cast(list[dict[str, object]], lock["items"])
    return (
        0
        if _emit(
            {
                "calibration_items": sum(item["split"] == "calibration" for item in items),
                "status": "pass",
                "test_items": sum(item["split"] == "test" for item in items),
            },
            sys.stdout,
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
