# SPDX-License-Identifier: Apache-2.0
"""Bounded offline vehicle detection through the accepted isolated runtime."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn, Protocol, cast, runtime_checkable

from visualworld.geometry import DetectorTransform
from visualworld.ingestion import FrameRef, MediaTime, Producer, Source, TimeBase
from visualworld.media import (
    MediaRuntime,
    _open_source,
    _sealed_snapshot,
)
from visualworld.media import (
    _capability_check as _verify_media_boundary,
)
from visualworld.perception import Observation
from visualworld.ports import (
    MAX_PERCEPTION_OBSERVATIONS,
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    DetectionResult,
    PerceptionResultState,
    PortCall,
    PortError,
    PortErrorCode,
    PortKind,
    _perception_frames,
)

DETECTOR_INPUT_WIDTH = 384
DETECTOR_INPUT_HEIGHT = 384
DETECTOR_CONFIDENCE_FLOOR_MILLIONTHS = 950_000
DETECTOR_RUNTIME_ID = "visualworld-perception-openvino-2026.3.1-vehicle-0201-v1"
DETECTOR_RUNTIME_CLOSURE_SHA256 = "5859db2175bcf154586052d891f7e4f06cfb6b38cea5c62ac625c7d7aa961530"
DETECTOR_MODEL_XML_SHA256 = "ae39ec7c4cc5c1ab5ef3db71c8fa307500f07a87f17d95bdb0d1c84762751d1a"
DETECTOR_MODEL_BIN_SHA256 = "612df843314c179460e754316d67e6eedd0e778f96ad1129fc0c34c8e935cb0b"
DETECTOR_WORKER_SHA256 = "6188e2960985bf983f829222bf0f4c8d174275077b46605e7b451c44780c66aa"
DETECTOR_MANIFEST_SHA256 = "6a3af4c3f82d9fc08a45971ad27562c583c02daa1e26df0811d8fa7588e5a3f5"
MEDIA_RUNTIME_ID = "visualworld-pyav-18.1.0-ffmpeg-9.0.1-v2"
MEDIA_MANIFEST_SHA256 = "58cf6f64280888ecc01c647044c38b9b56f197389b6bfc7ead7fbbe93a52ba32"
MEDIA_TREE_SHA256 = "7015262cd5dfdfd976d6ee092f0f93541597ea35e0331c3abb9ce6d4c54daeaf"

_EXPECTED_PYAV = "18.1.0"
_EXPECTED_OPENVINO = "2026.3.1-22476-759c5a6ab8c-releases/2026/3"
_EXPECTED_NUMPY = "2.5.3"
_EXPECTED_TELEMETRY = "2025.2.0"
_PERCEPTION_MANIFEST_NAME = "perception-runtime-manifest.json"
_PERCEPTION_RECEIPT_NAME = "visualworld-perception-runtime.json"
_PERCEPTION_WORKER = Path("worker/perception_worker.py")
_SYSTEMD_RUN = Path("/usr/bin/systemd-run")
_SYSTEMCTL = Path("/usr/bin/systemctl")
_BWRAP = Path("/usr/bin/bwrap")
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}\Z")
_MAX_U32 = 2**32 - 1
_MAX_U64 = 2**64 - 1
_MAX_I31 = 2**31 - 1
_MAX_RUNTIME_FILE_BYTES = 128 * 1024 * 1024
_TRUSTED_RUNTIME_UID = 0
_PYTHON_TREE_SHA256 = "7a39aea7cdb142c0c39fdbde871aded02c6c35549a76ca30d30a07eda0125b46"
_PYTHON_EXECUTABLE_SHA256 = "8a9082ea4d03f7bed8b8802934fb4f7c13ebcc182cf8e939946f52058968175f"
_SITE_PACKAGES_TREE_SHA256 = "345dfb0ec081283a609389cdd48090db8d74245ed32ade3574d4872a5801e253"
_SITE_PACKAGES_LOGICAL_BYTES = 235_889_859
_MODEL_TREE_SHA256 = "2ea6492e581586920ee6c41aa8c16b694637984331369f8b37657ba9e8da19b2"
_ARTIFACT_SHA256 = {
    "cpython": "8af9a8214c71b2dd698005e39fab87aad02a994330508857da4e6d1ba7e6ddb6",
    "numpy": "a5fa86b80fd24bcd1aff83ad23be44ea323de3f787be8f8b15d4a65621e25321",
    "openvino": "bb39ba741cea93277cc6c80cf7f70d1c19dea9a0f2a37f07543e0b4a7e00e0c4",
    "openvino-telemetry": ("bcb667e83a44f202ecf4cfa49281715c6d7e21499daec04ff853b7f964833599"),
    "vehicle-detection-0201-bin": DETECTOR_MODEL_BIN_SHA256,
    "vehicle-detection-0201-xml": DETECTOR_MODEL_XML_SHA256,
}
_CONFIGURATION = {
    "category": "vehicle",
    "confidence_floor_millionths": DETECTOR_CONFIDENCE_FLOOR_MILLIONTHS,
    "device": "CPU",
    "input": {
        "color_conversion": "RGB-to-BGR",
        "height": DETECTOR_INPUT_HEIGHT,
        "input_layout": "NHWC-u8",
        "model_layout": "NCHW",
        "resize": "OpenVINO RESIZE_LINEAR",
        "width": DETECTOR_INPUT_WIDTH,
    },
    "media_manifest_sha256": MEDIA_MANIFEST_SHA256,
    "media_runtime_id": MEDIA_RUNTIME_ID,
    "media_tree_sha256": MEDIA_TREE_SHA256,
    "model_bin_sha256": DETECTOR_MODEL_BIN_SHA256,
    "model_xml_sha256": DETECTOR_MODEL_XML_SHA256,
    "num_streams": 1,
    "performance_hint": "LATENCY",
    "runtime_closure_sha256": DETECTOR_RUNTIME_CLOSURE_SHA256,
    "runtime_id": DETECTOR_RUNTIME_ID,
    "schema": "visualworld.openvino-vehicle-detector-configuration",
    "schema_version": 1,
    "threads": 4,
    "worker_sha256": DETECTOR_WORKER_SHA256,
}
DETECTOR_CONFIGURATION_SHA256 = hashlib.sha256(
    json.dumps(_CONFIGURATION, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
DETECTOR_PRODUCER = Producer(
    "visualworld.openvino-vehicle-detector",
    "1",
    DETECTOR_CONFIGURATION_SHA256,
)


def _error(code: PortErrorCode, operation: str = "detect") -> PortError:
    return PortError(code, PortKind.DETECTOR, operation)


def _fail(code: PortErrorCode, operation: str = "detect") -> NoReturn:
    raise _error(code, operation) from None


def _plain_digest(value: object) -> str:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        _fail(PortErrorCode.DECODE_FAILED)
    return value


def _plain_token(value: object) -> str:
    if type(value) is not str or not _TOKEN_RE.fullmatch(value):
        _fail(PortErrorCode.DECODE_FAILED)
    return value


def _bounded_integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(PortErrorCode.DECODE_FAILED)
    return value


def _decimal(value: object, maximum: int = _MAX_U64) -> str:
    if (
        type(value) is not str
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
        or len(value) > 20
        or int(value) > maximum
    ):
        _fail(PortErrorCode.DECODE_FAILED)
    return value


def _signed_decimal(value: object) -> str:
    if type(value) is not str or not value.isascii() or not 1 <= len(value) <= 20:
        _fail(PortErrorCode.DECODE_FAILED)
    unsigned = value[1:] if value.startswith("-") else value
    if (
        not unsigned.isdecimal()
        or (len(unsigned) > 1 and unsigned.startswith("0"))
        or value == "-0"
        or not -(2**63) <= int(value) <= 2**63 - 1
    ):
        _fail(PortErrorCode.DECODE_FAILED)
    return value


def _mapping(value: object, fields: frozenset[str]) -> dict[str, object]:
    if (
        type(value) is not dict
        or set(value) != fields
        or not all(type(key) is str for key in value)
    ):
        _fail(PortErrorCode.DECODE_FAILED)
    return cast(dict[str, object], value)


def _validate_time(value: MediaTime) -> None:
    if (
        type(value) is not MediaTime
        or type(value.time_base) is not TimeBase
        or type(value.value) is not str
        or type(value.time_base.numerator) is not str
        or type(value.time_base.denominator) is not str
    ):
        _fail(PortErrorCode.DECODE_FAILED)
    invalid = False
    try:
        TimeBase.__post_init__(value.time_base)
        MediaTime.__post_init__(value)
    except (TypeError, ValueError):
        invalid = True
    if invalid:
        _fail(PortErrorCode.DECODE_FAILED)


@dataclass(frozen=True, slots=True)
class DetectionProvenance:
    """Pixel- and path-free binding for one detector result."""

    source_sha256: str
    source_bytes: int
    configuration_sha256: str = DETECTOR_CONFIGURATION_SHA256
    confidence_floor_millionths: int = DETECTOR_CONFIDENCE_FLOOR_MILLIONTHS
    model_xml_sha256: str = DETECTOR_MODEL_XML_SHA256
    model_bin_sha256: str = DETECTOR_MODEL_BIN_SHA256
    runtime_id: str = DETECTOR_RUNTIME_ID
    runtime_closure_sha256: str = DETECTOR_RUNTIME_CLOSURE_SHA256
    worker_sha256: str = DETECTOR_WORKER_SHA256
    perception_manifest_sha256: str = DETECTOR_MANIFEST_SHA256
    media_runtime_id: str = MEDIA_RUNTIME_ID
    media_manifest_sha256: str = MEDIA_MANIFEST_SHA256
    media_tree_sha256: str = MEDIA_TREE_SHA256

    def __post_init__(self) -> None:
        if (
            type(self.source_sha256) is not str
            or not _DIGEST_RE.fullmatch(self.source_sha256)
            or type(self.source_bytes) is not int
            or not 1 <= self.source_bytes <= 2**63 - 1
            or self.configuration_sha256 != DETECTOR_CONFIGURATION_SHA256
            or self.confidence_floor_millionths != DETECTOR_CONFIDENCE_FLOOR_MILLIONTHS
            or self.model_xml_sha256 != DETECTOR_MODEL_XML_SHA256
            or self.model_bin_sha256 != DETECTOR_MODEL_BIN_SHA256
            or self.runtime_id != DETECTOR_RUNTIME_ID
            or self.runtime_closure_sha256 != DETECTOR_RUNTIME_CLOSURE_SHA256
            or self.worker_sha256 != DETECTOR_WORKER_SHA256
            or self.perception_manifest_sha256 != DETECTOR_MANIFEST_SHA256
            or self.media_runtime_id != MEDIA_RUNTIME_ID
            or self.media_manifest_sha256 != MEDIA_MANIFEST_SHA256
            or self.media_tree_sha256 != MEDIA_TREE_SHA256
        ):
            raise ValueError("invalid detector provenance")


@dataclass(frozen=True, slots=True)
class WorkerDetection:
    box_xyxy: tuple[int, int, int, int]
    confidence_millionths: int

    def __post_init__(self) -> None:
        if type(self.box_xyxy) is not tuple or len(self.box_xyxy) != 4:
            raise ValueError("invalid worker detection")
        if any(type(value) is not int for value in self.box_xyxy):
            raise ValueError("invalid worker detection")
        left, top, right, bottom = self.box_xyxy
        if not (
            0 <= left < right <= DETECTOR_INPUT_WIDTH
            and 0 <= top < bottom <= DETECTOR_INPUT_HEIGHT
            and type(self.confidence_millionths) is int
            and DETECTOR_CONFIDENCE_FLOOR_MILLIONTHS <= self.confidence_millionths <= 1_000_000
        ):
            raise ValueError("invalid worker detection")


@dataclass(frozen=True, slots=True)
class WorkerFrame:
    decode_index: str
    pts: MediaTime
    duration: MediaTime | None
    key_frame: bool
    detections: tuple[WorkerDetection, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.decode_index) is not str
            or not self.decode_index.isascii()
            or not self.decode_index.isdecimal()
            or (len(self.decode_index) > 1 and self.decode_index.startswith("0"))
            or len(self.decode_index) > 20
            or int(self.decode_index) > _MAX_U64
            or type(self.key_frame) is not bool
            or type(self.detections) is not tuple
            or len(self.detections) > MAX_PORT_BATCH_ITEMS
            or not all(type(item) is WorkerDetection for item in self.detections)
        ):
            raise ValueError("invalid worker frame")
        _validate_time_for_record(self.pts)
        if self.duration is not None:
            _validate_time_for_record(self.duration)
            if self.duration.time_base != self.pts.time_base:
                raise ValueError("invalid worker frame")
        for item in self.detections:
            WorkerDetection.__post_init__(item)
        if len({(item.box_xyxy, item.confidence_millionths) for item in self.detections}) != len(
            self.detections
        ):
            raise ValueError("invalid worker frame")


def _validate_time_for_record(value: MediaTime) -> None:
    if (
        type(value) is not MediaTime
        or type(value.time_base) is not TimeBase
        or type(value.value) is not str
        or type(value.basis) is not str
        or value.basis != "measured"
        or value.estimate_method is not None
        or value.estimate_producer is not None
    ):
        raise ValueError("invalid worker time")
    invalid = False
    try:
        TimeBase.__post_init__(value.time_base)
        MediaTime.__post_init__(value)
    except (TypeError, ValueError):
        invalid = True
    if invalid:
        raise ValueError("invalid worker time")


@dataclass(frozen=True, slots=True)
class PerceptionWorkerResult:
    provenance: DetectionProvenance
    stream_index: int
    width: int
    height: int
    rotation_degrees: int
    time_base: TimeBase
    frames: tuple[WorkerFrame, ...]

    def __post_init__(self) -> None:
        if type(self.provenance) is not DetectionProvenance:
            raise ValueError("invalid worker result")
        DetectionProvenance.__post_init__(self.provenance)
        if (
            type(self.stream_index) is not int
            or not 0 <= self.stream_index <= _MAX_I31
            or type(self.width) is not int
            or not 1 <= self.width <= _MAX_I31
            or type(self.height) is not int
            or not 1 <= self.height <= _MAX_I31
            or type(self.rotation_degrees) is not int
            or self.rotation_degrees % 90
            or type(self.time_base) is not TimeBase
            or type(self.frames) is not tuple
            or len(self.frames) > MAX_PORT_BATCH_ITEMS
            or not all(type(item) is WorkerFrame for item in self.frames)
        ):
            raise ValueError("invalid worker result")
        try:
            TimeBase.__post_init__(self.time_base)
        except (TypeError, ValueError):
            raise ValueError("invalid worker result") from None
        for item in self.frames:
            WorkerFrame.__post_init__(item)
            if item.pts.time_base != self.time_base:
                raise ValueError("invalid worker result")
        if len({item.decode_index for item in self.frames}) != len(self.frames):
            raise ValueError("invalid worker result")
        if sum(len(item.detections) for item in self.frames) > MAX_PERCEPTION_OBSERVATIONS:
            raise ValueError("invalid worker result")


@runtime_checkable
class PerceptionWorker(Protocol):
    @property
    def supported(self) -> bool: ...

    def infer(self, source: Source, frames: tuple[FrameRef, ...]) -> PerceptionWorkerResult: ...


class FixturePerceptionWorker:
    """Deterministic pixel-free worker seam for ordinary cross-platform CI."""

    def __init__(self, result: PerceptionWorkerResult) -> None:
        if type(result) is not PerceptionWorkerResult:
            raise ValueError("result must use PerceptionWorkerResult")
        PerceptionWorkerResult.__post_init__(result)
        self._result = result
        self._calls = 0

    @property
    def supported(self) -> bool:
        return True

    @property
    def calls(self) -> int:
        return self._calls

    def infer(self, source: Source, frames: tuple[FrameRef, ...]) -> PerceptionWorkerResult:
        _perception_frames(source, frames, PortKind.DETECTOR, "detect")
        self._calls += 1
        return self._result


@dataclass(frozen=True, slots=True)
class PerceptionLimits:
    max_source_bytes: int = 64 * 1024 * 1024
    max_duration_seconds: int = 60 * 60
    max_width: int = 4096
    max_height: int = 4096
    max_pixels: int = 8_388_608
    max_frames: int = 300
    max_decoded_bytes: int = 384 * 1024 * 1024
    max_detections_per_frame: int = MAX_PORT_BATCH_ITEMS
    max_stdout_bytes: int = 16 * 1024 * 1024
    max_stderr_bytes: int = 1024 * 1024
    wall_timeout_ms: int = 300_000
    memory_bytes: int = 2 * 1024 * 1024 * 1024
    task_count: int = 1024

    def __post_init__(self) -> None:
        values = tuple(getattr(self, field) for field in self.__dataclass_fields__)
        if not all(type(value) is int and value > 0 for value in values):
            raise ValueError("perception limits must be positive integers")
        if (
            self.max_source_bytes > 2**30
            or self.max_duration_seconds > 24 * 60 * 60
            or self.max_width > _MAX_I31
            or self.max_height > _MAX_I31
            or self.max_pixels > 2**34
            or self.max_frames > 100_000
            or self.max_decoded_bytes > 2**40
            or self.max_detections_per_frame > MAX_PORT_BATCH_ITEMS
            or self.max_stdout_bytes > 16 * 1024 * 1024
            or self.max_stderr_bytes > 1024 * 1024
            or self.wall_timeout_ms > 300_000
            or self.memory_bytes > 2 * 1024 * 1024 * 1024
            or self.task_count > 1024
        ):
            raise ValueError("perception limit exceeds the approved bound")


@dataclass(frozen=True, slots=True)
class PerceptionRuntime:
    root: Path
    media: MediaRuntime

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or type(self.media) is not MediaRuntime:
            raise ValueError("runtime paths must use the approved records")
        object.__setattr__(self, "root", Path(os.path.abspath(os.fspath(self.root))))


def _read_runtime_file(path: Path, maximum: int = _MAX_RUNTIME_FILE_BYTES) -> bytes:
    descriptor = -1
    raw = b""
    read_failed = False
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != _TRUSTED_RUNTIME_UID
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o222
            or not 0 <= metadata.st_size <= maximum
        ):
            _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
        remaining = maximum + 1
        chunks: list[bytes] = []
        while remaining > 0 and (chunk := os.read(descriptor, min(1024 * 1024, remaining))):
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
        raw = b"".join(chunks)
    except PortError:
        raise
    except OSError:
        read_failed = True
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
    if read_failed:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    return raw


def _runtime_link_target(root: Path, path: Path) -> bytes:
    invalid = False
    resolved_root = root
    resolved = path
    try:
        link_text = os.readlink(path)
        if not link_text or "\\" in link_text or PurePosixPath(link_text).is_absolute():
            _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except PortError:
        raise
    except (OSError, RuntimeError, ValueError):
        invalid = True
    if invalid:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    if not resolved.is_file():
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    return os.fsencode(resolved.relative_to(resolved_root).as_posix())


def _tree_sha256(root: Path, *, normalize_python_root: bool = False) -> str:
    digest = hashlib.sha256()
    invalid = False
    try:
        paths = sorted(root.rglob("*"))
        for path in paths:
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                target = _runtime_link_target(root, path)
                kind = b"link"
                raw = target
            elif stat.S_ISREG(metadata.st_mode):
                kind = b"file"
                raw = _read_runtime_file(path)
                if normalize_python_root and path.name.startswith("_sysconfigdata_"):
                    raw = raw.replace(os.fsencode(root), b"/__PYTHON_ROOT__")
            elif stat.S_ISDIR(metadata.st_mode):
                continue
            else:
                _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
            content_sha256 = hashlib.sha256(raw).hexdigest()
            digest.update(relative.encode() + b"\0" + kind + b"\0")
            digest.update(str(len(raw)).encode() + b"\0" + content_sha256.encode() + b"\n")
    except PortError:
        raise
    except (OSError, RuntimeError, ValueError):
        invalid = True
    if invalid:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    return digest.hexdigest()


def _logical_size(root: Path) -> int:
    total = 0
    invalid = False
    try:
        for path in root.rglob("*"):
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
    except OSError:
        invalid = True
    if invalid:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    return total


def _validate_frozen_runtime(root: Path) -> None:
    invalid = False
    try:
        paths = (root, *root.rglob("*"))
        for path in paths:
            metadata = path.lstat()
            if metadata.st_uid != _TRUSTED_RUNTIME_UID:
                _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
            if stat.S_ISLNK(metadata.st_mode):
                _runtime_link_target(root, path)
            elif stat.S_ISDIR(metadata.st_mode):
                if metadata.st_mode & 0o222 or metadata.st_mode & stat.S_IXOTH == 0:
                    _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1 or metadata.st_mode & 0o222:
                    _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
            else:
                _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    except PortError:
        raise
    except (OSError, RuntimeError, ValueError):
        invalid = True
    if invalid:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)


def _trusted_traversable_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == _TRUSTED_RUNTIME_UID
        and metadata.st_mode & 0o022 == 0
        and metadata.st_mode & stat.S_IXOTH != 0
    )


def _expected_receipt() -> dict[str, object]:
    return {
        "application_worker_sha256": DETECTOR_WORKER_SHA256,
        "artifacts": _ARTIFACT_SHA256,
        "complete": True,
        "manifest_sha256": DETECTOR_MANIFEST_SHA256,
        "model_tree_sha256": _MODEL_TREE_SHA256,
        "python_runtime_tree_sha256": _PYTHON_TREE_SHA256,
        "runtime_closure_sha256": DETECTOR_RUNTIME_CLOSURE_SHA256,
        "runtime_id": DETECTOR_RUNTIME_ID,
        "schema": "visualworld.perception-runtime-receipt",
        "schema_version": 1,
        "site_packages_logical_bytes": _SITE_PACKAGES_LOGICAL_BYTES,
        "site_packages_tree_sha256": _SITE_PACKAGES_TREE_SHA256,
    }


def _verify_perception_boundary(runtime: PerceptionRuntime) -> None:
    root = runtime.root
    if not all(_trusted_traversable_directory(path) for path in (root, *root.parents)):
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    entries: set[str] | None = None
    with suppress(OSError):
        entries = {path.name for path in root.iterdir()}
    if entries is None:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    if entries != {
        "model",
        "python",
        "site-packages",
        "worker",
        _PERCEPTION_MANIFEST_NAME,
        _PERCEPTION_RECEIPT_NAME,
    }:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    _validate_frozen_runtime(root)
    manifest = _read_runtime_file(root / _PERCEPTION_MANIFEST_NAME, 256 * 1024)
    if hashlib.sha256(manifest).hexdigest() != DETECTOR_MANIFEST_SHA256:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    expected_receipt = (
        json.dumps(_expected_receipt(), sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    ).encode()
    if _read_runtime_file(root / _PERCEPTION_RECEIPT_NAME, 256 * 1024) != expected_receipt:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    worker = _read_runtime_file(root / _PERCEPTION_WORKER, 1024 * 1024)
    if hashlib.sha256(worker).hexdigest() != DETECTOR_WORKER_SHA256:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    if (
        _tree_sha256(root / "python", normalize_python_root=True) != _PYTHON_TREE_SHA256
        or _tree_sha256(root / "site-packages") != _SITE_PACKAGES_TREE_SHA256
        or _logical_size(root / "site-packages") != _SITE_PACKAGES_LOGICAL_BYTES
        or _tree_sha256(root / "model") != _MODEL_TREE_SHA256
    ):
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    executable = _read_runtime_file(root / "python/bin/python3.13")
    if hashlib.sha256(executable).hexdigest() != _PYTHON_EXECUTABLE_SHA256:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class _WorkerRun:
    stdout: bytes
    stderr: bytes
    returncode: int
    wall_ms: int
    cpu_ms: int
    memory_peak_bytes: int


def _namespace_argv(
    runtime: PerceptionRuntime,
    limits: PerceptionLimits,
    decode_indices: tuple[int, ...],
) -> list[str]:
    arguments = [
        "/perception-runtime/python/bin/python3.13",
        "/perception-runtime/worker/perception_worker.py",
        "--decode-indices",
        ",".join(str(value) for value in decode_indices),
        "--max-source-bytes",
        str(limits.max_source_bytes),
        "--max-duration-seconds",
        str(limits.max_duration_seconds),
        "--max-width",
        str(limits.max_width),
        "--max-height",
        str(limits.max_height),
        "--max-pixels",
        str(limits.max_pixels),
        "--max-frames",
        str(limits.max_frames),
        "--max-decoded-bytes",
        str(limits.max_decoded_bytes),
        "--max-detections-per-frame",
        str(limits.max_detections_per_frame),
    ]
    return [
        os.fspath(_BWRAP),
        "--unshare-user",
        "--uid",
        "65534",
        "--gid",
        "65534",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "PATH",
        "/perception-runtime/python/bin",
        "--setenv",
        "HOME",
        "/nonexistent",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "PYTHONPATH",
        ("/perception-runtime/site-packages:/media-runtime/venv/lib/python3.13/site-packages"),
        "--setenv",
        "LD_LIBRARY_PATH",
        "/perception-runtime/site-packages/openvino/libs:/media-runtime/ffmpeg/lib",
        "--setenv",
        "OPENVINO_TELEMETRY_CONSENT",
        "NO",
        "--ro-bind",
        os.fspath(runtime.root),
        "/perception-runtime",
        "--ro-bind",
        os.fspath(runtime.media.root),
        "/media-runtime",
        "--dir",
        "/lib",
        "--ro-bind",
        "/usr/lib/x86_64-linux-gnu",
        "/lib/x86_64-linux-gnu",
        "--ro-bind",
        "/usr/lib64",
        "/lib64",
        "--dir",
        "/sys/devices/system/cpu",
        "--ro-bind",
        "/sys/devices/system/cpu",
        "/sys/devices/system/cpu",
        "--dir",
        "/sys/devices/system/node",
        "--ro-bind",
        "/sys/devices/system/node",
        "/sys/devices/system/node",
        "--dir",
        "/sys/kernel/mm",
        "--ro-bind",
        "/sys/kernel/mm",
        "/sys/kernel/mm",
        "--dir",
        "/etc",
        "--ro-bind",
        "/etc/ld.so.cache",
        "/etc/ld.so.cache",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        "33554432",
        "--tmpfs",
        "/tmp",
        "--remount-ro",
        "/",
        "--chdir",
        "/",
        *arguments,
    ]


def _systemd_argv(
    runtime: PerceptionRuntime,
    limits: PerceptionLimits,
    snapshot_fd: int,
    unit: str,
    decode_indices: tuple[int, ...],
) -> list[str]:
    properties = (
        "User=nobody",
        "Group=nogroup",
        "NoNewPrivileges=yes",
        "KillMode=control-group",
        f"MemoryMax={limits.memory_bytes}",
        "MemorySwapMax=0",
        f"TasksMax={limits.task_count}",
        "CPUQuota=400%",
        f"RuntimeMaxSec={max(1, (limits.wall_timeout_ms + 999) // 1000)}s",
        "LimitNOFILE=64",
        f"LimitNPROC={limits.task_count}",
        f"LimitFSIZE={limits.max_stdout_bytes}",
        "UMask=0077",
        f"OpenFile=/proc/{os.getpid()}/fd/{snapshot_fd}:visualworld-source:read-only",
    )
    return [
        os.fspath(_SYSTEMD_RUN),
        "--quiet",
        "--pipe",
        "--wait",
        "--service-type=exec",
        f"--unit={unit}",
        *(f"--property={item}" for item in properties),
        *_namespace_argv(runtime, limits, decode_indices),
    ]


def _read_number(path: Path, field: str | None = None) -> int:
    try:
        text = path.read_text(encoding="ascii")
    except OSError:
        return 0
    if field is None:
        return int(text.strip()) if text.strip().isdigit() else 0
    for line in text.splitlines():
        name, separator, value = line.partition(" ")
        if separator and name == field and value.isdigit():
            return int(value)
    return 0


def _kill_unit(unit: str, cgroup: Path, process: subprocess.Popen[bytes]) -> bool:
    signalled = False
    try:
        descriptor = os.open(cgroup / "cgroup.kill", os.O_WRONLY | os.O_CLOEXEC)
    except OSError:
        pass
    else:
        try:
            signalled = os.write(descriptor, b"1") == 1
        except OSError:
            pass
        finally:
            with suppress(OSError):
                os.close(descriptor)
    if not signalled:
        try:
            result = subprocess.run(
                [os.fspath(_SYSTEMCTL), "kill", "--kill-whom=all", "--signal=SIGKILL", unit],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        else:
            signalled = result.returncode == 0
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    return signalled


def _cgroup_processes(cgroup: Path) -> tuple[int, ...] | None:
    try:
        values = (cgroup / "cgroup.procs").read_text(encoding="ascii").split()
    except FileNotFoundError:
        return ()
    except OSError:
        return None
    if any(not value.isdecimal() or int(value) <= 0 for value in values):
        return None
    return tuple(int(value) for value in values)


def _unit_inactive(unit: str) -> bool:
    try:
        result = subprocess.run(
            [os.fspath(_SYSTEMCTL), "show", "--property=ActiveState", "--value", unit],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode in {0, 4} and result.stdout.strip() in {b"inactive", b"failed"}


def _wait_unit_stopped(unit: str, cgroup: Path, timeout_seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if _cgroup_processes(cgroup) == () and _unit_inactive(unit):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _ensure_unit_stopped(
    unit: str,
    cgroup: Path,
    process: subprocess.Popen[bytes],
) -> bool:
    if process.poll() is None or _cgroup_processes(cgroup) != ():
        _kill_unit(unit, cgroup, process)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _kill_unit(unit, cgroup, process)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return False
    return _wait_unit_stopped(unit, cgroup)


def _drain_worker(
    process: subprocess.Popen[bytes],
    unit: str,
    limits: PerceptionLimits,
    cancelled: threading.Event | None,
) -> _WorkerRun:
    if process.stdout is None or process.stderr is None:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    selector = selectors.DefaultSelector()
    streams = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    bounds = {stdout_fd: limits.max_stdout_bytes, stderr_fd: limits.max_stderr_bytes}
    for descriptor in streams:
        os.set_blocking(descriptor, False)
        selector.register(descriptor, selectors.EVENT_READ)
    started = time.monotonic()
    deadline = started + limits.wall_timeout_ms / 1000
    cgroup = _CGROUP_ROOT / "system.slice" / f"{unit}.service"
    cpu_peak = 0
    memory_peak = 0
    failure: PortErrorCode | None = None
    killed = False
    termination_deadline: float | None = None
    try:
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            cpu_peak = max(cpu_peak, _read_number(cgroup / "cpu.stat", "usage_usec"))
            memory_peak = max(
                memory_peak,
                _read_number(cgroup / "memory.peak"),
                _read_number(cgroup / "memory.current"),
            )
            if cancelled is not None and cancelled.is_set():
                failure = PortErrorCode.CANCELLED
            elif now >= deadline:
                failure = PortErrorCode.TIMEOUT
            if failure is not None and not killed:
                _kill_unit(f"{unit}.service", cgroup, process)
                killed = True
                termination_deadline = time.monotonic() + 2
            if termination_deadline is not None and now >= termination_deadline:
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
                buffer = streams[descriptor]
                if len(buffer) + len(chunk) > bounds[descriptor]:
                    failure = PortErrorCode.LIMIT_EXCEEDED
                    if not killed:
                        _kill_unit(f"{unit}.service", cgroup, process)
                        killed = True
                        termination_deadline = time.monotonic() + 2
                    continue
                buffer.extend(chunk)
            if failure is not None and process.poll() is not None and not selector.get_map():
                break
        try:
            returncode = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_unit(f"{unit}.service", cgroup, process)
            try:
                returncode = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
        run = _WorkerRun(
            bytes(streams[stdout_fd]),
            bytes(streams[stderr_fd]),
            returncode,
            max(0, int((time.monotonic() - started) * 1000)),
            cpu_peak // 1000,
            memory_peak,
        )
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    if failure is not None:
        if not _wait_unit_stopped(f"{unit}.service", cgroup):
            _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
        _fail(failure)
    return run


def _run_worker(
    runtime: PerceptionRuntime,
    limits: PerceptionLimits,
    snapshot_fd: int,
    decode_indices: tuple[int, ...],
    cancelled: threading.Event | None,
) -> _WorkerRun:
    unit = f"visualworld-perception-{os.getpid()}-{time.monotonic_ns()}"
    process: subprocess.Popen[bytes] | None = None
    with suppress(OSError, subprocess.SubprocessError):
        process = subprocess.Popen(
            _systemd_argv(runtime, limits, snapshot_fd, unit, decode_indices),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    if process is None:
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    cgroup = _CGROUP_ROOT / "system.slice" / f"{unit}.service"
    try:
        try:
            run = _drain_worker(process, unit, limits, cancelled)
        except BaseException:
            if not _ensure_unit_stopped(f"{unit}.service", cgroup, process):
                _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
            raise
        if not _ensure_unit_stopped(f"{unit}.service", cgroup, process):
            _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
        return run
    finally:
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [os.fspath(_SYSTEMCTL), "reset-failed", f"{unit}.service"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )


def _supported_platform() -> bool:
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


class IsolatedPerceptionWorker:
    """Real composite decode/inference worker with no unsafe fallback."""

    def __init__(
        self,
        source_root: Path,
        relative_path: str,
        runtime: PerceptionRuntime,
        *,
        limits: PerceptionLimits | None = None,
        cancelled: threading.Event | None = None,
    ) -> None:
        if not isinstance(source_root, Path) or type(relative_path) is not str:
            raise ValueError("source location must use bounded path values")
        if type(runtime) is not PerceptionRuntime:
            raise ValueError("runtime must use PerceptionRuntime")
        selected_limits = PerceptionLimits() if limits is None else limits
        if type(selected_limits) is not PerceptionLimits:
            raise ValueError("limits must use PerceptionLimits")
        PerceptionLimits.__post_init__(selected_limits)
        if cancelled is not None and type(cancelled) is not threading.Event:
            raise ValueError("cancelled must be a threading.Event")
        self._source_root = Path(os.path.abspath(os.fspath(source_root)))
        self._relative_path = relative_path
        self._runtime = runtime
        self._limits = selected_limits
        self._cancelled = cancelled

    @property
    def supported(self) -> bool:
        return _supported_platform()

    def infer(self, source: Source, frames: tuple[FrameRef, ...]) -> PerceptionWorkerResult:
        _perception_frames(source, frames, PortKind.DETECTOR, "detect")
        if not self.supported:
            _fail(PortErrorCode.UNSUPPORTED)
        if not frames:
            raise ValueError("the adapter does not invoke a worker for an empty batch")
        stream_indexes = {frame.stream_index for frame in frames}
        if len(stream_indexes) != 1:
            _fail(PortErrorCode.INVALID_REQUEST)
        requested = tuple(sorted(int(frame.decode_index) for frame in frames))
        if requested[-1] >= self._limits.max_frames:
            _fail(PortErrorCode.LIMIT_EXCEEDED)
        media_error: PortErrorCode | None = None
        try:
            _verify_media_boundary(self._runtime.media)
        except PortError as error:
            media_error = error.code
        if media_error is not None:
            _fail(media_error)
        _verify_perception_boundary(self._runtime)
        source_error: PortErrorCode | None = None
        source_fd = -1
        try:
            source_fd, _ = _open_source(
                self._source_root,
                self._relative_path,
                self._limits.max_source_bytes,
            )
        except PortError as error:
            source_error = error.code
        if source_error is not None:
            _fail(source_error)
        snapshot_error: PortErrorCode | None = None
        snapshot_fd = -1
        digest = ""
        source_bytes = 0
        try:
            try:
                snapshot_fd, digest, source_bytes = _sealed_snapshot(
                    source_fd,
                    self._limits.max_source_bytes,
                )
            except PortError as error:
                snapshot_error = error.code
        finally:
            os.close(source_fd)
        if snapshot_error is not None:
            _fail(snapshot_error)
        if digest != source.fingerprint.digest or str(source_bytes) != source.fingerprint.bytes:
            os.close(snapshot_fd)
            _fail(PortErrorCode.CONFLICT)
        try:
            run = _run_worker(
                self._runtime,
                self._limits,
                snapshot_fd,
                requested,
                self._cancelled,
            )
        finally:
            os.close(snapshot_fd)
        return _decode_worker_output(run, source, frames, digest, source_bytes, self._limits)


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(PortErrorCode.DECODE_FAILED)
        result[key] = value
    return result


def _time_base(value: object) -> TimeBase:
    item = _mapping(value, frozenset({"numerator", "denominator"}))
    numerator = _decimal(item["numerator"], _MAX_U32)
    denominator = _decimal(item["denominator"], _MAX_U32)
    if numerator == "0" or denominator == "0":
        _fail(PortErrorCode.DECODE_FAILED)
    failed = False
    try:
        result = TimeBase(numerator, denominator)
    except (TypeError, ValueError):
        failed = True
    if failed:
        _fail(PortErrorCode.DECODE_FAILED)
    return result


def _media_time(value: object) -> MediaTime:
    item = _mapping(value, frozenset({"value", "time_base"}))
    failed = False
    try:
        result = MediaTime(_signed_decimal(item["value"]), _time_base(item["time_base"]))
    except (TypeError, ValueError):
        failed = True
    if failed:
        _fail(PortErrorCode.DECODE_FAILED)
    return result


def _decode_worker_output(
    run: _WorkerRun,
    source: Source,
    frames: tuple[FrameRef, ...],
    digest: str,
    source_bytes: int,
    limits: PerceptionLimits,
) -> PerceptionWorkerResult:
    if run.returncode != 0:
        if run.returncode == 20:
            _fail(PortErrorCode.LIMIT_EXCEEDED)
        if run.returncode == 22:
            _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
        _fail(PortErrorCode.DECODE_FAILED)
    expected_stderr = b'{"schema_version":1,"status":"ok"}\n'
    if run.stderr != expected_stderr:
        _fail(PortErrorCode.DECODE_FAILED)
    if not run.stdout or len(run.stdout) > limits.max_stdout_bytes:
        _fail(PortErrorCode.LIMIT_EXCEEDED)
    parse_failed = False
    try:
        raw = json.loads(run.stdout, object_pairs_hook=_no_duplicate_object)
        canonical = (
            json.dumps(raw, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    except PortError:
        raise
    except (RecursionError, TypeError, UnicodeError, ValueError):
        parse_failed = True
    if parse_failed:
        _fail(PortErrorCode.DECODE_FAILED)
    if run.stdout != canonical:
        _fail(PortErrorCode.DECODE_FAILED)
    top = _mapping(
        raw,
        frozenset(
            {"frames", "isolation", "runtime", "schema_version", "source", "status", "stream"}
        ),
    )
    if type(top["schema_version"]) is not int or top["schema_version"] != 1:
        _fail(PortErrorCode.DECODE_FAILED)
    if top["status"] != "ok":
        _fail(PortErrorCode.DECODE_FAILED)
    output_source = _mapping(top["source"], frozenset({"bytes", "sha256"}))
    if (
        _plain_digest(output_source["sha256"]) != digest
        or _bounded_integer(output_source["bytes"], 1, limits.max_source_bytes) != source_bytes
        or digest != source.fingerprint.digest
        or str(source_bytes) != source.fingerprint.bytes
    ):
        _fail(PortErrorCode.DECODE_FAILED)
    runtime = _mapping(
        top["runtime"],
        frozenset(
            {
                "model_bin_sha256",
                "model_xml_sha256",
                "numpy",
                "openvino",
                "pyav",
                "runtime_id",
                "telemetry",
                "worker_sha256",
            }
        ),
    )
    expected_runtime = {
        "model_bin_sha256": DETECTOR_MODEL_BIN_SHA256,
        "model_xml_sha256": DETECTOR_MODEL_XML_SHA256,
        "numpy": _EXPECTED_NUMPY,
        "openvino": _EXPECTED_OPENVINO,
        "pyav": _EXPECTED_PYAV,
        "runtime_id": DETECTOR_RUNTIME_ID,
        "telemetry": _EXPECTED_TELEMETRY,
        "worker_sha256": DETECTOR_WORKER_SHA256,
    }
    if runtime != expected_runtime or any(
        _plain_token(value) != value for value in runtime.values()
    ):
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    isolation = _mapping(
        top["isolation"],
        frozenset({"landlock_denied", "network_denied", "no_new_privileges", "non_root"}),
    )
    if any(value is not True for value in isolation.values()):
        _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
    stream = _mapping(
        top["stream"],
        frozenset({"height", "rotation_degrees", "stream_index", "time_base", "width"}),
    )
    stream_index = _bounded_integer(stream["stream_index"], 0, _MAX_I31)
    width = _bounded_integer(stream["width"], 1, limits.max_width)
    height = _bounded_integer(stream["height"], 1, limits.max_height)
    rotation = _bounded_integer(stream["rotation_degrees"], 0, 359)
    if rotation % 90:
        _fail(PortErrorCode.DECODE_FAILED)
    time_base = _time_base(stream["time_base"])
    source_streams = {item.stream_index: item for item in source.streams}
    source_stream = source_streams.get(stream_index)
    requested_streams = {frame.stream_index for frame in frames}
    if (
        source_stream is None
        or requested_streams != {stream_index}
        or source_stream.width != width
        or source_stream.height != height
        or source_stream.rotation_degrees % 360 != rotation
        or source_stream.time_base != time_base
    ):
        _fail(PortErrorCode.DECODE_FAILED)
    expected_frames = {frame.decode_index: frame for frame in frames}
    values = top["frames"]
    if type(values) is not list or len(values) != len(frames) or len(values) > MAX_PORT_BATCH_ITEMS:
        _fail(PortErrorCode.DECODE_FAILED)
    selected: list[WorkerFrame] = []
    total_detections = 0
    for value in values:
        item = _mapping(
            value,
            frozenset({"decode_index", "detections", "duration", "key_frame", "pts"}),
        )
        decode_index = _decimal(item["decode_index"])
        expected = expected_frames.get(decode_index)
        if expected is None:
            _fail(PortErrorCode.DECODE_FAILED)
        pts = _media_time(item["pts"])
        duration = None if item["duration"] is None else _media_time(item["duration"])
        key_frame = item["key_frame"]
        if (
            type(key_frame) is not bool
            or pts != expected.pts
            or duration != expected.duration
            or (expected.key_frame is not None and key_frame != expected.key_frame)
        ):
            _fail(PortErrorCode.DECODE_FAILED)
        raw_detections = item["detections"]
        if (
            type(raw_detections) is not list
            or len(raw_detections) > limits.max_detections_per_frame
        ):
            _fail(PortErrorCode.LIMIT_EXCEEDED)
        detections: list[WorkerDetection] = []
        for raw_detection in raw_detections:
            detection = _mapping(
                raw_detection,
                frozenset({"box_xyxy", "confidence_millionths"}),
            )
            raw_box = detection["box_xyxy"]
            if type(raw_box) is not list or len(raw_box) != 4:
                _fail(PortErrorCode.DECODE_FAILED)
            box = tuple(_bounded_integer(coordinate, 0, 384) for coordinate in raw_box)
            confidence = _bounded_integer(
                detection["confidence_millionths"],
                DETECTOR_CONFIDENCE_FLOOR_MILLIONTHS,
                1_000_000,
            )
            invalid_detection = False
            try:
                parsed_detection = WorkerDetection(cast(tuple[int, int, int, int], box), confidence)
            except ValueError:
                invalid_detection = True
            if invalid_detection:
                _fail(PortErrorCode.DECODE_FAILED)
            detections.append(parsed_detection)
        total_detections += len(detections)
        if total_detections > MAX_PERCEPTION_OBSERVATIONS:
            _fail(PortErrorCode.LIMIT_EXCEEDED)
        invalid_frame = False
        try:
            parsed_frame = WorkerFrame(decode_index, pts, duration, key_frame, tuple(detections))
        except ValueError:
            invalid_frame = True
        if invalid_frame:
            _fail(PortErrorCode.DECODE_FAILED)
        selected.append(parsed_frame)
    expected_order = tuple(sorted(expected_frames, key=int))
    if tuple(item.decode_index for item in selected) != expected_order:
        _fail(PortErrorCode.DECODE_FAILED)
    provenance = DetectionProvenance(digest, source_bytes)
    invalid_result = False
    try:
        result = PerceptionWorkerResult(
            provenance,
            stream_index,
            width,
            height,
            rotation,
            time_base,
            tuple(selected),
        )
    except ValueError:
        invalid_result = True
    if invalid_result:
        _fail(PortErrorCode.DECODE_FAILED)
    return result


class OpenVinoVehicleDetector:
    """The v0.2 Detector implementation for the exact approved CPU closure."""

    def __init__(self, worker: PerceptionWorker) -> None:
        if not isinstance(worker, PerceptionWorker):
            raise ValueError("worker must implement PerceptionWorker")
        self._worker = worker
        self._calls: list[PortCall] = []
        self._call_lock = threading.Lock()
        self._descriptor = CapabilityDescriptor(
            PortKind.DETECTOR,
            DETECTOR_PRODUCER.name,
            DETECTOR_PRODUCER.version,
            deterministic=True,
            offline=True,
            max_batch_items=MAX_PORT_BATCH_ITEMS,
            max_payload_bytes=16 * 1024 * 1024,
        )

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    @property
    def producer(self) -> Producer:
        return DETECTOR_PRODUCER

    @property
    def calls(self) -> tuple[PortCall, ...]:
        with self._call_lock:
            return tuple(self._calls)

    def provenance(self, source: Source) -> DetectionProvenance:
        _perception_frames(source, (), PortKind.DETECTOR, "detect")
        provenance: DetectionProvenance | None = None
        try:
            source_bytes = int(source.fingerprint.bytes)
            provenance = DetectionProvenance(source.fingerprint.digest, source_bytes)
        except (TypeError, ValueError):
            pass
        if provenance is None:
            _fail(PortErrorCode.INVALID_REQUEST)
        return provenance

    def _record_call(self, item_count: int) -> None:
        with self._call_lock:
            self._calls.append(PortCall(PortKind.DETECTOR, "detect", item_count))

    def detect(self, source: Source, frames: tuple[FrameRef, ...]) -> DetectionResult:
        _perception_frames(source, frames, PortKind.DETECTOR, "detect")
        if not frames:
            result = DetectionResult(PerceptionResultState.COMPLETE)
            self._record_call(0)
            return result
        stream_indexes = {frame.stream_index for frame in frames}
        if len(stream_indexes) != 1:
            result = DetectionResult(
                PerceptionResultState.UNSUPPORTED,
                reason="multi_stream_batch_unsupported",
            )
            self._record_call(len(frames))
            return result
        supported_failed = False
        try:
            supported = self._worker.supported
        except Exception:
            supported_failed = True
        if supported_failed:
            _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
        if type(supported) is not bool:
            _fail(PortErrorCode.ISOLATION_UNAVAILABLE)
        if not supported:
            result = DetectionResult(
                PerceptionResultState.UNSUPPORTED,
                reason="platform_unsupported",
            )
            self._record_call(len(frames))
            return result
        worker_failed = False
        try:
            output = self._worker.infer(source, frames)
            if type(output) is not PerceptionWorkerResult:
                _fail(PortErrorCode.DECODE_FAILED)
            PerceptionWorkerResult.__post_init__(output)
        except PortError:
            raise
        except Exception:
            worker_failed = True
        if worker_failed:
            _fail(PortErrorCode.DECODE_FAILED)
        stream_index = next(iter(stream_indexes))
        stream_by_index = {stream.stream_index: stream for stream in source.streams}
        stream = stream_by_index[stream_index]
        if (
            output.provenance != self.provenance(source)
            or output.stream_index != stream.stream_index
            or output.width != stream.width
            or output.height != stream.height
            or output.rotation_degrees % 360 != stream.rotation_degrees % 360
            or output.time_base != stream.time_base
            or {item.decode_index for item in output.frames}
            != {frame.decode_index for frame in frames}
        ):
            _fail(PortErrorCode.DECODE_FAILED)
        output_by_index = {item.decode_index: item for item in output.frames}
        observations: list[Observation] = []
        for frame in frames:
            worker_frame = output_by_index[frame.decode_index]
            if (
                worker_frame.pts != frame.pts
                or worker_frame.duration != frame.duration
                or (frame.key_frame is not None and worker_frame.key_frame != frame.key_frame)
            ):
                _fail(PortErrorCode.DECODE_FAILED)
            transform = DetectorTransform(
                stream.width,
                stream.height,
                DETECTOR_INPUT_WIDTH,
                DETECTOR_INPUT_HEIGHT,
                stream.rotation_degrees,
            )
            for detection in worker_frame.detections:
                observation: Observation | None = None
                try:
                    geometry = transform.map_box(detection.box_xyxy, measurement="inferred")
                    observation = Observation.create(
                        source.source_id,
                        frame.frame_id,
                        frame.stream_index,
                        frame.pts,
                        geometry,
                        "vehicle",
                        detection.confidence_millionths,
                        DETECTOR_PRODUCER,
                    )
                except (TypeError, ValueError):
                    pass
                if observation is None:
                    _fail(PortErrorCode.DECODE_FAILED)
                observations.append(observation)
        if len(observations) > MAX_PERCEPTION_OBSERVATIONS:
            _fail(PortErrorCode.LIMIT_EXCEEDED)
        result = DetectionResult(PerceptionResultState.COMPLETE, tuple(observations))
        self._record_call(len(frames))
        return result


__all__ = [
    "DETECTOR_CONFIDENCE_FLOOR_MILLIONTHS",
    "DETECTOR_CONFIGURATION_SHA256",
    "DETECTOR_INPUT_HEIGHT",
    "DETECTOR_INPUT_WIDTH",
    "DETECTOR_MANIFEST_SHA256",
    "DETECTOR_MODEL_BIN_SHA256",
    "DETECTOR_MODEL_XML_SHA256",
    "DETECTOR_PRODUCER",
    "DETECTOR_RUNTIME_CLOSURE_SHA256",
    "DETECTOR_RUNTIME_ID",
    "DETECTOR_WORKER_SHA256",
    "DetectionProvenance",
    "FixturePerceptionWorker",
    "IsolatedPerceptionWorker",
    "OpenVinoVehicleDetector",
    "PerceptionLimits",
    "PerceptionRuntime",
    "PerceptionWorker",
    "PerceptionWorkerResult",
    "WorkerDetection",
    "WorkerFrame",
]
