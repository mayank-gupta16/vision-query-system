# SPDX-License-Identifier: Apache-2.0
"""Tests for exact detector geometry and original-pixel RGB24 crops."""

from __future__ import annotations

import hashlib
import os
import stat
import traceback
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

import pytest

import visualworld.geometry as geometry_module
from visualworld.geometry import (
    CropError,
    DetectorTransform,
    Rgb24Crop,
    extract_rgb24_crop,
    source_geometry,
    write_rgb24_crop,
)
from visualworld.ingestion import (
    AffineCoefficients,
    Geometry,
    ProducerSpace,
    Rational,
    RecordValidationError,
)


def _frame(width: int, height: int) -> bytes:
    return bytes(
        channel for y in range(height) for x in range(width) for channel in (x, y, (x + y) % 256)
    )


def _private_directory(path: Path) -> Path:
    path.mkdir()
    path.chmod(0o700)
    return path


def _expected_crop(
    frame: bytes,
    width: int,
    box: tuple[int, int, int, int],
) -> bytes:
    x_min, y_min, x_max, y_max = box
    stride = width * 3
    return b"".join(
        frame[y * stride + x_min * 3 : y * stride + x_max * 3] for y in range(y_min, y_max)
    )


def _display_box(
    source_box: tuple[int, int, int, int],
    width: int,
    height: int,
    rotation: int,
) -> tuple[int, int, int, int]:
    x_min, y_min, x_max, y_max = source_box
    normalized = rotation % 360
    if normalized == 0:
        return source_box
    if normalized == 90:
        return (height - y_max, x_min, height - y_min, x_max)
    if normalized == 180:
        return (width - x_max, height - y_max, width - x_min, height - y_min)
    if normalized == 270:
        return (y_min, width - x_max, y_max, width - x_min)
    raise AssertionError("unsupported test rotation")


def test_non_square_resize_maps_with_exact_outward_rounding() -> None:
    transform = DetectorTransform(17, 11, 8, 6)

    geometry = transform.map_box((1, 1, 7, 5))

    assert geometry.box_xyxy == (2, 1, 15, 10)
    assert geometry.producer_space == ProducerSpace(8, 6, (1, 1, 7, 5))
    assert geometry.coefficients is not None
    assert geometry.coefficients.a.fraction() == Fraction(17, 8)
    assert geometry.coefficients.e.fraction() == Fraction(11, 6)


def test_letterbox_padding_is_clamped_to_source_bounds() -> None:
    transform = DetectorTransform(16, 12, 10, 10, content_box_xyxy=(0, 1, 10, 9))

    assert transform.map_box((0, 0, 2, 2)).box_xyxy == (0, 0, 4, 2)
    with pytest.raises(CropError, match="empty_source_box"):
        transform.map_box((0, 0, 10, 1))


@pytest.mark.parametrize("rotation", [0, 90, 180, 270, -90, 360])
def test_quarter_turns_map_display_boxes_back_to_source(rotation: int) -> None:
    width, height = 16, 12
    source_box = (2, 3, 5, 7)
    normalized = rotation % 360
    detector_width, detector_height = (
        (height, width) if normalized in {90, 270} else (width, height)
    )
    transform = DetectorTransform(
        width,
        height,
        detector_width,
        detector_height,
        rotation_degrees=rotation,
    )

    geometry = transform.map_box(_display_box(source_box, width, height, rotation))

    assert transform.rotation_degrees == normalized
    assert geometry.box_xyxy == source_box


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_every_small_source_box_round_trips_through_display_rotation(rotation: int) -> None:
    width, height = 5, 4
    detector_width, detector_height = (height, width) if rotation in {90, 270} else (width, height)
    transform = DetectorTransform(
        width,
        height,
        detector_width,
        detector_height,
        rotation_degrees=rotation,
    )
    for x_min in range(width):
        for x_max in range(x_min + 1, width + 1):
            for y_min in range(height):
                for y_max in range(y_min + 1, height + 1):
                    source_box = (x_min, y_min, x_max, y_max)
                    detector_box = _display_box(source_box, width, height, rotation)
                    assert transform.map_box(detector_box).box_xyxy == source_box


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_scaled_letterboxed_quarter_turns_preserve_source_boxes(rotation: int) -> None:
    width, height = 5, 4
    display_width, display_height = (height, width) if rotation in {90, 270} else (width, height)
    content = (2, 3, 2 + display_width * 2, 3 + display_height * 2)
    transform = DetectorTransform(
        width,
        height,
        content[2] + 2,
        content[3] + 3,
        rotation_degrees=rotation,
        content_box_xyxy=content,
    )
    for source_box in ((0, 0, width, height), (1, 1, 4, 3), (0, 2, 2, 4)):
        display_box = _display_box(source_box, width, height, rotation)
        detector_box = (
            content[0] + display_box[0] * 2,
            content[1] + display_box[1] * 2,
            content[0] + display_box[2] * 2,
            content[1] + display_box[3] * 2,
        )
        assert transform.map_box(detector_box).box_xyxy == source_box


def test_affine_record_validation_uses_the_same_source_clamp_policy() -> None:
    zero = Rational("0", "1")
    one = Rational("1", "1")
    minus_two = Rational("-2", "1")

    geometry = Geometry(
        5,
        5,
        (0, 0, 3, 3),
        "measured",
        "affine_rational",
        ProducerSpace(5, 5, (0, 0, 5, 5)),
        AffineCoefficients(one, zero, minus_two, zero, one, minus_two),
    )

    assert geometry.box_xyxy == (0, 0, 3, 3)
    with pytest.raises(RecordValidationError, match="affine_box_mismatch"):
        Geometry(
            5,
            5,
            (0, 0, 4, 4),
            "measured",
            "affine_rational",
            ProducerSpace(5, 5, (0, 0, 5, 5)),
            AffineCoefficients(one, zero, minus_two, zero, one, minus_two),
        )


@pytest.mark.parametrize(
    "create",
    [
        lambda: DetectorTransform(0, 1, 1, 1),
        lambda: DetectorTransform(1, 1, 0, 1),
        lambda: DetectorTransform(1, 1, 1, 1, rotation_degrees=45),
        lambda: DetectorTransform(1, 1, 1, 1, rotation_degrees=True),
        lambda: DetectorTransform(10, 10, 8, 8, content_box_xyxy=(0, 0, 9, 8)),
        lambda: DetectorTransform(10, 10, 8, 8, content_box_xyxy=(0, 0, 0, 8)),
    ],
)
def test_invalid_transform_configuration_fails_closed(create: Callable[[], object]) -> None:
    with pytest.raises(CropError):
        create()


@pytest.mark.parametrize(
    "box",
    [
        (0, 0, 0, 1),
        (-1, 0, 1, 1),
        (0, 0, 9, 1),
        (0, 0, 1),
        [0, 0, 1, 1],
        (False, 0, 1, 1),
    ],
)
def test_invalid_detector_boxes_fail_closed(box: object) -> None:
    with pytest.raises(CropError, match="invalid_detector_box"):
        DetectorTransform(10, 10, 8, 8).map_box(box)  # type: ignore[arg-type]


def test_invalid_measurement_and_source_box_fail_closed() -> None:
    with pytest.raises(CropError, match="invalid_measurement"):
        DetectorTransform(10, 10, 5, 5).map_box((0, 0, 1, 1), measurement="pixels")
    with pytest.raises(CropError, match="invalid_source_box"):
        source_geometry(10, 10, (0, 0, 11, 10))


def test_source_geometry_and_rgb24_crop_are_byte_exact() -> None:
    width, height = 4, 3
    frame = _frame(width, height)
    geometry = source_geometry(width, height, (1, 1, 4, 3))

    crop = extract_rgb24_crop(frame, width, height, geometry)

    assert crop.width == 3
    assert crop.height == 2
    assert crop.pixels == _expected_crop(frame, width, geometry.box_xyxy)
    assert crop.sha256 == hashlib.sha256(crop.pixels).hexdigest()
    assert crop.artifact().sha256 == crop.sha256
    assert crop.artifact().bytes == str(len(crop.pixels))
    assert frame.hex() not in repr(crop)
    assert "pixels=" not in repr(crop)


@pytest.mark.parametrize(
    "extract",
    [
        lambda: extract_rgb24_crop(b"short", 4, 3, source_geometry(4, 3, (1, 1, 4, 3))),
        lambda: extract_rgb24_crop(_frame(4, 3), 4, 3, source_geometry(5, 3, (1, 1, 4, 3))),
        lambda: extract_rgb24_crop(
            bytearray(_frame(4, 3)),  # type: ignore[arg-type]
            4,
            3,
            source_geometry(4, 3, (1, 1, 4, 3)),
        ),
    ],
)
def test_invalid_rgb24_inputs_fail_without_echoing_pixels(extract: Callable[[], object]) -> None:
    with pytest.raises(CropError) as raised:
        extract()
    assert "short" not in str(raised.value)


def test_crop_value_rejects_inconsistent_bytes() -> None:
    with pytest.raises(CropError, match="invalid_crop_bytes"):
        Rgb24Crop(2, 2, b"too short")


def test_crop_writer_is_root_confined_no_follow_and_exclusive(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    nested = root / "v1" / "sha256"
    nested.mkdir(parents=True)
    root.chmod(0o700)
    crop = extract_rgb24_crop(_frame(4, 3), 4, 3, source_geometry(4, 3, (1, 1, 4, 3)))

    artifact = write_rgb24_crop(
        crop,
        artifact_root=root,
        relative_destination="v1/sha256/crop.rgb24",
    )

    destination = nested / "crop.rgb24"
    assert destination.read_bytes() == crop.pixels
    assert stat.S_IMODE(destination.stat().st_mode) == 0o400
    assert artifact == crop.artifact()
    with pytest.raises(CropError, match="destination_unavailable"):
        write_rgb24_crop(
            crop,
            artifact_root=root,
            relative_destination="v1/sha256/crop.rgb24",
        )


@pytest.mark.parametrize(
    "destination",
    [
        "../outside.rgb24",
        "/outside.rgb24",
        "v1//crop.rgb24",
        "v1/./crop.rgb24",
        "v1/x\\y",
        "/".join("x" for _ in range(17)),
    ],
)
def test_crop_writer_rejects_non_relative_destination(tmp_path: Path, destination: str) -> None:
    root = _private_directory(tmp_path / "artifacts")
    crop = Rgb24Crop(1, 1, b"abc")

    with pytest.raises(CropError, match="invalid_destination"):
        write_rgb24_crop(crop, artifact_root=root, relative_destination=destination)

    assert not (tmp_path / "outside.rgb24").exists()


def test_crop_writer_rejects_symlink_root_and_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(real, target_is_directory=True)
    crop = Rgb24Crop(1, 1, b"abc")

    with pytest.raises(CropError, match="invalid_artifact_root"):
        write_rgb24_crop(
            crop,
            artifact_root=root_link,
            relative_destination="crop.rgb24",
        )

    root = _private_directory(tmp_path / "root")
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(CropError, match="destination_unavailable") as raised:
        write_rgb24_crop(
            crop,
            artifact_root=root,
            relative_destination="escape/outside.rgb24",
        )
    assert "escape" not in str(raised.value)
    assert raised.value.__suppress_context__ is True
    assert not (tmp_path / "outside.rgb24").exists()


def test_crop_writer_requires_an_absolute_existing_directory(tmp_path: Path) -> None:
    crop = Rgb24Crop(1, 1, b"abc")
    relative = Path(os.path.relpath(tmp_path, Path.cwd()))

    with pytest.raises(CropError, match="invalid_artifact_root"):
        write_rgb24_crop(
            crop,
            artifact_root=relative,
            relative_destination="crop.rgb24",
        )
    with pytest.raises(CropError, match="invalid_artifact_root"):
        write_rgb24_crop(
            crop,
            artifact_root=tmp_path / "missing",
            relative_destination="crop.rgb24",
        )


def test_crop_writer_removes_partial_file_and_redacts_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _private_directory(tmp_path / "artifacts")
    crop = Rgb24Crop(1, 1, b"abc")

    def fail_write(_: int, __: object) -> int:
        raise OSError("private pixels and path")

    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(CropError, match="write_failed") as raised:
        write_rgb24_crop(
            crop,
            artifact_root=root,
            relative_destination="crop.rgb24",
        )
    assert str(raised.value) == "write_failed at crop"
    assert raised.value.__suppress_context__ is True
    assert not (root / "crop.rgb24").exists()


@pytest.mark.parametrize("mode", [0o750, 0o707])
def test_crop_writer_rejects_non_private_root(tmp_path: Path, mode: int) -> None:
    root = _private_directory(tmp_path / "artifacts")
    root.chmod(mode)

    with pytest.raises(CropError, match="invalid_artifact_root"):
        write_rgb24_crop(
            Rgb24Crop(1, 1, b"abc"),
            artifact_root=root,
            relative_destination="crop.rgb24",
        )

    assert not (root / "crop.rgb24").exists()


def test_crop_writer_rejects_root_not_owned_by_effective_uid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _private_directory(tmp_path / "artifacts")
    monkeypatch.setattr(os, "geteuid", lambda: root.stat().st_uid + 1)

    with pytest.raises(CropError, match="invalid_artifact_root"):
        write_rgb24_crop(
            Rgb24Crop(1, 1, b"abc"),
            artifact_root=root,
            relative_destination="crop.rgb24",
        )


@pytest.mark.parametrize("mode", [0o770, 0o707])
def test_crop_writer_rejects_writable_destination_parent(tmp_path: Path, mode: int) -> None:
    root = _private_directory(tmp_path / "artifacts")
    parent = root / "parent"
    parent.mkdir(mode=mode)
    parent.chmod(mode)

    with pytest.raises(CropError, match="destination_unavailable"):
        write_rgb24_crop(
            Rgb24Crop(1, 1, b"abc"),
            artifact_root=root,
            relative_destination="parent/crop.rgb24",
        )

    assert not (parent / "crop.rgb24").exists()


@pytest.mark.parametrize("interruption", ["before_write", "after_write", "chmod", "fsync"])
def test_crop_writer_cleans_up_and_closes_after_interruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    interruption: str,
) -> None:
    root = _private_directory(tmp_path / "artifacts")
    private_pixels = b"secret-pixels!!"
    private_destination = "secret-destination.rgb24"
    crop = Rgb24Crop(5, 1, private_pixels)
    original_write_all = geometry_module._write_all
    original_fchmod = os.fchmod
    original_fsync = os.fsync
    captured_descriptor: int | None = None

    def interrupt_write(descriptor: int, content: bytes) -> None:
        nonlocal captured_descriptor
        captured_descriptor = descriptor
        if interruption == "after_write":
            original_write_all(descriptor, content)
        if interruption in {"before_write", "after_write"}:
            raise KeyboardInterrupt
        original_write_all(descriptor, content)

    def interrupt_chmod(descriptor: int, mode: int) -> None:
        nonlocal captured_descriptor
        captured_descriptor = descriptor
        if interruption == "chmod":
            raise KeyboardInterrupt
        original_fchmod(descriptor, mode)

    def interrupt_fsync(descriptor: int) -> None:
        nonlocal captured_descriptor
        captured_descriptor = descriptor
        if interruption == "fsync":
            raise KeyboardInterrupt
        original_fsync(descriptor)

    monkeypatch.setattr(geometry_module, "_write_all", interrupt_write)
    monkeypatch.setattr(os, "fchmod", interrupt_chmod)
    monkeypatch.setattr(os, "fsync", interrupt_fsync)
    with pytest.raises(KeyboardInterrupt) as raised:
        write_rgb24_crop(
            crop,
            artifact_root=root,
            relative_destination=private_destination,
        )

    assert captured_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(captured_descriptor)
    assert not (root / private_destination).exists()
    rendered = "".join(traceback.format_exception(raised.value))
    assert private_destination not in rendered
    assert private_pixels.decode("ascii") not in rendered


def test_crop_writer_does_not_unlink_a_replacement_after_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _private_directory(tmp_path / "artifacts")
    destination = root / "crop.rgb24"

    def replace_then_fail(descriptor: int, _: bytes) -> None:
        destination.unlink()
        destination.write_bytes(b"replacement")
        raise OSError("private pixels and path")

    monkeypatch.setattr(geometry_module, "_write_all", replace_then_fail)
    with pytest.raises(CropError, match="write_failed"):
        write_rgb24_crop(
            Rgb24Crop(1, 1, b"abc"),
            artifact_root=root,
            relative_destination="crop.rgb24",
        )

    assert destination.read_bytes() == b"replacement"


@pytest.mark.parametrize(
    "interruption", [KeyboardInterrupt(), CropError("destination_unavailable")]
)
def test_crop_writer_cleans_up_when_initial_inode_capture_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    interruption: BaseException,
) -> None:
    root = _private_directory(tmp_path / "artifacts")
    captured_descriptor: int | None = None

    def fail_inode(descriptor: int) -> tuple[int, int]:
        nonlocal captured_descriptor
        captured_descriptor = descriptor
        raise interruption

    monkeypatch.setattr(geometry_module, "_inode", fail_inode)
    with pytest.raises(type(interruption)):
        write_rgb24_crop(
            Rgb24Crop(1, 1, b"abc"),
            artifact_root=root,
            relative_destination="crop.rgb24",
        )

    assert captured_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(captured_descriptor)
    assert not (root / "crop.rgb24").exists()


def test_crop_writer_retains_descriptor_until_close_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _private_directory(tmp_path / "artifacts")
    original_close = os.close
    captured_descriptor: int | None = None
    interrupted = False

    def interrupt_final_close_once(descriptor: int) -> None:
        nonlocal captured_descriptor, interrupted
        if not interrupted and stat.S_ISREG(os.fstat(descriptor).st_mode):
            captured_descriptor = descriptor
            interrupted = True
            raise KeyboardInterrupt
        original_close(descriptor)

    monkeypatch.setattr(os, "close", interrupt_final_close_once)
    with pytest.raises(KeyboardInterrupt):
        write_rgb24_crop(
            Rgb24Crop(1, 1, b"abc"),
            artifact_root=root,
            relative_destination="crop.rgb24",
        )

    assert captured_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(captured_descriptor)
    assert not (root / "crop.rgb24").exists()
