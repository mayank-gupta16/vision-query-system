# SPDX-License-Identifier: Apache-2.0
"""Migration, graph, recovery, and integrity tests for v0.2 persistence."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

import visualworld.world_store as world_store
import visualworld.world_store_v2 as world_store_v2
from visualworld.evidence import (
    BestFrameEvidenceSelector,
    DetailResolution,
    EvidenceNeed,
    dumps_evidence_intent,
    loads_evidence_intent,
)
from visualworld.ingestion import (
    Artifact,
    EvidenceRef,
    Fingerprint,
    FrameRef,
    Geometry,
    MediaTime,
    Producer,
    Rational,
    RecordValidationError,
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
    dumps_record,
)
from visualworld.perception import Observation, Tracklet, TrackPoint
from visualworld.ports import MAX_PORT_BATCH_ITEMS, PortError, PortErrorCode
from visualworld.storage import LocalEvidenceStore
from visualworld.world_store import (
    DeletionState,
    EvidenceSelectionLink,
    LocalWorldStore,
    PersistedEvidenceSelection,
    WorldStoreStats,
)
from visualworld.world_store_v2 import MIGRATION_V2_CHECKSUM, SCHEMA_V2


@contextmanager
def _database(root: Path):  # type: ignore[no-untyped-def]
    connection = sqlite3.connect(root / "world.sqlite3", autocommit=True)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        yield connection
    finally:
        connection.close()


def _values(
    *,
    frame_count: int = 3,
    width: int = 100,
    height: int = 100,
    equal_pts: bool = False,
    category: str = "vehicle",
) -> tuple[
    Source,
    tuple[FrameRef, ...],
    tuple[Observation, ...],
    Tracklet,
    RunManifest,
    RunManifest,
]:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint("ab" * 32, "30000"),
        (SourceStream(0, width, height, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(
            source.source_id,
            0,
            str(index),
            MediaTime("0" if equal_pts else str(index * 200), time_base),
        )
        for index in range(frame_count)
    )
    observations = tuple(
        Observation.create(
            source.source_id,
            frame.frame_id,
            0,
            frame.pts,
            Geometry(width, height, (10 + index, 10, 50 + index, 50), "inferred"),
            category,
            900_000 + index,
            Producer("visualworld.test-detector", "1", "bc" * 32),
        )
        for index, frame in enumerate(frames)
    )
    tracklet = Tracklet.create(
        source.source_id,
        0,
        category,
        tuple(TrackPoint.from_observation(observation) for observation in observations),
        "source_end",
        Producer("visualworld.test-tracker", "1", "cd" * 32),
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
    return source, frames, observations, tracklet, preparing, committed


def _prepare(
    root: Path,
    *,
    equal_pts: bool = False,
    category: str = "vehicle",
) -> tuple[
    LocalEvidenceStore,
    LocalWorldStore,
    Source,
    tuple[FrameRef, ...],
    tuple[Observation, ...],
    Tracklet,
    RunManifest,
    RunManifest,
]:
    evidence_store = LocalEvidenceStore(root)
    store = LocalWorldStore(root)
    source, frames, observations, tracklet, preparing, committed = _values(
        equal_pts=equal_pts,
        category=category,
    )
    plan = BestFrameEvidenceSelector().plan(tracklet, observations)
    with evidence_store.writer_session() as session:
        store.commit((source, preparing), evidence_session=session)
        store.commit_for_run(preparing.run_id, frames, evidence_session=session)
        store.commit_perception_for_run(
            preparing.run_id,
            observations,
            evidence_session=session,
        )
        store.commit_perception_for_run(
            preparing.run_id,
            (tracklet,),
            plan.intents,
            evidence_session=session,
        )
    return (
        evidence_store,
        store,
        source,
        frames,
        observations,
        tracklet,
        preparing,
        committed,
    )


def _single_frame_values(
    fingerprint_byte: str,
) -> tuple[Source, FrameRef, RunManifest, RunManifest]:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(fingerprint_byte * 32, "3"),
        (SourceStream(0, 2, 2, 0, time_base),),
    )
    frame = FrameRef.create(source.source_id, 0, "0", MediaTime("0", time_base))
    sampling = Sampling(Rational("1", "1"))
    preparing = RunManifest.create(source.source_id, (), sampling, "preparing")
    committed = RunManifest.create(
        source.source_id,
        (),
        sampling,
        "committed",
        RunOutputs("1", hashlib.sha256(fingerprint_byte.encode()).hexdigest()),
    )
    return source, frame, preparing, committed


def _create_v1_store(root: Path, source: Source | None = None) -> bytes | None:
    root.mkdir(mode=0o700)
    lock = root / "writer.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    database = root / "world.sqlite3"
    encoded = None if source is None else dumps_record(source)
    with closing(sqlite3.connect(database, autocommit=True)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("BEGIN EXCLUSIVE")
        for statement in world_store._SCHEMA_V1:
            connection.execute(statement)
        connection.execute(
            """INSERT INTO schema_migrations(
                schema_version, migration_name, code_sha256, applied_at_utc
            ) VALUES (1, 'v1_ingestion_metadata', ?, '2026-09-08T00:00:00+00:00')""",
            (world_store._MIGRATION_CHECKSUM,),
        )
        if source is not None and encoded is not None:
            connection.execute(
                """INSERT INTO sources(
                    source_id, schema_version, identity_version, record_json, record_sha256
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    source.source_id,
                    source.schema_version,
                    source.identity_version,
                    encoded,
                    hashlib.sha256(encoded).hexdigest(),
                ),
            )
            connection.executemany(
                """INSERT INTO source_streams(
                    source_id, stream_index, width, height, rotation_degrees,
                    time_base_numerator, time_base_denominator
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                tuple(
                    (
                        source.source_id,
                        stream.stream_index,
                        stream.width,
                        stream.height,
                        stream.rotation_degrees,
                        stream.time_base.numerator,
                        stream.time_base.denominator,
                    )
                    for stream in source.streams
                ),
            )
        connection.execute("PRAGMA user_version = 1")
        connection.execute("COMMIT")
    database.chmod(0o600)
    for suffix in ("-wal", "-shm"):
        companion = Path(str(database) + suffix)
        if companion.exists():
            companion.chmod(0o600)
    return encoded


def _downgrade_empty_v2_schema_to_v1(root: Path) -> None:
    with _database(root) as connection:
        for statement in reversed(SCHEMA_V2):
            kind, name = statement.split()[1:3]
            connection.execute(f"DROP {kind} {name}")
        connection.execute("DELETE FROM schema_migrations WHERE schema_version = 2")
        connection.execute("PRAGMA user_version = 1")


def test_v1_migration_is_atomic_idempotent_and_preserves_record_bytes(tmp_path: Path) -> None:
    source = _values()[0]
    root = tmp_path / "migrated"
    original = _create_v1_store(root, source)

    store = LocalWorldStore(root)
    assert dumps_record(store.get(source.source_id)) == original
    assert LocalWorldStore(root).get(source.source_id) == source
    with _database(root) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute(
            """SELECT schema_version, migration_name, code_sha256
            FROM schema_migrations ORDER BY schema_version"""
        ).fetchall() == [
            (1, "v1_ingestion_metadata", world_store._MIGRATION_CHECKSUM),
            (2, "v2_perception_metadata", MIGRATION_V2_CHECKSUM),
        ]
        assert connection.execute(
            "SELECT record_json FROM sources WHERE source_id = ?", (source.source_id,)
        ).fetchone() == (original,)


def test_failed_v2_migration_leaves_exact_v1_and_retry_succeeds(tmp_path: Path) -> None:
    root = tmp_path / "store"
    _create_v1_store(root)

    class BrokenV2(LocalWorldStore):
        def _migration_statements(self, version: int) -> tuple[str, ...]:
            statements = super()._migration_statements(version)
            return (*statements, "CREATE TABLE invalid (") if version == 2 else statements

    with pytest.raises(PortError, match="storage_failed"):
        BrokenV2(root)
    with _database(root) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'observations'"
            ).fetchone()
            is None
        )
        assert connection.execute(
            "SELECT schema_version FROM schema_migrations ORDER BY schema_version"
        ).fetchall() == [(1,)]

    assert LocalWorldStore(root).verify() == WorldStoreStats(0, 0, 0)


@pytest.mark.parametrize(
    "corruption",
    [
        "UPDATE schema_migrations SET code_sha256 = '" + "0" * 64 + "'",
        "INSERT INTO schema_migrations VALUES "
        "(2, 'unknown_v2', '" + "1" * 64 + "', '2026-09-08T00:00:00+00:00')",
        "CREATE TABLE unexpected_v1_object (value INTEGER) STRICT",
    ],
)
def test_v1_is_validated_before_the_v2_migration(
    tmp_path: Path,
    corruption: str,
) -> None:
    root = tmp_path / "store"
    _create_v1_store(root)
    with _database(root) as connection:
        connection.execute(corruption)

    with pytest.raises(PortError) as rejected:
        LocalWorldStore(root)
    assert rejected.value.code is PortErrorCode.CORRUPT


@pytest.mark.parametrize("fail_after", range(len(SCHEMA_V2) + 3))
def test_each_v2_migration_durable_step_reopens_as_exact_v1_or_v2(
    tmp_path: Path,
    fail_after: int,
) -> None:
    root = tmp_path / f"store-{fail_after}"
    _create_v1_store(root)

    class InterruptedMigration(LocalWorldStore):
        migration_step = 0

        def _execute_v2_migration_step(
            self,
            connection: sqlite3.Connection,
            statement: str,
            parameters: tuple[object, ...] = (),
        ) -> sqlite3.Cursor:
            result = connection.execute(statement, parameters)
            current = self.migration_step
            self.migration_step += 1
            if current == fail_after:
                raise sqlite3.OperationalError("injected migration interruption")
            return result

    with pytest.raises(PortError, match="storage_failed"):
        InterruptedMigration(root)
    expected_version = 2 if fail_after == len(SCHEMA_V2) + 2 else 1
    with _database(root) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (expected_version,)
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone() == (
            expected_version,
        )
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name = 'observations'"
        ).fetchone() == (expected_version - 1,)

    assert LocalWorldStore(root).verify() == WorldStoreStats(0, 0, 0)


def test_migration_preserves_v1_pending_run_for_normal_recovery(tmp_path: Path) -> None:
    root = tmp_path / "store"
    evidence_store = LocalEvidenceStore(root)
    store = LocalWorldStore(root)
    source, frames, _, _, preparing, _ = _values()
    with evidence_store.writer_session() as session:
        store.commit((source, preparing), evidence_session=session)
        store.commit_for_run(preparing.run_id, frames[:1], evidence_session=session)
    _downgrade_empty_v2_schema_to_v1(root)

    migrated = LocalWorldStore(root)
    assert migrated.pending_runs(actionable_only=True) == (preparing,)
    with evidence_store.writer_session() as session:
        migrated.finish_run_cleanup(preparing.run_id, evidence_session=session)
    assert migrated.get(source.source_id) == source


def test_migration_preserves_pending_and_metadata_purged_deletion_recovery(
    tmp_path: Path,
) -> None:
    for purge_before_migration in (False, True):
        root = tmp_path / str(purge_before_migration)
        evidence_store = LocalEvidenceStore(root)
        store = LocalWorldStore(root)
        source = _values()[0]
        deletion_id = "del_" + ("34" if purge_before_migration else "56") * 32
        with evidence_store.writer_session() as session:
            store.commit((source,), evidence_session=session)
            store.begin_source_deletion(
                source.source_id,
                deletion_id,
                evidence_session=session,
            )
            if purge_before_migration:
                store.purge_deletion_metadata(deletion_id, evidence_session=session)
        _downgrade_empty_v2_schema_to_v1(root)

        migrated = LocalWorldStore(root)
        with evidence_store.writer_session() as session:
            if not purge_before_migration:
                migrated.purge_deletion_metadata(deletion_id, evidence_session=session)
            receipt = migrated.complete_deletion(deletion_id, evidence_session=session)
        assert receipt.state.value == "complete"


def test_preparing_graph_is_hidden_then_published_atomically_and_paged(tmp_path: Path) -> None:
    (
        _,
        store,
        _,
        _,
        observations,
        tracklet,
        preparing,
        committed,
    ) = _prepare(tmp_path / "store")

    for identifier in (observations[0].observation_id, tracklet.tracklet_id):
        with pytest.raises(PortError, match="not_found"):
            store.get_perception(identifier)
    with pytest.raises(PortError, match="not_found"):
        store.list_run_observations(preparing.run_id, stream_index=0, limit=1)

    store.finalize_run(committed)
    expected = observations
    first = store.list_run_observations(committed.run_id, stream_index=0, limit=2)
    second = store.list_run_observations(
        committed.run_id,
        stream_index=0,
        after_pts_value=first[-1].pts.value,
        after_observation_id=first[-1].observation_id,
        limit=2,
    )
    assert first + second == expected
    assert store.list_run_tracklets(committed.run_id, stream_index=0, limit=1) == (tracklet,)
    assert store.list_tracklet_observations(tracklet.tracklet_id, limit=2) == observations[:2]
    assert (
        store.list_tracklet_observations(tracklet.tracklet_id, after_ordinal=1, limit=2)
        == observations[2:]
    )
    assert store.get_perception(tracklet.tracklet_id) == tracklet
    assert store.verify() == WorldStoreStats(
        9,
        0,
        0,
        observation_count=3,
        tracklet_count=1,
        selection_count=3,
    )


def test_schema_v2_persists_reopens_filters_and_deletes_non_vehicle_graph(tmp_path: Path) -> None:
    root = tmp_path / "store"
    evidence_store, store, source, _, observations, tracklet, _, committed = _prepare(
        root,
        category="animal",
    )
    store.finalize_run(committed)

    reopened = LocalWorldStore(root)
    first = reopened.list_run_observations(
        committed.run_id,
        stream_index=0,
        category="animal",
        limit=2,
    )
    second = reopened.list_run_observations(
        committed.run_id,
        stream_index=0,
        after_pts_value=first[-1].pts.value,
        after_observation_id=first[-1].observation_id,
        category="animal",
        limit=2,
    )
    assert first + second == observations
    assert (
        reopened.list_run_observations(
            committed.run_id,
            stream_index=0,
            category="vehicle",
            limit=2,
        )
        == ()
    )
    assert reopened.list_run_tracklets(
        committed.run_id,
        stream_index=0,
        category="animal",
        limit=2,
    ) == (tracklet,)
    assert reopened.get_perception(tracklet.tracklet_id) == tracklet
    assert reopened.verify().observation_count == len(observations)

    deletion_id = "del_" + "89" * 32
    with evidence_store.writer_session() as session:
        reopened.begin_source_deletion(source.source_id, deletion_id, evidence_session=session)
        reopened.purge_deletion_metadata(deletion_id, evidence_session=session)
        reopened.complete_deletion(deletion_id, evidence_session=session)

    assert LocalWorldStore(root).verify() == WorldStoreStats(0, 0, 0)


def test_perception_batches_reject_missing_and_cross_source_membership_atomically(
    tmp_path: Path,
) -> None:
    evidence_store, store, _, _, observations, tracklet, preparing, _ = _prepare(tmp_path / "store")
    other_source, other_frames, other_observations, _, _, _ = _values(frame_count=1)
    object.__setattr__(other_source.fingerprint, "digest", "ef" * 32)
    other_source = Source.create(other_source.fingerprint, other_source.streams)
    other_frame = FrameRef.create(
        other_source.source_id,
        0,
        other_frames[0].decode_index,
        other_frames[0].pts,
    )
    other_observation = Observation.create(
        other_source.source_id,
        other_frame.frame_id,
        0,
        other_frame.pts,
        other_observations[0].geometry,
        other_observations[0].category,
        other_observations[0].confidence_millionths,
        other_observations[0].producer,
    )
    with evidence_store.writer_session() as session:
        store.commit((other_source, other_frame), evidence_session=session)
        with pytest.raises(PortError, match="conflict"):
            store.commit_perception_for_run(
                preparing.run_id,
                (other_observation,),
                evidence_session=session,
            )
        missing_tracklet = Tracklet.create(
            tracklet.source_id,
            tracklet.stream_index,
            tracklet.category,
            (TrackPoint.from_observation(observations[0]),),
            tracklet.termination_reason,
            Producer("other-tracker", "1", "de" * 32),
        )
        with _database(store.root) as connection:
            connection.execute(
                "DELETE FROM perception_run_records WHERE record_id = ?",
                (observations[0].observation_id,),
            )
        with pytest.raises(PortError, match="conflict"):
            store.commit_perception_for_run(
                preparing.run_id,
                (missing_tracklet,),
                evidence_session=session,
            )


def test_selected_evidence_reuses_cas_intents_and_requires_exact_link(tmp_path: Path) -> None:
    evidence_store, store, _, _, observations, tracklet, preparing, committed = _prepare(
        tmp_path / "store"
    )
    selector = BestFrameEvidenceSelector()
    intent = selector.plan(tracklet, observations).intents[0]
    materialized = selector.materialize(
        intent,
        tracklet,
        observations,
        need=EvidenceNeed.INSPECTION,
        detail_resolution=DetailResolution.UNKNOWN,
        frame_rgb24=bytes(intent.geometry.source_width * intent.geometry.source_height * 3),
    ).materialized
    assert materialized is not None
    link = EvidenceSelectionLink(
        tracklet.tracklet_id, intent.rank, materialized.reference.evidence_id
    )

    with pytest.raises(PortError, match="conflict"):
        store.link_selected_evidence(preparing.run_id, (link,))
    with evidence_store.writer_session() as session:
        with pytest.raises(PortError, match="conflict"):
            store.link_selected_evidence(
                preparing.run_id,
                (link,),
                evidence_session=session,
            )
        stage = session.stage(
            preparing.run_id,
            materialized.reference.artifact,
            materialized.crop.pixels,
        )
        store.record_artifact_intents(
            preparing.run_id,
            (stage,),
            evidence_session=session,
        )
        session.commit_stage(stage)
        store.commit_for_run(
            preparing.run_id,
            (materialized.reference,),
            evidence_session=session,
        )

    with pytest.raises(PortError, match="invalid_request"):
        store.link_selected_evidence(preparing.run_id, (link,))
    with evidence_store.writer_session() as session:
        store.link_selected_evidence(
            preparing.run_id,
            (link,),
            evidence_session=session,
        )
        store.finalize_run(committed, evidence_session=session)
        assert store.list_artifact_intents(committed.run_id) == ()
        store.finalize_run(committed, evidence_session=session)

    reopened = LocalWorldStore(store.root)
    with evidence_store.writer_session() as session:
        reopened.finalize_run(committed, evidence_session=session)
        assert committed.outputs is not None
        conflicting = RunManifest.create(
            committed.source_id,
            committed.producers,
            committed.sampling,
            "committed",
            RunOutputs(
                committed.outputs.sample_count,
                hashlib.sha256(b"conflicting-sample-index").hexdigest(),
            ),
        )
        assert conflicting.run_id == committed.run_id
        with pytest.raises(PortError, match="conflict"):
            reopened.finalize_run(conflicting, evidence_session=session)

    assert store.list_selected_evidence(committed.run_id, tracklet.tracklet_id, limit=8)[0] == (
        PersistedEvidenceSelection(committed.run_id, intent, materialized.reference)
    )
    with pytest.raises(PortError, match="not_found"):
        store.list_selected_evidence(
            committed.run_id,
            "trk_" + "0" * 64,
            limit=8,
        )


def test_finalize_inspects_more_than_one_artifact_batch(tmp_path: Path) -> None:
    root = tmp_path / "store"
    evidence_store = LocalEvidenceStore(root)
    store = LocalWorldStore(root)
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint("e1" * 32, "195"),
        (SourceStream(0, 1, 1, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(source.source_id, 0, str(index), MediaTime(str(index), time_base))
        for index in range(MAX_PORT_BATCH_ITEMS + 1)
    )
    detector = Producer("visualworld.batch-detector", "1", "e2" * 32)
    tracker = Producer("visualworld.batch-tracker", "1", "e3" * 32)
    observations = tuple(
        Observation.create(
            source.source_id,
            frame.frame_id,
            0,
            frame.pts,
            Geometry(1, 1, (0, 0, 1, 1), "inferred"),
            "vehicle",
            900_000,
            detector,
        )
        for frame in frames
    )
    tracklets = tuple(
        Tracklet.create(
            source.source_id,
            0,
            observation.category,
            (TrackPoint.from_observation(observation),),
            "source_end",
            tracker,
        )
        for observation in observations
    )
    selector = BestFrameEvidenceSelector()
    intents = tuple(
        selector.plan(tracklet, (observation,)).intents[0]
        for tracklet, observation in zip(tracklets, observations, strict=True)
    )
    sampling = Sampling(Rational("1", "1"))
    preparing = RunManifest.create(source.source_id, (), sampling, "preparing")
    committed = RunManifest.create(
        source.source_id,
        (),
        sampling,
        "committed",
        RunOutputs(
            str(len(frames)),
            hashlib.sha256(b"batch-sample-index").hexdigest(),
        ),
    )
    materialized = tuple(
        selector.materialize(
            intent,
            tracklet,
            (observation,),
            need=EvidenceNeed.INSPECTION,
            detail_resolution=DetailResolution.UNKNOWN,
            frame_rgb24=b"\x00\x00\x00",
        ).materialized
        for intent, tracklet, observation in zip(
            intents,
            tracklets,
            observations,
            strict=True,
        )
    )
    assert all(item is not None for item in materialized)
    materialized_items = tuple(item for item in materialized if item is not None)
    references = tuple(item.reference for item in materialized_items)
    assert len(references) == MAX_PORT_BATCH_ITEMS + 1
    assert len({item.artifact for item in references}) == 1

    with evidence_store.writer_session() as session:
        store.commit((source, preparing), evidence_session=session)
        for start in range(0, len(frames), MAX_PORT_BATCH_ITEMS):
            store.commit_for_run(
                preparing.run_id,
                frames[start : start + MAX_PORT_BATCH_ITEMS],
                evidence_session=session,
            )
            store.commit_perception_for_run(
                preparing.run_id,
                observations[start : start + MAX_PORT_BATCH_ITEMS],
                evidence_session=session,
            )
            store.commit_perception_for_run(
                preparing.run_id,
                tracklets[start : start + MAX_PORT_BATCH_ITEMS],
                intents[start : start + MAX_PORT_BATCH_ITEMS],
                evidence_session=session,
            )
        first = materialized_items[0]
        stage = session.stage(
            preparing.run_id,
            first.reference.artifact,
            first.crop.pixels,
        )
        store.record_artifact_intents(
            preparing.run_id,
            (stage,),
            evidence_session=session,
        )
        session.commit_stage(stage)
        links = tuple(
            EvidenceSelectionLink(tracklet.tracklet_id, 1, reference.evidence_id)
            for tracklet, reference in zip(tracklets, references, strict=True)
        )
        for start in range(0, len(references), MAX_PORT_BATCH_ITEMS):
            store.commit_for_run(
                preparing.run_id,
                references[start : start + MAX_PORT_BATCH_ITEMS],
                evidence_session=session,
            )
            store.link_selected_evidence(
                preparing.run_id,
                links[start : start + MAX_PORT_BATCH_ITEMS],
                evidence_session=session,
            )
        store.finalize_run(committed, evidence_session=session)

    assert store.list_artifact_intents(committed.run_id) == ()
    assert store.verify().selection_count == MAX_PORT_BATCH_ITEMS + 1


def test_incomplete_run_cleanup_removes_only_run_owned_v2_graph(tmp_path: Path) -> None:
    evidence_store, store, source, _, observations, tracklet, preparing, _ = _prepare(
        tmp_path / "store"
    )
    assert store.pending_runs(actionable_only=True) == (preparing,)

    with evidence_store.writer_session() as session:
        assert store.run_cleanup_plan(preparing.run_id, evidence_session=session).artifacts == ()
        store.finish_run_cleanup(preparing.run_id, evidence_session=session)

    for identifier in (observations[0].observation_id, tracklet.tracklet_id):
        with pytest.raises(PortError, match="not_found"):
            store.get_perception(identifier)
    assert store.get(source.source_id) == source
    assert store.verify() == WorldStoreStats(2, 0, 0)


def test_incomplete_run_cleanup_preserves_v2_records_shared_by_another_run(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    evidence_store = LocalEvidenceStore(root)
    store = LocalWorldStore(root)
    source, frames, observations, tracklet, first, _ = _values()
    second_producer = Producer("visualworld.second-run", "1", "91" * 32)
    second = RunManifest.create(
        source.source_id,
        (second_producer,),
        first.sampling,
        "preparing",
    )
    second_committed = RunManifest.create(
        source.source_id,
        (second_producer,),
        first.sampling,
        "committed",
        RunOutputs(str(len(frames)), hashlib.sha256(b"second-index").hexdigest()),
    )
    intents = BestFrameEvidenceSelector().plan(tracklet, observations).intents
    with evidence_store.writer_session() as session:
        store.commit((source, first, second), evidence_session=session)
        for run in (first, second):
            store.commit_for_run(run.run_id, frames, evidence_session=session)
            store.commit_perception_for_run(
                run.run_id,
                observations,
                evidence_session=session,
            )
            store.commit_perception_for_run(
                run.run_id,
                (tracklet,),
                intents,
                evidence_session=session,
            )
        store.finish_run_cleanup(first.run_id, evidence_session=session)
        store.finalize_run(second_committed, evidence_session=session)

    assert store.get_perception(tracklet.tracklet_id) == tracklet
    assert store.list_tracklet_observations(tracklet.tracklet_id, limit=64) == observations
    with _database(root) as connection:
        assert connection.execute("SELECT count(*) FROM observations").fetchone() == (
            len(observations),
        )
        assert connection.execute("SELECT count(*) FROM tracklets").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM perception_run_records WHERE run_id = ?",
            (first.run_id,),
        ).fetchone() == (0,)


def test_run_cleanup_preserves_cas_needed_by_another_runs_durable_intent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    evidence_store = LocalEvidenceStore(root)
    store = LocalWorldStore(root)
    first_source, first_frame, first_run, _ = _single_frame_values("a1")
    second_source, _, second_run, _ = _single_frame_values("a2")
    content = b"shared-pending-run-artifact"
    artifact = Artifact(hashlib.sha256(content).hexdigest(), str(len(content)))
    evidence = EvidenceRef.create(first_frame.frame_id, artifact, None)

    with evidence_store.writer_session() as session:
        store.commit(
            (first_source, first_run, second_source, second_run),
            evidence_session=session,
        )
        store.commit_for_run(
            first_run.run_id,
            (first_frame,),
            evidence_session=session,
        )
        first_stage = session.stage(first_run.run_id, artifact, content)
        store.record_artifact_intents(
            first_run.run_id,
            (first_stage,),
            evidence_session=session,
        )
        session.commit_stage(first_stage)
        store.commit_for_run(
            first_run.run_id,
            (evidence,),
            evidence_session=session,
        )
        second_stage = session.stage(second_run.run_id, artifact, content)
        store.record_artifact_intents(
            second_run.run_id,
            (second_stage,),
            evidence_session=session,
        )

        plan = store.run_cleanup_plan(first_run.run_id, evidence_session=session)
        assert plan.artifacts == ()
        store.finish_run_cleanup(first_run.run_id, evidence_session=session)

    assert evidence_store.get(artifact.sha256) == content
    assert store.list_artifact_intents(second_run.run_id) == (second_stage,)
    assert store.verify().artifact_count == 1


def test_source_deletion_preserves_cas_needed_by_another_runs_durable_intent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    evidence_store = LocalEvidenceStore(root)
    store = LocalWorldStore(root)
    first_source, first_frame, first_run, first_committed = _single_frame_values("b1")
    second_source, _, second_run, _ = _single_frame_values("b2")
    content = b"shared-pending-deletion-artifact"
    artifact = Artifact(hashlib.sha256(content).hexdigest(), str(len(content)))
    evidence = EvidenceRef.create(first_frame.frame_id, artifact, None)
    deletion_id = "del_" + "b3" * 32

    with evidence_store.writer_session() as session:
        store.commit((first_source, first_run), evidence_session=session)
        store.commit_for_run(
            first_run.run_id,
            (first_frame,),
            evidence_session=session,
        )
        first_stage = session.stage(first_run.run_id, artifact, content)
        store.record_artifact_intents(
            first_run.run_id,
            (first_stage,),
            evidence_session=session,
        )
        session.commit_stage(first_stage)
        store.commit_for_run(
            first_run.run_id,
            (evidence,),
            evidence_session=session,
        )
        store.finalize_run(first_committed, evidence_session=session)

        store.commit((second_source, second_run), evidence_session=session)
        second_stage = session.stage(second_run.run_id, artifact, content)
        store.record_artifact_intents(
            second_run.run_id,
            (second_stage,),
            evidence_session=session,
        )
        plan = store.begin_source_deletion(
            first_source.source_id,
            deletion_id,
            evidence_session=session,
        )

    assert plan.artifact_count == 0
    assert plan.shared_retention_count == 1
    assert len(plan.artifacts) == 1
    assert plan.artifacts[0].artifact == artifact
    assert not plan.artifacts[0].delete_required
    with evidence_store.writer_session() as session:
        store.purge_deletion_metadata(deletion_id, evidence_session=session)
        store.complete_deletion(deletion_id, evidence_session=session)

    assert evidence_store.get(artifact.sha256) == content
    assert store.list_artifact_intents(second_run.run_id) == (second_stage,)
    assert store.verify().artifact_count == 1


def test_source_deletion_freezes_hides_and_cascades_v2_graph(tmp_path: Path) -> None:
    evidence_store, store, source, _, observations, tracklet, _, committed = _prepare(
        tmp_path / "store"
    )
    store.finalize_run(committed)
    deletion_id = "del_" + "12" * 32

    with evidence_store.writer_session() as session:
        plan = store.begin_source_deletion(
            source.source_id,
            deletion_id,
            evidence_session=session,
        )
        assert plan.record_count == 9
        for identifier in (observations[0].observation_id, tracklet.tracklet_id):
            with pytest.raises(PortError, match="not_found"):
                store.get_perception(identifier)
        with pytest.raises(PortError, match="conflict"):
            store.commit_perception_for_run(
                committed.run_id,
                (),
                evidence_session=session,
            )
        store.purge_deletion_metadata(deletion_id, evidence_session=session)
        receipt = store.complete_deletion(deletion_id, evidence_session=session)

    assert receipt.record_count == 9
    assert store.verify() == WorldStoreStats(0, 0, 0)


def test_v2_source_deletion_reopens_safely_at_each_durable_phase(tmp_path: Path) -> None:
    root = tmp_path / "store"
    evidence_store, store, source, _, observations, tracklet, _, committed = _prepare(root)
    store.finalize_run(committed)
    deletion_id = "del_" + "78" * 32

    plan = store.begin_source_deletion(source.source_id, deletion_id)
    pending = LocalWorldStore(root)
    assert pending.get_deletion_plan(deletion_id) == plan
    assert pending.verify().observation_count == len(observations)
    for identifier in (observations[0].observation_id, tracklet.tracklet_id):
        with pytest.raises(PortError, match="not_found"):
            pending.get_perception(identifier)

    with evidence_store.writer_session() as session:
        purged = pending.purge_deletion_metadata(deletion_id, evidence_session=session)
    assert purged.state is DeletionState.METADATA_PURGED
    after_purge = LocalWorldStore(root)
    assert after_purge.verify() == WorldStoreStats(0, 0, 0)
    with evidence_store.writer_session() as session:
        completed = after_purge.complete_deletion(deletion_id, evidence_session=session)
    assert completed.state is DeletionState.COMPLETE
    assert LocalWorldStore(root).deletion_status(deletion_id) == completed


@pytest.mark.parametrize(
    "tamper",
    [
        "DELETE FROM perception_deletion_closure WHERE rowid = "
        "(SELECT rowid FROM perception_deletion_closure LIMIT 1)",
        "UPDATE perception_deletion_closure SET record_type = 'tracklet' "
        "WHERE record_type = 'observation'",
        "UPDATE deletion_jobs SET record_count = record_count + 1",
    ],
)
def test_v2_source_deletion_closure_tampering_fails_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    _, store, source, _, _, _, _, committed = _prepare(tmp_path / "store")
    store.finalize_run(committed)
    deletion_id = "del_" + hashlib.sha256(tamper.encode()).hexdigest()
    store.begin_source_deletion(source.source_id, deletion_id)
    with _database(store.root) as connection:
        connection.execute(tamper)

    with pytest.raises(PortError) as corrupt:
        store.get_deletion_plan(deletion_id)
    assert corrupt.value.code is PortErrorCode.CORRUPT


@pytest.mark.parametrize(
    "corruption",
    [
        "UPDATE observations SET confidence_millionths = 1",
        "UPDATE observations SET record_json = x'7b7d'",
        "UPDATE observations SET record_sha256 = '" + "0" * 64 + "'",
        "UPDATE tracklets SET start_pts_order = start_pts_order + 1",
        "UPDATE tracklet_points SET pts_order = pts_order + 1 WHERE ordinal = 0",
        "UPDATE selected_evidence SET selector_version = 'damaged' WHERE rank = 1",
        "UPDATE selected_evidence SET intent_sha256 = '" + "0" * 64 + "' WHERE rank = 1",
        "UPDATE selected_evidence SET observation_id = 'obs_" + "0" * 64 + "' WHERE rank = 1",
        "UPDATE perception_run_records SET record_type = 'tracklet' "
        "WHERE rowid = (SELECT rowid FROM perception_run_records "
        "WHERE record_type = 'observation' LIMIT 1)",
    ],
)
def test_v2_projection_membership_and_selection_corruption_fail_closed(
    tmp_path: Path,
    corruption: str,
) -> None:
    _, store, _, _, _, _, _, committed = _prepare(tmp_path / "store")
    store.finalize_run(committed)
    with _database(store.root) as connection:
        connection.execute(corruption)

    with pytest.raises(PortError) as raised:
        store.verify()
    assert raised.value.code is PortErrorCode.CORRUPT


def test_missing_same_run_frame_ownership_fails_all_graph_reads_closed(
    tmp_path: Path,
) -> None:
    _, store, _, frames, observations, tracklet, _, committed = _prepare(tmp_path / "store")
    store.finalize_run(committed)
    with _database(store.root) as connection:
        connection.execute(
            """DELETE FROM run_records WHERE run_id = ? AND record_id = ?
            AND record_type = 'frame'""",
            (committed.run_id, frames[0].frame_id),
        )

    reads = (
        lambda: store.get_perception(observations[0].observation_id),
        lambda: store.get_perception(tracklet.tracklet_id),
        lambda: store.list_run_observations(committed.run_id, stream_index=0, limit=64),
        lambda: store.list_run_tracklets(committed.run_id, stream_index=0, limit=64),
        lambda: store.list_tracklet_observations(tracklet.tracklet_id, limit=64),
        lambda: store.list_selected_evidence(committed.run_id, tracklet.tracklet_id, limit=8),
        store.verify,
    )
    for read in reads:
        with pytest.raises(PortError) as corrupt:
            read()
        assert corrupt.value.code is PortErrorCode.CORRUPT


def test_run_observation_query_rejects_dangling_ownership(tmp_path: Path) -> None:
    _, store, _, _, _, _, _, committed = _prepare(tmp_path / "store")
    store.finalize_run(committed)
    with _database(store.root) as connection:
        connection.execute(
            """UPDATE perception_run_records SET record_id = ?
            WHERE rowid = (
                SELECT rowid FROM perception_run_records
                WHERE run_id = ? AND record_type = 'observation' LIMIT 1
            )""",
            ("obs_" + "f1" * 32, committed.run_id),
        )

    with pytest.raises(PortError) as corrupt:
        store.list_run_observations(committed.run_id, stream_index=0, limit=64)
    assert corrupt.value.code is PortErrorCode.CORRUPT


def test_tracklet_observation_query_rejects_corrupt_ordinal_projection(
    tmp_path: Path,
) -> None:
    _, store, _, _, _, tracklet, _, committed = _prepare(tmp_path / "store")
    store.finalize_run(committed)
    with _database(store.root) as connection:
        connection.execute(
            """UPDATE tracklet_points SET ordinal = 63
            WHERE tracklet_id = ? AND ordinal = 1""",
            (tracklet.tracklet_id,),
        )

    with pytest.raises(PortError) as corrupt:
        store.list_tracklet_observations(tracklet.tracklet_id, limit=64)
    assert corrupt.value.code is PortErrorCode.CORRUPT


def test_source_mismatched_committed_ownership_never_exposes_preparing_observation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    store = LocalWorldStore(root)
    first_source, first_frame, first_run, _ = _single_frame_values("c1")
    second_source, _, second_run, _ = _single_frame_values("c2")
    second_committed = RunManifest.create(
        second_source.source_id,
        (),
        second_run.sampling,
        "committed",
        RunOutputs("0", hashlib.sha256(b"empty-second-run").hexdigest()),
    )
    observation = Observation.create(
        first_source.source_id,
        first_frame.frame_id,
        0,
        first_frame.pts,
        Geometry(2, 2, (0, 0, 1, 1), "inferred"),
        "vehicle",
        900_000,
        Producer("visualworld.visibility-detector", "1", "c3" * 32),
    )
    store.commit((first_source, first_run, second_source, second_run))
    store.commit_for_run(first_run.run_id, (first_frame,))
    store.commit_perception_for_run(first_run.run_id, (observation,))
    store.finalize_run(second_committed)
    with _database(root) as connection:
        connection.execute(
            """INSERT INTO perception_run_records(run_id, record_id, record_type)
            VALUES (?, ?, 'observation')""",
            (second_committed.run_id, observation.observation_id),
        )

    with pytest.raises(PortError) as corrupt:
        store.get_perception(observation.observation_id)
    assert corrupt.value.code is PortErrorCode.CORRUPT


def test_v2_queries_have_matching_stable_indexes(tmp_path: Path) -> None:
    _, store, _, _, _, _, _, _ = _prepare(tmp_path / "store")
    with _database(store.root) as connection:
        observation_plan = connection.execute(
            "EXPLAIN QUERY PLAN " + world_store_v2.LIST_RUN_OBSERVATIONS_SQL,
            (
                "src_" + "0" * 64,
                0,
                None,
                None,
                0,
                0,
                "obs_" + "0" * 64,
                "run_" + "0" * 64,
                64,
            ),
        ).fetchall()
        point_plan = connection.execute(
            "EXPLAIN QUERY PLAN " + world_store_v2.LIST_TRACKLET_OBSERVATIONS_SQL,
            ("trk_" + "0" * 64, -1, 64),
        ).fetchall()
    rendered = " ".join(str(row[3]) for row in (*observation_plan, *point_plan))
    assert "observations_source_time" in rendered
    assert "sqlite_autoindex_tracklet_points_1" in rendered
    assert "USE TEMP B-TREE" not in rendered


def test_evidence_intent_codec_is_canonical_bounded_and_strict() -> None:
    _, _, observations, tracklet, _, _ = _values()
    intent = BestFrameEvidenceSelector().plan(tracklet, observations).intents[0]
    encoded = dumps_evidence_intent(intent)

    assert loads_evidence_intent(encoded) == intent
    with pytest.raises(ValueError):
        loads_evidence_intent(encoded[:-1] + b',"extra":1}')
    with pytest.raises(RecordValidationError):
        loads_evidence_intent(b" " * (256 * 1024 + 1))


def test_schema_v2_is_generic_and_contains_no_adapter_or_vertical_terms() -> None:
    rendered = "\n".join(SCHEMA_V2).lower()
    for forbidden in ("vehicle", "openvino", "yolo", "iou", "reid", "5 fps"):
        assert forbidden not in rendered
