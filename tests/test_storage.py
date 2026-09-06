# SPDX-License-Identifier: Apache-2.0
"""Contract, recovery, security, and audit tests for the local artifact CAS."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import os
import stat
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import visualworld.storage as storage
from visualworld.ingestion import Artifact
from visualworld.ports import EvidenceStore, FakeEvidenceStore, PortError, PortErrorCode, PortKind
from visualworld.storage import (
    ArtifactCheck,
    ArtifactState,
    CommitDisposition,
    CommitResult,
    DeleteDisposition,
    InventoryEntry,
    InventoryKind,
    InventoryPage,
    LocalEvidenceStore,
    StageHandle,
    StagingCleanupHandle,
)

RUN_ID = "run_" + "1" * 64


def _artifact(content: bytes) -> Artifact:
    return Artifact(hashlib.sha256(content).hexdigest(), str(len(content)))


def _store(tmp_path: Path, *, maximum: int = 1024 * 1024) -> LocalEvidenceStore:
    return LocalEvidenceStore(tmp_path / "store", max_payload_bytes=maximum)


def _artifact_path(root: Path, artifact: Artifact) -> Path:
    digest = artifact.sha256
    return root / "artifacts" / "v1" / "sha256" / digest[:2] / digest[2:4] / digest


def _staged_path(root: Path, staged: StageHandle) -> Path:
    return root / "staging" / "v1" / staged.run_id / staged.staging_name


def _prepare_artifact_parent(root: Path, artifact: Artifact) -> Path:
    destination = _artifact_path(root, artifact)
    destination.parent.parent.mkdir(mode=0o700)
    destination.parent.mkdir(mode=0o700)
    return destination


def _inventory(store: LocalEvidenceStore, *, limit: int = 2) -> tuple[InventoryEntry, ...]:
    entries: list[InventoryEntry] = []
    after: str | None = None
    while True:
        page = store.inventory(after=after, limit=limit)
        entries.extend(page.entries)
        if page.next_after is None:
            return tuple(entries)
        after = page.next_after


def test_initialization_creates_private_versioned_layout_and_probe_is_clean(tmp_path: Path) -> None:
    root = tmp_path / "store"

    store = LocalEvidenceStore(root, max_payload_bytes=1234)

    assert isinstance(store, EvidenceStore)
    assert store.descriptor.port is PortKind.EVIDENCE_STORE
    assert store.descriptor.implementation == "local-cas"
    assert store.descriptor.implementation_version == "1"
    assert store.descriptor.max_payload_bytes == 1234
    expected_modes = {
        root: 0o700,
        root / "writer.lock": 0o600,
        root / "artifacts": 0o700,
        root / "artifacts" / "v1": 0o700,
        root / "artifacts" / "v1" / "sha256": 0o700,
        root / "staging": 0o700,
        root / "staging" / "v1": 0o700,
    }
    for path, mode in expected_modes.items():
        assert stat.S_IMODE(path.stat().st_mode) == mode
    assert list((root / "staging" / "v1").iterdir()) == []
    assert list((root / "artifacts" / "v1" / "sha256").iterdir()) == []


def test_configuration_and_value_objects_reject_invalid_shapes(tmp_path: Path) -> None:
    artifact = _artifact(b"value")
    token = "inv_" + "0" * 64
    constructors: tuple[Callable[[], object], ...] = (
        lambda: LocalEvidenceStore(Path("relative")),
        lambda: LocalEvidenceStore(tmp_path / "a", max_payload_bytes=-1),
        lambda: LocalEvidenceStore(tmp_path / "b", max_inventory_entries=0),
        lambda: LocalEvidenceStore(tmp_path / "c", max_inventory_entries=1_000_001),
        lambda: LocalEvidenceStore(tmp_path / "d", max_inventory_bytes=-1),
        lambda: LocalEvidenceStore(tmp_path / "e", lock_timeout_ms=0),
        lambda: LocalEvidenceStore(tmp_path / "f", lock_timeout_ms=60_001),
    )
    for construct in constructors:
        with pytest.raises(PortError) as raised:
            construct()
        assert raised.value.code is PortErrorCode.INVALID_REQUEST

    invalid_values: tuple[Callable[[], object], ...] = (
        lambda: StageHandle(RUN_ID, "invalid", artifact),
        lambda: StageHandle(RUN_ID, "0" * 32 + ".part", cast(Artifact, object())),
        lambda: StagingCleanupHandle(RUN_ID, None, -1, 1, 0, 0, 0),
        lambda: StagingCleanupHandle(RUN_ID, "invalid", 1, 1, 0, 0, 0),
        lambda: CommitResult(cast(Artifact, object()), CommitDisposition.PROMOTED),
        lambda: CommitResult(artifact, cast(CommitDisposition, "promoted")),
        lambda: ArtifactCheck(cast(Artifact, object()), ArtifactState.VALID),
        lambda: ArtifactCheck(artifact, cast(ArtifactState, "valid")),
        lambda: InventoryEntry("invalid", InventoryKind.CORRUPT),
        lambda: InventoryEntry(token, cast(InventoryKind, "corrupt")),
        lambda: InventoryEntry(token, InventoryKind.ARTIFACT),
        lambda: InventoryPage(cast(tuple[InventoryEntry, ...], []), None),
        lambda: InventoryPage((), "invalid"),
    )
    for construct in invalid_values:
        with pytest.raises(ValueError):
            construct()

    assert "<redacted>" in repr(CommitResult(artifact, CommitDisposition.PROMOTED))


def test_new_store_root_is_synced_through_its_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    original = os.fsync
    synced: list[tuple[int, int]] = []

    def record_sync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        synced.append((metadata.st_dev, metadata.st_ino))
        original(descriptor)

    monkeypatch.setattr(os, "fsync", record_sync)

    LocalEvidenceStore(root)

    assert parent_identity in synced


def test_root_parent_sync_failure_is_retried_on_reinitialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    original = os.fsync
    failed = False

    def fail_parent_once(descriptor: int) -> None:
        nonlocal failed
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == parent_identity and not failed:
            failed = True
            raise OSError("private parent sync failure")
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fail_parent_once)
    with pytest.raises(PortError) as raised:
        LocalEvidenceStore(root)
    assert raised.value.code is PortErrorCode.UNSUPPORTED

    parent_syncs = 0

    def record_parent_sync(descriptor: int) -> None:
        nonlocal parent_syncs
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == parent_identity:
            parent_syncs += 1
        original(descriptor)

    monkeypatch.setattr(os, "fsync", record_parent_sync)
    LocalEvidenceStore(root)

    assert parent_syncs == 1


def test_put_get_and_deduplication_follow_exact_cas_layout(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content = b"deterministic private pixels"
    artifact = _artifact(content)

    assert store.put(artifact, content) == artifact
    first = _artifact_path(store.root, artifact)
    first_inode = first.stat().st_ino
    assert first.read_bytes() == content
    assert stat.S_IMODE(first.stat().st_mode) == 0o400

    assert store.put(artifact, content) == artifact
    assert first.stat().st_ino == first_inode
    assert store.get(artifact.sha256) == content
    assert list((store.root / "staging" / "v1").iterdir()) == []


def test_stage_and_commit_are_separate_durable_idempotent_steps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content = b"stage then commit"
    artifact = _artifact(content)

    staged = store.stage(RUN_ID, artifact, content)

    staged_path = _staged_path(store.root, staged)
    assert staged.artifact == artifact
    assert staged_path.read_bytes() == content
    assert stat.S_IMODE(staged_path.stat().st_mode) == 0o400
    assert not _artifact_path(store.root, artifact).exists()

    promoted = store.commit_stage(staged)
    assert promoted.artifact == artifact
    assert promoted.disposition is CommitDisposition.PROMOTED
    assert _artifact_path(store.root, artifact).read_bytes() == content
    assert not staged_path.exists()
    retried = store.commit_stage(staged)
    assert retried.artifact == artifact
    assert retried.disposition is CommitDisposition.ALREADY_COMMITTED


def test_stage_handle_round_trips_as_coordinator_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staged = store.stage(RUN_ID, _artifact(b"intent"), b"intent")

    assert StageHandle.from_mapping(staged.to_mapping()) == staged

    for invalid in ({}, {**staged.to_mapping(), "protocol_version": 2}):
        with pytest.raises(ValueError, match="invalid stage handle"):
            StageHandle.from_mapping(invalid)

    cleanup = StagingCleanupHandle(RUN_ID, "0" * 32 + ".part", 1, 2, 3, 4, 5)
    assert StagingCleanupHandle.from_mapping(cleanup.to_mapping()) == cleanup
    assert "<redacted>" in repr(cleanup)
    with pytest.raises(ValueError, match="invalid staging cleanup handle"):
        StagingCleanupHandle.from_mapping({**cleanup.to_mapping(), "device": -1})
    with pytest.raises(ValueError, match="invalid staging cleanup handle"):
        StagingCleanupHandle.from_mapping({})


def test_stage_and_cleanup_operation_boundaries_reject_noncanonical_values(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    staged = store.stage(RUN_ID, _artifact(b"stage"), b"stage")
    object.__setattr__(staged, "protocol_version", 2)

    operations: tuple[Callable[[], object], ...] = (
        lambda: store.commit_stage(cast(StageHandle, object())),
        lambda: store.commit_stage(staged),
        lambda: store.discard_incomplete(cast(StagingCleanupHandle, object())),
    )
    for operation in operations:
        with pytest.raises(PortError) as raised:
            operation()
        assert raised.value.code is PortErrorCode.INVALID_REQUEST


def test_commit_deduplicates_a_separately_staged_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = _artifact(b"same content")
    store.put(artifact, b"same content")
    staged = store.stage(RUN_ID, artifact, b"same content")
    inode = _artifact_path(store.root, artifact).stat().st_ino

    result = store.commit_stage(staged)

    assert result.disposition is CommitDisposition.DEDUPLICATED
    assert _artifact_path(store.root, artifact).stat().st_ino == inode
    assert not _staged_path(store.root, staged).exists()


def test_writer_session_holds_one_lock_across_coordinator_steps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    contender = LocalEvidenceStore(store.root, lock_timeout_ms=10)
    artifact = _artifact(b"coordinated")

    with store.writer_session() as writer:
        staged = writer.stage(RUN_ID, artifact, b"coordinated")
        assert writer.inspect((artifact,))[0].state is ArtifactState.MISSING
        assert any(entry.kind is InventoryKind.STAGED for entry in writer.inventory().entries)
        with pytest.raises(PortError) as raised:
            contender.get(artifact.sha256)
        assert raised.value.code is PortErrorCode.TIMEOUT
        assert writer.commit_stage(staged).disposition is CommitDisposition.PROMOTED
        assert writer.inspect((artifact,))[0].state is ArtifactState.VALID
        discarded = writer.stage("run_" + "2" * 64, _artifact(b"discard"), b"discard")
        assert writer.discard_stage(discarded) is DeleteDisposition.DELETED
        deleted_artifact = _artifact(b"delete")
        deleted = writer.stage("run_" + "3" * 64, deleted_artifact, b"delete")
        writer.commit_stage(deleted)
        assert writer.delete_artifact(deleted_artifact) is DeleteDisposition.DELETED

    assert store.get(artifact.sha256) == b"coordinated"
    with pytest.raises(PortError) as raised:
        writer.discard_stage(staged)
    assert raised.value.code is PortErrorCode.INVALID_REQUEST


def test_discard_stage_is_idempotent_and_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.stage(RUN_ID, _artifact(b"first"), b"first")
    second = store.stage(RUN_ID, _artifact(b"second"), b"second")

    assert store.discard_stage(first) is DeleteDisposition.DELETED
    assert store.discard_stage(first) is DeleteDisposition.ALREADY_ABSENT
    assert _staged_path(store.root, second).exists()


def test_inventory_cleanup_handles_recover_incomplete_files_and_empty_runs(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    staging = store.root / "staging" / "v1"
    partial_run = staging / RUN_ID
    partial_run.mkdir(mode=0o700)
    partial = partial_run / ("2" * 32 + ".part")
    partial.write_bytes(b"partial")
    partial.chmod(0o600)
    empty_run = staging / ("run_" + "3" * 64)
    empty_run.mkdir(mode=0o700)

    with store.writer_session() as writer:
        entries = writer.inventory(limit=20).entries
        incomplete = next(entry for entry in entries if entry.kind is InventoryKind.INCOMPLETE)
        empty = next(entry for entry in entries if entry.kind is InventoryKind.EMPTY_RUN)
        assert incomplete.cleanup is not None
        assert empty.cleanup is not None
        assert writer.discard_incomplete(incomplete.cleanup) is DeleteDisposition.DELETED
        assert writer.discard_incomplete(incomplete.cleanup) is DeleteDisposition.ALREADY_ABSENT
        assert writer.discard_incomplete(empty.cleanup) is DeleteDisposition.DELETED
        assert writer.discard_incomplete(empty.cleanup) is DeleteDisposition.ALREADY_ABSENT

    assert not partial.exists()
    assert not partial_run.exists()
    assert not empty_run.exists()


def test_stale_cleanup_handle_never_deletes_replacement_if_inode_is_reused(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = store.root / "staging" / "v1" / RUN_ID
    run.mkdir(mode=0o700)
    partial = run / ("2" * 32 + ".part")
    partial.write_bytes(b"old")
    partial.chmod(0o600)
    entry = next(item for item in _inventory(store) if item.kind is InventoryKind.INCOMPLETE)
    assert entry.cleanup is not None
    partial.unlink()
    partial.write_bytes(b"replacement")
    partial.chmod(0o600)
    replacement = partial.stat()
    # Deterministically model a filesystem that immediately recycles the inode.
    object.__setattr__(entry.cleanup, "device", replacement.st_dev)
    object.__setattr__(entry.cleanup, "inode", replacement.st_ino)

    with pytest.raises(PortError) as raised:
        store.discard_incomplete(entry.cleanup)

    assert raised.value.code is PortErrorCode.CORRUPT
    assert partial.read_bytes() == b"replacement"


def test_mutated_cleanup_handle_cannot_escape_the_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.root / "staging" / "v1" / RUN_ID
    run.mkdir(mode=0o700)
    partial = run / ("2" * 32 + ".part")
    partial.write_bytes(b"partial")
    partial.chmod(0o600)
    entry = next(item for item in _inventory(store) if item.kind is InventoryKind.INCOMPLETE)
    assert entry.cleanup is not None
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o700)
    outside = victim / "private.part"
    outside.write_bytes(b"outside")
    outside.chmod(0o600)
    outside_metadata = outside.stat()
    object.__setattr__(entry.cleanup, "run_id", "../../../victim")
    object.__setattr__(entry.cleanup, "staging_name", "private.part")
    object.__setattr__(entry.cleanup, "device", outside_metadata.st_dev)
    object.__setattr__(entry.cleanup, "inode", outside_metadata.st_ino)

    with pytest.raises(PortError) as raised:
        store.discard_incomplete(entry.cleanup)

    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert outside.read_bytes() == b"outside"


def test_cleanup_handle_refuses_occupied_or_replaced_empty_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging = store.root / "staging" / "v1"
    run = staging / RUN_ID
    run.mkdir(mode=0o700)
    entry = next(item for item in _inventory(store) if item.kind is InventoryKind.EMPTY_RUN)
    assert entry.cleanup is not None
    (run / "unknown").write_bytes(b"occupied")

    with pytest.raises(PortError) as occupied:
        store.discard_incomplete(entry.cleanup)
    assert occupied.value.code is PortErrorCode.CONFLICT

    (run / "unknown").unlink()
    object.__setattr__(entry.cleanup, "inode", entry.cleanup.inode + 1)
    with pytest.raises(PortError) as replaced:
        store.discard_incomplete(entry.cleanup)
    assert replaced.value.code is PortErrorCode.CORRUPT


def test_incomplete_cleanup_retry_resyncs_a_still_occupied_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.root / "staging" / "v1" / RUN_ID
    run.mkdir(mode=0o700)
    first = run / ("2" * 32 + ".part")
    second = run / ("3" * 32 + ".part")
    for path in (first, second):
        path.write_bytes(path.name.encode("ascii"))
        path.chmod(0o600)
    cleanup = next(
        item.cleanup
        for item in _inventory(store)
        if item.kind is InventoryKind.INCOMPLETE
        and item.cleanup is not None
        and item.cleanup.staging_name == first.name
    )

    assert store.discard_incomplete(cleanup) is DeleteDisposition.DELETED
    assert store.discard_incomplete(cleanup) is DeleteDisposition.ALREADY_ABSENT
    assert second.exists()


def test_store_local_delete_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content = b"delete me"
    artifact = _artifact(content)
    store.put(artifact, content)

    assert store.delete_artifact(artifact) is DeleteDisposition.DELETED
    assert store.delete_artifact(artifact) is DeleteDisposition.ALREADY_ABSENT
    with pytest.raises(PortError) as raised:
        store.get(artifact.sha256)
    assert raised.value.code is PortErrorCode.NOT_FOUND


def test_inspect_and_paginated_inventory_enable_external_orphan_reconciliation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    valid = _artifact(b"valid")
    orphan = _artifact(b"orphan")
    missing = _artifact(b"missing")
    pending = _artifact(b"pending")
    store.put(valid, b"valid")
    store.put(orphan, b"orphan")
    staged = store.stage(RUN_ID, pending, b"pending")

    checks = store.inspect((valid, missing))
    entries = _inventory(store, limit=1)

    assert tuple(check.state for check in checks) == (ArtifactState.VALID, ArtifactState.MISSING)
    artifacts = {
        entry.artifact.sha256
        for entry in entries
        if entry.kind is InventoryKind.ARTIFACT and entry.artifact is not None
    }
    referenced = {valid.sha256, missing.sha256}
    assert artifacts - referenced == {orphan.sha256}
    assert [entry.stage for entry in entries if entry.kind is InventoryKind.STAGED] == [staged]
    assert len({entry.token for entry in entries}) == len(entries)


def test_audit_is_bounded_and_invalid_cursors_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = _artifact(b"four")
    store.put(artifact, b"four")

    entry_bounded = LocalEvidenceStore(
        store.root,
        max_inventory_entries=2,
        max_inventory_bytes=1024,
    )
    byte_bounded = LocalEvidenceStore(
        store.root,
        max_inventory_entries=100,
        max_inventory_bytes=3,
    )
    for operation in (
        entry_bounded.inventory,
        byte_bounded.inventory,
        lambda: byte_bounded.inspect((artifact,)),
    ):
        with pytest.raises(PortError) as raised:
            operation()
        assert raised.value.code is PortErrorCode.LIMIT_EXCEEDED

    with pytest.raises(PortError) as raised:
        store.inventory(after="inv_" + "f" * 64)
    assert raised.value.code is PortErrorCode.INVALID_REQUEST


def test_staging_inventory_streams_with_maximum_supported_payload_limit(tmp_path: Path) -> None:
    store = LocalEvidenceStore(
        tmp_path / "store",
        max_payload_bytes=2**63 - 1,
        max_inventory_bytes=1024,
    )
    run = store.root / "staging" / "v1" / RUN_ID
    run.mkdir(mode=0o700)
    staged = run / ("2" * 32 + ".part")
    staged.write_bytes(b"bounded")
    staged.chmod(0o400)

    entry = next(item for item in _inventory(store) if item.kind is InventoryKind.STAGED)

    assert entry.stage is not None
    assert entry.stage.artifact == _artifact(b"bounded")


def test_coordinator_and_audit_representations_redact_identifiers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content = b"private evidence"
    artifact = _artifact(content)
    staged = store.stage(RUN_ID, artifact, content)
    page = store.inventory(limit=1)
    entry = next(item for item in _inventory(store) if item.kind is InventoryKind.STAGED)
    check = store.inspect((artifact,))[0]

    for value in (staged, entry, check, page):
        rendered = repr(value)
        assert artifact.sha256 not in rendered
        assert staged.staging_name not in rendered
        assert staged.run_id not in rendered


def test_inspection_and_inventory_report_corruption_incomplete_and_unknown_without_importing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    corrupt = _artifact(b"original")
    store.put(corrupt, b"original")
    _artifact_path(store.root, corrupt).chmod(0o600)
    run = store.root / "staging" / "v1" / RUN_ID
    run.mkdir(mode=0o700)
    (run / ("2" * 32 + ".part")).write_bytes(b"partial")
    (run / ("2" * 32 + ".part")).chmod(0o600)
    (store.root / "staging" / "v1" / "unexpected").write_bytes(b"unknown")

    checks = store.inspect((corrupt,))
    entries = _inventory(store)

    assert checks[0].state is ArtifactState.CORRUPT
    assert sum(entry.kind is InventoryKind.INCOMPLETE for entry in entries) == 1
    assert sum(entry.kind is InventoryKind.INVALID for entry in entries) == 1
    assert all(entry.kind is not InventoryKind.STAGED for entry in entries)
    assert _artifact_path(store.root, corrupt).exists()


def test_inventory_reports_invalid_objects_at_each_known_layout_level(tmp_path: Path) -> None:
    store = _store(tmp_path, maximum=4)
    artifacts = store.root / "artifacts" / "v1" / "sha256"
    staging = store.root / "staging" / "v1"

    (artifacts / "not-a-shard").write_bytes(b"unknown")
    bad_first = artifacts / "aa"
    bad_first.mkdir(mode=0o700)
    bad_first.chmod(0o755)
    good_first = artifacts / "bb"
    good_first.mkdir(mode=0o700)
    bad_second = good_first / "cc"
    bad_second.mkdir(mode=0o700)
    bad_second.chmod(0o755)
    good_second = good_first / "dd"
    good_second.mkdir(mode=0o700)
    (good_second / "not-a-digest").write_bytes(b"unknown")
    corrupt_digest = "bbdd" + "0" * 60
    corrupt_file = good_second / corrupt_digest
    corrupt_file.write_bytes(b"bad")
    corrupt_file.chmod(0o400)

    bad_run = staging / ("run_" + "4" * 64)
    bad_run.mkdir(mode=0o700)
    bad_run.chmod(0o755)
    good_run = staging / ("run_" + "5" * 64)
    good_run.mkdir(mode=0o700)
    (good_run / "bad-name").write_bytes(b"bad")
    oversized = good_run / ("6" * 32 + ".part")
    oversized.write_bytes(b"large")
    oversized.chmod(0o400)

    entries = _inventory(store, limit=20)

    assert sum(entry.kind is InventoryKind.INVALID for entry in entries) >= 5
    assert sum(entry.kind is InventoryKind.CORRUPT for entry in entries) >= 2


def test_missing_store_or_lock_after_initialization_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lock = store.root / "writer.lock"
    lock.unlink()
    with pytest.raises(PortError) as raised:
        store.get("0" * 64)
    assert raised.value.code is PortErrorCode.CORRUPT

    reopened = _store(tmp_path)
    reopened.root.rename(tmp_path / "moved-store")
    with pytest.raises(PortError) as raised:
        reopened.get("0" * 64)
    assert raised.value.code is PortErrorCode.UNSUPPORTED


@pytest.mark.parametrize(
    ("artifact", "content", "maximum", "code"),
    [
        (_artifact(b"expected"), b"changed", 1024, PortErrorCode.CONFLICT),
        (_artifact(b"too large"), b"too large", 2, PortErrorCode.LIMIT_EXCEEDED),
        (cast(Artifact, object()), b"content", 1024, PortErrorCode.INVALID_REQUEST),
        (
            _artifact(b"content"),
            cast(bytes, bytearray(b"content")),
            1024,
            PortErrorCode.INVALID_REQUEST,
        ),
    ],
)
def test_put_rejects_invalid_or_mismatched_content_without_writing(
    tmp_path: Path,
    artifact: Artifact,
    content: bytes,
    maximum: int,
    code: PortErrorCode,
) -> None:
    store = _store(tmp_path, maximum=maximum)

    with pytest.raises(PortError) as raised:
        store.put(artifact, content)

    assert raised.value.code is code
    assert list((store.root / "artifacts" / "v1" / "sha256").iterdir()) == []
    assert list((store.root / "staging" / "v1").iterdir()) == []


@pytest.mark.parametrize("run_id", ["", "../escape", "run_" + "g" * 64, "run_" + "0" * 63])
def test_stage_rejects_invalid_run_identifiers(tmp_path: Path, run_id: str) -> None:
    store = _store(tmp_path)

    with pytest.raises(PortError) as raised:
        store.stage(run_id, _artifact(b"content"), b"content")

    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    if run_id:
        assert run_id not in str(raised.value)
    assert not (tmp_path / "escape").exists()


def test_exact_builtin_validation_blocks_path_and_payload_subclasses(tmp_path: Path) -> None:
    store = _store(tmp_path)
    fake = FakeEvidenceStore(max_payload_bytes=0)

    class SlicedTraversal(str):
        def __getitem__(self, key: object) -> str:
            return "../outside"

    class LyingBytes(bytes):
        def __len__(self) -> int:
            return 0

    content = LyingBytes(b"secret")
    hostile_artifact = Artifact(SlicedTraversal("0" * 64), "6")

    for operation in (
        lambda: store.get(SlicedTraversal("0" * 64)),
        lambda: store.put(hostile_artifact, b"secret"),
        lambda: store.put(_artifact(b"secret"), content),
        lambda: fake.put(Artifact(hashlib.sha256(content).hexdigest(), "0"), content),
    ):
        with pytest.raises(PortError) as raised:
            operation()
        assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert not (tmp_path / "outside").exists()


def test_mutated_stage_handles_are_revalidated_before_any_filesystem_access(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    artifact = _artifact(b"private")
    staged = store.stage(RUN_ID, artifact, b"private")
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o700)
    outside = victim / "private.part"
    outside.write_bytes(b"private")
    outside.chmod(0o400)
    object.__setattr__(staged, "run_id", "../../../victim")
    object.__setattr__(staged, "staging_name", "private.part")

    for operation in (store.commit_stage, store.discard_stage):
        with pytest.raises(PortError) as raised:
            operation(staged)
        assert raised.value.code is PortErrorCode.INVALID_REQUEST

    assert outside.read_bytes() == b"private"
    assert victim.exists()


def test_mutated_artifacts_fail_as_redacted_invalid_requests(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = _artifact(b"private")
    object.__setattr__(artifact, "bytes", "private-byte-count")

    with pytest.raises(PortError) as raised:
        store.delete_artifact(artifact)

    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert "private-byte-count" not in repr(raised.value)


def test_get_and_delete_reject_invalid_digests_without_echoing_them(tmp_path: Path) -> None:
    store = _store(tmp_path)
    private = "../private-artifact"

    for operation in (store.get, lambda _: store.delete_artifact(cast(Artifact, private))):
        with pytest.raises(PortError) as raised:
            operation(private)
        assert raised.value.code is PortErrorCode.INVALID_REQUEST
        assert private not in str(raised.value)


@pytest.mark.parametrize("after_write", [False, True])
def test_interrupted_stage_removes_partial_and_closes_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    after_write: bool,
) -> None:
    store = _store(tmp_path)
    original = storage._write_all
    captured: int | None = None

    def interrupt(stream: storage.BinaryFile, content: bytes) -> str:
        nonlocal captured
        captured = stream.fileno()
        if after_write:
            original(stream, content)
        raise KeyboardInterrupt

    monkeypatch.setattr(storage, "_write_all", interrupt)
    with pytest.raises(KeyboardInterrupt):
        store.stage(RUN_ID, _artifact(b"content"), b"content")

    assert captured is not None
    with pytest.raises(OSError):
        os.fstat(captured)
    assert _inventory(store) == ()


def test_commit_retry_recovers_when_rename_completed_before_interruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    content = b"atomic rename"
    artifact = _artifact(content)
    staged = store.stage(RUN_ID, artifact, content)
    original = os.rename
    interrupted = False

    def rename_then_interrupt(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal interrupted
        original(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "rename", rename_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        store.commit_stage(staged)
    monkeypatch.setattr(os, "rename", original)

    assert store.commit_stage(staged).disposition is CommitDisposition.ALREADY_COMMITTED
    assert store.get(artifact.sha256) == content


def test_commit_retry_recovers_after_post_rename_sync_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    artifact = _artifact(b"sync recovery")
    staged = store.stage(RUN_ID, artifact, b"sync recovery")
    destination = _artifact_path(store.root, artifact)
    original = os.fsync
    failed = False

    def fail_after_rename(descriptor: int) -> None:
        nonlocal failed
        if destination.exists() and not failed:
            failed = True
            raise OSError("private sync failure")
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fail_after_rename)
    with pytest.raises(PortError) as raised:
        store.commit_stage(staged)
    assert raised.value.code is PortErrorCode.STORAGE_FAILED
    destination_parent_identity = (
        destination.parent.stat().st_dev,
        destination.parent.stat().st_ino,
    )
    run = store.root / "staging" / "v1" / staged.run_id
    run_identity = (run.stat().st_dev, run.stat().st_ino)
    staging = store.root / "staging" / "v1"
    staging_identity = (staging.stat().st_dev, staging.stat().st_ino)
    synced: list[tuple[int, int]] = []

    def record_sync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        synced.append((metadata.st_dev, metadata.st_ino))
        original(descriptor)

    monkeypatch.setattr(os, "fsync", record_sync)

    assert store.commit_stage(staged).disposition is CommitDisposition.ALREADY_COMMITTED
    assert destination_parent_identity in synced
    assert run_identity in synced
    assert staging_identity in synced
    assert store.get(artifact.sha256) == b"sync recovery"


def test_commit_detects_synthetic_hash_collision_without_replacing_final(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    digest = "a" * 64

    class ConstantHash:
        def __init__(self, content: bytes = b"") -> None:
            self._content = bytearray(content)

        def update(self, content: bytes | memoryview) -> None:
            self._content.extend(content)

        def hexdigest(self) -> str:
            return digest

    monkeypatch.setattr("visualworld.storage.hashlib.sha256", ConstantHash)
    artifact = Artifact(digest, "4")
    store.put(artifact, b"left")
    staged = store.stage(RUN_ID, artifact, b"rite")

    with pytest.raises(PortError) as raised:
        store.commit_stage(staged)

    assert raised.value.code is PortErrorCode.CONFLICT
    assert _artifact_path(store.root, artifact).read_bytes() == b"left"
    assert _staged_path(store.root, staged).read_bytes() == b"rite"


@pytest.mark.parametrize("failure_point", ["fchmod", "fsync"])
def test_stage_io_failure_removes_only_owned_partial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    store = _store(tmp_path)
    original_fsync = os.fsync

    def fail(*_: object, **__: object) -> None:
        raise OSError("private backend failure")

    if failure_point == "fsync":

        def fail_file_fsync(descriptor: int) -> None:
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                fail()
            original_fsync(descriptor)

        monkeypatch.setattr(os, "fsync", fail_file_fsync)
    else:
        monkeypatch.setattr(os, failure_point, fail)
    with pytest.raises(PortError) as raised:
        store.stage(RUN_ID, _artifact(b"content"), b"content")

    assert raised.value.code is PortErrorCode.STORAGE_FAILED
    run = store.root / "staging" / "v1" / RUN_ID
    assert not run.exists() or list(run.iterdir()) == []


def test_stage_close_failure_does_not_close_reused_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim")
    released: int | None = None
    victim_descriptor: int | None = None

    class ErrorAfterRelease(io.FileIO):
        def close(self) -> None:
            nonlocal released, victim_descriptor
            if not self.closed:
                released = self.fileno()
                super().close()
                victim_descriptor = os.open(victim, os.O_RDONLY | os.O_CLOEXEC)
                if victim_descriptor != released:
                    os.dup2(victim_descriptor, released, inheritable=False)
                    os.close(victim_descriptor)
                    victim_descriptor = released
                raise OSError("private close failure")
            super().close()

    monkeypatch.setattr("visualworld.storage.io.FileIO", ErrorAfterRelease)
    with pytest.raises(PortError) as raised:
        store.stage(RUN_ID, _artifact(b"content"), b"content")

    assert raised.value.code is PortErrorCode.STORAGE_FAILED
    assert victim_descriptor == released
    assert victim_descriptor is not None
    assert os.fstat(victim_descriptor).st_ino == victim.stat().st_ino
    os.close(victim_descriptor)


def test_ambiguous_writer_lock_close_poisons_store_and_never_reports_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    artifact = _artifact(b"locked")
    store.put(artifact, b"locked")
    lock_metadata = (store.root / "writer.lock").stat()
    lock_identity = (lock_metadata.st_dev, lock_metadata.st_ino)
    original_file_io = io.FileIO

    class ErrorBeforeRelease(io.FileIO):
        def close(self) -> None:
            if not self.closed:
                metadata = os.fstat(self.fileno())
                if (metadata.st_dev, metadata.st_ino) == lock_identity:
                    raise OSError("private close failure")
            super().close()

    monkeypatch.setattr("visualworld.storage.io.FileIO", ErrorBeforeRelease)

    with pytest.raises(PortError) as raised:
        store.get(artifact.sha256)
    assert raised.value.code is PortErrorCode.STORAGE_FAILED
    with pytest.raises(PortError) as poisoned:
        store.delete_artifact(artifact)
    assert poisoned.value.code is PortErrorCode.STORAGE_FAILED
    assert _artifact_path(store.root, artifact).exists()

    retained = store._retained_lock_streams
    assert len(retained) == 1
    original_file_io.close(retained[0])


def test_staging_name_collision_retries_are_bounded_and_non_destructive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = store.root / "staging" / "v1" / RUN_ID
    run.mkdir(mode=0o700)
    existing = run / ("f" * 32 + ".part")
    existing.write_bytes(b"existing")
    existing.chmod(0o600)
    monkeypatch.setattr("visualworld.storage.secrets.token_hex", lambda _: "f" * 32)

    with pytest.raises(PortError) as raised:
        store.stage(RUN_ID, _artifact(b"new"), b"new")

    assert raised.value.code is PortErrorCode.STORAGE_FAILED
    assert existing.read_bytes() == b"existing"


def test_corrupt_existing_artifact_fails_closed_without_overwrite(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content = b"expected"
    artifact = _artifact(content)
    store.put(artifact, content)
    destination = _artifact_path(store.root, artifact)
    destination.chmod(0o600)
    destination.write_bytes(b"tampered")
    destination.chmod(0o400)

    for operation in (
        lambda: store.get(artifact.sha256),
        lambda: store.put(artifact, content),
    ):
        with pytest.raises(PortError) as raised:
            operation()
        assert raised.value.code is PortErrorCode.CORRUPT
    assert destination.read_bytes() == b"tampered"


def test_symlink_components_and_files_never_escape_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content = b"private pixels"
    artifact = _artifact(content)
    digest = artifact.sha256
    outside = tmp_path / "outside"
    outside.mkdir()
    first = store.root / "artifacts" / "v1" / "sha256" / digest[:2]
    first.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PortError) as raised:
        store.put(artifact, content)
    assert raised.value.code is PortErrorCode.CORRUPT
    assert list(outside.iterdir()) == []

    first.unlink()
    destination = _prepare_artifact_parent(store.root, artifact)
    target = outside / "target"
    target.write_bytes(b"outside")
    destination.symlink_to(target)
    with pytest.raises(PortError) as raised:
        store.get(digest)
    assert raised.value.code is PortErrorCode.CORRUPT
    assert target.read_bytes() == b"outside"


def test_symlink_root_and_lock_are_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(PortError) as raised:
        LocalEvidenceStore(linked)
    assert raised.value.code is PortErrorCode.UNSUPPORTED

    root = tmp_path / "store"
    root.mkdir(mode=0o700)
    target = tmp_path / "lock-target"
    target.write_bytes(b"untouched")
    (root / "writer.lock").symlink_to(target)
    with pytest.raises(PortError) as raised:
        LocalEvidenceStore(root)
    assert raised.value.code is PortErrorCode.UNSUPPORTED
    assert target.read_bytes() == b"untouched"


def test_existing_lock_permissions_are_rejected_without_repair(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lock = store.root / "writer.lock"
    lock.chmod(0o644)

    with pytest.raises(PortError) as raised:
        LocalEvidenceStore(store.root)

    assert raised.value.code is PortErrorCode.UNSUPPORTED
    assert stat.S_IMODE(lock.stat().st_mode) == 0o644


def test_initialization_reconciles_only_valid_reserved_probe_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    staging_probe = store.root / "staging" / "v1" / ".visualworld-probe.part"
    destination_probe = store.root / "artifacts" / "v1" / ".visualworld-probe.ready"
    for probe in (staging_probe, destination_probe):
        probe.write_bytes(b"stale")
        probe.chmod(0o600)

    LocalEvidenceStore(store.root)

    assert not staging_probe.exists()
    assert not destination_probe.exists()
    staging_probe.write_bytes(b"foreign")
    staging_probe.chmod(0o400)
    with pytest.raises(PortError) as raised:
        LocalEvidenceStore(store.root)
    assert raised.value.code is PortErrorCode.UNSUPPORTED
    assert staging_probe.read_bytes() == b"foreign"


def test_initialization_probe_failure_is_redacted_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"

    def unsupported_rename(*_: object, **__: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(os, "rename", unsupported_rename)
    with pytest.raises(PortError) as raised:
        LocalEvidenceStore(root)

    assert raised.value.code is PortErrorCode.UNSUPPORTED
    assert list((root / "staging" / "v1").iterdir()) == []
    assert "NotImplementedError" not in repr(raised.value)


def test_lock_contention_times_out_as_retryable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store = LocalEvidenceStore(store.root, lock_timeout_ms=10)
    descriptor = os.open(store.root / "writer.lock", os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PortError) as raised:
            store.get("0" * 64)
    finally:
        os.close(descriptor)

    assert raised.value.code is PortErrorCode.TIMEOUT
    assert raised.value.retryable is True


@pytest.mark.parametrize(
    ("failure", "expected", "retryable"),
    [
        (errno.EACCES, PortErrorCode.TIMEOUT, True),
        (errno.EIO, PortErrorCode.STORAGE_FAILED, False),
    ],
)
def test_lock_errors_have_stable_port_mappings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: int,
    expected: PortErrorCode,
    retryable: bool,
) -> None:
    store = LocalEvidenceStore(tmp_path / "store", lock_timeout_ms=1)

    def fail_lock(*_: object, **__: object) -> None:
        raise OSError(failure, "private backend detail")

    monkeypatch.setattr(fcntl, "flock", fail_lock)
    with pytest.raises(PortError) as raised:
        store.get("0" * 64)

    assert raised.value.code is expected
    assert raised.value.retryable is retryable
    assert "private backend detail" not in repr(raised.value)


def test_fifo_and_hardlink_artifacts_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    fifo_artifact = _artifact(b"fifo")
    fifo = _prepare_artifact_parent(store.root, fifo_artifact)
    os.mkfifo(fifo, mode=0o400)

    with pytest.raises(PortError) as raised:
        store.get(fifo_artifact.sha256)
    assert raised.value.code is PortErrorCode.CORRUPT

    artifact = _artifact(b"linked")
    store.put(artifact, b"linked")
    outside = tmp_path / "outside-hardlink"
    os.link(_artifact_path(store.root, artifact), outside)
    with pytest.raises(PortError) as raised:
        store.delete_artifact(artifact)
    assert raised.value.code is PortErrorCode.CORRUPT
    assert outside.read_bytes() == b"linked"


def test_delete_retry_recovers_after_directory_sync_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    artifact = _artifact(b"delete recovery")
    store.put(artifact, b"delete recovery")
    original = os.fsync

    def fail_sync(_: int) -> None:
        raise OSError("private sync failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(PortError) as raised:
        store.delete_artifact(artifact)
    assert raised.value.code is PortErrorCode.STORAGE_FAILED
    parent = _artifact_path(store.root, artifact).parent
    parent_identity = (parent.stat().st_dev, parent.stat().st_ino)
    synced: list[tuple[int, int]] = []

    def record_sync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        synced.append((metadata.st_dev, metadata.st_ino))
        original(descriptor)

    monkeypatch.setattr(os, "fsync", record_sync)

    assert store.delete_artifact(artifact) is DeleteDisposition.ALREADY_ABSENT
    assert parent_identity in synced


def test_discard_retry_recovers_after_directory_sync_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    staged = store.stage(RUN_ID, _artifact(b"discard recovery"), b"discard recovery")
    run = _staged_path(store.root, staged).parent
    run_identity = (run.stat().st_dev, run.stat().st_ino)
    staging = run.parent
    staging_identity = (staging.stat().st_dev, staging.stat().st_ino)
    original = os.fsync

    def fail_sync(_: int) -> None:
        raise OSError("private sync failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(PortError) as raised:
        store.discard_stage(staged)
    assert raised.value.code is PortErrorCode.STORAGE_FAILED
    synced: list[tuple[int, int]] = []

    def record_sync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        synced.append((metadata.st_dev, metadata.st_ino))
        original(descriptor)

    monkeypatch.setattr(os, "fsync", record_sync)

    assert store.discard_stage(staged) is DeleteDisposition.ALREADY_ABSENT
    assert run_identity in synced
    assert staging_identity in synced


def test_root_permissions_and_owner_are_enforced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    root.mkdir(mode=0o700)
    root.chmod(0o750)
    with pytest.raises(PortError) as raised:
        LocalEvidenceStore(root)
    assert raised.value.code is PortErrorCode.UNSUPPORTED

    root.chmod(0o700)
    monkeypatch.setattr(os, "geteuid", lambda: root.stat().st_uid + 1)
    with pytest.raises(PortError) as raised:
        LocalEvidenceStore(root)
    assert raised.value.code is PortErrorCode.UNSUPPORTED


def test_errors_and_tracebacks_never_echo_root_digest_or_content(tmp_path: Path) -> None:
    private_root = tmp_path / "private-root"
    store = LocalEvidenceStore(private_root)
    private_content = b"private-content"
    private_digest = hashlib.sha256(private_content).hexdigest()
    destination = _prepare_artifact_parent(private_root, _artifact(private_content))
    destination.write_bytes(b"wrong")

    with pytest.raises(PortError) as raised:
        store.get(private_digest)

    rendered = "".join(traceback.format_exception(raised.value))
    assert os.fspath(private_root) not in rendered
    assert private_digest not in rendered
    assert private_content.decode() not in rendered


def test_existing_root_reopens_without_changing_committed_artifacts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = _artifact(b"persistent")
    store.put(artifact, b"persistent")
    before = _artifact_path(store.root, artifact).stat()

    reopened = LocalEvidenceStore(store.root, max_payload_bytes=1024 * 1024)

    after = _artifact_path(store.root, artifact).stat()
    assert reopened.get(artifact.sha256) == b"persistent"
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
