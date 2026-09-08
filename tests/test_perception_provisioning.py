# SPDX-License-Identifier: Apache-2.0
"""Hostile-input tests for the explicit perception runtime provisioner."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any, cast

import provision_perception_runtime as provision
import pytest


def _artifact(
    name: str,
    filename: str,
    raw: bytes,
    *,
    notices: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "filename": filename,
        "name": name,
        "notices": [] if notices is None else notices,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "url": f"https://files.pythonhosted.org/packages/test/{filename}",
    }


def _tar(entries: dict[str, bytes], *, symlinks: dict[str, str] | None = None) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, raw in entries.items():
            info = tarfile.TarInfo(name)
            info.mode = 0o755 if name.endswith("python3") else 0o644
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return output.getvalue()


def _wheel(files: dict[str, bytes], distribution: str) -> bytes:
    selected = dict(files)
    record = f"{distribution}.dist-info/RECORD"
    rows = []
    for name, raw in selected.items():
        encoded = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        rows.append(f"{name},sha256={encoded},{len(raw)}\r\n")
    rows.append(f"{record},,\r\n")
    selected[record] = "".join(rows).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for name, raw in selected.items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, raw)
    return output.getvalue()


def _manifest() -> tuple[dict[str, Any], str]:
    return provision.load_manifest()


def test_approved_manifest_is_canonical_complete_and_not_redistributed() -> None:
    manifest, raw_sha256 = _manifest()
    manifest_path = Path(__file__).resolve().parents[1] / "workers/perception-runtime-v1.json"

    assert raw_sha256 == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert tuple(artifact["name"] for artifact in manifest["artifacts"]) == (
        "cpython",
        "numpy",
        "openvino",
        "openvino-telemetry",
        "vehicle-detection-0201-xml",
        "vehicle-detection-0201-bin",
    )
    assert manifest["distribution"] == {
        "application_wheel": "denied",
        "container_or_installer": "denied",
        "model_and_runtime": "user-provisioned-only",
        "reason": (
            "composite notices, corresponding-source, patent, policy, and jurisdiction "
            "review incomplete"
        ),
        "release_sbom_membership": "excluded-unless-separately-approved",
    }
    assert manifest["policy"]["download_during_ingest"] is False
    assert manifest["policy"]["telemetry"] is False
    assert all(
        artifact["distribution_status"] == "user-provisioned-only"
        for artifact in manifest["artifacts"]
    )


def test_manifest_artifacts_match_reviewed_research_and_media_locks() -> None:
    repository = Path(__file__).resolve().parents[1]
    manifest, _ = _manifest()
    artifacts = {artifact["name"]: artifact for artifact in manifest["artifacts"]}
    candidates = json.loads(
        (repository / "fixtures/v02-detection-research/candidates.json").read_bytes()
    )
    selected = next(
        candidate
        for candidate in candidates["candidates"]
        if candidate["name"] == "vehicle-detection-0201-fp32-openvino-2026.3.1"
    )
    for extension in ("xml", "bin"):
        approved = artifacts[f"vehicle-detection-0201-{extension}"]
        evidence = selected[f"model_{extension}"]
        assert {key: approved[key] for key in ("sha256", "size", "url")} == {
            key: evidence[key] for key in ("sha256", "size", "url")
        }
        assert approved["revision"] == selected["omz_revision"]

    for manifest_name, evidence_name in (
        ("numpy", "numpy"),
        ("openvino", "openvino"),
        ("openvino-telemetry", "openvino_telemetry"),
    ):
        approved = artifacts[manifest_name]
        evidence = candidates["runtime"][evidence_name]
        assert {
            key: approved[key]
            for key in ("license_expression", "revision", "sha256", "size", "url", "version")
        } == {
            key: evidence[key]
            for key in ("license_expression", "revision", "sha256", "size", "url", "version")
        }
        if manifest_name == "openvino":
            assert evidence["license_evidence"]["bundled_components"] == []
            assert len(approved["bundled_components"]) == 31
        else:
            assert (
                approved["bundled_components"] == evidence["license_evidence"]["bundled_components"]
            )
        assert approved["notices"] == evidence["license_evidence"]["notice_files"]

    python = candidates["runtime"]["python"]
    approved_python = artifacts["cpython"]
    assert approved_python["sha256"] == python["archive_sha256"]
    assert {
        key: approved_python[key] for key in ("license_expression", "revision", "url", "version")
    } == {key: python[key] for key in ("license_expression", "revision", "url", "version")}
    assert {component["name"] for component in approved_python["bundled_components"]} == {
        "Berkeley DB 6.0.19",
        "CPython",
        "Expat",
        "OpenSSL 3.5.8",
        "SQLite",
        "Tcl/Tk 9.0",
        "Tix",
        "bzip2/libbzip2",
        "libX11",
        "libXau",
        "libedit",
        "libffi",
        "liblzma/XZ Utils",
        "libuuid",
        "libxcb",
        "mpdecimal",
        "ncurses",
        "pip 26.2.1",
        "zlib",
    }
    openvino_components = {
        component["name"]: component["license_expression"]
        for component in artifacts["openvino"]["bundled_components"]
    }
    assert openvino_components["oneTBB"] == (
        "Apache-2.0 AND BSD-3-Clause AND (GPL-3.0-or-later WITH GCC-exception-3.1) AND MIT"
    )
    assert {"oneDNN", "oneTBB", "hwloc", "OpenCV and FlatBuffers"} <= set(openvino_components)
    assert (
        manifest["closure"]["evaluation_python_runtime_tree_sha256"]
        == python["runtime_tree_sha256"]
    )
    assert manifest["closure"]["python_executable_sha256"] == python["executable_sha256"]
    raw_results = json.loads(
        (repository / "fixtures/v02-detection-research/results/raw-results.json").read_bytes()
    )
    environment = raw_results["environment"]
    assert (
        manifest["closure"]["evaluation_runtime_closure_sha256"]
        == environment["runtime_closure_sha256"]
    )
    assert (
        manifest["closure"]["site_packages_tree_sha256"]
        == environment["installed_runtime_tree_sha256"]
    )
    assert (
        manifest["closure"]["site_packages_logical_bytes"]
        == environment["installed_runtime_logical_bytes"]
    )

    media_path = repository / "workers/visualworld-runtime.json"
    media = json.loads(media_path.read_bytes())
    assert manifest["media_runtime"] == {
        "manifest_sha256": hashlib.sha256(media_path.read_bytes()).hexdigest(),
        "runtime_id": media["runtime_id"],
        "tree_sha256": media["tree_sha256"],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unknown": True}),
        lambda value: value["support"].update({"architecture": "aarch64"}),
        lambda value: value["distribution"].update({"application_wheel": "approved"}),
        lambda value: value["policy"].update({"remote_code": True}),
        lambda value: value["worker"].update({"clear_environment": False}),
        lambda value: value["artifacts"][1].update({"license_expression": "MIT"}),
        lambda value: value["artifacts"][1].pop("notices"),
        lambda value: value["artifacts"][1].update({"filename": "../numpy.whl"}),
        lambda value: value["artifacts"][1].update({"url": "https://example.com/numpy.whl"}),
        lambda value: value["artifacts"][1].update({"size": True}),
        lambda value: value["artifacts"].reverse(),
    ],
)
def test_manifest_rejects_unknown_or_drifted_contract(mutation: Any) -> None:
    manifest, _ = _manifest()
    hostile = copy.deepcopy(manifest)
    mutation(hostile)
    with pytest.raises(provision.ProvisioningError, match="manifest"):
        provision.validate_manifest(hostile)


def test_manifest_file_rejects_duplicates_noncanonical_and_links(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema":1,"schema":2}', encoding="utf-8")
    with pytest.raises(provision.ProvisioningError, match="invalid_manifest"):
        provision.load_manifest(duplicate)

    manifest, _ = _manifest()
    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(provision.ProvisioningError, match="noncanonical_manifest"):
        provision.load_manifest(noncanonical)

    linked = tmp_path / "linked.json"
    linked.symlink_to(provision.DEFAULT_MANIFEST)
    with pytest.raises(provision.ProvisioningError, match="invalid_manifest_file"):
        provision.load_manifest(linked)


def test_artifact_verification_rejects_tamper_link_and_nonregular(tmp_path: Path) -> None:
    raw = b"approved-artifact"
    artifact = _artifact("example", "example.whl", raw)
    path = tmp_path / "example.whl"
    path.write_bytes(raw)
    assert provision.verify_artifact(path, artifact) == raw

    path.write_bytes(b"tampered-artifact")
    with pytest.raises(provision.ProvisioningError, match="artifact_invalid"):
        provision.verify_artifact(path, artifact)

    path.unlink()
    target = tmp_path / "target"
    target.write_bytes(raw)
    path.symlink_to(target)
    with pytest.raises(provision.ProvisioningError, match="artifact_invalid"):
        provision.verify_artifact(path, artifact)
    path.unlink()
    path.mkdir()
    with pytest.raises(provision.ProvisioningError, match="artifact_invalid"):
        provision.verify_artifact(path, artifact)
    path.rmdir()
    path.write_bytes(raw)
    hardlink = tmp_path / "hardlink.whl"
    hardlink.hardlink_to(path)
    with pytest.raises(provision.ProvisioningError, match="artifact_invalid"):
        provision.verify_artifact(path, artifact)


def test_fifo_manifest_fails_without_blocking_or_leaking_path(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    fifo = tmp_path / "private-runtime-manifest"
    os.mkfifo(fifo)
    script = Path(__file__).resolve().parents[1] / "scripts/provision_perception_runtime.py"

    result = subprocess.run(
        [
            sys.executable,
            os.fspath(script),
            "--manifest",
            os.fspath(fifo),
            "verify-manifest",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert result.returncode == 2
    assert os.fspath(fifo) not in result.stderr
    assert json.loads(result.stderr) == {"error": "invalid_manifest_file", "status": "error"}


def test_cache_requires_private_exact_regular_closure(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    raw = b"one"
    artifact = _artifact("one", "one.whl", raw)
    manifest = {"artifacts": [artifact]}
    (cache / "one.whl").write_bytes(raw)
    provision.verify_cache(cache, manifest)

    partial = cache / ".one.whl.partial-hostile"
    partial.write_bytes(b"partial")
    with pytest.raises(provision.ProvisioningError, match="cache_invalid"):
        provision.verify_cache(cache, manifest)
    partial.unlink()
    cache.chmod(0o755)
    with pytest.raises(provision.ProvisioningError, match="unsafe_cache_root"):
        provision.verify_cache(cache, manifest)


def test_cache_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    cache = real / "cache"
    cache.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(provision.ProvisioningError, match="unsafe_cache_root"):
        provision.verify_cache(linked / "cache", {"artifacts": []})


class _Response:
    def __init__(self, raw: bytes, url: str) -> None:
        self._raw = io.BytesIO(raw)
        self._url = url

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self._raw.read(size)

    def geturl(self) -> str:
        return self._url


class _Opener:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def open(self, request: Any, *, timeout: int) -> _Response:
        assert timeout == 60
        url = cast(str, request.full_url)
        self.requested.append(url)
        return _Response(self.responses[url], url)


def test_fetch_is_explicit_atomic_and_resumes_completed_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    first_raw = b"already-complete"
    second_raw = b"download-me"
    first = _artifact("first", "first.whl", first_raw)
    second = _artifact("second", "second.whl", second_raw)
    manifest = {"artifacts": [first, second]}
    (cache / "first.whl").write_bytes(first_raw)
    stale = cache / ".second.whl.partial-stale"
    stale.write_bytes(b"partial")
    opener = _Opener({cast(str, second["url"]): second_raw})
    monkeypatch.setattr(provision, "_URL_OPENER", opener)

    provision.fetch_artifacts(cache, manifest)

    assert opener.requested == [second["url"]]
    assert (cache / "second.whl").read_bytes() == second_raw
    assert not stale.exists()
    provision.fetch_artifacts(cache, manifest)
    assert opener.requested == [second["url"]]


def test_fetch_recovers_twice_from_publish_crash_hardlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    raw = b"published-before-partial-unlink"
    artifact = _artifact("one", "one.whl", raw)
    manifest = {"artifacts": [artifact]}
    final = cache / "one.whl"
    partial = cache / ".one.whl.partial-interrupted"
    final.write_bytes(raw)
    partial.hardlink_to(final)
    assert final.stat().st_nlink == 2
    opener = _Opener({})
    monkeypatch.setattr(provision, "_URL_OPENER", opener)

    provision.fetch_artifacts(cache, manifest)
    provision.fetch_artifacts(cache, manifest)

    assert final.read_bytes() == raw
    assert final.stat().st_nlink == 1
    assert not partial.exists()
    assert opener.requested == []


def test_download_directory_fsync_failure_requires_a_durable_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    raw = b"published-before-directory-fsync"
    artifact = _artifact("one", "one.whl", raw)
    opener = _Opener({cast(str, artifact["url"]): raw})
    monkeypatch.setattr(provision, "_URL_OPENER", opener)
    real_fsync_directory = provision._fsync_directory
    synced: list[Path] = []
    directory_fsync_fails = [True]

    def fail_cache(path: Path, *, code: str = "install_failed") -> None:
        synced.append(path)
        if path == cache and directory_fsync_fails[0]:
            provision._fail(code)
        real_fsync_directory(path, code=code)

    monkeypatch.setattr(provision, "_fsync_directory", fail_cache)
    with pytest.raises(provision.ProvisioningError, match="download_failed"):
        provision._download_artifact(cache, artifact)
    assert (cache / "one.whl").read_bytes() == raw
    assert opener.requested == [artifact["url"]]

    with pytest.raises(provision.ProvisioningError, match="download_failed"):
        provision._download_artifact(cache, artifact)
    assert synced.count(cache) == 2
    assert opener.requested == [artifact["url"]]

    directory_fsync_fails[0] = False
    provision._download_artifact(cache, artifact)
    assert synced.count(cache) == 3
    assert opener.requested == [artifact["url"]]


def test_download_close_interruption_is_not_retried_and_cleans_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    raw = b"downloaded"
    artifact = _artifact("one", "one.whl", raw)
    monkeypatch.setattr(provision, "_URL_OPENER", _Opener({cast(str, artifact["url"]): raw}))
    real_close = os.close
    interrupted: list[int] = []

    def fail_before_release(descriptor: int) -> None:
        if not interrupted:
            interrupted.append(descriptor)
            raise OSError("ambiguous close interruption")
        real_close(descriptor)

    monkeypatch.setattr(os, "close", fail_before_release)
    with pytest.raises(provision.ProvisioningError, match="download_failed"):
        provision._download_artifact(cache, artifact)

    assert len(interrupted) == 1
    os.fstat(interrupted[0])
    real_close(interrupted[0])
    assert list(cache.iterdir()) == []


def test_download_close_error_cannot_close_reused_descriptor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    raw = b"downloaded"
    artifact = _artifact("one", "one.whl", raw)
    monkeypatch.setattr(provision, "_URL_OPENER", _Opener({cast(str, artifact["url"]): raw}))
    victim_path = tmp_path / "victim"
    victim_path.write_bytes(b"keep-open")
    real_close = os.close
    victim = [-1]

    def fail_after_release(descriptor: int) -> None:
        if victim[0] < 0:
            real_close(descriptor)
            victim[0] = os.open(victim_path, os.O_RDONLY | os.O_CLOEXEC)
            assert victim[0] == descriptor
            raise OSError("close reported after release")
        real_close(descriptor)

    monkeypatch.setattr(os, "close", fail_after_release)
    with pytest.raises(provision.ProvisioningError, match="download_failed"):
        provision._download_artifact(cache, artifact)

    os.fstat(victim[0])
    real_close(victim[0])
    assert list(cache.iterdir()) == []


def test_failed_fetch_leaves_no_visible_or_partial_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    artifact = _artifact("one", "one.whl", b"expected")
    opener = _Opener({cast(str, artifact["url"]): b"short"})
    monkeypatch.setattr(provision, "_URL_OPENER", opener)

    with pytest.raises(provision.ProvisioningError, match="artifact_invalid"):
        provision.fetch_artifacts(cache, {"artifacts": [artifact]})

    assert {path.name for path in cache.iterdir()} == {".provision.lock"}


@pytest.mark.parametrize("name", ["../escape", "/absolute", "dir\\escape", "./"])
def test_archive_paths_reject_escape(name: str) -> None:
    with pytest.raises(provision.ProvisioningError):
        provision._safe_archive_path(name, code="hostile")


def test_python_extraction_accepts_internal_link_and_rejects_hostile_members(
    tmp_path: Path,
) -> None:
    raw = _tar(
        {
            "python/bin/python3.13": b"python",
            "python/lib/python3.13/LICENSE.txt": b"license",
        },
        symlinks={"python/bin/python3": "python3.13"},
    )
    destination = tmp_path / "python"
    provision._extract_python(raw, destination)
    assert (destination / "bin/python3").resolve() == (destination / "bin/python3.13")

    for hostile in (
        _tar({"python/../../escape": b"bad"}),
        _tar({"outside/file": b"bad"}),
        _tar({"python/file": b"ok"}, symlinks={"python/escape": "../../outside"}),
    ):
        target = tmp_path / hashlib.sha256(hostile).hexdigest()
        with pytest.raises(provision.ProvisioningError, match="python_archive_invalid"):
            provision._extract_python(hostile, target)
        assert not (tmp_path / "escape").exists()


def test_installed_symlink_must_be_relative_and_internal(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    target = root / "target"
    target.write_bytes(b"target")
    linked = root / "linked"
    linked.symlink_to(target)

    with pytest.raises(provision.ProvisioningError, match="installed_runtime_invalid"):
        provision._validate_internal_symlink(root, linked)


def test_wheel_record_notices_and_paths_are_verified(tmp_path: Path) -> None:
    license_raw = b"license"
    notice = {
        "path": "demo-1.0.dist-info/LICENSE",
        "sha256": hashlib.sha256(license_raw).hexdigest(),
    }
    raw = _wheel(
        {"demo/__init__.py": b"", "demo-1.0.dist-info/LICENSE": license_raw},
        "demo-1.0",
    )
    destination = tmp_path / "site-packages"
    provision._extract_wheels([(raw, {"notices": [notice]})], destination)
    assert (destination / "demo/__init__.py").is_file()

    missing_notice = tmp_path / "missing-notice"
    with pytest.raises(provision.ProvisioningError, match="notice_invalid"):
        provision._extract_wheels(
            [(raw, {"notices": [{"path": "missing", "sha256": "0" * 64}]})],
            missing_notice,
        )
    hostile = _wheel({"../escape": b"bad"}, "hostile-1.0")
    with pytest.raises(provision.ProvisioningError, match="wheel_invalid"):
        provision._extract_wheels([(hostile, {"notices": []})], tmp_path / "hostile")
    customizer = _wheel({"sitecustomize.py": b"bad"}, "customizer-1.0")
    with pytest.raises(provision.ProvisioningError, match="wheel_invalid"):
        provision._extract_wheels([(customizer, {"notices": []})], tmp_path / "customizer")


def _synthetic_install_manifest(tmp_path: Path) -> tuple[dict[str, Any], Path, str]:
    python_raw = _tar(
        {
            "python/bin/python3": b"python",
            "python/lib/python3.13/encodings/__pycache__/approved.pyc": b"approved-bytecode",
        }
    )
    wheel_raw: dict[str, bytes] = {
        name: _wheel({f"{name.replace('-', '_')}/__init__.py": name.encode()}, f"{name}-1.0")
        for name in ("numpy", "openvino", "openvino-telemetry")
    }
    model_raw = {
        "vehicle-detection-0201-xml": b"xml",
        "vehicle-detection-0201-bin": b"bin",
    }
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    artifacts: list[dict[str, object]] = []
    python_artifact = _artifact("cpython", "python.tar.gz", python_raw)
    artifacts.append(python_artifact)
    (cache / "python.tar.gz").write_bytes(python_raw)
    for name, raw in wheel_raw.items():
        filename = f"{name}.whl"
        artifacts.append(_artifact(name, filename, raw))
        (cache / filename).write_bytes(raw)
    for name, raw in model_raw.items():
        filename = "model.xml" if name.endswith("xml") else "model.bin"
        artifacts.append(_artifact(name, filename, raw))
        (cache / filename).write_bytes(raw)

    expected = tmp_path / "expected"
    expected.mkdir()
    provision._extract_python(python_raw, expected / "python")
    provision._extract_wheels(
        [(wheel_raw[name], {"notices": []}) for name in provision._WHEEL_NAMES],
        expected / "site-packages",
    )
    provision._copy_models(
        cache,
        [artifact for artifact in artifacts if artifact["name"] in provision._MODEL_NAMES],
        expected / "model",
    )
    manifest: dict[str, Any] = {
        "artifacts": artifacts,
        "closure": {
            "site_packages_logical_bytes": provision._logical_size(expected / "site-packages"),
            "model_tree_sha256": provision._tree_sha256(expected / "model"),
            "python_executable_sha256": hashlib.sha256(b"python").hexdigest(),
            "python_runtime_tree_sha256": provision._tree_sha256(
                expected / "python", normalize_python_root=True
            ),
            "runtime_closure_sha256": "1" * 64,
            "site_packages_tree_sha256": provision._tree_sha256(expected / "site-packages"),
        },
        "runtime_id": "synthetic-runtime",
    }
    shutil.rmtree(expected)
    manifest_sha256 = hashlib.sha256(provision._pretty_json(manifest)).hexdigest()
    return manifest, cache, manifest_sha256


def _allow_synthetic_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    try:
        provision._assert_supported_platform()
    except provision.ProvisioningError:
        pytest.skip("perception runtime installation is unsupported on this host")
    monkeypatch.setattr(provision, "_require_privileged_install", lambda: None)
    monkeypatch.setattr(provision, "_TRUSTED_RUNTIME_UID", tmp_path.stat().st_uid)


def test_install_is_atomic_verifiable_resumable_and_rejects_partial_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, cache, manifest_sha256 = _synthetic_install_manifest(tmp_path)
    _allow_synthetic_install(monkeypatch, tmp_path)
    destination = tmp_path / "runtime"

    provision.install_runtime(cache, destination, manifest, manifest_sha256)
    provision.verify_installed_runtime(destination, manifest, manifest_sha256)
    provision.install_runtime(cache, destination, manifest, manifest_sha256)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o555

    destination.chmod(0o755)
    (destination / "unexpected").write_bytes(b"hostile")
    with pytest.raises(provision.ProvisioningError, match="installed_runtime_invalid"):
        provision.verify_installed_runtime(destination, manifest, manifest_sha256)

    partial = tmp_path / "partial"
    partial.mkdir()
    with pytest.raises(provision.ProvisioningError, match="installed_runtime_invalid"):
        provision.install_runtime(cache, partial, manifest, manifest_sha256)


def test_install_failure_does_not_publish_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, cache, manifest_sha256 = _synthetic_install_manifest(tmp_path)
    _allow_synthetic_install(monkeypatch, tmp_path)
    model_path = cache / "model.bin"
    model_path.write_bytes(b"tampered")
    destination = tmp_path / "runtime"

    with pytest.raises(provision.ProvisioningError, match="artifact_invalid"):
        provision.install_runtime(cache, destination, manifest, manifest_sha256)

    assert not destination.exists()
    assert not list(tmp_path.glob(".runtime.staging-*"))


def test_write_exclusive_fsyncs_before_releasing_descriptor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[tuple[str, int]] = []
    real_fsync = os.fsync
    real_close = os.close

    def record_fsync(descriptor: int) -> None:
        events.append(("fsync", descriptor))
        real_fsync(descriptor)

    def record_close(descriptor: int) -> None:
        events.append(("close", descriptor))
        real_close(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "close", record_close)
    destination = tmp_path / "durable"
    provision._write_exclusive(destination, b"complete")

    assert destination.read_bytes() == b"complete"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o444
    assert events[0][0] == "fsync"
    assert events[1] == ("close", events[0][1])


def test_directory_sync_failure_before_publish_leaves_no_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, cache, manifest_sha256 = _synthetic_install_manifest(tmp_path)
    _allow_synthetic_install(monkeypatch, tmp_path)
    monkeypatch.setattr(
        provision, "_sync_directory_tree", lambda _root: provision._fail("install_failed")
    )
    real_rmtree = shutil.rmtree

    def require_writable_directories(path: Path, *, ignore_errors: bool) -> None:
        assert ignore_errors is True
        directories = [
            candidate
            for candidate in (path, *path.rglob("*"))
            if stat.S_ISDIR(candidate.lstat().st_mode)
        ]
        assert directories
        assert all(
            stat.S_IMODE(directory.lstat().st_mode) & 0o700 == 0o700 for directory in directories
        )
        real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(shutil, "rmtree", require_writable_directories)
    destination = tmp_path / "runtime"

    with pytest.raises(provision.ProvisioningError, match="install_failed"):
        provision.install_runtime(cache, destination, manifest, manifest_sha256)

    assert not destination.exists()
    assert not list(tmp_path.glob(".runtime.staging-*"))


def test_parent_fsync_failure_keeps_only_verified_resumable_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, cache, manifest_sha256 = _synthetic_install_manifest(tmp_path)
    _allow_synthetic_install(monkeypatch, tmp_path)
    real_fsync_directory = provision._fsync_directory
    synced: list[Path] = []
    parent_fsync_fails = [True]

    def fail_parent(path: Path) -> None:
        synced.append(path)
        if path == tmp_path and parent_fsync_fails[0]:
            provision._fail("install_failed")
        real_fsync_directory(path)

    monkeypatch.setattr(provision, "_fsync_directory", fail_parent)
    destination = tmp_path / "runtime"
    with pytest.raises(provision.ProvisioningError, match="install_failed"):
        provision.install_runtime(cache, destination, manifest, manifest_sha256)

    assert synced[-1] == tmp_path
    assert synced[-2].name.startswith(".runtime.staging-")
    assert destination.is_dir()
    assert not list(tmp_path.glob(".runtime.staging-*"))
    provision.verify_installed_runtime(destination, manifest, manifest_sha256)
    with pytest.raises(provision.ProvisioningError, match="install_failed"):
        provision.install_runtime(cache, destination, manifest, manifest_sha256)
    assert synced.count(tmp_path) == 2
    parent_fsync_fails[0] = False
    provision.install_runtime(cache, destination, manifest, manifest_sha256)
    assert synced.count(tmp_path) == 3


@pytest.mark.parametrize("mutation", ["modify", "add", "remove"])
def test_installed_runtime_rejects_bytecode_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str
) -> None:
    manifest, cache, manifest_sha256 = _synthetic_install_manifest(tmp_path)
    _allow_synthetic_install(monkeypatch, tmp_path)
    destination = tmp_path / "runtime"
    provision.install_runtime(cache, destination, manifest, manifest_sha256)
    pycache = destination / "python/lib/python3.13/encodings/__pycache__"
    approved = pycache / "approved.pyc"

    pycache.chmod(0o755)
    if mutation == "modify":
        approved.chmod(0o644)
        approved.write_bytes(b"modified-bytecode")
        approved.chmod(0o444)
    elif mutation == "add":
        added = pycache / "added.pyc"
        added.write_bytes(b"added-bytecode")
        added.chmod(0o444)
    else:
        approved.unlink()
    pycache.chmod(0o555)

    with pytest.raises(provision.ProvisioningError, match="installed_runtime_invalid"):
        provision.verify_installed_runtime(destination, manifest, manifest_sha256)


def test_non_linux_platform_is_explicitly_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(provision.ProvisioningError, match="unsupported"):
        provision._assert_supported_platform()


@pytest.mark.parametrize(
    ("libc", "version"),
    [("musl", "1.2.5"), ("glibc", "2.27"), ("glibc", "unparseable")],
)
def test_incompatible_linux_libc_is_explicitly_unsupported(
    monkeypatch: pytest.MonkeyPatch, libc: str, version: str
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(platform, "libc_ver", lambda: (libc, version))
    with pytest.raises(provision.ProvisioningError, match="unsupported"):
        provision._assert_supported_platform()


def test_glibc_228_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(platform, "libc_ver", lambda: ("glibc", "2.28"))
    provision._assert_supported_platform()


def test_cli_redacts_unexpected_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_load(_path: Path) -> tuple[dict[str, Any], str]:
        raise OSError("/private/secret/runtime")

    monkeypatch.setattr(provision, "load_manifest", fail_load)
    assert provision.main(["verify-manifest"]) == 2
    captured = capsys.readouterr()
    assert "/private" not in captured.err
    assert json.loads(captured.err) == {"error": "provisioning_failed", "status": "error"}


def test_cli_redacts_hostile_arguments_and_survives_closed_streams(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/provision_perception_runtime.py"
    marker = os.fspath(tmp_path / "private-token-never-print")
    invalid_command = [sys.executable, os.fspath(script), "verify-manifest", marker]
    invalid = subprocess.run(
        invalid_command,
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert invalid.returncode == 2
    assert invalid.stdout == b""
    assert json.loads(invalid.stderr) == {"error": "invalid_arguments", "status": "error"}
    assert marker.encode() not in invalid.stdout + invalid.stderr

    success_command = [sys.executable, os.fspath(script), "verify-manifest"]
    redirections = ["exec 1>&-; exec 2>&-"]
    if Path("/dev/full").is_char_device():
        redirections.insert(0, "exec 1>/dev/full")
    for command in (success_command, invalid_command):
        for redirection in redirections:
            broken = subprocess.run(
                ["/bin/bash", "-c", f'{redirection}; exec "$@"', "bash", *command],
                check=False,
                capture_output=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                timeout=10,
            )
            assert broken.returncode == 2
            assert b"Traceback" not in broken.stderr
            assert b"Exception ignored" not in broken.stderr


def test_media_runtime_manifest_and_tree_are_both_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(provision, "_TRUSTED_RUNTIME_UID", tmp_path.stat().st_uid)
    media = tmp_path / "media"
    worker = media / "worker/worker.py"
    worker.parent.mkdir(parents=True)
    worker.write_bytes(b"worker")
    worker.chmod(0o444)
    tree_sha256 = provision._runtime_tree_digest(media, "visualworld-runtime.json")
    media_manifest = {
        "network_enabled": False,
        "runtime_id": "media-v1",
        "tree_sha256": tree_sha256,
    }
    raw = json.dumps(media_manifest, sort_keys=True).encode()
    manifest_path = media / "visualworld-runtime.json"
    manifest_path.write_bytes(raw)
    manifest_path.chmod(0o444)
    manifest = {
        "media_runtime": {
            "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "runtime_id": "media-v1",
            "tree_sha256": tree_sha256,
        }
    }

    provision.verify_media_runtime(media, manifest)
    external = tmp_path / "external-worker-link"
    external.hardlink_to(worker)
    with pytest.raises(provision.ProvisioningError, match="media_runtime_untrusted"):
        provision.verify_media_runtime(media, manifest)
    external.unlink()
    worker.chmod(0o644)
    worker.write_bytes(b"tampered")
    worker.chmod(0o444)
    with pytest.raises(provision.ProvisioningError, match="media_runtime_invalid"):
        provision.verify_media_runtime(media, manifest)


def test_media_runtime_rejects_symlinks_outside_the_bound_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(provision, "_TRUSTED_RUNTIME_UID", tmp_path.stat().st_uid)
    media = tmp_path / "media"
    media.mkdir()
    media.chmod(0o755)
    external = tmp_path / "external"
    external.write_bytes(b"unbound")
    escaped = media / "escaped"
    escaped.symlink_to("../external")
    tree_sha256 = "0" * 64
    media_manifest = {
        "network_enabled": False,
        "runtime_id": "media-v1",
        "tree_sha256": tree_sha256,
    }
    raw = json.dumps(media_manifest, sort_keys=True).encode()
    manifest_path = media / "visualworld-runtime.json"
    manifest_path.write_bytes(raw)
    manifest_path.chmod(0o444)
    manifest = {
        "media_runtime": {
            "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "runtime_id": "media-v1",
            "tree_sha256": tree_sha256,
        }
    }

    with pytest.raises(provision.ProvisioningError, match="media_runtime_invalid"):
        provision.verify_media_runtime(media, manifest)
