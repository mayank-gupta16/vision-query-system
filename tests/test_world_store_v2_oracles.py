# SPDX-License-Identifier: Apache-2.0
"""Independent black-box oracles for the v0.2 WorldStore record graph."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

import visualworld.world_store as world_store_module
from visualworld.evidence import BestFrameEvidenceSelector, EvidenceIntent
from visualworld.ingestion import (
    Artifact,
    EvidenceRef,
    Fingerprint,
    FrameRef,
    Geometry,
    MediaTime,
    Producer,
    Rational,
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
)
from visualworld.perception import Observation, PerceptionRecord, Tracklet, TrackPoint
from visualworld.ports import PortError, PortErrorCode
from visualworld.storage import LocalEvidenceStore
from visualworld.world_store import (
    DeletionState,
    EvidenceSelectionLink,
    LocalWorldStore,
    PersistedEvidenceSelection,
    WorldStoreStats,
)
from visualworld.world_store_v2 import MIGRATION_V2_CHECKSUM, MIGRATION_V2_NAME


@dataclass(frozen=True, slots=True)
class _Graph:
    source: Source
    frames: tuple[FrameRef, ...]
    observations: tuple[Observation, ...]
    tracklets: tuple[Tracklet, ...]
    intents: tuple[EvidenceIntent, ...]
    preparing: RunManifest
    committed: RunManifest


def _graph(
    count: int = 3,
    *,
    fingerprint_byte: str = "a0",
    pts_values: tuple[int, ...] | None = None,
    tracklet_slices: tuple[tuple[int, int], ...] | None = None,
) -> _Graph:
    if pts_values is None:
        pts_values = tuple(index * 100 for index in range(count))
    assert len(pts_values) == count
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(fingerprint_byte * 32, "1000"),
        (SourceStream(0, 100, 80, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(
            source.source_id,
            0,
            str(index),
            MediaTime(str(pts_values[index]), time_base),
        )
        for index in range(count)
    )
    detector = Producer("visualworld.oracle-detector", "1", "b0" * 32)
    observations = tuple(
        Observation.create(
            source.source_id,
            frame.frame_id,
            0,
            frame.pts,
            Geometry(100, 80, (10 + index % 10, 10, 40 + index % 10, 50), "inferred"),
            "vehicle",
            900_000 + index,
            detector,
        )
        for index, frame in enumerate(frames)
    )
    if tracklet_slices is None:
        tracklet_slices = ((0, count),) if count and len(set(pts_values)) == count else ()
    tracker = Producer("visualworld.oracle-tracker", "1", "c0" * 32)
    tracklets = tuple(
        Tracklet.create(
            source.source_id,
            0,
            "vehicle",
            tuple(TrackPoint.from_observation(item) for item in observations[start:end]),
            "source_end",
            tracker,
        )
        for start, end in tracklet_slices
    )
    selector = BestFrameEvidenceSelector()
    intents = tuple(
        intent
        for tracklet, (start, end) in zip(tracklets, tracklet_slices, strict=True)
        for intent in selector.plan(tracklet, observations[start:end]).intents
    )
    sampling = Sampling(Rational("5", "1"))
    preparing = RunManifest.create(source.source_id, (), sampling, "preparing")
    committed = RunManifest.create(
        source.source_id,
        (),
        sampling,
        "committed",
        RunOutputs(str(count), hashlib.sha256(b"oracle-sample-index").hexdigest()),
    )
    return _Graph(
        source,
        frames,
        observations,
        tracklets,
        intents,
        preparing,
        committed,
    )


def _store(tmp_path: Path, name: str = "store", **options: int) -> LocalWorldStore:
    return LocalWorldStore(tmp_path / name, **options)


def _stage_graph(store: LocalWorldStore, graph: _Graph) -> None:
    store.commit((graph.source, graph.preparing))
    for start in range(0, len(graph.frames), 64):
        store.commit_for_run(graph.preparing.run_id, graph.frames[start : start + 64])
    for start in range(0, len(graph.observations), 64):
        store.commit_perception_for_run(
            graph.preparing.run_id,
            graph.observations[start : start + 64],
        )
    if graph.tracklets or graph.intents:
        store.commit_perception_for_run(
            graph.preparing.run_id,
            cast(tuple[PerceptionRecord, ...], graph.tracklets),
            graph.intents,
        )


def _publish_graph(store: LocalWorldStore, graph: _Graph) -> None:
    _stage_graph(store, graph)
    store.finalize_run(graph.committed)


def _publish_materialized_graph(
    store: LocalWorldStore,
    evidence_store: LocalEvidenceStore,
    graph: _Graph,
    content: bytes,
) -> EvidenceRef:
    intent = graph.intents[0]
    artifact = Artifact(hashlib.sha256(content).hexdigest(), str(len(content)))
    evidence = EvidenceRef.create(intent.frame_id, artifact, intent.geometry)
    with evidence_store.writer_session() as session:
        store.commit((graph.source, graph.preparing), evidence_session=session)
        store.commit_for_run(
            graph.preparing.run_id,
            graph.frames,
            evidence_session=session,
        )
        store.commit_perception_for_run(
            graph.preparing.run_id,
            (*graph.observations, *graph.tracklets),
            graph.intents,
            evidence_session=session,
        )
        stage = session.stage(graph.preparing.run_id, artifact, content)
        store.record_artifact_intents(
            graph.preparing.run_id,
            (stage,),
            evidence_session=session,
        )
        session.commit_stage(stage)
        store.commit_for_run(
            graph.preparing.run_id,
            (evidence,),
            evidence_session=session,
        )
        store.link_selected_evidence(
            graph.preparing.run_id,
            (EvidenceSelectionLink(graph.tracklets[0].tracklet_id, 1, evidence.evidence_id),),
            evidence_session=session,
        )
        store.finalize_run(graph.committed, evidence_session=session)
    return evidence


@contextmanager
def _database(root: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(root / "world.sqlite3", autocommit=True)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        yield connection
    finally:
        connection.close()


def _database_contract(root: Path) -> tuple[tuple[object, ...], tuple[object, ...]]:
    with _database(root) as connection:
        schema = tuple(
            connection.execute(
                """SELECT type, name, sql FROM sqlite_schema
                WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"""
            ).fetchall()
        )
        ledger = tuple(
            connection.execute(
                """SELECT schema_version, migration_name, code_sha256
                FROM schema_migrations ORDER BY schema_version"""
            ).fetchall()
        )
    return schema, ledger


def _create_v1_database(root: Path) -> None:
    root.mkdir(mode=0o700)
    database = root / "world.sqlite3"
    connection = sqlite3.connect(database, autocommit=True)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        for statement in world_store_module._SCHEMA_V1:
            connection.execute(statement)
        connection.execute(
            """INSERT INTO schema_migrations(
                schema_version, migration_name, code_sha256, applied_at_utc
            ) VALUES (1, ?, ?, ?)""",
            (
                world_store_module._MIGRATION_V1_NAME,
                world_store_module._MIGRATION_V1_CHECKSUM,
                "2026-09-08T00:00:00.000000+00:00",
            ),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.execute("COMMIT")
    finally:
        connection.close()
    database.chmod(0o600)


def _assert_code(error: pytest.ExceptionInfo[PortError], code: PortErrorCode) -> None:
    assert error.value.code is code
    assert error.value.__cause__ is None


def test_fresh_and_migrated_v2_schemas_converge_and_reopen_idempotently(
    tmp_path: Path,
) -> None:
    fresh = _store(tmp_path, "fresh")
    migrated_root = tmp_path / "migrated"
    _create_v1_database(migrated_root)

    migrated = LocalWorldStore(migrated_root)
    before_reopen = _database_contract(migrated.root)
    reopened = LocalWorldStore(migrated.root)

    assert _database_contract(fresh.root) == before_reopen
    assert _database_contract(reopened.root) == before_reopen
    assert before_reopen[1] == (
        (1, "v1_ingestion_metadata", world_store_module._MIGRATION_V1_CHECKSUM),
        (2, MIGRATION_V2_NAME, MIGRATION_V2_CHECKSUM),
    )
    assert fresh.verify() == migrated.verify() == WorldStoreStats(0, 0, 0)


def test_migration_checksum_and_unknown_newer_schema_fail_closed(tmp_path: Path) -> None:
    checksum_store = _store(tmp_path, "checksum")
    with _database(checksum_store.root) as connection:
        connection.execute(
            "UPDATE schema_migrations SET code_sha256 = ? WHERE schema_version = 2",
            ("0" * 64,),
        )
    with pytest.raises(PortError) as checksum_error:
        LocalWorldStore(checksum_store.root)
    _assert_code(checksum_error, PortErrorCode.CORRUPT)

    newer_store = _store(tmp_path, "newer")
    with _database(newer_store.root) as connection:
        connection.execute("PRAGMA user_version = 3")
    with pytest.raises(PortError) as newer_error:
        LocalWorldStore(newer_store.root)
    _assert_code(newer_error, PortErrorCode.UNSUPPORTED)


def test_failed_v2_migration_rolls_back_and_is_retryable(tmp_path: Path) -> None:
    root = tmp_path / "migration-rollback"
    _create_v1_database(root)

    class _BrokenMigrationStore(LocalWorldStore):
        def _migration_statements(self, version: int) -> tuple[str, ...]:
            statements = super()._migration_statements(version)
            return (*statements, "CREATE TABLE invalid (") if version == 2 else statements

    with pytest.raises(PortError):
        _BrokenMigrationStore(root)
    with _database(root) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE schema_version = 2"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name = 'observations'"
        ).fetchone() == (0,)

    recovered = LocalWorldStore(root)
    assert recovered.verify() == WorldStoreStats(0, 0, 0)


def test_preparing_graph_is_invisible_and_publication_exposes_one_atomic_graph(
    tmp_path: Path,
) -> None:
    graph = _graph()
    store = _store(tmp_path)
    _stage_graph(store, graph)

    for record in (*graph.observations, *graph.tracklets):
        if isinstance(record, Observation):
            record_id = record.observation_id
        else:
            assert isinstance(record, Tracklet)
            record_id = record.tracklet_id
        with pytest.raises(PortError) as hidden:
            store.get_perception(record_id)
        _assert_code(hidden, PortErrorCode.NOT_FOUND)
    for read in (
        lambda: store.list_run_observations(graph.preparing.run_id, stream_index=0, limit=64),
        lambda: store.list_run_tracklets(graph.preparing.run_id, stream_index=0, limit=64),
        lambda: store.list_tracklet_observations(graph.tracklets[0].tracklet_id, limit=64),
        lambda: store.list_selected_evidence(
            graph.preparing.run_id,
            graph.tracklets[0].tracklet_id,
            limit=8,
        ),
    ):
        with pytest.raises(PortError) as hidden_page:
            read()
        _assert_code(hidden_page, PortErrorCode.NOT_FOUND)

    store.finalize_run(graph.committed)
    store.finalize_run(graph.committed)

    assert store.list_run_observations(graph.committed.run_id, stream_index=0, limit=64) == tuple(
        sorted(graph.observations, key=lambda item: (int(item.pts.value), item.observation_id))
    )
    assert (
        store.list_run_tracklets(graph.committed.run_id, stream_index=0, limit=64)
        == graph.tracklets
    )
    assert store.list_tracklet_observations(graph.tracklets[0].tracklet_id, limit=64) == (
        graph.observations
    )
    assert (
        tuple(
            selection.intent
            for selection in store.list_selected_evidence(
                graph.committed.run_id,
                graph.tracklets[0].tracklet_id,
                limit=8,
            )
        )
        == graph.intents
    )
    assert store.verify() == WorldStoreStats(
        2 + len(graph.frames) + len(graph.observations) + len(graph.tracklets),
        0,
        0,
        observation_count=len(graph.observations),
        tracklet_count=len(graph.tracklets),
        selection_count=len(graph.intents),
    )


def test_composite_pagination_handles_same_pts_and_more_than_sixty_four_graph_rows(
    tmp_path: Path,
) -> None:
    same_pts = _graph(4, pts_values=(-1, 0, 0, 0), tracklet_slices=())
    same_pts_store = _store(tmp_path, "same-pts")
    _publish_graph(same_pts_store, same_pts)
    expected_ties = tuple(
        sorted(same_pts.observations, key=lambda item: (int(item.pts.value), item.observation_id))
    )
    first = same_pts_store.list_run_observations(same_pts.committed.run_id, stream_index=0, limit=2)
    second = same_pts_store.list_run_observations(
        same_pts.committed.run_id,
        stream_index=0,
        after_pts_value=first[-1].pts.value,
        after_observation_id=first[-1].observation_id,
        limit=2,
    )
    assert first + second == expected_ties

    large = _graph(70, fingerprint_byte="a1", tracklet_slices=((0, 35), (35, 70)))
    large_store = _store(tmp_path, "large")
    _publish_graph(large_store, large)
    page = large_store.list_run_observations(large.committed.run_id, stream_index=0, limit=64)
    tail = large_store.list_run_observations(
        large.committed.run_id,
        stream_index=0,
        after_pts_value=page[-1].pts.value,
        after_observation_id=page[-1].observation_id,
        limit=64,
    )
    assert page + tail == large.observations
    assert len(page) == 64
    assert len(tail) == 6
    for tracklet, selected in zip(large.tracklets, ((0, 35), (35, 70)), strict=True):
        start, end = selected
        members = large_store.list_tracklet_observations(tracklet.tracklet_id, limit=64)
        assert members == large.observations[start:end]


def test_selected_evidence_links_exact_intent_reference_and_durable_artifact(
    tmp_path: Path,
) -> None:
    graph = _graph(1)
    root = tmp_path / "materialized"
    evidence_store = LocalEvidenceStore(root)
    store = LocalWorldStore(root)
    intent = graph.intents[0]
    content = b"independent-selected-evidence-oracle"
    evidence = _publish_materialized_graph(store, evidence_store, graph, content)

    assert evidence_store.get(evidence.artifact.sha256) == content
    assert store.list_selected_evidence(
        graph.committed.run_id,
        graph.tracklets[0].tracklet_id,
        limit=8,
    ) == (PersistedEvidenceSelection(graph.committed.run_id, intent, evidence),)
    assert store.verify().selection_count == 1


def test_selected_evidence_rejects_missing_and_cross_source_references(
    tmp_path: Path,
) -> None:
    graph = _graph(1)
    foreign = _graph(1, fingerprint_byte="d3")
    store = _store(tmp_path)
    _stage_graph(store, graph)
    foreign_artifact = Artifact("e3" * 32, "1")
    foreign_evidence = EvidenceRef.create(
        foreign.frames[0].frame_id,
        foreign_artifact,
        foreign.observations[0].geometry,
    )
    store.commit((foreign.source, foreign.frames[0], foreign_evidence))

    links = (
        EvidenceSelectionLink(graph.tracklets[0].tracklet_id, 1, "evi_" + "0" * 64),
        EvidenceSelectionLink(
            graph.tracklets[0].tracklet_id,
            1,
            foreign_evidence.evidence_id,
        ),
    )
    for link in links:
        with pytest.raises(PortError) as rejected:
            store.link_selected_evidence(graph.preparing.run_id, (link,))
        _assert_code(rejected, PortErrorCode.CONFLICT)

    selections = store.verify().selection_count
    assert selections == len(graph.intents)


def test_missing_cross_source_and_reused_observation_graphs_conflict_atomically(
    tmp_path: Path,
) -> None:
    graph = _graph(3)
    store = _store(tmp_path)
    store.commit((graph.source, graph.preparing))
    store.commit_for_run(graph.preparing.run_id, graph.frames)

    missing_store = _store(tmp_path, "missing")
    missing_store.commit((graph.source, graph.preparing))
    missing_store.commit_for_run(graph.preparing.run_id, graph.frames)
    with pytest.raises(PortError) as missing:
        missing_store.commit_perception_for_run(
            graph.preparing.run_id,
            cast(tuple[PerceptionRecord, ...], graph.tracklets),
        )
    _assert_code(missing, PortErrorCode.CONFLICT)
    assert missing_store.verify().tracklet_count == 0

    foreign = _graph(1, fingerprint_byte="d0")
    store.commit((foreign.source, foreign.frames[0]))
    with pytest.raises(PortError) as cross_source:
        store.commit_perception_for_run(
            graph.preparing.run_id,
            (foreign.observations[0],),
        )
    _assert_code(cross_source, PortErrorCode.CONFLICT)
    assert store.verify().observation_count == 0

    store.commit_perception_for_run(graph.preparing.run_id, tuple(reversed(graph.observations)))
    store.commit_perception_for_run(
        graph.preparing.run_id,
        (graph.observations[0], graph.observations[0]),
    )
    first = Tracklet.create(
        graph.source.source_id,
        0,
        "vehicle",
        tuple(TrackPoint.from_observation(item) for item in graph.observations[:2]),
        "source_end",
        Producer("visualworld.oracle-tracker-a", "1", "d1" * 32),
    )
    second = Tracklet.create(
        graph.source.source_id,
        0,
        "vehicle",
        tuple(TrackPoint.from_observation(item) for item in graph.observations[1:]),
        "source_end",
        Producer("visualworld.oracle-tracker-b", "1", "d2" * 32),
    )
    with pytest.raises(PortError) as reused:
        store.commit_perception_for_run(graph.preparing.run_id, (first, second))
    _assert_code(reused, PortErrorCode.CONFLICT)
    assert store.verify().tracklet_count == 0


def test_bounded_verify_and_membership_corruption_fail_closed(tmp_path: Path) -> None:
    graph = _graph(4)
    store = _store(tmp_path)
    _publish_graph(store, graph)

    with pytest.raises(PortError) as bounded:
        LocalWorldStore(store.root, max_audit_records=4).verify()
    _assert_code(bounded, PortErrorCode.LIMIT_EXCEEDED)

    with _database(store.root) as connection:
        connection.execute(
            "UPDATE tracklet_points SET ordinal = 63 WHERE tracklet_id = ? AND ordinal = 1",
            (graph.tracklets[0].tracklet_id,),
        )
    with pytest.raises(PortError) as corrupt:
        store.verify()
    _assert_code(corrupt, PortErrorCode.CORRUPT)


def test_partial_v2_graph_cleanup_removes_hidden_children_and_keeps_failed_marker(
    tmp_path: Path,
) -> None:
    graph = _graph()
    store = _store(tmp_path)
    _stage_graph(store, graph)

    assert store.run_cleanup_plan(graph.preparing.run_id).run_id == graph.preparing.run_id
    store.finish_run_cleanup(graph.preparing.run_id)
    store.finish_run_cleanup(graph.preparing.run_id)

    pending = store.pending_runs(actionable_only=False)
    assert len(pending) == 1
    assert pending[0].run_id == graph.preparing.run_id
    assert pending[0].state == "failed"
    stats = store.verify()
    assert stats.observation_count == 0
    assert stats.tracklet_count == 0
    assert stats.selection_count == 0
    with _database(store.root) as connection:
        assert connection.execute("SELECT count(*) FROM tracklet_points").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM perception_run_records").fetchone() == (0,)


def test_source_deletion_hides_then_purges_the_complete_v2_graph(tmp_path: Path) -> None:
    graph = _graph()
    root = tmp_path / "deletion"
    evidence_store = LocalEvidenceStore(root)
    store = LocalWorldStore(root)
    _publish_graph(store, graph)

    deletion_id = "del_" + "e0" * 32
    plan = store.begin_source_deletion(graph.source.source_id, deletion_id)
    assert plan.record_count == 2 + len(graph.frames) + len(graph.observations) + len(
        graph.tracklets
    )
    for read in (
        lambda: store.get(graph.source.source_id),
        lambda: store.get_perception(graph.observations[0].observation_id),
        lambda: store.list_run_observations(graph.committed.run_id, stream_index=0, limit=64),
        lambda: store.list_tracklet_observations(graph.tracklets[0].tracklet_id, limit=64),
    ):
        with pytest.raises(PortError) as hidden:
            read()
        _assert_code(hidden, PortErrorCode.NOT_FOUND)

    with evidence_store.writer_session() as session:
        purged = store.purge_deletion_metadata(deletion_id, evidence_session=session)
        assert purged.state is DeletionState.METADATA_PURGED
        complete = store.complete_deletion(deletion_id, evidence_session=session)
    assert complete.state is DeletionState.COMPLETE
    assert complete.completed_at_utc is not None
    assert store.verify() == WorldStoreStats(0, 0, 0)


def test_source_deletion_preserves_a_shared_selected_evidence_artifact(tmp_path: Path) -> None:
    root = tmp_path / "shared-deletion"
    evidence_store = LocalEvidenceStore(root)
    store = LocalWorldStore(root)
    first = _graph(1, fingerprint_byte="e4")
    second = _graph(1, fingerprint_byte="e5")
    shared_content = b"shared-selected-evidence"
    first_evidence = _publish_materialized_graph(store, evidence_store, first, shared_content)
    second_evidence = _publish_materialized_graph(store, evidence_store, second, shared_content)
    assert first_evidence.artifact == second_evidence.artifact

    deletion_id = "del_" + "e6" * 32
    plan = store.begin_source_deletion(first.source.source_id, deletion_id)
    assert plan.artifact_count == 0
    assert plan.shared_retention_count == 1
    assert len(plan.artifacts) == 1
    assert not plan.artifacts[0].delete_required

    with evidence_store.writer_session() as session:
        store.purge_deletion_metadata(deletion_id, evidence_session=session)
        store.complete_deletion(deletion_id, evidence_session=session)

    assert evidence_store.get(second_evidence.artifact.sha256) == shared_content
    assert store.get(second.source.source_id) == second.source
    assert store.list_selected_evidence(
        second.committed.run_id,
        second.tracklets[0].tracklet_id,
        limit=8,
    ) == (
        PersistedEvidenceSelection(
            second.committed.run_id,
            second.intents[0],
            second_evidence,
        ),
    )
    with pytest.raises(PortError) as removed:
        store.get_perception(first.observations[0].observation_id)
    _assert_code(removed, PortErrorCode.NOT_FOUND)


def test_public_boundaries_reject_hostile_dynamic_serializers_with_static_errors(
    tmp_path: Path,
) -> None:
    graph = _graph(1)
    store = _store(tmp_path)
    store.commit((graph.source, graph.preparing))
    store.commit_for_run(graph.preparing.run_id, graph.frames)
    touched: list[str] = []

    class _HostileObservation(Observation):
        def to_mapping(self) -> dict[str, object]:
            touched.append("called")
            raise AssertionError("private/path/secret")

    hostile = _HostileObservation(
        graph.observations[0].observation_id,
        graph.observations[0].source_id,
        graph.observations[0].frame_id,
        graph.observations[0].stream_index,
        graph.observations[0].pts,
        graph.observations[0].geometry,
        graph.observations[0].category,
        graph.observations[0].confidence_millionths,
        graph.observations[0].producer,
    )
    with pytest.raises(PortError) as invalid:
        store.commit_perception_for_run(
            graph.preparing.run_id,
            (cast(PerceptionRecord, hostile),),
        )
    _assert_code(invalid, PortErrorCode.INVALID_REQUEST)
    assert str(invalid.value) == "invalid_request at world_store.commit_perception_for_run"
    assert "private" not in str(invalid.value)
    assert touched == []
