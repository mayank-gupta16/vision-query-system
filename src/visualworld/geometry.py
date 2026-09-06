# SPDX-License-Identifier: Apache-2.0
"""Exact detector-to-source geometry and byte-preserving RGB24 crops."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import NoReturn

from visualworld.ingestion import (
    MAX_I31,
    AffineCoefficients,
    Artifact,
    Geometry,
    ProducerSpace,
    Rational,
)

Box = tuple[int, int, int, int]

_MEASUREMENTS = frozenset({"measured", "calibrated", "estimated", "inferred", "unknown"})
_DESTINATION_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MAX_DESTINATION_BYTES = 4_096
_MAX_DESTINATION_DEPTH = 16


class CropError(ValueError):
    """A stable crop failure that never echoes pixels or caller paths."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"{code} at crop")


def _fail(code: str) -> NoReturn:
    raise CropError(code) from None


def _dimension(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_I31:
        _fail("invalid_dimension")
    return value


def _box(value: object, width: int, height: int, code: str) -> Box:
    if not isinstance(value, tuple) or len(value) != 4:
        _fail(code)
    if any(type(coordinate) is not int for coordinate in value):
        _fail(code)
    x_min, y_min, x_max, y_max = value
    if not (0 <= x_min < x_max <= width and 0 <= y_min < y_max <= height):
        _fail(code)
    return value


def _measurement(value: object) -> str:
    if not isinstance(value, str) or value not in _MEASUREMENTS:
        _fail("invalid_measurement")
    return value


def _rational(value: Fraction) -> Rational:
    return Rational(str(value.numerator), str(value.denominator))


def _source_box(
    producer_box: Box,
    coefficients: AffineCoefficients,
    source_width: int,
    source_height: int,
) -> Box:
    x_min, y_min, x_max, y_max = producer_box
    a, b, c = (
        coefficients.a.fraction(),
        coefficients.b.fraction(),
        coefficients.c.fraction(),
    )
    d, e, f = (
        coefficients.d.fraction(),
        coefficients.e.fraction(),
        coefficients.f.fraction(),
    )
    corners = ((x_min, y_min), (x_min, y_max), (x_max, y_min), (x_max, y_max))
    xs = tuple(a * x + b * y + c for x, y in corners)
    ys = tuple(d * x + e * y + f for x, y in corners)
    mapped = (
        max(0, min(source_width, min(xs).__floor__())),
        max(0, min(source_height, min(ys).__floor__())),
        max(0, min(source_width, max(xs).__ceil__())),
        max(0, min(source_height, max(ys).__ceil__())),
    )
    if mapped[0] >= mapped[2] or mapped[1] >= mapped[3]:
        _fail("empty_source_box")
    return mapped


@dataclass(frozen=True, slots=True)
class DetectorTransform:
    """Exact mapping from a resized, display-oriented detector input to source pixels.

    ``content_box_xyxy`` is the detector-input rectangle occupied by the resized
    display image. Areas outside it are letterbox padding. Rotation is clockwise
    display metadata; supported quarter turns are normalized modulo 360.
    """

    source_width: int
    source_height: int
    detector_width: int
    detector_height: int
    rotation_degrees: int = 0
    content_box_xyxy: Box | None = None

    def __post_init__(self) -> None:
        source_width = _dimension(self.source_width)
        source_height = _dimension(self.source_height)
        detector_width = _dimension(self.detector_width)
        detector_height = _dimension(self.detector_height)
        rotation = self.rotation_degrees
        if type(rotation) is not int or rotation % 90 != 0:
            _fail("unsupported_rotation")
        content = self.content_box_xyxy
        if content is None:
            content = (0, 0, detector_width, detector_height)
        else:
            content = _box(
                content,
                detector_width,
                detector_height,
                "invalid_content_box",
            )
        object.__setattr__(self, "source_width", source_width)
        object.__setattr__(self, "source_height", source_height)
        object.__setattr__(self, "detector_width", detector_width)
        object.__setattr__(self, "detector_height", detector_height)
        object.__setattr__(self, "rotation_degrees", rotation % 360)
        object.__setattr__(self, "content_box_xyxy", content)

    @property
    def coefficients(self) -> AffineCoefficients:
        """Return the exact detector-boundary to encoded-source affine map."""

        content = self.content_box_xyxy
        if content is None:  # narrowed defensively for static type checking
            _fail("invalid_content_box")
        left, top, right, bottom = content
        content_width = right - left
        content_height = bottom - top
        width = self.source_width
        height = self.source_height
        zero = Fraction(0)
        if self.rotation_degrees == 0:
            x_scale = Fraction(width, content_width)
            y_scale = Fraction(height, content_height)
            values = (x_scale, zero, -left * x_scale, zero, y_scale, -top * y_scale)
        elif self.rotation_degrees == 90:
            x_scale = Fraction(width, content_height)
            y_scale = Fraction(height, content_width)
            values = (
                zero,
                x_scale,
                -top * x_scale,
                -y_scale,
                zero,
                Fraction(height) + left * y_scale,
            )
        elif self.rotation_degrees == 180:
            x_scale = Fraction(width, content_width)
            y_scale = Fraction(height, content_height)
            values = (
                -x_scale,
                zero,
                Fraction(width) + left * x_scale,
                zero,
                -y_scale,
                Fraction(height) + top * y_scale,
            )
        else:
            x_scale = Fraction(width, content_height)
            y_scale = Fraction(height, content_width)
            values = (
                zero,
                -x_scale,
                Fraction(width) + top * x_scale,
                y_scale,
                zero,
                -left * y_scale,
            )
        a, b, c, d, e, f = (_rational(value) for value in values)
        return AffineCoefficients(a, b, c, d, e, f)

    def map_box(self, box_xyxy: Box, *, measurement: str = "calibrated") -> Geometry:
        """Map one half-open integer detector box to a bounded source ``Geometry``."""

        producer_box = _box(
            box_xyxy,
            self.detector_width,
            self.detector_height,
            "invalid_detector_box",
        )
        selected_measurement = _measurement(measurement)
        coefficients = self.coefficients
        mapped = _source_box(
            producer_box,
            coefficients,
            self.source_width,
            self.source_height,
        )
        return Geometry(
            self.source_width,
            self.source_height,
            mapped,
            selected_measurement,
            transform_kind="affine_rational",
            producer_space=ProducerSpace(
                self.detector_width,
                self.detector_height,
                producer_box,
            ),
            coefficients=coefficients,
        )


def source_geometry(
    source_width: int,
    source_height: int,
    box_xyxy: Box,
    *,
    measurement: str = "measured",
) -> Geometry:
    """Construct identity geometry for a manually specified source-pixel region."""

    width = _dimension(source_width)
    height = _dimension(source_height)
    box = _box(box_xyxy, width, height, "invalid_source_box")
    return Geometry(width, height, box, _measurement(measurement))


@dataclass(frozen=True, slots=True)
class Rgb24Crop:
    """An immutable packed RGB24 crop whose representation never exposes pixels."""

    width: int
    height: int
    pixels: bytes = field(repr=False)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        width = _dimension(self.width)
        height = _dimension(self.height)
        if type(self.pixels) is not bytes or len(self.pixels) != width * height * 3:
            _fail("invalid_crop_bytes")
        object.__setattr__(self, "sha256", hashlib.sha256(self.pixels).hexdigest())

    def artifact(self) -> Artifact:
        """Describe the crop for the version-1 EvidenceStore contract."""

        return Artifact(self.sha256, str(len(self.pixels)))


def extract_rgb24_crop(
    frame_rgb24: bytes,
    source_width: int,
    source_height: int,
    geometry: Geometry,
) -> Rgb24Crop:
    """Copy a source-space half-open box from a packed RGB24 frame byte-for-byte."""

    width = _dimension(source_width)
    height = _dimension(source_height)
    if type(frame_rgb24) is not bytes or len(frame_rgb24) != width * height * 3:
        _fail("invalid_frame_bytes")
    if not isinstance(geometry, Geometry):
        _fail("invalid_geometry")
    if geometry.source_width != width or geometry.source_height != height:
        _fail("geometry_source_mismatch")
    x_min, y_min, x_max, y_max = geometry.box_xyxy
    source_stride = width * 3
    row_start = x_min * 3
    row_end = x_max * 3
    view = memoryview(frame_rgb24)
    pixels = b"".join(
        view[y * source_stride + row_start : y * source_stride + row_end]
        for y in range(y_min, y_max)
    )
    return Rgb24Crop(x_max - x_min, y_max - y_min, pixels)


def _destination_parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        _fail("invalid_destination")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        _fail("invalid_destination")
    if not encoded or len(encoded) > _MAX_DESTINATION_BYTES or "\\" in value:
        _fail("invalid_destination")
    parts = value.split("/")
    if len(parts) > _MAX_DESTINATION_DEPTH or any(
        part in {"", ".", ".."} or not _DESTINATION_COMPONENT.fullmatch(part) for part in parts
    ):
        _fail("invalid_destination")
    return tuple(parts)


def _root_descriptor(root: Path) -> int:
    if not isinstance(root, Path) or not root.is_absolute():
        _fail("invalid_artifact_root")
    try:
        metadata = root.lstat()
    except OSError:
        _fail("invalid_artifact_root")
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        _fail("invalid_artifact_root")
    required_attributes = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "geteuid")
    if any(not hasattr(os, name) for name in required_attributes):
        _fail("unsupported_destination_platform")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(root, flags)
        opened = os.fstat(descriptor)
    except OSError:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        _fail("invalid_artifact_root")
    if (
        (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        or not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) & 0o077
    ):
        os.close(descriptor)
        _fail("invalid_artifact_root")
    return descriptor


def _trusted_destination_directory(descriptor: int) -> bool:
    try:
        metadata = os.fstat(descriptor)
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) & 0o022 == 0
    )


def _inode(descriptor: int) -> tuple[int, int]:
    try:
        metadata = os.fstat(descriptor)
    except OSError:
        _fail("destination_unavailable")
    if not stat.S_ISREG(metadata.st_mode):
        _fail("destination_unavailable")
    return (metadata.st_dev, metadata.st_ino)


def _unlink_created(
    name: str,
    parent_descriptor: int,
    created_inode: tuple[int, int] | None,
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISREG(current.st_mode) and (
            created_inode is None or (current.st_dev, current.st_ino) == created_inode
        ):
            os.unlink(name, dir_fd=parent_descriptor)
    except OSError:
        pass


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    offset = 0
    while offset < len(view):
        written = os.write(descriptor, view[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def write_rgb24_crop(
    crop: Rgb24Crop,
    *,
    artifact_root: Path,
    relative_destination: str,
) -> Artifact:
    """Write one crop beneath a private root without following links or overwriting.

    This small sink exists for crop validation and bounded hand-off. The accepted
    EvidenceStore remains responsible for its later CAS layout and commit protocol.
    The root must be private and owned by the effective UID; opened parents must
    share that owner and deny group/other writes. Same-principal mutation is a
    trusted caller concern and must be serialized for the duration of this call.
    """

    if not isinstance(crop, Rgb24Crop):
        _fail("invalid_crop")
    artifact = crop.artifact()
    parts = _destination_parts(relative_destination)
    root_descriptor = _root_descriptor(artifact_root)
    descriptors = [root_descriptor]
    parent_descriptor = root_descriptor
    file_descriptor: int | None = None
    created = False
    created_inode: tuple[int, int] | None = None
    completed = False
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        try:
            for component in parts[:-1]:
                candidate = os.open(
                    component,
                    directory_flags,
                    dir_fd=parent_descriptor,
                )
                if not _trusted_destination_directory(candidate):
                    os.close(candidate)
                    _fail("destination_unavailable")
                parent_descriptor = candidate
                descriptors.append(candidate)
            file_descriptor = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_descriptor,
            )
            created = True
            created_inode = _inode(file_descriptor)
        except OSError:
            _fail("destination_unavailable")
        try:
            _write_all(file_descriptor, crop.pixels)
            os.fchmod(file_descriptor, 0o400)
            os.fsync(file_descriptor)
            os.close(file_descriptor)
            file_descriptor = None
        except OSError:
            _fail("write_failed")
        completed = True
    finally:
        if file_descriptor is not None:
            with suppress(OSError):
                os.close(file_descriptor)
        if created and not completed:
            _unlink_created(parts[-1], parent_descriptor, created_inode)
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)
    return artifact
