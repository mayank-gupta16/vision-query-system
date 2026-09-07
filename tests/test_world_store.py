# SPDX-License-Identifier: Apache-2.0
"""Contract, migration, recovery, and security tests for local SQLite metadata."""

from __future__ import annotations

import hashlib
import inspect
import os
import sqlite3
import stat
import threading
import traceback
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import cast

import pytest

import visualworld.storage as filesystem
import visualworld.world_store as world_store
from visualworld.ingestion import (
    Artifact,
    EvidenceRef,
    Fingerprint,
    FrameRef,
    MediaTime,
    Rational,
    Record,
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
    dumps_record,
)
from visualworld.ports import Effect, PortError, PortErrorCode, PortKind, WorldStore
from visualworld.storage import EvidenceWriterSession, LocalEvidenceStore, StageHandle
from visualworld.world_store import LocalWorldStore, WorldStoreStats


def _records(
    *,
    frame_count: int = 3,
) -> tuple[Source, tuple[FrameRef, ...], tuple[EvidenceRef, ...], RunManifest, RunManifest]:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint("a0" * 32, "10"),
        (SourceStream(0, 16, 12, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(
            source.source_id,
            0,
            str(index),
            MediaTime(str(index * 100), time_base),
        )
        for index in range(frame_count)
    )
    evidence = tuple(
        EvidenceRef.create(
            frame.frame_id,
            Artifact(
                hashlib.sha256(f"pixels-{index}".encode()).hexdigest(),
                str(8 + len(str(index))),
            ),
            None,
        )
        for index, frame in enumerate(frames)
    )
    sampling = Sampling(Rational("5", "1"))
    preparing = RunManifest.create(source.source_id, (), sampling, "preparing")
    committed = RunManifest.create(
        source.source_id,
        (),
        sampling,
        "committed",
        RunOutputs(str(frame_count), hashlib.sha256(b"sample-index").hexdigest()),
    )
    return source, frames, evidence, preparing, committed


def _store(tmp_path: Path, **options: int) -> LocalWorldStore:
    return LocalWorldStore(tmp_path / "store", **options)


@contextmanager
def _database(root: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(root / "world.sqlite3", autocommit=True)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        yield connection
    finally:
        connection.close()


def test_initialization_creates_private_wal_schema_and_satisfies_port(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert isinstance(store, WorldStore)
    assert store.descriptor.port is PortKind.WORLD_STORE
    assert store.descriptor.implementation == "local-sqlite"
    assert store.descriptor.implementation_version == "1"
    assert store.descriptor.allowed_effects == ()
    for name in ("writer.lock", "world.sqlite3"):
        metadata = (store.root / name).stat()
        assert stat.S_ISREG(metadata.st_mode)
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        assert metadata.st_nlink == 1
    for name in ("world.sqlite3-wal", "world.sqlite3-shm"):
        path = store.root / name
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700

    with _database(store.root) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute(
            "SELECT schema_version, migration_name, code_sha256 FROM schema_migrations"
        ).fetchone() == (1, "v1_ingestion_metadata", world_store._MIGRATION_CHECKSUM)


@pytest.mark.parametrize(
    "options",
    [
        {"busy_timeout_ms": 0},
        {"busy_timeout_ms": 60_001},
        {"lock_timeout_ms": 0},
        {"lock_timeout_ms": 60_001},
        {"max_audit_records": 0},
        {"max_audit_records": 1_000_001},
    ],
)
def test_configuration_rejects_invalid_values(tmp_path: Path, options: dict[str, int]) -> None:
    with pytest.raises(PortError) as raised:
        _store(tmp_path, **options)
    assert raised.value.code is PortErrorCode.INVALID_REQUEST

    with pytest.raises(PortError, match="invalid_request"):
        LocalWorldStore(Path("relative"))

    with pytest.raises(ValueError, match="invalid world store statistics"):
        WorldStoreStats(-1, 0, 0)
    with pytest.raises(ValueError, match="unsupported migration"):
        LocalWorldStore._migration_statements(cast(LocalWorldStore, object()), 2)


def test_stable_contract_is_atomic_idempotent_ordered_and_reopenable(tmp_path: Path) -> None:
    source, frames, evidence, _, _ = _records()
    store = _store(tmp_path)
    batch: tuple[Record, ...] = (source, frames[2], evidence[1], frames[0], frames[1])

    store.commit(batch)
    store.commit(batch)

    assert store.get(source.source_id) == source
    assert store.get(frames[1].frame_id) == frames[1]
    assert store.list_frames(source.source_id, stream_index=0, limit=2) == frames[:2]
    assert (
        store.list_frames(
            source.source_id,
            stream_index=0,
            after_decode_index="1",
            limit=2,
        )
        == frames[2:]
    )
    assert store.list_evidence(frames[1].frame_id, limit=2) == (evidence[1],)
    assert store.verify() == WorldStoreStats(record_count=5, artifact_count=1, intent_count=0)

    reopened = LocalWorldStore(store.root)
    assert reopened.get(source.source_id) == source
    assert reopened.list_frames(source.source_id, stream_index=0, limit=3) == frames


def test_frame_paging_orders_the_complete_unsigned_64_bit_range(tmp_path: Path) -> None:
    source, _, _, _, _ = _records(frame_count=0)
    indexes = ("0", "2", "10", str(2**63), str(2**64 - 1))
    frames = tuple(
        FrameRef.create(
            source.source_id,
            0,
            value,
            MediaTime("0", TimeBase("1", "1000")),
        )
        for value in indexes
    )
    store = _store(tmp_path)
    store.commit((source, *reversed(frames)))

    assert store.list_frames(source.source_id, stream_index=0, limit=5) == frames
    assert (
        store.list_frames(
            source.source_id,
            stream_index=0,
            after_decode_index=str(2**63),
            limit=5,
        )
        == frames[-1:]
    )


def test_valid_source_stream_order_is_canonicalized_for_projection_checks(tmp_path: Path) -> None:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint("b0" * 32, "10"),
        (
            SourceStream(1, 20, 10, 0, time_base),
            SourceStream(0, 16, 12, 0, time_base),
        ),
    )
    store = _store(tmp_path)

    store.commit((source,))
    store.commit((source,))
    assert store.get(source.source_id) == source


def test_foreign_key_or_position_conflict_rolls_back_the_whole_batch(tmp_path: Path) -> None:
    source, frames, _, _, _ = _records()
    store = _store(tmp_path)
    orphan = FrameRef.create(
        "src_" + "0" * 64,
        0,
        "0",
        MediaTime("0", TimeBase("1", "1000")),
    )

    with pytest.raises(PortError, match="conflict"):
        store.commit((source, frames[0], orphan))
    with pytest.raises(PortError, match="not_found"):
        store.get(source.source_id)

    conflicting_position = FrameRef.create(
        source.source_id,
        0,
        frames[0].decode_index,
        MediaTime("1", TimeBase("1", "1000")),
    )
    store.commit((source, frames[0]))
    with pytest.raises(PortError, match="conflict"):
        store.commit((frames[1], conflicting_position))
    with pytest.raises(PortError, match="not_found"):
        store.get(frames[1].frame_id)


def test_batch_run_association_is_unambiguous_and_atomic(tmp_path: Path) -> None:
    source, frames, _, preparing, _ = _records()
    other = RunManifest.create(source.source_id, (), Sampling(Rational("1", "1")), "preparing")
    store = _store(tmp_path)

    store.commit((source, preparing, frames[0]))
    assert store.list_frames(source.source_id, stream_index=0, limit=1) == ()
    published = RunManifest.create(
        source.source_id,
        (),
        preparing.sampling,
        "committed",
        RunOutputs("1", hashlib.sha256(b"one-frame-index").hexdigest()),
    )
    store.finalize_run(published)
    assert store.list_frames(source.source_id, stream_index=0, limit=1) == frames[:1]

    ambiguous_store = LocalWorldStore(tmp_path / "ambiguous-store")
    with pytest.raises(PortError, match="invalid_request"):
        ambiguous_store.commit((source, preparing, other, frames[1]))
    with pytest.raises(PortError, match="not_found"):
        ambiguous_store.get(source.source_id)


def test_divergent_duplicate_identifier_in_one_batch_is_rejected(tmp_path: Path) -> None:
    source, frames, _, _, _ = _records()
    changed = FrameRef.create(
        frames[0].source_id,
        frames[0].stream_index,
        frames[0].decode_index,
        frames[0].pts,
        MediaTime("1", frames[0].pts.time_base),
    )
    store = _store(tmp_path)

    with pytest.raises(PortError, match="conflict"):
        store.commit((source, frames[0], changed))
    with pytest.raises(PortError, match="not_found"):
        store.get(source.source_id)


def test_same_identifier_with_different_nonidentity_content_is_a_conflict(tmp_path: Path) -> None:
    source, frames, _, _, _ = _records()
    store = _store(tmp_path)
    original = frames[0]
    changed = FrameRef.create(
        original.source_id,
        original.stream_index,
        original.decode_index,
        original.pts,
        MediaTime("1", original.pts.time_base),
    )
    assert changed.frame_id == original.frame_id

    store.commit((source, original))
    with pytest.raises(PortError, match="conflict"):
        store.commit((changed,))
    assert store.get(original.frame_id) == original


def test_idempotent_write_rejects_a_damaged_existing_projection(tmp_path: Path) -> None:
    source, frames, _, _, _ = _records()
    store = _store(tmp_path)
    store.commit((source, frames[0]))
    with _database(store.root) as connection:
        connection.execute(
            "UPDATE frames SET decode_index = '9' WHERE frame_id = ?",
            (frames[0].frame_id,),
        )

    with pytest.raises(PortError) as raised:
        store.commit((frames[0],))
    assert raised.value.code is PortErrorCode.CORRUPT


def test_preparing_run_batches_remain_hidden_until_atomic_finalization(tmp_path: Path) -> None:
    source, frames, evidence, preparing, committed = _records()
    store = _store(tmp_path)
    stages = tuple(
        StageHandle(
            preparing.run_id,
            f"{index:032x}.part",
            item.artifact,
        )
        for index, item in enumerate(evidence)
    )

    store.commit((source, preparing))
    store.record_artifact_intents(preparing.run_id, stages)
    store.record_artifact_intents(preparing.run_id, stages)
    store.commit_for_run(preparing.run_id, (*frames, *evidence))

    assert store.list_frames(source.source_id, stream_index=0, limit=3) == ()
    assert store.list_evidence(frames[0].frame_id, limit=2) == ()
    with pytest.raises(PortError, match="not_found"):
        store.get(frames[0].frame_id)
    with pytest.raises(PortError, match="not_found"):
        store.get(evidence[0].evidence_id)
    assert store.list_artifact_intents(preparing.run_id) == stages
    assert store.pending_runs() == (preparing,)

    store.finalize_run(committed)
    store.finalize_run(committed)

    assert store.get(preparing.run_id) == committed
    assert store.list_run_frames(preparing.run_id, limit=3) == frames
    assert store.list_run_evidence(preparing.run_id, limit=3) == tuple(
        sorted(evidence, key=lambda item: item.evidence_id)
    )
    assert store.list_frames(source.source_id, stream_index=0, limit=3) == frames
    assert store.list_evidence(frames[0].frame_id, limit=2) == evidence[:1]
    assert store.list_artifact_intents(preparing.run_id) == ()
    assert store.pending_runs() == ()


def test_run_owned_reads_are_ordered_paged_and_reject_hidden_runs(tmp_path: Path) -> None:
    source, frames, evidence, preparing, committed = _records()
    store = _store(tmp_path)
    store.commit((source, preparing))
    store.commit_for_run(preparing.run_id, (*reversed(frames), *reversed(evidence)))

    for method in (store.list_run_frames, store.list_run_evidence):
        with pytest.raises(PortError) as hidden:
            method(preparing.run_id, limit=3)
        assert hidden.value.code is PortErrorCode.NOT_FOUND

    store.finalize_run(committed)

    assert store.list_run_frames(preparing.run_id, limit=2) == frames[:2]
    assert (
        store.list_run_frames(
            preparing.run_id,
            after_stream_index=frames[1].stream_index,
            after_decode_index=frames[1].decode_index,
            limit=2,
        )
        == frames[2:]
    )
    ordered_evidence = tuple(sorted(evidence, key=lambda item: item.evidence_id))
    assert store.list_run_evidence(preparing.run_id, limit=2) == ordered_evidence[:2]
    assert (
        store.list_run_evidence(
            preparing.run_id,
            after_evidence_id=ordered_evidence[1].evidence_id,
            limit=2,
        )
        == ordered_evidence[2:]
    )

    with pytest.raises(PortError) as invalid_run:
        store.list_run_frames("run_invalid\x1b[31m", limit=1)
    assert invalid_run.value.code is PortErrorCode.INVALID_REQUEST
    with pytest.raises(PortError) as invalid_cursor:
        store.list_run_evidence(
            preparing.run_id,
            after_evidence_id="../escape",
            limit=1,
        )
    assert invalid_cursor.value.code is PortErrorCode.INVALID_REQUEST


def test_run_frame_cursor_is_complete_and_deterministic_across_streams(tmp_path: Path) -> None:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint("b1" * 32, "10"),
        (
            SourceStream(1, 16, 12, 0, time_base),
            SourceStream(0, 16, 12, 0, time_base),
        ),
    )
    frames = (
        FrameRef.create(source.source_id, 0, "0", MediaTime("0", time_base)),
        FrameRef.create(source.source_id, 1, "0", MediaTime("0", time_base)),
    )
    sampling = Sampling(Rational("5", "1"))
    preparing = RunManifest.create(source.source_id, (), sampling, "preparing")
    committed = RunManifest.create(
        source.source_id,
        (),
        sampling,
        "committed",
        RunOutputs("2", hashlib.sha256(b"two-stream-index").hexdigest()),
    )
    store = _store(tmp_path)
    store.commit((source, preparing))
    store.commit_for_run(preparing.run_id, tuple(reversed(frames)))
    store.finalize_run(committed)

    first = store.list_run_frames(preparing.run_id, limit=1)
    second = store.list_run_frames(
        preparing.run_id,
        after_stream_index=first[-1].stream_index,
        after_decode_index=first[-1].decode_index,
        limit=1,
    )

    assert first + second == frames
    with pytest.raises(PortError) as partial_cursor:
        store.list_run_frames(preparing.run_id, after_decode_index="0", limit=1)
    assert partial_cursor.value.code is PortErrorCode.INVALID_REQUEST
    with pytest.raises(PortError) as other_partial_cursor:
        store.list_run_frames(preparing.run_id, after_stream_index=0, limit=1)
    assert other_partial_cursor.value.code is PortErrorCode.INVALID_REQUEST


@pytest.mark.parametrize("record_kind", ["frame", "evidence"])
def test_run_owned_reads_hold_one_snapshot_across_concurrent_source_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_kind: str,
) -> None:
    source, frames, evidence, preparing, committed = _records()
    store = _store(tmp_path)
    store.commit((source, preparing))
    store.commit_for_run(preparing.run_id, (*frames, *evidence))
    store.finalize_run(committed)
    reached_snapshot = threading.Event()
    resume = threading.Event()
    original = LocalWorldStore._committed_run

    def pause_after_manifest(
        selected: LocalWorldStore,
        connection: sqlite3.Connection,
        run_id: str,
        operation: str,
    ) -> RunManifest:
        manifest = original(selected, connection, run_id, operation)
        reached_snapshot.set()
        if not resume.wait(timeout=5):
            raise TimeoutError
        return manifest

    monkeypatch.setattr(LocalWorldStore, "_committed_run", pause_after_manifest)
    pages: list[object] = []
    failures: list[BaseException] = []

    def read_page() -> None:
        try:
            if record_kind == "frame":
                pages.append(store.list_run_frames(preparing.run_id, limit=3))
            else:
                pages.append(store.list_run_evidence(preparing.run_id, limit=3))
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=read_page)
    thread.start()
    try:
        assert reached_snapshot.wait(timeout=5)
        store.begin_source_deletion(source.source_id, "del_" + "d" * 64)
    finally:
        resume.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert failures == []
    expected: object = (
        frames if record_kind == "frame" else tuple(sorted(evidence, key=lambda x: x.evidence_id))
    )
    assert pages == [expected]


@pytest.mark.parametrize("record_type", ["frame", "evidence"])
def test_run_owned_reads_fail_closed_on_dangling_associations(
    tmp_path: Path, record_type: str
) -> None:
    source, frames, evidence, preparing, committed = _records()
    store = _store(tmp_path)
    store.commit((source, preparing))
    store.commit_for_run(preparing.run_id, (*frames, *evidence))
    store.finalize_run(committed)
    prefix = "frm" if record_type == "frame" else "evi"
    with _database(store.root) as connection:
        connection.execute(
            """UPDATE run_records SET record_id = ?
            WHERE run_id = ? AND record_type = ? AND record_id = (
                SELECT record_id FROM run_records
                WHERE run_id = ? AND record_type = ? LIMIT 1
            )""",
            (
                prefix + "_" + "f" * 64,
                preparing.run_id,
                record_type,
                preparing.run_id,
                record_type,
            ),
        )

    method = store.list_run_frames if record_type == "frame" else store.list_run_evidence
    with pytest.raises(PortError) as corrupt:
        method(preparing.run_id, limit=3)
    assert corrupt.value.code is PortErrorCode.CORRUPT


def test_artifact_intents_are_recoverable_through_bounded_pages(tmp_path: Path) -> None:
    source, _, _, preparing, _ = _records(frame_count=0)
    store = _store(tmp_path)
    store.commit((source, preparing))
    stages = tuple(
        StageHandle(
            preparing.run_id,
            f"{index:032x}.part",
            Artifact(hashlib.sha256(f"intent-{index}".encode()).hexdigest(), "1"),
        )
        for index in range(70)
    )
    store.record_artifact_intents(preparing.run_id, stages[:64])
    store.record_artifact_intents(preparing.run_id, stages[64:])

    first = store.list_artifact_intents(preparing.run_id, limit=64)
    second = store.list_artifact_intents(
        preparing.run_id,
        after_staging_name=first[-1].staging_name,
        limit=64,
    )

    assert first + second == stages
    with pytest.raises(PortError, match="limit_exceeded"):
        LocalWorldStore(store.root, max_audit_records=2).verify()
    with pytest.raises(PortError, match="invalid_request"):
        store.list_artifact_intents(
            preparing.run_id,
            after_staging_name="../escape",
            limit=1,
        )


def test_finalization_requires_every_intent_to_have_an_owned_reference(tmp_path: Path) -> None:
    source, frames, evidence, preparing, committed = _records()
    store = _store(tmp_path)
    stage = StageHandle(preparing.run_id, "0" * 32 + ".part", evidence[0].artifact)
    store.commit((source, preparing))
    store.record_artifact_intents(preparing.run_id, (stage,))
    store.commit_for_run(preparing.run_id, (frames[0],))

    with pytest.raises(PortError, match="conflict"):
        store.finalize_run(committed)

    assert store.get(preparing.run_id) == preparing
    assert store.list_artifact_intents(preparing.run_id) == (stage,)
    assert store.list_frames(source.source_id, stream_index=0, limit=2) == ()


def test_run_batches_reject_foreign_frames_and_unowned_evidence(tmp_path: Path) -> None:
    source, frames, evidence, preparing, _ = _records()
    time_base = TimeBase("1", "1000")
    other_source = Source.create(
        Fingerprint("c0" * 32, "10"),
        (SourceStream(0, 16, 12, 0, time_base),),
    )
    other_frame = FrameRef.create(
        other_source.source_id,
        0,
        "0",
        MediaTime("0", time_base),
    )
    store = _store(tmp_path)
    store.commit((source, other_source, frames[0], other_frame))
    store.commit((preparing,))

    with pytest.raises(PortError, match="conflict"):
        store.commit_for_run(preparing.run_id, (other_frame,))
    with pytest.raises(PortError, match="conflict"):
        store.commit_for_run(preparing.run_id, (evidence[0],))
    with pytest.raises(PortError, match="not_found"):
        store.get(evidence[0].evidence_id)


def test_unowned_evidence_inherits_its_frame_visibility(tmp_path: Path) -> None:
    source, frames, evidence, preparing, _ = _records(frame_count=1)
    store = _store(tmp_path)
    store.commit((source, preparing, frames[0]))
    store.commit((evidence[0],))

    with pytest.raises(PortError, match="not_found"):
        store.get(evidence[0].evidence_id)
    assert store.list_evidence(frames[0].frame_id, limit=1) == ()


def test_failed_and_cancelled_runs_are_recoverable_but_committed_is_final(tmp_path: Path) -> None:
    source, _, _, preparing, committed = _records(frame_count=0)
    failed = RunManifest.create(source.source_id, (), preparing.sampling, "failed")
    cancelled = RunManifest.create(source.source_id, (), preparing.sampling, "cancelled")
    store = _store(tmp_path)

    store.commit((source, preparing))
    store.commit((failed,))
    assert store.pending_runs() == (failed,)
    store.commit((preparing,))
    store.commit((cancelled,))
    assert store.pending_runs() == (cancelled,)
    store.commit((preparing,))
    store.finalize_run(committed)

    with pytest.raises(PortError, match="conflict"):
        store.commit((failed,))
    assert store.get(committed.run_id) == committed


def test_pending_runs_are_recoverable_through_bounded_pages(tmp_path: Path) -> None:
    source, _, _, _, _ = _records(frame_count=0)
    runs = tuple(
        RunManifest.create(
            source.source_id,
            (),
            Sampling(Rational(str(index + 1), "1")),
            "preparing",
        )
        for index in range(70)
    )
    store = _store(tmp_path)
    store.commit((source, *runs[:63]))
    store.commit(runs[63:])

    expected = tuple(sorted(runs, key=lambda run: run.run_id))
    first = store.pending_runs(limit=64)
    second = store.pending_runs(after_run_id=first[-1].run_id, limit=64)

    assert first + second == expected
    with pytest.raises(PortError, match="invalid_request"):
        store.pending_runs(after_run_id="invalid", limit=1)
    with pytest.raises(PortError, match="invalid_request"):
        store.pending_runs(actionable_only=cast(bool, 1))


def test_intents_reject_forged_mismatched_and_colliding_handles(tmp_path: Path) -> None:
    source, _, evidence, preparing, _ = _records()
    store = _store(tmp_path)
    store.commit((source, preparing))
    first = StageHandle(preparing.run_id, "0" * 32 + ".part", evidence[0].artifact)
    collision = StageHandle(preparing.run_id, "1" * 32 + ".part", evidence[0].artifact)

    with pytest.raises(PortError, match="conflict"):
        store.record_artifact_intents("run_" + "0" * 64, (first,))
    store.record_artifact_intents(preparing.run_id, (first,))
    with pytest.raises(PortError, match="conflict"):
        store.record_artifact_intents(preparing.run_id, (collision,))

    object.__setattr__(first, "staging_name", "../escape")
    with pytest.raises(PortError, match="invalid_request"):
        store.record_artifact_intents(preparing.run_id, (first,))
    with pytest.raises(PortError, match="invalid_request"):
        store.record_artifact_intents(
            preparing.run_id,
            (cast(StageHandle, object()),),
        )

    with pytest.raises(PortError, match="limit_exceeded"):
        store.record_artifact_intents(
            preparing.run_id,
            cast(tuple[StageHandle, ...], []),
        )


def test_existing_intent_mismatch_and_nonpreparing_owner_are_rejected(tmp_path: Path) -> None:
    source, _, evidence, preparing, _ = _records()
    failed = RunManifest.create(source.source_id, (), preparing.sampling, "failed")
    stage = StageHandle(preparing.run_id, "0" * 32 + ".part", evidence[0].artifact)
    store = _store(tmp_path)
    store.commit((source, preparing))
    store.record_artifact_intents(preparing.run_id, (stage,))
    with _database(store.root) as connection:
        connection.execute(
            "UPDATE artifact_intents SET byte_count = '999' WHERE run_id = ?",
            (preparing.run_id,),
        )
    with pytest.raises(PortError, match="conflict"):
        store.record_artifact_intents(preparing.run_id, (stage,))

    with _database(store.root) as connection:
        encoded = dumps_record(failed)
        connection.execute(
            """UPDATE runs SET state = ?, record_json = ?, record_sha256 = ?
            WHERE run_id = ?""",
            (failed.state, encoded, hashlib.sha256(encoded).hexdigest(), failed.run_id),
        )
    with pytest.raises(PortError, match="conflict"):
        store.record_artifact_intents(preparing.run_id, ())


def test_run_extension_state_and_record_shapes_fail_closed(tmp_path: Path) -> None:
    source, frames, _, preparing, committed = _records()
    store = _store(tmp_path)

    with pytest.raises(PortError, match="conflict"):
        store.commit_for_run(preparing.run_id, (frames[0],))
    store.commit((source, preparing))
    with pytest.raises(PortError, match="invalid_request"):
        store.commit_for_run(preparing.run_id, (source,))
    with pytest.raises(PortError, match="invalid_request"):
        store.finalize_run(preparing)
    with pytest.raises(PortError, match="invalid_request"):
        store.finalize_run(committed, (source,))

    failed = RunManifest.create(source.source_id, (), preparing.sampling, "failed")
    store.commit((failed,))
    with pytest.raises(PortError, match="conflict"):
        store.commit_for_run(preparing.run_id, ())
    with pytest.raises(PortError, match="conflict"):
        store.finalize_run(committed)

    other_store = LocalWorldStore(tmp_path / "other-store")
    with pytest.raises(PortError, match="conflict"):
        other_store.finalize_run(committed)


def test_committed_run_rejects_a_different_retry_receipt(tmp_path: Path) -> None:
    source, _, _, preparing, committed = _records(frame_count=0)
    store = _store(tmp_path)
    store.commit((source, preparing))
    store.finalize_run(committed)
    changed = RunManifest.create(
        source.source_id,
        (),
        preparing.sampling,
        "committed",
        RunOutputs("0", hashlib.sha256(b"different-index").hexdigest()),
    )
    assert changed.run_id == committed.run_id

    with pytest.raises(PortError, match="conflict"):
        store.finalize_run(changed)


def test_commit_cannot_bypass_finalization_or_extend_a_committed_run(tmp_path: Path) -> None:
    source, frames, _, preparing, _ = _records()
    published = RunManifest.create(
        source.source_id,
        (),
        preparing.sampling,
        "committed",
        RunOutputs("1", hashlib.sha256(b"one-frame-index").hexdigest()),
    )
    store = _store(tmp_path)
    store.commit((source, preparing, frames[0]))

    with pytest.raises(PortError, match="conflict"):
        store.commit((published,))
    assert store.get(preparing.run_id) == preparing
    assert store.list_frames(source.source_id, stream_index=0, limit=2) == ()

    store.finalize_run(published)
    store.commit((published, frames[0]))
    with pytest.raises(PortError, match="conflict"):
        store.commit((published, frames[1]))
    with pytest.raises(PortError, match="conflict"):
        store.finalize_run(published, (frames[1],))
    with pytest.raises(PortError, match="not_found"):
        store.get(frames[1].frame_id)


def test_operation_boundaries_revalidate_mutated_records_without_raw_errors(tmp_path: Path) -> None:
    source, _, _, _, _ = _records()
    store = _store(tmp_path)
    object.__setattr__(source, "streams", cast(tuple[SourceStream, ...], []))

    with pytest.raises(PortError) as raised:
        store.commit((source,))
    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert raised.value.__cause__ is None

    with pytest.raises(PortError, match="invalid_request"):
        store.commit((cast(Record, object()),))
    with pytest.raises(PortError, match="limit_exceeded"):
        store.commit(cast(tuple[Record, ...], []))


@pytest.mark.parametrize(
    ("statement", "parameters", "lookup"),
    [
        (
            "UPDATE frames SET decode_index = '9' WHERE frame_id = ?",
            lambda frame: (frame.frame_id,),
            lambda store, frame: store.get(frame.frame_id),
        ),
        (
            "UPDATE frames SET record_json = x'7b7d' WHERE frame_id = ?",
            lambda frame: (frame.frame_id,),
            lambda store, frame: store.get(frame.frame_id),
        ),
        (
            "UPDATE frames SET record_sha256 = ? WHERE frame_id = ?",
            lambda frame: ("0" * 64, frame.frame_id),
            lambda store, frame: store.get(frame.frame_id),
        ),
    ],
)
def test_projection_json_and_hash_corruption_fail_closed(
    tmp_path: Path,
    statement: str,
    parameters: Callable[[FrameRef], tuple[object, ...]],
    lookup: Callable[[LocalWorldStore, FrameRef], object],
) -> None:
    source, frames, _, _, _ = _records()
    store = _store(tmp_path)
    store.commit((source, frames[0]))
    with _database(store.root) as connection:
        connection.execute(statement, parameters(frames[0]))

    with pytest.raises(PortError) as raised:
        lookup(store, frames[0])
    assert raised.value.code is PortErrorCode.CORRUPT


def test_foreign_key_corruption_is_detected_before_reads(tmp_path: Path) -> None:
    source, frames, _, _, _ = _records()
    store = _store(tmp_path)
    store.commit((source, frames[0]))
    with _database(store.root) as connection:
        connection.execute(
            "UPDATE frames SET source_id = ? WHERE frame_id = ?",
            ("src_" + "0" * 64, frames[0].frame_id),
        )

    with pytest.raises(PortError) as raised:
        store.get(frames[0].frame_id)
    assert raised.value.code is PortErrorCode.CORRUPT


def test_corrupt_intent_and_pending_run_projection_are_detected(tmp_path: Path) -> None:
    source, _, evidence, preparing, _ = _records()
    store = _store(tmp_path)
    stage = StageHandle(preparing.run_id, "0" * 32 + ".part", evidence[0].artifact)
    store.commit((source, preparing))
    store.record_artifact_intents(preparing.run_id, (stage,))
    with _database(store.root) as connection:
        connection.execute(
            "UPDATE artifact_intents SET staging_name = 'invalid' WHERE run_id = ?",
            (preparing.run_id,),
        )

    with pytest.raises(PortError) as raised:
        store.verify()
    assert raised.value.code is PortErrorCode.CORRUPT
    with pytest.raises(PortError, match="corrupt"):
        store.list_artifact_intents(preparing.run_id)


def test_pending_run_rejects_canonical_json_of_the_wrong_record_type(tmp_path: Path) -> None:
    source, _, _, preparing, _ = _records()
    store = _store(tmp_path)
    store.commit((source, preparing))
    encoded = dumps_record(source)
    with _database(store.root) as connection:
        connection.execute(
            "UPDATE runs SET record_json = ?, record_sha256 = ? WHERE run_id = ?",
            (encoded, hashlib.sha256(encoded).hexdigest(), preparing.run_id),
        )

    with pytest.raises(PortError) as raised:
        store.pending_runs()
    assert raised.value.code is PortErrorCode.CORRUPT


def test_get_rejects_a_valid_record_stored_under_the_wrong_identifier(tmp_path: Path) -> None:
    source, frames, _, _, _ = _records()
    store = _store(tmp_path)
    store.commit((source, frames[0], frames[1]))
    encoded = dumps_record(frames[1])
    with _database(store.root) as connection:
        connection.execute(
            "UPDATE frames SET record_json = ?, record_sha256 = ? WHERE frame_id = ?",
            (encoded, hashlib.sha256(encoded).hexdigest(), frames[0].frame_id),
        )

    with pytest.raises(PortError) as raised:
        store.get(frames[0].frame_id)
    assert raised.value.code is PortErrorCode.CORRUPT


def test_source_stream_and_artifact_reference_corruption_are_detected(tmp_path: Path) -> None:
    source, frames, evidence, _, _ = _records()
    store = _store(tmp_path)
    store.commit((source, frames[0], evidence[0]))
    with _database(store.root) as connection:
        connection.execute(
            "UPDATE source_streams SET width = 99 WHERE source_id = ?",
            (source.source_id,),
        )
    with pytest.raises(PortError, match="corrupt"):
        store.get(source.source_id)

    with _database(store.root) as connection:
        connection.execute(
            "UPDATE source_streams SET width = 16 WHERE source_id = ?",
            (source.source_id,),
        )
        connection.execute(
            "UPDATE artifact_catalog SET byte_count = '999' WHERE digest = ?",
            (evidence[0].artifact.sha256,),
        )
    with pytest.raises(PortError, match="corrupt"):
        store.get(evidence[0].evidence_id)


def test_pending_deletion_closure_hides_records_and_source_streams(tmp_path: Path) -> None:
    source, frames, _, _, _ = _records()
    store = _store(tmp_path)
    store.commit((source, frames[0]))
    deletion_id = "del_" + "0" * 64
    with _database(store.root) as connection:
        connection.execute(
            """INSERT INTO deletion_jobs(
                deletion_id, root_kind, root_id, protocol_version, state,
                record_count, artifact_count, shared_retention_count, completed_at_utc
            ) VALUES (?, 'source', ?, 1, 'pending', 2, 0, 0, NULL)""",
            (deletion_id, source.source_id),
        )
        connection.executemany(
            """INSERT INTO deletion_closure(deletion_id, record_id, record_type)
            VALUES (?, ?, ?)""",
            (
                (deletion_id, source.source_id, "source"),
                (deletion_id, frames[0].frame_id, "frame"),
            ),
        )

    with pytest.raises(PortError, match="not_found"):
        store.get(source.source_id)
    with pytest.raises(PortError, match="not_found"):
        store.list_frames(source.source_id, stream_index=0, limit=1)


@pytest.mark.parametrize(
    "corruption",
    [
        "PRAGMA user_version = 2",
        "PRAGMA user_version = -1",
        "UPDATE schema_migrations SET code_sha256 = " + repr("0" * 64),
        "DROP INDEX frames_exact_pts",
    ],
)
def test_version_ledger_and_schema_corruption_prevent_reopen(
    tmp_path: Path,
    corruption: str,
) -> None:
    store = _store(tmp_path)
    with _database(store.root) as connection:
        connection.execute(corruption)

    with pytest.raises(PortError) as raised:
        LocalWorldStore(store.root)
    assert raised.value.code in {PortErrorCode.CORRUPT, PortErrorCode.UNSUPPORTED}


def test_failed_migration_rolls_back_completely_and_can_be_retried(tmp_path: Path) -> None:
    root = tmp_path / "store"

    class BrokenMigrationStore(LocalWorldStore):
        def _migration_statements(self, version: int) -> tuple[str, ...]:
            return (*super()._migration_statements(version), "CREATE TABLE invalid (")

    with pytest.raises(PortError) as raised:
        BrokenMigrationStore(root)
    assert raised.value.code is PortErrorCode.STORAGE_FAILED

    with _database(root) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone() == (0,)

    recovered = LocalWorldStore(root)
    assert recovered.verify() == WorldStoreStats(0, 0, 0)


def test_unversioned_unknown_schema_is_never_adopted(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir(mode=0o700)
    database = root / "world.sqlite3"
    with closing(sqlite3.connect(database, autocommit=True)) as connection:
        connection.execute("CREATE TABLE foreign_data (secret TEXT)")
    database.chmod(0o600)

    with pytest.raises(PortError) as raised:
        LocalWorldStore(root)
    assert raised.value.code is PortErrorCode.CORRUPT


def test_invalid_database_bytes_fail_as_redacted_corruption(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir(mode=0o700)
    database = root / "world.sqlite3"
    database.write_bytes(b"not a sqlite database")
    database.chmod(0o600)

    with pytest.raises(PortError) as raised:
        LocalWorldStore(root)
    assert raised.value.code is PortErrorCode.CORRUPT
    assert str(root) not in str(raised.value)


def test_database_creation_io_failure_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = filesystem._open_file

    def fail_database(parent: int, name: str, mode: str, permissions: int) -> object:
        if name == "world.sqlite3":
            raise OSError("private backend detail")
        return original(parent, name, mode, permissions)

    monkeypatch.setattr(filesystem, "_open_file", fail_database)
    with pytest.raises(PortError) as raised:
        _store(tmp_path)
    assert raised.value.code is PortErrorCode.UNSUPPORTED
    assert "private" not in str(raised.value)


def test_root_database_and_companions_reject_links_and_wrong_modes(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(target, target_is_directory=True)
    with pytest.raises(PortError, match="unsupported"):
        LocalWorldStore(linked_root)

    root = tmp_path / "db-link"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_bytes(b"")
    outside.chmod(0o600)
    (root / "world.sqlite3").symlink_to(outside)
    with pytest.raises(PortError, match="corrupt"):
        LocalWorldStore(root)

    good = LocalWorldStore(tmp_path / "good-store")
    companion = good.root / "world.sqlite3-wal"
    companion.write_bytes(b"")
    companion.chmod(0o644)
    with pytest.raises(PortError, match="corrupt"):
        LocalWorldStore(good.root)


def test_unexpected_rollback_journal_and_missing_database_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    journal = store.root / "world.sqlite3-journal"
    journal.write_bytes(b"")
    journal.chmod(0o600)
    with pytest.raises(PortError, match="corrupt"):
        LocalWorldStore(store.root)
    journal.unlink()

    (store.root / "world.sqlite3").unlink()
    with pytest.raises(PortError, match="corrupt"):
        store.verify()


def test_database_hard_link_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    os.link(store.root / "world.sqlite3", tmp_path / "database-copy")

    with pytest.raises(PortError, match="corrupt"):
        LocalWorldStore(store.root)


def test_shared_writer_lock_serializes_world_and_evidence_mutations(tmp_path: Path) -> None:
    root = tmp_path / "store"
    evidence_store = LocalEvidenceStore(root, lock_timeout_ms=20)
    store = LocalWorldStore(root, lock_timeout_ms=20)

    with evidence_store.writer_session(), pytest.raises(PortError) as raised:
        store.commit(())
    assert raised.value.code is PortErrorCode.TIMEOUT
    assert raised.value.retryable is True
    store.commit(())


def test_active_evidence_session_composes_cross_store_mutations(tmp_path: Path) -> None:
    root = tmp_path / "store"
    evidence_store = LocalEvidenceStore(root)
    store = LocalWorldStore(root)
    source, frames, _, preparing, committed = _records(frame_count=1)
    content = b"coordinated-pixels"
    artifact = Artifact(hashlib.sha256(content).hexdigest(), str(len(content)))
    evidence = EvidenceRef.create(frames[0].frame_id, artifact, None)

    with evidence_store.writer_session() as session:
        store.commit((source, preparing), evidence_session=session)
        stage = session.stage(preparing.run_id, artifact, content)
        store.record_artifact_intents(
            preparing.run_id,
            (stage,),
            evidence_session=session,
        )
        session.commit_stage(stage)
        store.commit_for_run(
            preparing.run_id,
            (frames[0], evidence),
            evidence_session=session,
        )
        store.finalize_run(committed, evidence_session=session)

    assert evidence_store.get(artifact.sha256) == content
    assert store.get(committed.run_id) == committed
    assert store.list_evidence(frames[0].frame_id, limit=1) == (evidence,)


def test_closed_or_foreign_evidence_session_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "store"
    evidence_store = LocalEvidenceStore(root)
    store = LocalWorldStore(root)
    with evidence_store.writer_session() as closed:
        pass
    with pytest.raises(PortError, match="invalid_request"):
        store.commit((), evidence_session=closed)

    other_evidence = LocalEvidenceStore(tmp_path / "other-store")
    with (
        other_evidence.writer_session() as foreign,
        pytest.raises(PortError, match="invalid_request"),
    ):
        store.commit((), evidence_session=foreign)


def test_forged_evidence_session_cannot_bypass_the_shared_writer_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    evidence_store = LocalEvidenceStore(root, lock_timeout_ms=20)
    store = LocalWorldStore(root, lock_timeout_ms=20)
    source, _, _, _, _ = _records()
    raw_root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    forged = EvidenceWriterSession(evidence_store, raw_root_descriptor)
    try:
        with pytest.raises(PortError, match="invalid_request"):
            store.commit((source,), evidence_session=forged)
        with pytest.raises(PortError, match="invalid_request"):
            forged.inventory(limit=1)
    finally:
        os.close(raw_root_descriptor)


def test_interrupted_session_teardown_cannot_leave_a_live_lock_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    evidence_store = LocalEvidenceStore(root, lock_timeout_ms=20)
    store = LocalWorldStore(root, lock_timeout_ms=20)
    source, _, _, _, _ = _records()
    captured: EvidenceWriterSession | None = None

    def interrupt_before_invalidation(self: EvidenceWriterSession) -> None:
        del self
        raise KeyboardInterrupt

    monkeypatch.setattr(EvidenceWriterSession, "_deactivate", interrupt_before_invalidation)
    with pytest.raises(KeyboardInterrupt), evidence_store.writer_session() as session:
        captured = session

    assert captured is not None
    assert captured._lock_owner is not None
    assert captured._lock_owner.closed
    with pytest.raises(PortError, match="invalid_request"):
        store.commit((source,), evidence_session=captured)
    with pytest.raises(PortError, match="invalid_request"):
        captured.inventory(limit=1)
    monkeypatch.undo()
    with evidence_store.writer_session() as recovered:
        store.commit((source,), evidence_session=recovered)
    assert store.get(source.source_id) == source


def test_poisoned_writer_instance_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store._poisoned = True

    with pytest.raises(PortError) as raised:
        store.commit(())
    assert raised.value.code is PortErrorCode.STORAGE_FAILED


def test_backend_write_error_rolls_back_and_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, _, _, _, _ = _records()
    store = _store(tmp_path)

    def fail_write(*_: object) -> None:
        error = sqlite3.OperationalError("private sqlite failure")
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        raise error

    monkeypatch.setattr(store, "_write_record", fail_write)
    with pytest.raises(PortError) as raised:
        store.commit((source,))
    assert raised.value.code is PortErrorCode.STORAGE_FAILED
    assert raised.value.retryable is True
    assert "private" not in str(raised.value)


def test_sqlite_busy_is_bounded_and_retryable(tmp_path: Path) -> None:
    source, _, _, _, _ = _records()
    store = _store(tmp_path, busy_timeout_ms=20)
    with _database(store.root) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(PortError) as raised:
            store.commit((source,))
        assert raised.value.code is PortErrorCode.TIMEOUT
        assert raised.value.retryable is True
        blocker.execute("ROLLBACK")
    store.commit((source,))


def test_verify_is_bounded_and_reports_only_aggregate_counts(tmp_path: Path) -> None:
    source, frames, evidence, _, _ = _records()
    store = _store(tmp_path, max_audit_records=2)
    store.commit((source, frames[0], evidence[0]))

    with pytest.raises(PortError) as raised:
        store.verify()
    assert raised.value.code is PortErrorCode.LIMIT_EXCEEDED
    assert source.source_id not in str(raised.value)


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("pending_runs", {"limit": 0}),
        ("list_artifact_intents", {"run_id": "bad", "limit": 1}),
        ("list_artifact_intents", {"run_id": "run_" + "0" * 64, "limit": 65}),
        ("list_evidence", {"frame_id": "bad", "limit": 1}),
        ("list_evidence", {"frame_id": "frm_" + "0" * 64, "limit": 0}),
    ],
)
def test_read_extension_bounds_are_structured(
    tmp_path: Path,
    method: str,
    arguments: dict[str, object],
) -> None:
    store = _store(tmp_path)
    with pytest.raises(PortError):
        getattr(store, method)(**arguments)


def test_frame_listing_rejects_invalid_positions_and_missing_streams(tmp_path: Path) -> None:
    source, _, _, _, _ = _records()
    store = _store(tmp_path)
    store.commit((source,))

    for stream_index in (-1, 2**31):
        with pytest.raises(PortError, match="invalid_request"):
            store.list_frames(source.source_id, stream_index=stream_index, limit=1)
    with pytest.raises(PortError, match="not_found"):
        store.list_frames(source.source_id, stream_index=1, limit=1)
    with pytest.raises(PortError, match="invalid_request"):
        store.list_frames(source.source_id, stream_index=0, after_decode_index="01", limit=1)
    with pytest.raises(PortError, match="limit_exceeded"):
        store.list_frames(
            source.source_id,
            stream_index=0,
            after_decode_index=str(2**64),
            limit=1,
        )


@pytest.mark.parametrize(
    ("sqlite_code", "expected", "retryable"),
    [
        (sqlite3.SQLITE_BUSY, PortErrorCode.TIMEOUT, True),
        (sqlite3.SQLITE_CORRUPT, PortErrorCode.CORRUPT, False),
        (sqlite3.SQLITE_IOERR, PortErrorCode.STORAGE_FAILED, True),
        (sqlite3.SQLITE_READONLY, PortErrorCode.STORAGE_FAILED, False),
    ],
)
def test_sqlite_failures_are_mapped_without_backend_text(
    sqlite_code: int,
    expected: PortErrorCode,
    retryable: bool,
) -> None:
    backend = sqlite3.OperationalError("secret backend detail")
    backend.sqlite_errorcode = sqlite_code
    with pytest.raises(PortError) as raised:
        world_store._sqlite_error(backend, "verify")
    assert raised.value.code is expected
    assert raised.value.retryable is retryable
    assert "secret" not in str(raised.value)


def test_fixed_api_denies_raw_sql_and_authorizer_blocks_unsafe_features(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(PortError, match="capability_denied"):
        store.descriptor.require(Effect.RAW_SQL)
    public = {
        name: member
        for name, member in inspect.getmembers(LocalWorldStore, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public
    assert all(
        forbidden not in inspect.signature(member).parameters
        for member in public.values()
        for forbidden in ("sql", "query", "connection", "path")
    )
    assert LocalWorldStore._authorizer(sqlite3.SQLITE_ATTACH, None, None, None, None) == (
        sqlite3.SQLITE_DENY
    )
    assert (
        LocalWorldStore._authorizer(
            sqlite3.SQLITE_FUNCTION,
            None,
            "load_extension",
            None,
            None,
        )
        == sqlite3.SQLITE_DENY
    )
    assert LocalWorldStore._authorizer(sqlite3.SQLITE_SELECT, None, None, None, None) == (
        sqlite3.SQLITE_OK
    )
    for action in (sqlite3.SQLITE_DETACH, sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE):
        assert LocalWorldStore._authorizer(action, None, None, None, None) == sqlite3.SQLITE_DENY


def test_invalid_inputs_and_backend_errors_never_echo_identifiers_or_paths(tmp_path: Path) -> None:
    store = _store(tmp_path)
    secret = "secret-path\nattack"

    with pytest.raises(PortError) as raised:
        store.get(secret)
    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert raised.value.__cause__ is None
    assert secret not in rendered
    assert str(store.root) not in rendered
