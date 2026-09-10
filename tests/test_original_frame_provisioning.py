# SPDX-License-Identifier: Apache-2.0
"""Offline installation tests for the original-frame worker overlay."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import provision_perception_runtime as provision
import pytest


def _allow_local_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(provision, "_assert_supported_platform", lambda: None)
    monkeypatch.setattr(provision, "_require_privileged_install", lambda: None)
    monkeypatch.setattr(provision, "_TRUSTED_RUNTIME_UID", tmp_path.stat().st_uid)


def test_approved_overlay_manifest_is_canonical_and_pins_first_party_worker() -> None:
    manifest, manifest_sha256 = provision.load_original_frame_manifest()

    assert (
        manifest_sha256
        == hashlib.sha256(provision.DEFAULT_ORIGINAL_FRAME_MANIFEST.read_bytes()).hexdigest()
    )
    assert manifest["application_worker"] == {
        "install_path": "worker/original_frame_worker.py",
        "license_expression": "Apache-2.0",
        "sha256": hashlib.sha256(provision.DEFAULT_ORIGINAL_FRAME_WORKER.read_bytes()).hexdigest(),
    }
    assert (
        manifest["media_runtime"]["manifest_sha256"]
        == hashlib.sha256(
            (provision.ROOT / "workers/visualworld-runtime.json").read_bytes()
        ).hexdigest()
    )
    assert manifest["policy"]["download_during_decode"] is False
    assert manifest["policy"]["mutable_repo_imports"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(unknown=True),
        lambda value: value["application_worker"].update(sha256="0" * 64),
        lambda value: value["media_runtime"].update(tree_sha256="0" * 64),
        lambda value: value["policy"].update(download_during_decode=True),
        lambda value: value["ipc"]["source"].update(descriptor=5),
        lambda value: value["ipc"]["uncompressed_output"].update(initial_seals=[]),
        lambda value: value["worker"]["isolation"].update(read_only_overlay=False),
        lambda value: value["limits"].update(max_requested_frames=65),
    ],
)
def test_overlay_manifest_rejects_unknown_or_drifted_contract(mutation: Any) -> None:
    manifest, _ = provision.load_original_frame_manifest()
    hostile = copy.deepcopy(manifest)
    mutation(hostile)

    with pytest.raises(provision.ProvisioningError, match="manifest"):
        provision.validate_original_frame_manifest(hostile)


def test_overlay_install_is_offline_atomic_frozen_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest, manifest_sha256 = provision.load_original_frame_manifest()
    _allow_local_install(monkeypatch, tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    media_verifications: list[Path] = []

    def verify_media(path: Path, selected: object) -> None:
        assert selected is manifest
        media_verifications.append(path)

    monkeypatch.setattr(provision, "verify_media_runtime", verify_media)

    def deny_network(*arguments: object, **keywords: object) -> None:
        raise AssertionError("overlay installation attempted a download")

    monkeypatch.setattr(provision._URL_OPENER, "open", deny_network)
    destination = tmp_path / "overlay"

    provision.install_original_frame_overlay(destination, media_root, manifest, manifest_sha256)
    provision.verify_installed_original_frame_overlay(
        destination, media_root, manifest, manifest_sha256
    )
    provision.install_original_frame_overlay(destination, media_root, manifest, manifest_sha256)

    assert {path.name for path in destination.iterdir()} == {
        "worker",
        provision.ORIGINAL_FRAME_INSTALL_MANIFEST_NAME,
        provision.ORIGINAL_FRAME_INSTALL_RECEIPT_NAME,
    }
    assert (destination / "worker/original_frame_worker.py").read_bytes() == (
        provision.DEFAULT_ORIGINAL_FRAME_WORKER.read_bytes()
    )
    receipt = json.loads((destination / provision.ORIGINAL_FRAME_INSTALL_RECEIPT_NAME).read_bytes())
    assert receipt == provision._original_frame_receipt(manifest, manifest_sha256)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o555
    assert all(
        not stat.S_IMODE(path.lstat().st_mode) & 0o222
        for path in (destination, *destination.rglob("*"))
        if not path.is_symlink()
    )
    assert media_verifications == [media_root] * 6
    assert not list(tmp_path.glob(".overlay.staging-*"))


def test_overlay_install_uses_owner_write_only_for_portable_atomic_rename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest, manifest_sha256 = provision.load_original_frame_manifest()
    _allow_local_install(monkeypatch, tmp_path)
    monkeypatch.setattr(provision, "verify_media_runtime", lambda *arguments: None)
    destination = tmp_path / "overlay"
    original_rename = Path.rename
    rename_modes: list[int] = []

    def require_owner_write(source: Path, target: Path) -> Path:
        mode = stat.S_IMODE(source.stat().st_mode)
        rename_modes.append(mode)
        if not mode & stat.S_IWUSR:
            raise PermissionError("directory owner write is required for rename")
        assert not mode & (stat.S_IWGRP | stat.S_IWOTH)
        assert all(
            not stat.S_IMODE(path.lstat().st_mode) & 0o222
            for path in source.rglob("*")
            if not path.is_symlink()
        )
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", require_owner_write)

    provision.install_original_frame_overlay(
        destination, tmp_path / "media", manifest, manifest_sha256
    )

    assert rename_modes == [0o755]
    assert stat.S_IMODE(destination.stat().st_mode) == 0o555
    assert all(
        not stat.S_IMODE(path.lstat().st_mode) & 0o222
        for path in (destination, *destination.rglob("*"))
        if not path.is_symlink()
    )


def test_descriptor_bound_publish_refreezes_displaced_inode_and_preserves_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _allow_local_install(monkeypatch, tmp_path)
    staging = tmp_path / ".overlay.staging-test"
    destination = tmp_path / "overlay"
    displaced = tmp_path / "displaced-overlay"
    staging.mkdir(mode=0o700)
    staging.chmod(0o555)
    staging_identity = (staging.stat().st_dev, staging.stat().st_ino)
    original_open = os.open
    original_close = os.close
    original_rename = Path.rename
    opened: list[tuple[int, int]] = []
    closed: list[int] = []

    def record_open(path: Path, flags: int) -> int:
        descriptor = original_open(path, flags)
        opened.append((descriptor, flags))
        return descriptor

    def replace_then_interrupt(source: Path, target: Path) -> Path:
        original_rename(source, target)
        os.rename(target, displaced)
        destination.mkdir(mode=0o700)
        raise KeyboardInterrupt("rename interrupted")

    def close_then_fail(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)
        raise PermissionError("close failed")

    monkeypatch.setattr(
        Path,
        "chmod",
        lambda _path, _mode: pytest.fail("descriptor-bound publication used path chmod"),
    )
    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", close_then_fail)
    monkeypatch.setattr(Path, "rename", replace_then_interrupt)

    with pytest.raises(KeyboardInterrupt, match="rename interrupted"):
        provision._publish_frozen_directory(staging, destination)

    required_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    assert len(opened) == 1
    assert opened[0][1] == required_flags
    assert closed == [opened[0][0]]
    with pytest.raises(OSError):
        os.fstat(opened[0][0])
    assert (displaced.stat().st_dev, displaced.stat().st_ino) == staging_identity
    assert stat.S_IMODE(displaced.stat().st_mode) == 0o555
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("failed_mode", "published", "final_mode"),
    [(0o755, False, 0o555), (0o555, True, 0o755)],
)
def test_descriptor_bound_publish_preserves_fchmod_failure_and_closes_fd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_mode: int,
    published: bool,
    final_mode: int,
) -> None:
    _allow_local_install(monkeypatch, tmp_path)
    staging = tmp_path / ".overlay.staging-test"
    destination = tmp_path / "overlay"
    staging.mkdir(mode=0o700)
    staging.chmod(0o555)
    original_open = os.open
    original_fchmod = os.fchmod
    opened: list[int] = []

    def record_open(path: Path, flags: int) -> int:
        descriptor = original_open(path, flags)
        opened.append(descriptor)
        return descriptor

    def deny_mode(descriptor: int, mode: int) -> None:
        if mode == failed_mode:
            raise PermissionError(f"fchmod {mode:o} denied")
        original_fchmod(descriptor, mode)

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "fchmod", deny_mode)

    with pytest.raises(PermissionError, match=f"fchmod {failed_mode:o} denied"):
        provision._publish_frozen_directory(staging, destination)

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    root = destination if published else staging
    assert root.is_dir()
    assert stat.S_IMODE(root.stat().st_mode) == final_mode
    assert destination.is_dir() is published


def test_overlay_install_failure_does_not_publish_or_leave_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest, manifest_sha256 = provision.load_original_frame_manifest()
    _allow_local_install(monkeypatch, tmp_path)
    monkeypatch.setattr(provision, "verify_media_runtime", lambda *arguments: None)
    hostile_worker = tmp_path / "hostile-worker.py"
    hostile_worker.write_bytes(b"tampered")
    monkeypatch.setattr(provision, "DEFAULT_ORIGINAL_FRAME_WORKER", hostile_worker)
    destination = tmp_path / "overlay"

    with pytest.raises(provision.ProvisioningError, match="application_worker_invalid"):
        provision.install_original_frame_overlay(
            destination, tmp_path / "media", manifest, manifest_sha256
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".overlay.staging-*"))


def test_overlay_existing_partial_or_mutated_root_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest, manifest_sha256 = provision.load_original_frame_manifest()
    _allow_local_install(monkeypatch, tmp_path)
    monkeypatch.setattr(provision, "verify_media_runtime", lambda *arguments: None)
    destination = tmp_path / "overlay"
    destination.mkdir()

    with pytest.raises(provision.ProvisioningError, match="installed_overlay_invalid"):
        provision.install_original_frame_overlay(
            destination, tmp_path / "media", manifest, manifest_sha256
        )

    destination.rmdir()
    provision.install_original_frame_overlay(
        destination, tmp_path / "media", manifest, manifest_sha256
    )
    worker_directory = destination / "worker"
    worker = worker_directory / "original_frame_worker.py"
    destination.chmod(0o755)
    worker_directory.chmod(0o755)
    worker.chmod(0o644)
    worker.write_bytes(b"tampered")
    worker.chmod(0o444)
    worker_directory.chmod(0o555)
    destination.chmod(0o555)

    with pytest.raises(provision.ProvisioningError, match="installed_overlay_invalid"):
        provision.verify_installed_original_frame_overlay(
            destination, tmp_path / "media", manifest, manifest_sha256
        )


def test_overlay_verification_rejects_extra_importable_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest, manifest_sha256 = provision.load_original_frame_manifest()
    _allow_local_install(monkeypatch, tmp_path)
    monkeypatch.setattr(provision, "verify_media_runtime", lambda *arguments: None)
    destination = tmp_path / "overlay"
    provision.install_original_frame_overlay(
        destination, tmp_path / "media", manifest, manifest_sha256
    )
    worker_directory = destination / "worker"
    destination.chmod(0o755)
    worker_directory.chmod(0o755)
    (worker_directory / "av.py").write_text("raise RuntimeError\n", encoding="utf-8")
    (worker_directory / "av.py").chmod(0o444)
    worker_directory.chmod(0o555)
    destination.chmod(0o555)

    with pytest.raises(provision.ProvisioningError, match="installed_overlay_invalid"):
        provision.verify_installed_original_frame_overlay(
            destination, tmp_path / "media", manifest, manifest_sha256
        )


@pytest.mark.parametrize(
    ("command", "operation"),
    [
        ("install-original-frame-overlay", "install_original_frame_overlay"),
        ("verify-original-frame-overlay", "verify_installed_original_frame_overlay"),
    ],
)
def test_overlay_cli_subcommands_route_without_loading_perception_or_fetching(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    operation: str,
) -> None:
    manifest = {"runtime_id": "overlay"}
    manifest_sha256 = "12" * 32
    overlay_root = Path("/trusted/overlay")
    media_root = Path("/trusted/media")
    calls: list[tuple[Path, Path, object, str]] = []
    monkeypatch.setattr(
        provision,
        "load_original_frame_manifest",
        lambda path: (manifest, manifest_sha256),
    )
    monkeypatch.setattr(
        provision,
        "load_manifest",
        lambda path: pytest.fail("perception manifest loaded for overlay command"),
    )
    monkeypatch.setattr(
        provision,
        operation,
        lambda overlay, media, selected, digest: calls.append((overlay, media, selected, digest)),
    )
    monkeypatch.setattr(
        provision._URL_OPENER,
        "open",
        lambda *arguments, **keywords: pytest.fail("overlay command attempted a download"),
    )

    assert (
        provision.main(
            [
                command,
                "--overlay-root",
                str(overlay_root),
                "--media-runtime-root",
                str(media_root),
            ]
        )
        == 0
    )

    assert calls == [(overlay_root, media_root, manifest, manifest_sha256)]
    assert json.loads(capsys.readouterr().out) == {
        "command": command,
        "schema": "visualworld.perception-provision-result",
        "schema_version": 1,
        "status": "ok",
    }
