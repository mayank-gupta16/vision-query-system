#!/usr/bin/env python3
"""Explicitly acquire and verify the locked v0.2 perception runtime.

This command is intentionally separate from application execution.  It uses only
the standard library, never resolves dependencies, and never downloads unless
the ``fetch`` subcommand is selected.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import fcntl
import hashlib
import http.client
import io
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import IO, Any, NoReturn, TextIO, cast

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "workers" / "perception-runtime-v1.json"
DEFAULT_APPLICATION_WORKER = ROOT / "workers" / "perception_worker.py"
DEFAULT_ORIGINAL_FRAME_MANIFEST = ROOT / "workers" / "original-frame-runtime-v1.json"
DEFAULT_ORIGINAL_FRAME_WORKER = ROOT / "workers" / "original_frame_worker.py"
INSTALL_MANIFEST_NAME = "perception-runtime-manifest.json"
INSTALL_RECEIPT_NAME = "visualworld-perception-runtime.json"
ORIGINAL_FRAME_INSTALL_MANIFEST_NAME = "original-frame-runtime-v1.json"
ORIGINAL_FRAME_INSTALL_RECEIPT_NAME = "visualworld-original-frame-overlay.json"
_APPROVED_CANONICAL_MANIFEST_SHA256 = (
    "7c658028266f92f94c388b24fc5f122162182362a58a9997c0255931926ba29d"
)
_APPROVED_ORIGINAL_FRAME_CANONICAL_MANIFEST_SHA256 = (
    "faf721af53b7e861772b7fd9f978fc225c96a51832334095a2860c57ccde2e9f"
)
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_MEMBER_BYTES = 128 * 1024 * 1024
_MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100_000
_DOWNLOAD_CHUNK = 1024 * 1024
_TRUSTED_RUNTIME_UID = 0
_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "files.pythonhosted.org",
        "github.com",
        "release-assets.githubusercontent.com",
        "storage.openvinotoolkit.org",
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "application_worker",
        "artifacts",
        "closure",
        "distribution",
        "inference",
        "media_runtime",
        "policy",
        "runtime_id",
        "schema",
        "schema_version",
        "support",
        "worker",
    }
)
_ARTIFACT_NAMES = (
    "cpython",
    "numpy",
    "openvino",
    "openvino-telemetry",
    "vehicle-detection-0201-xml",
    "vehicle-detection-0201-bin",
)
_WHEEL_NAMES = ("numpy", "openvino", "openvino-telemetry")
_MODEL_NAMES = ("vehicle-detection-0201-xml", "vehicle-detection-0201-bin")
_RESULT_SCHEMA = "visualworld.perception-provision-result"
_RESULT_SCHEMA_VERSION = 1


class ProvisioningError(RuntimeError):
    """A stable, path-redacted provisioning failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise ProvisioningError(code) from None


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
        with contextlib.suppress(OSError):
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
        raw = (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
    except (TypeError, UnicodeError, ValueError):
        _silence_stream(stream)
        return False
    return _write_text(raw, stream)


def _result(status: str, **fields: object) -> dict[str, object]:
    return {
        **fields,
        "schema": _RESULT_SCHEMA,
        "schema_version": _RESULT_SCHEMA_VERSION,
        "status": status,
    }


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        _fail("invalid_arguments")

    def _print_message(self, message: str, file: object | None = None) -> None:
        if message:
            _write_text(message, sys.stderr if file is None else cast(TextIO, file))


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("invalid_manifest")
        result[key] = value
    return result


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _pretty_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()


def _mapping(value: object, code: str = "invalid_manifest") -> dict[str, Any]:
    if type(value) is not dict:
        _fail(code)
    return cast(dict[str, Any], value)


def _sequence(value: object, code: str = "invalid_manifest") -> list[Any]:
    if type(value) is not list:
        _fail(code)
    return value


def _expect_fields(value: Mapping[str, object], fields: set[str] | frozenset[str]) -> None:
    if set(value) != fields:
        _fail("invalid_manifest")


def _sha256_text(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail("invalid_manifest")
    return value


def _bounded_text(value: object, *, maximum: int = 512) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        _fail("invalid_manifest")
    selected = value
    if any(ord(character) < 32 or ord(character) == 127 for character in selected):
        _fail("invalid_manifest")
    return selected


def _positive_integer(value: object, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        _fail("invalid_manifest")
    return value


def _safe_relative_path(value: object, *, single: bool = False) -> PurePosixPath:
    text = _bounded_text(value, maximum=1024)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in text
        or (single and len(path.parts) != 1)
    ):
        _fail("invalid_manifest")
    return path


def _validate_url(value: object, *, artifact: bool = True) -> str:
    text = _bounded_text(value, maximum=2048)
    parsed = urllib.parse.urlsplit(text)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS
        or parsed.fragment
        or not parsed.path
    ):
        _fail("invalid_manifest")
    if (
        artifact
        and parsed.hostname == "github.com"
        and "/releases/download/20260825/" not in parsed.path
    ):
        _fail("invalid_manifest")
    return text


def _validate_notices(value: object, *, may_be_empty: bool) -> list[dict[str, Any]]:
    notices = _sequence(value)
    if not notices and not may_be_empty:
        _fail("invalid_manifest")
    paths: set[str] = set()
    selected: list[dict[str, Any]] = []
    for raw_notice in notices:
        notice = _mapping(raw_notice)
        _expect_fields(notice, {"path", "sha256"})
        path = _safe_relative_path(notice["path"]).as_posix()
        if path in paths:
            _fail("invalid_manifest")
        paths.add(path)
        _sha256_text(notice["sha256"])
        selected.append(notice)
    return selected


def _validate_bundled_components(value: object) -> None:
    components = _sequence(value)
    names: set[str] = set()
    for raw_component in components:
        component = _mapping(raw_component)
        _expect_fields(component, {"license_expression", "name"})
        name = _bounded_text(component["name"], maximum=128)
        _bounded_text(component["license_expression"], maximum=256)
        if name in names:
            _fail("invalid_manifest")
        names.add(name)


def validate_manifest(value: object) -> dict[str, Any]:
    """Validate the exact approved manifest and return its typed mapping."""

    manifest = _mapping(value)
    _expect_fields(manifest, _TOP_LEVEL_FIELDS)
    if (
        manifest["schema"] != "visualworld.perception-runtime"
        or manifest["schema_version"] != 1
        or manifest["runtime_id"] != "visualworld-perception-openvino-2026.3.1-vehicle-0201-v1"
    ):
        _fail("invalid_manifest")

    application_worker = _mapping(manifest["application_worker"])
    _expect_fields(application_worker, {"install_path", "license_expression", "sha256"})
    if (
        _safe_relative_path(application_worker["install_path"]).as_posix()
        != "worker/perception_worker.py"
        or application_worker["license_expression"] != "Apache-2.0"
    ):
        _fail("invalid_manifest")
    _sha256_text(application_worker["sha256"])

    support = _mapping(manifest["support"])
    if support != {
        "architecture": "x86_64",
        "device": "CPU",
        "libc": "glibc",
        "minimum_libc_version": "2.28",
        "operating_system": "Linux",
        "python_abi": "cp313",
        "unsupported_elsewhere": True,
    }:
        _fail("invalid_manifest")
    distribution = _mapping(manifest["distribution"])
    if distribution != {
        "application_wheel": "denied",
        "container_or_installer": "denied",
        "model_and_runtime": "user-provisioned-only",
        "reason": (
            "composite notices, corresponding-source, patent, policy, and jurisdiction "
            "review incomplete"
        ),
        "release_sbom_membership": "excluded-unless-separately-approved",
    }:
        _fail("invalid_manifest")
    policy = _mapping(manifest["policy"])
    if policy != {
        "ambient_network": False,
        "ambient_shell": False,
        "arbitrary_paths": False,
        "download_during_ingest": False,
        "executable_model_serialization": False,
        "mutable_revisions": False,
        "remote_code": False,
        "telemetry": False,
        "telemetry_environment": {"OPENVINO_TELEMETRY_CONSENT": "NO"},
        "unsafe_fallback": False,
    }:
        _fail("invalid_manifest")

    inference = _mapping(manifest["inference"])
    if inference != {
        "color_conversion": "RGB-to-BGR",
        "confidence_floor_millionths": 950000,
        "device": "CPU",
        "input_height": 384,
        "input_layout": "NHWC-u8",
        "input_width": 384,
        "model_layout": "NCHW",
        "num_streams": 1,
        "output_coordinate_scale": 1000000,
        "output_coordinate_space": "normalized_millionths",
        "performance_hint": "LATENCY",
        "resize": "OpenVINO RESIZE_LINEAR",
        "threads": 4,
    }:
        _fail("invalid_manifest")
    media_runtime = _mapping(manifest["media_runtime"])
    if media_runtime != {
        "manifest_sha256": "58cf6f64280888ecc01c647044c38b9b56f197389b6bfc7ead7fbbe93a52ba32",
        "runtime_id": "visualworld-pyav-18.1.0-ffmpeg-9.0.1-v2",
        "tree_sha256": "7015262cd5dfdfd976d6ee092f0f93541597ea35e0331c3abb9ce6d4c54daeaf",
    }:
        _fail("invalid_manifest")
    closure = _mapping(manifest["closure"])
    _expect_fields(
        closure,
        {
            "evaluation_python_runtime_tree_sha256",
            "evaluation_runtime_closure_sha256",
            "model_tree_sha256",
            "python_executable_sha256",
            "python_runtime_tree_sha256",
            "runtime_closure_sha256",
            "site_packages_logical_bytes",
            "site_packages_tree_sha256",
        },
    )
    _positive_integer(closure["site_packages_logical_bytes"], _MAX_EXTRACTED_BYTES)
    for key in (
        "evaluation_python_runtime_tree_sha256",
        "evaluation_runtime_closure_sha256",
        "model_tree_sha256",
        "python_executable_sha256",
        "python_runtime_tree_sha256",
        "runtime_closure_sha256",
        "site_packages_tree_sha256",
    ):
        _sha256_text(closure[key])

    worker = _mapping(manifest["worker"])
    _expect_fields(
        worker,
        {
            "boundary",
            "clear_environment",
            "input",
            "isolation",
            "limits",
            "output",
            "whole_cgroup_cancel_and_cleanup",
        },
    )
    isolation = _mapping(worker["isolation"])
    limits = _mapping(worker["limits"])
    if (
        worker["boundary"] != "single-composite-decode-and-inference-worker"
        or worker["clear_environment"] is not True
        or worker["input"] != "sealed-inherited-read-only-video-fd"
        or worker["output"] != "bounded-canonical-json-pixel-free-observations"
        or worker["whole_cgroup_cancel_and_cleanup"] is not True
        or isolation
        != {
            "cgroup_v2": True,
            "landlock": True,
            "network_namespace": "unshared",
            "no_new_privileges": True,
            "non_root": True,
            "read_only_runtime": True,
            "seccomp": True,
            "user_mount_pid_ipc_uts_namespaces": "unshared",
        }
        or limits
        != {
            "cpu_quota_percent": 400,
            "max_output_bytes": 16777216,
            "max_rss_bytes": 2147483648,
            "max_tasks": 1024,
            "max_wall_time_ms": 300000,
            "stderr_bytes": 1048576,
            "stdout_bytes": 16777216,
        }
    ):
        _fail("invalid_manifest")

    artifacts = _sequence(manifest["artifacts"])
    if len(artifacts) != len(_ARTIFACT_NAMES):
        _fail("invalid_manifest")
    names: list[str] = []
    filenames: set[str] = set()
    urls: set[str] = set()
    for raw_artifact in artifacts:
        artifact = _mapping(raw_artifact)
        common = {
            "bundled_components",
            "distribution_status",
            "filename",
            "format",
            "kind",
            "license_expression",
            "name",
            "notice_status",
            "notices",
            "revision",
            "sha256",
            "size",
            "url",
            "version",
        }
        if artifact.get("name") in _MODEL_NAMES:
            common.add("terms_url")
        _expect_fields(artifact, common)
        name = _bounded_text(artifact["name"], maximum=128)
        filename = _safe_relative_path(artifact["filename"], single=True).as_posix()
        url = _validate_url(artifact["url"])
        if urllib.parse.unquote(PurePosixPath(urllib.parse.urlsplit(url).path).name) != filename:
            _fail("invalid_manifest")
        _bounded_text(artifact["revision"], maximum=256)
        _bounded_text(artifact["version"], maximum=64)
        _bounded_text(artifact["license_expression"], maximum=256)
        _bounded_text(artifact["format"], maximum=64)
        _bounded_text(artifact["kind"], maximum=64)
        _sha256_text(artifact["sha256"])
        _positive_integer(artifact["size"], _MAX_MEMBER_BYTES)
        if artifact["distribution_status"] != "user-provisioned-only":
            _fail("invalid_manifest")
        is_model = name in _MODEL_NAMES
        notices = _validate_notices(artifact["notices"], may_be_empty=is_model)
        if is_model:
            _validate_url(artifact["terms_url"], artifact=False)
            if artifact["notice_status"] != "external-license-evidence-only" or notices:
                _fail("invalid_manifest")
        elif not notices or artifact["notice_status"] not in {
            "complete-in-wheel-for-private-use",
            "incomplete-for-redistribution",
        }:
            _fail("invalid_manifest")
        _validate_bundled_components(artifact["bundled_components"])
        if name in names or filename in filenames or url in urls:
            _fail("invalid_manifest")
        names.append(name)
        filenames.add(filename)
        urls.add(url)
    if tuple(names) != _ARTIFACT_NAMES:
        _fail("invalid_manifest")
    artifact_by_name = {
        cast(str, artifact["name"]): artifact for artifact in cast(list[dict[str, Any]], artifacts)
    }
    wheel_hashes = {
        "numpy": artifact_by_name["numpy"]["sha256"],
        "openvino": artifact_by_name["openvino"]["sha256"],
        "openvino_telemetry": artifact_by_name["openvino-telemetry"]["sha256"],
    }
    python_artifact = artifact_by_name["cpython"]
    for tree_key, closure_key in (
        ("python_runtime_tree_sha256", "runtime_closure_sha256"),
        ("evaluation_python_runtime_tree_sha256", "evaluation_runtime_closure_sha256"),
    ):
        calculated = hashlib.sha256(
            _canonical_json(
                {
                    "python": {
                        "archive_sha256": python_artifact["sha256"],
                        "executable_sha256": closure["python_executable_sha256"],
                        "runtime_tree_sha256": closure[tree_key],
                        "version": python_artifact["version"],
                    },
                    "runtime_tree_sha256": closure["site_packages_tree_sha256"],
                    "wheels": wheel_hashes,
                }
            )
        ).hexdigest()
        if calculated != closure[closure_key]:
            _fail("invalid_manifest")
    canonical_sha256 = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    if canonical_sha256 != _APPROVED_CANONICAL_MANIFEST_SHA256:
        _fail("unapproved_manifest")
    return manifest


def load_manifest(path: Path = DEFAULT_MANIFEST) -> tuple[dict[str, Any], str]:
    raw = _read_regular(path, maximum=_MAX_MANIFEST_BYTES, code="invalid_manifest_file")
    try:
        value = json.loads(raw, object_pairs_hook=_no_duplicate_object)
    except (UnicodeError, json.JSONDecodeError, ProvisioningError):
        _fail("invalid_manifest")
    manifest = validate_manifest(value)
    if raw != _pretty_json(manifest):
        _fail("noncanonical_manifest")
    return manifest, hashlib.sha256(raw).hexdigest()


def validate_original_frame_manifest(value: object) -> dict[str, Any]:
    """Validate the exact first-party, offline original-frame overlay manifest."""

    manifest = _mapping(value)
    _expect_fields(
        manifest,
        {
            "application_worker",
            "ipc",
            "limits",
            "media_runtime",
            "policy",
            "runtime_id",
            "schema",
            "schema_version",
            "support",
            "worker",
        },
    )
    if (
        manifest["schema"] != "visualworld.original-frame-runtime"
        or manifest["schema_version"] != 1
        or manifest["runtime_id"] != "visualworld-original-frame-overlay-v1"
    ):
        _fail("invalid_manifest")
    application_worker = _mapping(manifest["application_worker"])
    _expect_fields(application_worker, {"install_path", "license_expression", "sha256"})
    if (
        _safe_relative_path(application_worker["install_path"]).as_posix()
        != "worker/original_frame_worker.py"
        or application_worker["license_expression"] != "Apache-2.0"
    ):
        _fail("invalid_manifest")
    _sha256_text(application_worker["sha256"])
    if manifest["support"] != {
        "architecture": "x86_64",
        "libc": "glibc",
        "minimum_libc_version": "2.28",
        "operating_system": "Linux",
        "python_abi": "cp313",
        "unsupported_elsewhere": True,
    }:
        _fail("invalid_manifest")
    if manifest["policy"] != {
        "ambient_network": False,
        "ambient_shell": False,
        "arbitrary_paths": False,
        "download_during_decode": False,
        "mutable_repo_imports": False,
        "remote_code": False,
        "unsafe_fallback": False,
    }:
        _fail("invalid_manifest")
    if manifest["limits"] != {
        "max_decoded_bytes": 402653184,
        "max_duration_seconds": 3600,
        "max_frame_bytes": 33554432,
        "max_frames": 300,
        "max_height": 4096,
        "max_output_bytes": 134217728,
        "max_pixels": 8388608,
        "max_request_bytes": 262144,
        "max_requested_frames": 64,
        "max_source_bytes": 67108864,
        "max_width": 4096,
    }:
        _fail("invalid_manifest")
    if manifest["media_runtime"] != {
        "ffmpeg_version": "9.0.1",
        "libavcodec_version": "63.1.101",
        "libavformat_version": "63.1.101",
        "manifest_path": "workers/visualworld-runtime.json",
        "manifest_sha256": ("58cf6f64280888ecc01c647044c38b9b56f197389b6bfc7ead7fbbe93a52ba32"),
        "pyav_version": "18.1.0",
        "runtime_id": "visualworld-pyav-18.1.0-ffmpeg-9.0.1-v2",
        "tree_sha256": "7015262cd5dfdfd976d6ee092f0f93541597ea35e0331c3abb9ce6d4c54daeaf",
        "worker_sha256": "6104c56592b5007a882f131e6defd42d31d56248d1337a3668f09978bbf340a2",
    }:
        _fail("invalid_manifest")
    ipc = _mapping(manifest["ipc"])
    _expect_fields(ipc, {"request", "response", "source", "stderr", "uncompressed_output"})
    if ipc != {
        "request": {
            "canonical_json": True,
            "maximum_bytes": 262144,
            "pixel_free": True,
            "schema": "visualworld.original_frame_request",
            "schema_version": 1,
            "transport": "stdin",
        },
        "response": {
            "canonical_json": True,
            "pixel_free": True,
            "schema": "visualworld.original_frame_result",
            "schema_version": 1,
            "transport": "stdout",
        },
        "source": {
            "descriptor": 3,
            "required_seals": [
                "F_SEAL_GROW",
                "F_SEAL_SEAL",
                "F_SEAL_SHRINK",
                "F_SEAL_WRITE",
            ],
            "transport": "sealed-memfd",
        },
        "stderr": {
            "canonical_json": True,
            "pixel_free": True,
            "static_status_only": True,
        },
        "uncompressed_output": {
            "descriptor": 4,
            "initial_seals": ["F_SEAL_GROW", "F_SEAL_SHRINK"],
            "layout": "request_order_contiguous_packed_rgb24_encoded_source",
            "required_final_seals": [
                "F_SEAL_GROW",
                "F_SEAL_SEAL",
                "F_SEAL_SHRINK",
                "F_SEAL_WRITE",
            ],
            "size": "exactly-pre-sized-from-request",
            "transport": "memfd",
        },
    }:
        _fail("invalid_manifest")
    if manifest["worker"] != {
        "boundary": "single-original-frame-decode-worker",
        "clear_environment": True,
        "isolation": {
            "cgroup_v2": True,
            "landlock": True,
            "network_namespace": "unshared",
            "no_new_privileges": True,
            "non_root": True,
            "read_only_overlay": True,
            "read_only_runtime": True,
            "seccomp": True,
            "user_mount_pid_ipc_uts_namespaces": "unshared",
        },
        "whole_cgroup_cancel_and_cleanup": True,
    }:
        _fail("invalid_manifest")
    canonical_sha256 = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    if canonical_sha256 != _APPROVED_ORIGINAL_FRAME_CANONICAL_MANIFEST_SHA256:
        _fail("unapproved_manifest")
    return manifest


def load_original_frame_manifest(
    path: Path = DEFAULT_ORIGINAL_FRAME_MANIFEST,
) -> tuple[dict[str, Any], str]:
    raw = _read_regular(path, maximum=_MAX_MANIFEST_BYTES, code="invalid_manifest_file")
    try:
        value = json.loads(raw, object_pairs_hook=_no_duplicate_object)
    except (UnicodeError, json.JSONDecodeError, ProvisioningError):
        _fail("invalid_manifest")
    manifest = validate_original_frame_manifest(value)
    if raw != _pretty_json(manifest):
        _fail("noncanonical_manifest")
    return manifest, hashlib.sha256(raw).hexdigest()


def _read_regular(
    path: Path,
    *,
    maximum: int,
    code: str,
    expected_size: int | None = None,
    single_link: bool = False,
) -> bytes:
    descriptor = -1
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
            or metadata.st_size > maximum
            or (expected_size is not None and metadata.st_size != expected_size)
            or (single_link and metadata.st_nlink != 1)
        ):
            _fail(code)
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(_DOWNLOAD_CHUNK, maximum + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                _fail(code)
        if expected_size is not None and total != expected_size:
            _fail(code)
        return b"".join(chunks)
    except (OSError, ProvisioningError):
        _fail(code)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _artifact_map(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        cast(str, artifact["name"]): artifact
        for artifact in cast(list[dict[str, Any]], manifest["artifacts"])
    }


def verify_artifact(path: Path, artifact: Mapping[str, object]) -> bytes:
    """Read and verify one exact regular artifact."""

    expected_size = cast(int, artifact["size"])
    raw = _read_regular(
        path,
        maximum=expected_size,
        expected_size=expected_size,
        code="artifact_invalid",
        single_link=True,
    )
    if _sha256_bytes(raw) != artifact["sha256"]:
        _fail("artifact_invalid")
    return raw


def _check_private_directory(path: Path, *, create: bool) -> None:
    if not path.is_absolute():
        _fail("unsafe_cache_root")
    _reject_symlink_ancestors(path.parent, code="unsafe_cache_root")
    try:
        if create and not path.exists():
            parent_metadata = path.parent.lstat()
            if (
                not stat.S_ISDIR(parent_metadata.st_mode)
                or parent_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(parent_metadata.st_mode) & 0o022
            ):
                _fail("unsafe_cache_root")
            path.mkdir(mode=0o700)
        metadata = path.lstat()
    except OSError:
        _fail("unsafe_cache_root")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        _fail("unsafe_cache_root")


def _reject_symlink_ancestors(path: Path, *, code: str) -> None:
    if not path.is_absolute():
        _fail(code)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError:
            _fail(code)
        mode = stat.S_IMODE(metadata.st_mode)
        trusted_sticky = metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or (bool(mode & 0o022) and not trusted_sticky)
        ):
            _fail(code)


@contextlib.contextmanager
def _cache_lock(cache_root: Path, *, create: bool) -> Iterator[None]:
    _check_private_directory(cache_root, create=create)
    descriptor = -1
    try:
        descriptor = os.open(
            cache_root / ".provision.lock",
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            _fail("unsafe_cache_root")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError:
        _fail("unsafe_cache_root")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _allowed_cache_names(manifest: Mapping[str, Any]) -> set[str]:
    return {
        cast(str, artifact["filename"])
        for artifact in cast(list[dict[str, Any]], manifest["artifacts"])
    } | {".provision.lock"}


def _verify_cache_unlocked(cache_root: Path, manifest: Mapping[str, Any]) -> None:
    allowed = _allowed_cache_names(manifest)
    try:
        actual = {path.name for path in cache_root.iterdir()}
    except OSError:
        _fail("cache_invalid")
    if actual - allowed:
        _fail("cache_invalid")
    for artifact in cast(list[dict[str, Any]], manifest["artifacts"]):
        verify_artifact(cache_root / cast(str, artifact["filename"]), artifact)


def verify_cache(cache_root: Path, manifest: Mapping[str, Any]) -> None:
    with _cache_lock(cache_root, create=False):
        _verify_cache_unlocked(cache_root, manifest)


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        parsed = urllib.parse.urlsplit(newurl)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
        ):
            _fail("download_denied")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_URL_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _AllowlistedRedirectHandler(),
)


def _download_artifact(cache_root: Path, artifact: Mapping[str, object]) -> None:
    final_path = cache_root / cast(str, artifact["filename"])
    prefix = f".{artifact['filename']}.partial-"
    for candidate in cache_root.iterdir():
        if not candidate.name.startswith(prefix):
            continue
        try:
            metadata = candidate.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                _fail("cache_invalid")
            candidate.unlink()
        except OSError:
            _fail("cache_invalid")
    if final_path.exists():
        verify_artifact(final_path, artifact)
        _fsync_directory(cache_root, code="download_failed")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=prefix, dir=cache_root)
    temporary = Path(temporary_name)
    os.fchmod(descriptor, 0o600)
    expected_size = cast(int, artifact["size"])
    digest = hashlib.sha256()
    total = 0
    try:
        request = urllib.request.Request(
            cast(str, artifact["url"]),
            headers={"User-Agent": "VisualWorld-runtime-provisioner/1"},
        )
        with _URL_OPENER.open(request, timeout=60) as response:
            final_url = response.geturl()
            parsed = urllib.parse.urlsplit(final_url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port is not None
                or parsed.fragment
            ):
                _fail("download_denied")
            while chunk := response.read(_DOWNLOAD_CHUNK):
                total += len(chunk)
                if total > expected_size:
                    _fail("artifact_invalid")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
        if total != expected_size or digest.hexdigest() != artifact["sha256"]:
            _fail("artifact_invalid")
        os.fsync(descriptor)
        owned_descriptor = descriptor
        descriptor = -1
        os.close(owned_descriptor)
        try:
            os.link(temporary, final_path, follow_symlinks=False)
            temporary.unlink()
        except FileExistsError:
            verify_artifact(final_path, artifact)
        _fsync_directory(cache_root, code="download_failed")
    except ProvisioningError:
        raise
    except (OSError, urllib.error.URLError):
        _fail("download_failed")
    finally:
        if descriptor >= 0:
            owned_descriptor = descriptor
            descriptor = -1
            with contextlib.suppress(OSError):
                os.close(owned_descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()


def fetch_artifacts(cache_root: Path, manifest: Mapping[str, Any]) -> None:
    """Explicitly fetch every approved artifact into a private cache."""

    with _cache_lock(cache_root, create=True):
        allowed = _allowed_cache_names(manifest)
        try:
            entries = list(cache_root.iterdir())
        except OSError:
            _fail("cache_invalid")
        for entry in entries:
            if entry.name in allowed:
                continue
            if not any(
                entry.name.startswith(f".{artifact['filename']}.partial-")
                for artifact in cast(list[dict[str, Any]], manifest["artifacts"])
            ):
                _fail("cache_invalid")
        for artifact in cast(list[dict[str, Any]], manifest["artifacts"]):
            _download_artifact(cache_root, artifact)
        _verify_cache_unlocked(cache_root, manifest)


def _safe_archive_path(value: str, *, code: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
    ):
        _fail(code)
    return path


def _safe_symlink_target(value: str) -> PurePosixPath:
    if not value or len(value) > 1024 or "\\" in value:
        _fail("python_archive_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", "."} or any(ord(character) < 32 or ord(character) == 127 for character in part)
        for part in path.parts
    ):
        _fail("python_archive_invalid")
    return path


def _write_exclusive(path: Path, raw: bytes, *, executable: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            0o700 if executable else 0o600,
        )
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, 0o555 if executable else 0o444)
        os.fsync(descriptor)
        owned_descriptor = descriptor
        descriptor = -1
        os.close(owned_descriptor)
    except OSError:
        _fail("install_failed")
    finally:
        if descriptor >= 0:
            owned_descriptor = descriptor
            descriptor = -1
            with contextlib.suppress(OSError):
                os.close(owned_descriptor)


def _resolved_inside(root: Path, path: Path, *, code: str = "installed_runtime_invalid") -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        _fail(code)
    if not resolved.is_file():
        _fail(code)
    return resolved


def _validate_internal_symlink(
    root: Path, path: Path, *, code: str = "installed_runtime_invalid"
) -> Path:
    try:
        link_text = os.readlink(path)
    except OSError:
        _fail(code)
    if not link_text or "\\" in link_text or PurePosixPath(link_text).is_absolute():
        _fail(code)
    return _resolved_inside(root, path, code=code)


def _tree_sha256(root: Path, *, normalize_python_root: bool = False) -> str:
    digest = hashlib.sha256()
    try:
        candidates = sorted(root.rglob("*"))
    except OSError:
        _fail("installed_runtime_invalid")
    for path in candidates:
        relative_text = path.relative_to(root).as_posix()
        try:
            metadata = path.lstat()
        except OSError:
            _fail("installed_runtime_invalid")
        if stat.S_ISLNK(metadata.st_mode):
            resolved = _resolved_inside(root, path)
            target = resolved.relative_to(root.resolve(strict=True)).as_posix().encode()
            kind = b"link"
            content_sha256 = hashlib.sha256(target).hexdigest()
            size = len(target)
        elif stat.S_ISREG(metadata.st_mode):
            kind = b"file"
            raw = _read_regular(
                path,
                maximum=_MAX_MEMBER_BYTES,
                code="installed_runtime_invalid",
            )
            if normalize_python_root and path.name.startswith("_sysconfigdata_"):
                raw = raw.replace(os.fsencode(root), b"/__PYTHON_ROOT__")
            content_sha256 = _sha256_bytes(raw)
            size = len(raw)
        elif stat.S_ISDIR(metadata.st_mode):
            continue
        else:
            _fail("installed_runtime_invalid")
        digest.update(relative_text.encode() + b"\0" + kind + b"\0")
        digest.update(str(size).encode() + b"\0" + content_sha256.encode() + b"\n")
    return digest.hexdigest()


def _extract_python(raw: bytes, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")  # noqa: SIM115
    except (tarfile.TarError, OSError):
        _fail("python_archive_invalid")
    with archive:
        members = archive.getmembers()
        if not members or len(members) > _MAX_ARCHIVE_MEMBERS:
            _fail("python_archive_invalid")
        seen: set[str] = set()
        files: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        links: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        total = 0
        for member in members:
            path = _safe_archive_path(member.name, code="python_archive_invalid")
            if path.parts[0] != "python" or path.as_posix() in seen:
                _fail("python_archive_invalid")
            seen.add(path.as_posix())
            if member.isdir():
                continue
            if member.isreg():
                if member.size < 0 or member.size > _MAX_MEMBER_BYTES:
                    _fail("python_archive_invalid")
                total += member.size
                if total > _MAX_EXTRACTED_BYTES:
                    _fail("python_archive_invalid")
                files.append((member, path))
            elif member.issym():
                links.append((member, path))
            else:
                _fail("python_archive_invalid")
        for member, path in files:
            stream = archive.extractfile(member)
            if stream is None:
                _fail("python_archive_invalid")
            content = stream.read(_MAX_MEMBER_BYTES + 1)
            if len(content) != member.size:
                _fail("python_archive_invalid")
            relative = PurePosixPath(*path.parts[1:])
            if not relative.parts:
                _fail("python_archive_invalid")
            _write_exclusive(
                destination.joinpath(*relative.parts),
                content,
                executable=bool(member.mode & 0o111),
            )
        root_parts = ("__root__",)
        for member, path in links:
            link = _safe_symlink_target(member.linkname)
            parent_parts = path.parts[:-1]
            stack = list(root_parts + parent_parts[1:])
            for part in link.parts:
                if part == "..":
                    if len(stack) <= 1:
                        _fail("python_archive_invalid")
                    stack.pop()
                elif part not in {"", "."}:
                    stack.append(part)
            target = destination.joinpath(*path.parts[1:])
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                target.symlink_to(member.linkname)
                _resolved_inside(destination, target)
            except OSError:
                _fail("python_archive_invalid")


def _zip_member(archive: zipfile.ZipFile, name: str, maximum: int) -> bytes:
    try:
        info = archive.getinfo(name)
        if info.file_size > maximum:
            _fail("wheel_invalid")
        raw = archive.read(info)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        _fail("wheel_invalid")
    if len(raw) != info.file_size:
        _fail("wheel_invalid")
    return raw


def _verify_wheel_record(archive: zipfile.ZipFile) -> None:
    names = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
    if len(names) != 1:
        _fail("wheel_invalid")
    raw = _zip_member(archive, names[0], _MAX_MANIFEST_BYTES)
    try:
        rows = list(csv.reader(io.StringIO(raw.decode(), newline="")))
    except (UnicodeError, csv.Error):
        _fail("wheel_invalid")
    files = sorted(name for name in archive.namelist() if not name.endswith("/"))
    if not rows or any(len(row) != 3 for row in rows):
        _fail("wheel_invalid")
    if sorted(row[0] for row in rows) != files or len(files) != len(set(files)):
        _fail("wheel_invalid")
    for path, encoded_digest, size_text in rows:
        content = _zip_member(archive, path, _MAX_MEMBER_BYTES)
        if path == names[0]:
            if encoded_digest or size_text:
                _fail("wheel_invalid")
            continue
        try:
            algorithm, encoded = encoded_digest.split("=", maxsplit=1)
            expected_digest = base64.urlsafe_b64decode(encoded + "===")
            expected_size = int(size_text)
        except (TypeError, ValueError):
            _fail("wheel_invalid")
        if (
            algorithm != "sha256"
            or type(expected_size) is not int
            or expected_size != len(content)
            or hashlib.sha256(content).digest() != expected_digest
        ):
            _fail("wheel_invalid")


def _extract_wheels(
    raw_wheels: Sequence[tuple[bytes, Mapping[str, object]]], destination: Path
) -> None:
    destination.mkdir(mode=0o700)
    extracted: set[str] = set()
    total = 0
    for raw, artifact in raw_wheels:
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            _fail("wheel_invalid")
        with archive:
            if len(archive.infolist()) > _MAX_ARCHIVE_MEMBERS:
                _fail("wheel_invalid")
            _verify_wheel_record(archive)
            for info in sorted(archive.infolist(), key=lambda item: item.filename):
                path = _safe_archive_path(info.filename, code="wheel_invalid")
                if path.name in {"sitecustomize.py", "usercustomize.py"}:
                    _fail("wheel_invalid")
                if info.filename in extracted:
                    _fail("wheel_invalid")
                extracted.add(info.filename)
                if info.is_dir():
                    continue
                mode = info.external_attr >> 16
                if mode and not stat.S_ISREG(mode):
                    _fail("wheel_invalid")
                total += info.file_size
                if info.file_size > _MAX_MEMBER_BYTES or total > _MAX_EXTRACTED_BYTES:
                    _fail("wheel_invalid")
                content = _zip_member(archive, info.filename, _MAX_MEMBER_BYTES)
                _write_exclusive(
                    destination.joinpath(*path.parts),
                    content,
                    executable=bool(mode & 0o111),
                )
        for notice in cast(list[dict[str, Any]], artifact["notices"]):
            notice_path = destination.joinpath(*PurePosixPath(cast(str, notice["path"])).parts)
            content = _read_regular(notice_path, maximum=_MAX_MEMBER_BYTES, code="notice_invalid")
            if _sha256_bytes(content) != notice["sha256"]:
                _fail("notice_invalid")


def _copy_models(
    cache_root: Path, artifacts: Sequence[Mapping[str, object]], destination: Path
) -> None:
    destination.mkdir(mode=0o700)
    for artifact in artifacts:
        raw = verify_artifact(cache_root / cast(str, artifact["filename"]), artifact)
        _write_exclusive(destination / cast(str, artifact["filename"]), raw)


def _logical_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            metadata = path.lstat()
        except OSError:
            _fail("installed_runtime_invalid")
        if stat.S_ISREG(metadata.st_mode):
            total += metadata.st_size
    return total


def _expected_receipt(manifest: Mapping[str, Any], manifest_sha256: str) -> dict[str, object]:
    artifacts = cast(list[dict[str, Any]], manifest["artifacts"])
    closure = cast(dict[str, Any], manifest["closure"])
    return {
        "application_worker_sha256": cast(dict[str, Any], manifest["application_worker"])["sha256"],
        "artifacts": {artifact["name"]: artifact["sha256"] for artifact in artifacts},
        "complete": True,
        "manifest_sha256": manifest_sha256,
        "model_tree_sha256": closure["model_tree_sha256"],
        "python_runtime_tree_sha256": closure["python_runtime_tree_sha256"],
        "runtime_closure_sha256": closure["runtime_closure_sha256"],
        "runtime_id": manifest["runtime_id"],
        "schema": "visualworld.perception-runtime-receipt",
        "schema_version": 1,
        "site_packages_logical_bytes": closure["site_packages_logical_bytes"],
        "site_packages_tree_sha256": closure["site_packages_tree_sha256"],
    }


def _freeze_tree(root: Path) -> None:
    paths = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in paths:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            continue
        if stat.S_ISDIR(metadata.st_mode):
            path.chmod(0o555)
        elif stat.S_ISREG(metadata.st_mode):
            expected_mode = 0o555 if metadata.st_mode & 0o111 else 0o444
            if stat.S_IMODE(metadata.st_mode) != expected_mode:
                _fail("install_failed")
        else:
            _fail("install_failed")
    root.chmod(0o555)


def _fsync_directory(path: Path, *, code: str = "install_failed") -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            _fail(code)
        os.fsync(descriptor)
        owned_descriptor = descriptor
        descriptor = -1
        os.close(owned_descriptor)
    except OSError:
        _fail(code)
    finally:
        if descriptor >= 0:
            owned_descriptor = descriptor
            descriptor = -1
            with contextlib.suppress(OSError):
                os.close(owned_descriptor)


def _sync_directory_tree(root: Path) -> None:
    try:
        directories = [path for path in root.rglob("*") if stat.S_ISDIR(path.lstat().st_mode)]
    except OSError:
        _fail("install_failed")
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(root)


def _discard_staging(root: Path) -> None:
    try:
        candidates = [root, *root.rglob("*")]
    except OSError:
        candidates = [root]
    for path in candidates:
        with contextlib.suppress(OSError):
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                path.chmod(0o700)
    shutil.rmtree(root, ignore_errors=True)


def _publish_frozen_directory(staging: Path, destination: Path) -> None:
    """Atomically publish and refreeze the verified staging inode by descriptor."""

    descriptor = -1
    writable = False
    primary_error: BaseException | None = None
    try:
        path_metadata = staging.lstat()
        descriptor = os.open(
            staging,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != _TRUSTED_RUNTIME_UID
            or stat.S_IMODE(metadata.st_mode) != 0o555
            or (metadata.st_dev, metadata.st_ino) != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            _fail("install_failed")
        writable = True
        os.fchmod(descriptor, 0o755)
        try:
            staging.rename(destination)
        except FileExistsError:
            _fail("destination_exists")
        os.fchmod(descriptor, 0o555)
        writable = False
        os.fsync(descriptor)
    except BaseException as error:
        primary_error = error
        if descriptor >= 0 and writable:
            with contextlib.suppress(BaseException):
                os.fchmod(descriptor, 0o555)
        raise
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except BaseException:
                if primary_error is None:
                    raise


def _libc_version(value: str) -> tuple[int, ...]:
    parts = value.split(".")
    if not parts or any(not part.isascii() or not part.isdigit() for part in parts):
        _fail("unsupported")
    return tuple(int(part) for part in parts)


def _assert_supported_platform() -> None:
    libc, libc_version = platform.libc_ver()
    if (
        sys.platform != "linux"
        or platform.machine() != "x86_64"
        or libc != "glibc"
        or _libc_version(libc_version) < (2, 28)
    ):
        _fail("unsupported")


def _require_privileged_install() -> None:
    if os.geteuid() != 0:
        _fail("root_required")


def install_runtime(
    cache_root: Path,
    destination: Path,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
) -> None:
    """Build a verified immutable closure and publish it with one atomic rename."""

    _assert_supported_platform()
    _require_privileged_install()
    if not destination.is_absolute() or destination.name in {"", ".", ".."}:
        _fail("unsafe_destination")
    try:
        parent = destination.parent
        _reject_symlink_ancestors(parent, code="unsafe_destination")
        parent_metadata = parent.lstat()
    except OSError:
        _fail("unsafe_destination")
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != _TRUSTED_RUNTIME_UID
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
    ):
        _fail("unsafe_destination")
    if destination.exists() or destination.is_symlink():
        verify_installed_runtime(destination, manifest, manifest_sha256)
        _fsync_directory(parent)
        return

    with _cache_lock(cache_root, create=False):
        _verify_cache_unlocked(cache_root, manifest)
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=parent))
        staging.chmod(0o700)
        published = False
        try:
            artifacts = _artifact_map(manifest)
            python_raw = verify_artifact(
                cache_root / cast(str, artifacts["cpython"]["filename"]),
                artifacts["cpython"],
            )
            _extract_python(python_raw, staging / "python")
            raw_wheels = [
                (
                    verify_artifact(
                        cache_root / cast(str, artifacts[name]["filename"]), artifacts[name]
                    ),
                    artifacts[name],
                )
                for name in _WHEEL_NAMES
            ]
            _extract_wheels(raw_wheels, staging / "site-packages")
            _copy_models(
                cache_root,
                [artifacts[name] for name in _MODEL_NAMES],
                staging / "model",
            )
            application_worker = cast(dict[str, Any], manifest["application_worker"])
            worker_raw = _read_regular(
                DEFAULT_APPLICATION_WORKER,
                maximum=1024 * 1024,
                code="application_worker_invalid",
                single_link=True,
            )
            if _sha256_bytes(worker_raw) != application_worker["sha256"]:
                _fail("application_worker_invalid")
            worker_path = _safe_relative_path(application_worker["install_path"])
            _write_exclusive(staging.joinpath(*worker_path.parts), worker_raw)
            closure = cast(dict[str, Any], manifest["closure"])
            if (
                _tree_sha256(staging / "python", normalize_python_root=True)
                != closure["python_runtime_tree_sha256"]
                or _tree_sha256(staging / "site-packages") != closure["site_packages_tree_sha256"]
                or _tree_sha256(staging / "model") != closure["model_tree_sha256"]
                or _logical_size(staging / "site-packages")
                != closure["site_packages_logical_bytes"]
            ):
                _fail("runtime_closure_mismatch")
            executable = _resolved_inside(
                staging / "python", staging / "python" / "bin" / "python3"
            )
            executable_raw = _read_regular(
                executable,
                maximum=_MAX_MEMBER_BYTES,
                code="runtime_closure_mismatch",
            )
            if _sha256_bytes(executable_raw) != closure["python_executable_sha256"]:
                _fail("runtime_closure_mismatch")
            _write_exclusive(staging / INSTALL_MANIFEST_NAME, _pretty_json(manifest))
            _write_exclusive(
                staging / INSTALL_RECEIPT_NAME,
                _pretty_json(_expected_receipt(manifest, manifest_sha256)),
            )
            _freeze_tree(staging)
            verify_installed_runtime(staging, manifest, manifest_sha256)
            _sync_directory_tree(staging)
            try:
                staging.rename(destination)
                published = True
            except FileExistsError:
                _fail("destination_exists")
            _fsync_directory(parent)
            verify_installed_runtime(destination, manifest, manifest_sha256)
        except BaseException:
            if not published and staging.exists():
                _discard_staging(staging)
            raise


def _validate_frozen_tree(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        try:
            metadata = path.lstat()
        except OSError:
            _fail("installed_runtime_invalid")
        if metadata.st_uid != _TRUSTED_RUNTIME_UID:
            _fail("installed_runtime_untrusted")
        if stat.S_ISLNK(metadata.st_mode):
            _validate_internal_symlink(root, path)
        elif stat.S_ISDIR(metadata.st_mode):
            try:
                next(path.iterdir())
            except StopIteration:
                _fail("installed_runtime_invalid")
            except OSError:
                _fail("installed_runtime_invalid")
            if stat.S_IMODE(metadata.st_mode) & 0o222:
                _fail("installed_runtime_untrusted")
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o222:
                _fail("installed_runtime_untrusted")
        else:
            _fail("installed_runtime_invalid")


def verify_installed_runtime(
    runtime_root: Path, manifest: Mapping[str, Any], manifest_sha256: str
) -> None:
    """Verify a complete root-owned runtime before it may be executed."""

    _assert_supported_platform()
    if not runtime_root.is_absolute():
        _fail("installed_runtime_invalid")
    _reject_symlink_ancestors(runtime_root.parent, code="installed_runtime_invalid")
    try:
        entries = {path.name for path in runtime_root.iterdir()}
    except OSError:
        _fail("installed_runtime_invalid")
    if entries != {
        "model",
        "python",
        "site-packages",
        "worker",
        INSTALL_MANIFEST_NAME,
        INSTALL_RECEIPT_NAME,
    }:
        _fail("installed_runtime_invalid")
    installed_manifest_raw = _read_regular(
        runtime_root / INSTALL_MANIFEST_NAME,
        maximum=_MAX_MANIFEST_BYTES,
        code="installed_runtime_invalid",
    )
    if hashlib.sha256(installed_manifest_raw).hexdigest() != manifest_sha256:
        _fail("installed_runtime_invalid")
    receipt_raw = _read_regular(
        runtime_root / INSTALL_RECEIPT_NAME,
        maximum=_MAX_MANIFEST_BYTES,
        code="installed_runtime_invalid",
    )
    try:
        receipt = json.loads(receipt_raw, object_pairs_hook=_no_duplicate_object)
    except (UnicodeError, json.JSONDecodeError, ProvisioningError):
        _fail("installed_runtime_invalid")
    expected_receipt = _expected_receipt(manifest, manifest_sha256)
    if receipt != expected_receipt or receipt_raw != _pretty_json(expected_receipt):
        _fail("installed_runtime_invalid")
    closure = cast(dict[str, Any], manifest["closure"])
    if (
        _tree_sha256(runtime_root / "python", normalize_python_root=True)
        != closure["python_runtime_tree_sha256"]
        or _tree_sha256(runtime_root / "site-packages") != closure["site_packages_tree_sha256"]
        or _tree_sha256(runtime_root / "model") != closure["model_tree_sha256"]
        or _logical_size(runtime_root / "site-packages") != closure["site_packages_logical_bytes"]
    ):
        _fail("installed_runtime_invalid")
    application_worker = cast(dict[str, Any], manifest["application_worker"])
    worker_path = _safe_relative_path(application_worker["install_path"])
    worker_raw = _read_regular(
        runtime_root.joinpath(*worker_path.parts),
        maximum=1024 * 1024,
        code="installed_runtime_invalid",
        single_link=True,
    )
    if _sha256_bytes(worker_raw) != application_worker["sha256"]:
        _fail("installed_runtime_invalid")
    executable = _resolved_inside(
        runtime_root / "python", runtime_root / "python" / "bin" / "python3"
    )
    if (
        _sha256_bytes(
            _read_regular(executable, maximum=_MAX_MEMBER_BYTES, code="installed_runtime_invalid")
        )
        != closure["python_executable_sha256"]
    ):
        _fail("installed_runtime_invalid")
    artifacts = _artifact_map(manifest)
    for name in (*_WHEEL_NAMES, "cpython"):
        base = runtime_root if name == "cpython" else runtime_root / "site-packages"
        for notice in cast(list[dict[str, Any]], artifacts[name]["notices"]):
            path = base.joinpath(*PurePosixPath(cast(str, notice["path"])).parts)
            raw = _read_regular(path, maximum=_MAX_MEMBER_BYTES, code="notice_invalid")
            if _sha256_bytes(raw) != notice["sha256"]:
                _fail("notice_invalid")
    _validate_frozen_tree(runtime_root)


def _digest_field(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _runtime_tree_digest(root: Path, manifest_name: str) -> str:
    digest = hashlib.sha256()
    try:
        paths = sorted(root.rglob("*"), key=lambda path: os.fsencode(path.relative_to(root)))
        for path in paths:
            relative_path = path.relative_to(root)
            if relative_path == Path(manifest_name):
                continue
            metadata = path.lstat()
            name = os.fsencode(relative_path)
            mode = stat.S_IMODE(metadata.st_mode)
            if metadata.st_uid != _TRUSTED_RUNTIME_UID:
                _fail("media_runtime_untrusted")
            if stat.S_ISLNK(metadata.st_mode):
                _validate_internal_symlink(root, path, code="media_runtime_invalid")
                kind = b"link"
                payload = os.fsencode(os.readlink(path))
            elif stat.S_ISDIR(metadata.st_mode):
                if mode & 0o022 or mode & stat.S_IXOTH == 0:
                    _fail("media_runtime_untrusted")
                kind = b"directory"
                payload = b""
            elif stat.S_ISREG(metadata.st_mode):
                if mode & 0o022 or metadata.st_nlink != 1:
                    _fail("media_runtime_untrusted")
                kind = b"file"
                raw = _read_regular(
                    path,
                    maximum=_MAX_MEMBER_BYTES,
                    code="media_runtime_invalid",
                    single_link=True,
                )
                payload = f"{len(raw)}:{_sha256_bytes(raw)}".encode()
            else:
                _fail("media_runtime_invalid")
            for field in (kind, name, f"{mode:o}".encode(), payload):
                _digest_field(digest, field)
    except (OSError, ValueError):
        _fail("media_runtime_invalid")
    return digest.hexdigest()


def verify_media_runtime(media_root: Path, manifest: Mapping[str, Any]) -> None:
    if not media_root.is_absolute():
        _fail("media_runtime_invalid")
    _reject_symlink_ancestors(media_root.parent, code="media_runtime_invalid")
    expected = cast(dict[str, Any], manifest["media_runtime"])
    try:
        root_metadata = media_root.lstat()
        manifest_metadata = (media_root / "visualworld-runtime.json").lstat()
    except OSError:
        _fail("media_runtime_invalid")
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != _TRUSTED_RUNTIME_UID
        or stat.S_IMODE(root_metadata.st_mode) & 0o022
        or stat.S_IMODE(root_metadata.st_mode) & stat.S_IXOTH == 0
        or not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_metadata.st_uid != _TRUSTED_RUNTIME_UID
        or manifest_metadata.st_nlink != 1
        or stat.S_IMODE(manifest_metadata.st_mode) & 0o022
    ):
        _fail("media_runtime_untrusted")
    raw = _read_regular(
        media_root / "visualworld-runtime.json",
        maximum=_MAX_MANIFEST_BYTES,
        code="media_runtime_invalid",
    )
    if _sha256_bytes(raw) != expected["manifest_sha256"]:
        _fail("media_runtime_invalid")
    try:
        media_manifest = json.loads(raw, object_pairs_hook=_no_duplicate_object)
    except (UnicodeError, json.JSONDecodeError, ProvisioningError):
        _fail("media_runtime_invalid")
    if (
        type(media_manifest) is not dict
        or media_manifest.get("runtime_id") != expected["runtime_id"]
        or media_manifest.get("tree_sha256") != expected["tree_sha256"]
        or media_manifest.get("network_enabled") is not False
        or _runtime_tree_digest(media_root, "visualworld-runtime.json") != expected["tree_sha256"]
    ):
        _fail("media_runtime_invalid")


def verify_ready_runtime(
    runtime_root: Path,
    media_root: Path,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
) -> None:
    verify_installed_runtime(runtime_root, manifest, manifest_sha256)
    verify_media_runtime(media_root, manifest)


def _original_frame_receipt(manifest: Mapping[str, Any], manifest_sha256: str) -> dict[str, object]:
    worker = cast(dict[str, Any], manifest["application_worker"])
    media = cast(dict[str, Any], manifest["media_runtime"])
    return {
        "complete": True,
        "manifest_sha256": manifest_sha256,
        "media_runtime_manifest_sha256": media["manifest_sha256"],
        "media_runtime_tree_sha256": media["tree_sha256"],
        "runtime_id": manifest["runtime_id"],
        "schema": "visualworld.original-frame-overlay-receipt",
        "schema_version": 1,
        "worker_sha256": worker["sha256"],
    }


def verify_installed_original_frame_overlay(
    overlay_root: Path,
    media_root: Path,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
) -> None:
    """Verify the frozen first-party overlay and its bound media runtime."""

    _assert_supported_platform()
    if not overlay_root.is_absolute():
        _fail("installed_overlay_invalid")
    _reject_symlink_ancestors(overlay_root.parent, code="installed_overlay_invalid")
    try:
        entries = {path.name for path in overlay_root.iterdir()}
    except OSError:
        _fail("installed_overlay_invalid")
    if entries != {
        "worker",
        ORIGINAL_FRAME_INSTALL_MANIFEST_NAME,
        ORIGINAL_FRAME_INSTALL_RECEIPT_NAME,
    }:
        _fail("installed_overlay_invalid")
    installed_manifest = _read_regular(
        overlay_root / ORIGINAL_FRAME_INSTALL_MANIFEST_NAME,
        maximum=_MAX_MANIFEST_BYTES,
        code="installed_overlay_invalid",
        single_link=True,
    )
    if _sha256_bytes(installed_manifest) != manifest_sha256:
        _fail("installed_overlay_invalid")
    receipt_raw = _read_regular(
        overlay_root / ORIGINAL_FRAME_INSTALL_RECEIPT_NAME,
        maximum=_MAX_MANIFEST_BYTES,
        code="installed_overlay_invalid",
        single_link=True,
    )
    try:
        receipt = json.loads(receipt_raw, object_pairs_hook=_no_duplicate_object)
    except (UnicodeError, json.JSONDecodeError, ProvisioningError):
        _fail("installed_overlay_invalid")
    expected_receipt = _original_frame_receipt(manifest, manifest_sha256)
    if receipt != expected_receipt or receipt_raw != _pretty_json(expected_receipt):
        _fail("installed_overlay_invalid")
    application_worker = cast(dict[str, Any], manifest["application_worker"])
    worker_path = _safe_relative_path(application_worker["install_path"])
    worker_directory = overlay_root.joinpath(*worker_path.parts[:-1])
    try:
        worker_entries = {path.name for path in worker_directory.iterdir()}
    except OSError:
        _fail("installed_overlay_invalid")
    if worker_entries != {worker_path.name}:
        _fail("installed_overlay_invalid")
    worker_raw = _read_regular(
        overlay_root.joinpath(*worker_path.parts),
        maximum=1024 * 1024,
        code="installed_overlay_invalid",
        single_link=True,
    )
    if _sha256_bytes(worker_raw) != application_worker["sha256"]:
        _fail("installed_overlay_invalid")
    _validate_frozen_tree(overlay_root)
    verify_media_runtime(media_root, manifest)


def _overlay_install_parent(destination: Path) -> Path:
    if not destination.is_absolute() or destination.name in {"", ".", ".."}:
        _fail("unsafe_destination")
    try:
        parent = destination.parent
        _reject_symlink_ancestors(parent, code="unsafe_destination")
        metadata = parent.lstat()
    except OSError:
        _fail("unsafe_destination")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != _TRUSTED_RUNTIME_UID
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        _fail("unsafe_destination")
    return parent


def install_original_frame_overlay(
    destination: Path,
    media_root: Path,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
) -> None:
    """Atomically install the offline worker overlay; never fetch any artifact."""

    _assert_supported_platform()
    _require_privileged_install()
    parent = _overlay_install_parent(destination)
    verify_media_runtime(media_root, manifest)
    if destination.exists() or destination.is_symlink():
        verify_installed_original_frame_overlay(destination, media_root, manifest, manifest_sha256)
        _fsync_directory(parent)
        return

    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=parent))
    staging.chmod(0o700)
    published = False
    try:
        application_worker = cast(dict[str, Any], manifest["application_worker"])
        worker_raw = _read_regular(
            DEFAULT_ORIGINAL_FRAME_WORKER,
            maximum=1024 * 1024,
            code="application_worker_invalid",
            single_link=True,
        )
        if _sha256_bytes(worker_raw) != application_worker["sha256"]:
            _fail("application_worker_invalid")
        worker_path = _safe_relative_path(application_worker["install_path"])
        _write_exclusive(staging.joinpath(*worker_path.parts), worker_raw)
        _write_exclusive(
            staging / ORIGINAL_FRAME_INSTALL_MANIFEST_NAME,
            _pretty_json(manifest),
        )
        _write_exclusive(
            staging / ORIGINAL_FRAME_INSTALL_RECEIPT_NAME,
            _pretty_json(_original_frame_receipt(manifest, manifest_sha256)),
        )
        _freeze_tree(staging)
        verify_installed_original_frame_overlay(staging, media_root, manifest, manifest_sha256)
        _sync_directory_tree(staging)
        # Darwin can require owner-write permission on the source directory for
        # rename. Only the verified staging inode receives that temporary bit.
        _publish_frozen_directory(staging, destination)
        published = True
        _fsync_directory(parent)
        verify_installed_original_frame_overlay(destination, media_root, manifest, manifest_sha256)
    except BaseException:
        if not published and staging.exists():
            _discard_staging(staging)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = _StableArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify-manifest")
    for name in ("fetch", "verify-cache"):
        command = subparsers.add_parser(name)
        command.add_argument("--cache-root", required=True, type=Path)
    install = subparsers.add_parser("install")
    install.add_argument("--cache-root", required=True, type=Path)
    install.add_argument("--runtime-root", required=True, type=Path)
    verify = subparsers.add_parser("verify-runtime")
    verify.add_argument("--runtime-root", required=True, type=Path)
    verify.add_argument("--media-runtime-root", required=True, type=Path)
    for name in ("install-original-frame-overlay", "verify-original-frame-overlay"):
        overlay = subparsers.add_parser(name)
        overlay.add_argument("--overlay-root", required=True, type=Path)
        overlay.add_argument("--media-runtime-root", required=True, type=Path)
        overlay.add_argument(
            "--overlay-manifest",
            default=DEFAULT_ORIGINAL_FRAME_MANIFEST,
            type=Path,
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command in {
            "install-original-frame-overlay",
            "verify-original-frame-overlay",
        }:
            manifest, manifest_sha256 = load_original_frame_manifest(arguments.overlay_manifest)
            if arguments.command == "install-original-frame-overlay":
                install_original_frame_overlay(
                    arguments.overlay_root,
                    arguments.media_runtime_root,
                    manifest,
                    manifest_sha256,
                )
            else:
                verify_installed_original_frame_overlay(
                    arguments.overlay_root,
                    arguments.media_runtime_root,
                    manifest,
                    manifest_sha256,
                )
        else:
            manifest, manifest_sha256 = load_manifest(arguments.manifest)
            if arguments.command == "fetch":
                fetch_artifacts(arguments.cache_root, manifest)
            elif arguments.command == "verify-cache":
                verify_cache(arguments.cache_root, manifest)
            elif arguments.command == "install":
                install_runtime(
                    arguments.cache_root,
                    arguments.runtime_root,
                    manifest,
                    manifest_sha256,
                )
            elif arguments.command == "verify-runtime":
                verify_ready_runtime(
                    arguments.runtime_root,
                    arguments.media_runtime_root,
                    manifest,
                    manifest_sha256,
                )
        return 0 if _emit(_result("ok", command=arguments.command), sys.stdout) else 2
    except ProvisioningError as error:
        _emit(_result("error", error=error.code), sys.stderr)
        return 2
    except KeyboardInterrupt:
        _emit(_result("error", error="cancelled"), sys.stderr)
        return 130
    except Exception:
        _emit(_result("error", error="provisioning_failed"), sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
