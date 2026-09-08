# SPDX-License-Identifier: Apache-2.0
"""Disjoint coverage oracles for v0.2 WorldStore fail-closed branches."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest

from visualworld.evidence import (
    BestFrameEvidenceSelector,
    EvidenceIntent,
    EvidenceSelectionLimits,
    dumps_evidence_intent,
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
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
    dumps_record,
)
from visualworld.perception import (
    Observation,
    PerceptionRecord,
    Tracklet,
    TrackPoint,
    dumps_perception_record,
)
from visualworld.ports import PortError, PortErrorCode
from visualworld.storage import EvidenceWriterSession, LocalEvidenceStore
from visualworld.world_store import EvidenceSelectionLink, LocalWorldStore


@dataclass(frozen=True, slots=True)
class _Graph:
    source: Source
    frames: tuple[FrameRef, ...]
    observations: tuple[Observation, ...]
    tracklet: Tracklet
    intents: tuple[EvidenceIntent, ...]
    preparing: RunManifest
    committed: RunManifest


def _graph(
    *,
    fingerprint_byte: str = "71",
    sample_rate: str = "5",
    frame_count: int = 3,
) -> _Graph:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(fingerprint_byte * 32, "1000"),
        (SourceStream(0, 64, 48, 0, time_base),),
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
    detector = Producer("visualworld.coverage-detector", "1", "72" * 32)
    observations = tuple(
        Observation.create(
            source.source_id,
            frame.frame_id,
            0,
            frame.pts,
            Geometry(64, 48, (4 + index, 5, 24 + index, 30), "inferred"),
            "vehicle",
            800_000 + index,
            detector,
        )
        for index, frame in enumerate(frames)
    )
    tracklet = Tracklet.create(
        source.source_id,
        0,
        "vehicle",
        tuple(TrackPoint.from_observation(item) for item in observations),
        "source_end",
        Producer("visualworld.coverage-tracker", "1", "73" * 32),
    )
    intents = BestFrameEvidenceSelector().plan(tracklet, observations).intents
    sampling = Sampling(Rational(sample_rate, "1"))
    preparing = RunManifest.create(source.source_id, (), sampling, "preparing")
    committed = RunManifest.create(
        source.source_id,
        (),
        sampling,
        "committed",
        RunOutputs(str(frame_count), hashlib.sha256(b"coverage-samples").hexdigest()),
    )
    return _Graph(source, frames, observations, tracklet, intents, preparing, committed)


def _prepare(
    root: Path, graph: _Graph | None = None
) -> tuple[LocalEvidenceStore, LocalWorldStore, _Graph]:
    selected = _graph() if graph is None else graph
    evidence_store = LocalEvidenceStore(root)
    store = LocalWorldStore(root)
    with evidence_store.writer_session() as session:
        store.commit((selected.source, selected.preparing), evidence_session=session)
        store.commit_for_run(
            selected.preparing.run_id,
            selected.frames,
            evidence_session=session,
        )
        store.commit_perception_for_run(
            selected.preparing.run_id,
            selected.observations,
            evidence_session=session,
        )
        store.commit_perception_for_run(
            selected.preparing.run_id,
            (selected.tracklet,),
            selected.intents,
            evidence_session=session,
        )
    return evidence_store, store, selected


def _publish(
    root: Path, graph: _Graph | None = None
) -> tuple[LocalEvidenceStore, LocalWorldStore, _Graph]:
    evidence_store, store, selected = _prepare(root, graph)
    store.finalize_run(selected.committed)
    return evidence_store, store, selected


@contextmanager
def _database(root: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(root / "world.sqlite3", autocommit=True)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        yield connection
    finally:
        connection.close()


def _assert_error(
    callback: Callable[[], object],
    code: PortErrorCode,
) -> None:
    with pytest.raises(PortError) as raised:
        callback()
    assert raised.value.code is code
    assert raised.value.__cause__ is None


def _artifact_path(root: Path, artifact: Artifact) -> Path:
    digest = artifact.sha256
    return root / "artifacts" / "v1" / "sha256" / digest[:2] / digest[2:4] / digest


def _add_evidence(
    store: LocalWorldStore,
    session: EvidenceWriterSession,
    graph: _Graph,
    content: bytes,
    *,
    geometry: Geometry | None,
    commit_artifact: bool = True,
) -> EvidenceRef:
    artifact = Artifact(hashlib.sha256(content).hexdigest(), str(len(content)))
    evidence = EvidenceRef.create(graph.intents[0].frame_id, artifact, geometry)
    stage = session.stage(graph.preparing.run_id, artifact, content)
    store.record_artifact_intents(
        graph.preparing.run_id,
        (stage,),
        evidence_session=session,
    )
    if commit_artifact:
        session.commit_stage(stage)
    store.commit_for_run(
        graph.preparing.run_id,
        (evidence,),
        evidence_session=session,
    )
    return evidence


def _link(
    store: LocalWorldStore,
    session: EvidenceWriterSession,
    graph: _Graph,
    evidence: EvidenceRef,
) -> EvidenceSelectionLink:
    link = EvidenceSelectionLink(graph.tracklet.tracklet_id, 1, evidence.evidence_id)
    store.link_selected_evidence(
        graph.preparing.run_id,
        (link,),
        evidence_session=session,
    )
    return link


def test_public_validation_rejects_wrong_batches_links_and_identifiers(tmp_path: Path) -> None:
    store = LocalWorldStore(tmp_path / "store")
    run_id = "run_" + "1" * 64
    tracklet_id = "trk_" + "2" * 64
    observation_id = "obs_" + "3" * 64
    evidence_id = "evi_" + "4" * 64
    valid_link = EvidenceSelectionLink(tracklet_id, 1, evidence_id)
    object.__setattr__(valid_link, "rank", 0)

    invalid_calls: tuple[tuple[Callable[[], object], PortErrorCode], ...] = (
        (
            lambda: store.commit_perception_for_run(
                run_id,
                cast(tuple[PerceptionRecord, ...], []),
            ),
            PortErrorCode.LIMIT_EXCEEDED,
        ),
        (
            lambda: store.commit_perception_for_run(
                run_id,
                cast(tuple[PerceptionRecord, ...], (cast(PerceptionRecord, object()),) * 65),
            ),
            PortErrorCode.LIMIT_EXCEEDED,
        ),
        (
            lambda: store.commit_perception_for_run(
                run_id,
                evidence_intents=cast(tuple[EvidenceIntent, ...], []),
            ),
            PortErrorCode.LIMIT_EXCEEDED,
        ),
        (
            lambda: store.commit_perception_for_run(
                run_id,
                evidence_intents=cast(
                    tuple[EvidenceIntent, ...],
                    (cast(EvidenceIntent, object()),) * 65,
                ),
            ),
            PortErrorCode.LIMIT_EXCEEDED,
        ),
        (
            lambda: store.link_selected_evidence(
                run_id,
                cast(tuple[EvidenceSelectionLink, ...], []),
            ),
            PortErrorCode.LIMIT_EXCEEDED,
        ),
        (
            lambda: store.link_selected_evidence(
                run_id,
                cast(tuple[EvidenceSelectionLink, ...], (valid_link,) * 65),
            ),
            PortErrorCode.LIMIT_EXCEEDED,
        ),
        (
            lambda: store.link_selected_evidence(run_id, (valid_link,)),
            PortErrorCode.INVALID_REQUEST,
        ),
        (
            lambda: store.link_selected_evidence(
                run_id,
                (cast(EvidenceSelectionLink, object()),),
            ),
            PortErrorCode.INVALID_REQUEST,
        ),
        (lambda: store.get_perception(cast(str, 1)), PortErrorCode.INVALID_REQUEST),
        (lambda: store.get_perception("obs_private/path"), PortErrorCode.INVALID_REQUEST),
        (
            lambda: store.list_run_observations(
                run_id,
                stream_index=cast(int, True),
                limit=1,
            ),
            PortErrorCode.INVALID_REQUEST,
        ),
        (
            lambda: store.list_run_observations(
                run_id,
                stream_index=0,
                after_pts_value="0",
                limit=1,
            ),
            PortErrorCode.INVALID_REQUEST,
        ),
        (
            lambda: store.list_run_observations(
                run_id,
                stream_index=0,
                after_observation_id=observation_id,
                limit=1,
            ),
            PortErrorCode.INVALID_REQUEST,
        ),
        (
            lambda: store.list_run_observations(
                run_id,
                stream_index=0,
                category="private/path",
                limit=1,
            ),
            PortErrorCode.INVALID_REQUEST,
        ),
        (
            lambda: store.list_run_tracklets(
                run_id,
                stream_index=-1,
                limit=1,
            ),
            PortErrorCode.INVALID_REQUEST,
        ),
        (
            lambda: store.list_run_tracklets(
                run_id,
                stream_index=0,
                after_start_pts_value="0",
                limit=1,
            ),
            PortErrorCode.INVALID_REQUEST,
        ),
        (
            lambda: store.list_run_tracklets(
                run_id,
                stream_index=0,
                after_start_pts_value="0",
                after_tracklet_id="bad",
                limit=1,
            ),
            PortErrorCode.INVALID_REQUEST,
        ),
        (
            lambda: store.list_run_tracklets(
                run_id,
                stream_index=0,
                termination_reason="private/path",
                limit=1,
            ),
            PortErrorCode.INVALID_REQUEST,
        ),
        (
            lambda: store.list_tracklet_observations(
                tracklet_id,
                after_ordinal=cast(int, True),
                limit=1,
            ),
            PortErrorCode.INVALID_REQUEST,
        ),
        (
            lambda: store.list_selected_evidence(
                run_id,
                tracklet_id,
                after_rank=9,
                limit=1,
            ),
            PortErrorCode.INVALID_REQUEST,
        ),
    )
    for callback, code in invalid_calls:
        _assert_error(callback, code)


def test_missing_frame_mismatched_frame_and_unowned_frame_roll_back(tmp_path: Path) -> None:
    graph = _graph()
    store = LocalWorldStore(tmp_path / "store")
    store.commit((graph.source, graph.preparing))
    missing = Observation.create(
        graph.source.source_id,
        "frm_" + "5" * 64,
        0,
        graph.frames[0].pts,
        graph.observations[0].geometry,
        "vehicle",
        700_000,
        graph.observations[0].producer,
    )
    _assert_error(
        lambda: store.commit_perception_for_run(graph.preparing.run_id, (missing,)),
        PortErrorCode.CONFLICT,
    )

    store.commit((graph.frames[0],))
    mismatched = Observation.create(
        graph.source.source_id,
        graph.frames[0].frame_id,
        0,
        MediaTime("1", graph.frames[0].pts.time_base),
        graph.observations[0].geometry,
        "vehicle",
        700_001,
        graph.observations[0].producer,
    )
    _assert_error(
        lambda: store.commit_perception_for_run(graph.preparing.run_id, (mismatched,)),
        PortErrorCode.CONFLICT,
    )
    _assert_error(
        lambda: store.commit_perception_for_run(
            graph.preparing.run_id,
            (graph.observations[0],),
        ),
        PortErrorCode.CONFLICT,
    )
    assert store.verify().observation_count == 0


def test_existing_observation_tracklet_and_intent_are_idempotent_but_not_mutable(
    tmp_path: Path,
) -> None:
    _, store, graph = _prepare(tmp_path / "idempotent")
    store.commit_perception_for_run(graph.preparing.run_id, graph.observations)
    store.commit_perception_for_run(
        graph.preparing.run_id,
        (graph.tracklet,),
        graph.intents,
    )

    corruptions = (
        "UPDATE observations SET record_json = x'7b7d' WHERE observation_id = ?",
        "UPDATE tracklets SET record_json = x'7b7d' WHERE tracklet_id = ?",
        """UPDATE selected_evidence SET selector_version = 'changed'
        WHERE run_id = ? AND tracklet_id = ? AND rank = 1""",
    )
    for index, statement in enumerate(corruptions):
        root = tmp_path / f"conflict-{index}"
        _, selected_store, selected_graph = _prepare(root)
        selected_parameters = (
            (selected_graph.observations[0].observation_id,),
            (selected_graph.tracklet.tracklet_id,),
            (selected_graph.preparing.run_id, selected_graph.tracklet.tracklet_id),
        )
        with _database(selected_store.root) as connection:
            connection.execute(statement, selected_parameters[index])
        with pytest.raises(PortError) as rejected:
            if index == 0:
                selected_store.commit_perception_for_run(
                    selected_graph.preparing.run_id,
                    (selected_graph.observations[0],),
                )
            elif index == 1:
                selected_store.commit_perception_for_run(
                    selected_graph.preparing.run_id,
                    (selected_graph.tracklet,),
                )
            else:
                selected_store.commit_perception_for_run(
                    selected_graph.preparing.run_id,
                    evidence_intents=(selected_graph.intents[0],),
                )
        assert rejected.value.code is PortErrorCode.CONFLICT
        assert rejected.value.__cause__ is None


def test_cross_source_tracklet_and_inconsistent_intent_are_rejected(tmp_path: Path) -> None:
    local = _graph(fingerprint_byte="81")
    foreign = _graph(fingerprint_byte="82", sample_rate="8")
    store = LocalWorldStore(tmp_path / "store")
    store.commit((local.source, local.preparing, foreign.source, foreign.preparing))
    store.commit_for_run(local.preparing.run_id, local.frames)
    store.commit_for_run(foreign.preparing.run_id, foreign.frames)
    store.commit_perception_for_run(foreign.preparing.run_id, foreign.observations)
    store.commit_perception_for_run(foreign.preparing.run_id, (foreign.tracklet,))

    _assert_error(
        lambda: store.commit_perception_for_run(
            local.preparing.run_id,
            (foreign.tracklet,),
        ),
        PortErrorCode.CONFLICT,
    )

    store.commit_perception_for_run(local.preparing.run_id, local.observations)
    _assert_error(
        lambda: store.commit_perception_for_run(
            local.preparing.run_id,
            evidence_intents=(local.intents[0],),
        ),
        PortErrorCode.CONFLICT,
    )
    store.commit_perception_for_run(local.preparing.run_id, (local.tracklet,))
    inconsistent = replace(local.intents[0], frame_id=local.frames[1].frame_id)
    _assert_error(
        lambda: store.commit_perception_for_run(
            local.preparing.run_id,
            evidence_intents=(inconsistent,),
        ),
        PortErrorCode.CONFLICT,
    )


def test_tracklet_and_intent_reject_canonical_records_of_the_wrong_type(tmp_path: Path) -> None:
    graph = _graph()
    store = LocalWorldStore(tmp_path / "store")
    store.commit((graph.source, graph.preparing))
    store.commit_for_run(graph.preparing.run_id, graph.frames)
    store.commit_perception_for_run(graph.preparing.run_id, graph.observations)

    with _database(store.root) as connection:
        connection.execute(
            "UPDATE observations SET record_json = ? WHERE observation_id = ?",
            (
                dumps_perception_record(graph.tracklet),
                graph.observations[0].observation_id,
            ),
        )
    _assert_error(
        lambda: store.commit_perception_for_run(
            graph.preparing.run_id,
            (graph.tracklet,),
        ),
        PortErrorCode.CORRUPT,
    )

    _, intent_store, selected = _prepare(tmp_path / "intent")
    with _database(intent_store.root) as connection:
        connection.execute(
            "UPDATE tracklets SET record_json = ? WHERE tracklet_id = ?",
            (
                dumps_perception_record(selected.observations[0]),
                selected.tracklet.tracklet_id,
            ),
        )
    _assert_error(
        lambda: intent_store.commit_perception_for_run(
            selected.preparing.run_id,
            evidence_intents=(selected.intents[0],),
        ),
        PortErrorCode.CORRUPT,
    )


def test_committed_run_rejects_further_perception_writes(tmp_path: Path) -> None:
    _, store, graph = _publish(tmp_path / "store")
    _assert_error(
        lambda: store.commit_perception_for_run(graph.committed.run_id),
        PortErrorCode.CONFLICT,
    )
    assert tuple(
        item.intent.rank
        for item in store.list_selected_evidence(
            graph.committed.run_id,
            graph.tracklet.tracklet_id,
            after_rank=1,
            limit=8,
        )
    ) == (2, 3)


@pytest.mark.parametrize("artifact_state", ["missing", "corrupt"])
def test_link_selected_evidence_requires_valid_cas_bytes(
    tmp_path: Path,
    artifact_state: str,
) -> None:
    evidence_store, store, graph = _prepare(tmp_path / artifact_state)
    content = f"coverage-{artifact_state}".encode()
    with evidence_store.writer_session() as session:
        evidence = _add_evidence(
            store,
            session,
            graph,
            content,
            geometry=graph.intents[0].geometry,
            commit_artifact=artifact_state == "corrupt",
        )
        if artifact_state == "corrupt":
            path = _artifact_path(store.root, evidence.artifact)
            path.chmod(0o600)
            path.write_bytes(b"x" * len(content))
            path.chmod(0o400)
        _assert_error(
            lambda: _link(store, session, graph, evidence),
            PortErrorCode.CONFLICT if artifact_state == "missing" else PortErrorCode.CORRUPT,
        )


def test_link_rejects_metadata_mismatch_and_a_different_second_reference(tmp_path: Path) -> None:
    evidence_store, store, graph = _prepare(tmp_path / "store")
    with evidence_store.writer_session() as session:
        mismatched = _add_evidence(
            store,
            session,
            graph,
            b"wrong-geometry",
            geometry=None,
        )
        _assert_error(
            lambda: _link(store, session, graph, mismatched),
            PortErrorCode.CONFLICT,
        )

        first = _add_evidence(
            store,
            session,
            graph,
            b"first-valid-reference",
            geometry=graph.intents[0].geometry,
        )
        second = _add_evidence(
            store,
            session,
            graph,
            b"second-valid-reference",
            geometry=graph.intents[0].geometry,
        )
        _link(store, session, graph, first)
        _assert_error(
            lambda: _link(store, session, graph, second),
            PortErrorCode.CONFLICT,
        )


@pytest.mark.parametrize("state", ["no-session", "missing", "corrupt"])
def test_finalize_requires_session_and_rechecks_linked_cas(tmp_path: Path, state: str) -> None:
    root = tmp_path / state
    evidence_store, store, graph = _prepare(root)
    content = f"finalize-{state}".encode()
    with evidence_store.writer_session() as session:
        evidence = _add_evidence(
            store,
            session,
            graph,
            content,
            geometry=graph.intents[0].geometry,
        )
        _link(store, session, graph, evidence)
        if state == "missing":
            session.delete_artifact(evidence.artifact)
        elif state == "corrupt":
            path = _artifact_path(root, evidence.artifact)
            path.chmod(0o600)
            path.write_bytes(b"z" * len(content))
            path.chmod(0o400)
        if state != "no-session":
            _assert_error(
                lambda: store.finalize_run(graph.committed, evidence_session=session),
                PortErrorCode.CONFLICT if state == "missing" else PortErrorCode.CORRUPT,
            )
    if state == "no-session":
        _assert_error(
            lambda: store.finalize_run(graph.committed),
            PortErrorCode.INVALID_REQUEST,
        )


def test_finalize_requires_artifact_intent_for_every_selected_reference(tmp_path: Path) -> None:
    evidence_store, store, graph = _prepare(tmp_path / "store")
    with evidence_store.writer_session() as session:
        evidence = _add_evidence(
            store,
            session,
            graph,
            b"removed-intent",
            geometry=graph.intents[0].geometry,
        )
        _link(store, session, graph, evidence)
        with _database(store.root) as connection:
            connection.execute(
                "DELETE FROM artifact_intents WHERE run_id = ?",
                (graph.preparing.run_id,),
            )
        _assert_error(
            lambda: store.finalize_run(graph.committed, evidence_session=session),
            PortErrorCode.CONFLICT,
        )


@pytest.mark.parametrize(
    "case",
    [
        "get-wrong-id",
        "observation-is-tracklet",
        "tracklet-is-observation",
        "member-is-tracklet",
        "selection-tracklet-is-observation",
        "invalid-observation-json",
        "invalid-intent-json",
    ],
)
def test_public_reads_fail_closed_on_wrong_record_types_and_invalid_json(
    tmp_path: Path,
    case: str,
) -> None:
    _, store, graph = _publish(tmp_path / case)
    other_observation = graph.observations[1]
    with _database(store.root) as connection:
        if case == "get-wrong-id":
            connection.execute(
                "UPDATE observations SET record_json = ? WHERE observation_id = ?",
                (
                    dumps_perception_record(other_observation),
                    graph.observations[0].observation_id,
                ),
            )
        elif case == "observation-is-tracklet":
            connection.execute(
                "UPDATE observations SET record_json = ? WHERE observation_id = ?",
                (
                    dumps_perception_record(graph.tracklet),
                    graph.observations[0].observation_id,
                ),
            )
        elif case == "tracklet-is-observation":
            connection.execute(
                "UPDATE tracklets SET record_json = ? WHERE tracklet_id = ?",
                (
                    dumps_perception_record(graph.observations[0]),
                    graph.tracklet.tracklet_id,
                ),
            )
        elif case == "member-is-tracklet":
            connection.execute(
                "UPDATE observations SET record_json = ? WHERE observation_id = ?",
                (
                    dumps_perception_record(graph.tracklet),
                    graph.observations[0].observation_id,
                ),
            )
        elif case == "selection-tracklet-is-observation":
            connection.execute(
                "UPDATE tracklets SET record_json = ? WHERE tracklet_id = ?",
                (
                    dumps_perception_record(graph.observations[0]),
                    graph.tracklet.tracklet_id,
                ),
            )
        elif case == "invalid-observation-json":
            connection.execute(
                "UPDATE observations SET record_json = x'7b7d' WHERE observation_id = ?",
                (graph.observations[0].observation_id,),
            )
        else:
            connection.execute(
                """UPDATE selected_evidence SET intent_json = x'7b7d'
                WHERE run_id = ? AND tracklet_id = ? AND rank = 1""",
                (graph.committed.run_id, graph.tracklet.tracklet_id),
            )

    def read() -> object:
        if case in {"get-wrong-id", "invalid-observation-json"}:
            return store.get_perception(graph.observations[0].observation_id)
        if case == "observation-is-tracklet":
            return store.list_run_observations(
                graph.committed.run_id,
                stream_index=0,
                limit=64,
            )
        if case == "tracklet-is-observation":
            return store.list_run_tracklets(
                graph.committed.run_id,
                stream_index=0,
                limit=64,
            )
        if case == "member-is-tracklet":
            return store.list_tracklet_observations(graph.tracklet.tracklet_id, limit=64)
        return store.list_selected_evidence(
            graph.committed.run_id,
            graph.tracklet.tracklet_id,
            limit=8,
        )

    _assert_error(read, PortErrorCode.CORRUPT)


@pytest.mark.parametrize("case", ["projection", "ownership", "intent-scope"])
def test_selected_evidence_read_verifies_projection_ownership_and_intent_scope(
    tmp_path: Path,
    case: str,
) -> None:
    _, store, graph = _publish(tmp_path / case)
    with _database(store.root) as connection:
        if case == "projection":
            connection.execute(
                """UPDATE selected_evidence SET selector_version = 'changed'
                WHERE run_id = ? AND tracklet_id = ? AND rank = 1""",
                (graph.committed.run_id, graph.tracklet.tracklet_id),
            )
        elif case == "ownership":
            connection.execute(
                """DELETE FROM perception_run_records
                WHERE run_id = ? AND record_id = ? AND record_type = 'observation'""",
                (graph.committed.run_id, graph.intents[0].observation_id),
            )
        else:
            changed = replace(graph.intents[0], frame_id=graph.frames[1].frame_id)
            encoded = dumps_evidence_intent(changed)
            connection.execute(
                """UPDATE selected_evidence SET frame_id = ?, intent_json = ?,
                intent_sha256 = ? WHERE run_id = ? AND tracklet_id = ? AND rank = 1""",
                (
                    changed.frame_id,
                    encoded,
                    hashlib.sha256(encoded).hexdigest(),
                    graph.committed.run_id,
                    graph.tracklet.tracklet_id,
                ),
            )
    _assert_error(
        lambda: store.list_selected_evidence(
            graph.committed.run_id,
            graph.tracklet.tracklet_id,
            limit=8,
        ),
        PortErrorCode.CORRUPT,
    )


@pytest.mark.parametrize(
    "case",
    ["wrong-record-type", "metadata-mismatch", "invalid-evidence-id"],
)
def test_materialized_selection_read_revalidates_exact_evidence(
    tmp_path: Path,
    case: str,
) -> None:
    root = tmp_path / case
    evidence_store, store, graph = _prepare(root)
    with evidence_store.writer_session() as session:
        correct = _add_evidence(
            store,
            session,
            graph,
            b"correct-evidence",
            geometry=graph.intents[0].geometry,
        )
        mismatched = _add_evidence(
            store,
            session,
            graph,
            b"mismatched-evidence",
            geometry=None,
        )
        _link(store, session, graph, correct)
        store.finalize_run(graph.committed, evidence_session=session)

    with _database(store.root) as connection:
        if case == "wrong-record-type":
            connection.execute(
                "UPDATE evidence SET record_json = ? WHERE evidence_id = ?",
                (dumps_record(graph.frames[0]), correct.evidence_id),
            )
        else:
            selected_evidence_id = mismatched.evidence_id
            if case == "invalid-evidence-id":
                selected_evidence_id = "bad_" + "f" * 64
                connection.execute(
                    """INSERT INTO evidence(
                        evidence_id, frame_id, artifact_digest, schema_version,
                        identity_version, record_json, record_sha256
                    ) SELECT ?, frame_id, artifact_digest, schema_version,
                        identity_version, record_json, record_sha256
                    FROM evidence WHERE evidence_id = ?""",
                    (selected_evidence_id, correct.evidence_id),
                )
                connection.execute(
                    """INSERT INTO artifact_references(evidence_id, artifact_digest)
                    SELECT ?, artifact_digest FROM artifact_references WHERE evidence_id = ?""",
                    (selected_evidence_id, correct.evidence_id),
                )
                connection.execute(
                    """INSERT INTO run_records(run_id, record_id, record_type)
                    VALUES (?, ?, 'evidence')""",
                    (graph.committed.run_id, selected_evidence_id),
                )
            connection.execute(
                """UPDATE selected_evidence SET evidence_id = ?
                WHERE run_id = ? AND tracklet_id = ? AND rank = 1""",
                (
                    selected_evidence_id,
                    graph.committed.run_id,
                    graph.tracklet.tracklet_id,
                ),
            )
    _assert_error(
        lambda: store.list_selected_evidence(
            graph.committed.run_id,
            graph.tracklet.tracklet_id,
            limit=8,
        ),
        PortErrorCode.CORRUPT,
    )


def test_get_tracklet_rejects_a_wrong_type_child_record(tmp_path: Path) -> None:
    _, store, graph = _publish(tmp_path / "store")
    with _database(store.root) as connection:
        connection.execute(
            "UPDATE observations SET record_json = ? WHERE observation_id = ?",
            (
                dumps_perception_record(graph.tracklet),
                graph.observations[0].observation_id,
            ),
        )
    _assert_error(
        lambda: store.get_perception(graph.tracklet.tracklet_id),
        PortErrorCode.CORRUPT,
    )


def test_finalize_rejects_foreign_source_and_incomplete_tracklet_ownership(
    tmp_path: Path,
) -> None:
    local = _graph(fingerprint_byte="91")
    foreign = _graph(fingerprint_byte="92", sample_rate="8")
    store = LocalWorldStore(tmp_path / "foreign")
    store.commit((local.source, local.preparing, foreign.source, foreign.preparing))
    store.commit_for_run(local.preparing.run_id, local.frames)
    store.commit_for_run(foreign.preparing.run_id, foreign.frames)
    store.commit_perception_for_run(foreign.preparing.run_id, foreign.observations)
    store.commit_perception_for_run(foreign.preparing.run_id, (foreign.tracklet,))
    with _database(store.root) as connection:
        connection.execute(
            """INSERT INTO perception_run_records(run_id, record_id, record_type)
            VALUES (?, ?, 'observation')""",
            (local.preparing.run_id, foreign.observations[0].observation_id),
        )
    _assert_error(lambda: store.finalize_run(local.committed), PortErrorCode.CORRUPT)

    shared = _graph(fingerprint_byte="93")
    second_sampling = Sampling(Rational("8", "1"))
    second_preparing = RunManifest.create(shared.source.source_id, (), second_sampling, "preparing")
    second_store = LocalWorldStore(tmp_path / "incomplete")
    second_store.commit((shared.source, shared.preparing, second_preparing))
    second_store.commit_for_run(shared.preparing.run_id, shared.frames)
    second_store.commit_for_run(second_preparing.run_id, shared.frames)
    second_store.commit_perception_for_run(second_preparing.run_id, shared.observations)
    second_store.commit_perception_for_run(second_preparing.run_id, (shared.tracklet,))
    with _database(second_store.root) as connection:
        connection.execute(
            """INSERT INTO perception_run_records(run_id, record_id, record_type)
            VALUES (?, ?, 'tracklet')""",
            (shared.preparing.run_id, shared.tracklet.tracklet_id),
        )
    _assert_error(lambda: second_store.finalize_run(shared.committed), PortErrorCode.CORRUPT)


def test_finalize_bounds_perception_graph_and_rejects_dangling_owned_record(
    tmp_path: Path,
) -> None:
    _, _, bounded_graph = _prepare(tmp_path / "bounded")
    bounded_store = LocalWorldStore(tmp_path / "bounded", max_audit_records=1)
    _assert_error(
        lambda: bounded_store.finalize_run(bounded_graph.committed),
        PortErrorCode.LIMIT_EXCEEDED,
    )

    _, dangling_store, dangling = _prepare(tmp_path / "dangling")
    with _database(dangling_store.root) as connection:
        connection.execute(
            """INSERT INTO perception_run_records(run_id, record_id, record_type)
            VALUES (?, ?, 'observation')""",
            (dangling.preparing.run_id, "obs_" + "f" * 64),
        )
    _assert_error(
        lambda: dangling_store.finalize_run(dangling.committed),
        PortErrorCode.CORRUPT,
    )


def test_finalize_rejects_observation_reuse_rank_gaps_and_mixed_selectors(
    tmp_path: Path,
) -> None:
    graph = _graph(fingerprint_byte="a1")
    other_sampling = Sampling(Rational("8", "1"))
    other_preparing = RunManifest.create(graph.source.source_id, (), other_sampling, "preparing")
    other_tracklet = Tracklet.create(
        graph.source.source_id,
        0,
        "vehicle",
        tuple(TrackPoint.from_observation(item) for item in graph.observations),
        "source_end",
        Producer("visualworld.coverage-tracker-two", "1", "a2" * 32),
    )
    reuse_store = LocalWorldStore(tmp_path / "reuse")
    reuse_store.commit((graph.source, graph.preparing, other_preparing))
    reuse_store.commit_for_run(graph.preparing.run_id, graph.frames)
    reuse_store.commit_for_run(other_preparing.run_id, graph.frames)
    reuse_store.commit_perception_for_run(graph.preparing.run_id, graph.observations)
    reuse_store.commit_perception_for_run(graph.preparing.run_id, (graph.tracklet,))
    reuse_store.commit_perception_for_run(other_preparing.run_id, graph.observations)
    reuse_store.commit_perception_for_run(other_preparing.run_id, (other_tracklet,))
    with _database(reuse_store.root) as connection:
        connection.execute(
            """INSERT INTO perception_run_records(run_id, record_id, record_type)
            VALUES (?, ?, 'tracklet')""",
            (graph.preparing.run_id, other_tracklet.tracklet_id),
        )
    _assert_error(lambda: reuse_store.finalize_run(graph.committed), PortErrorCode.CORRUPT)

    _, gap_store, gap = _prepare(tmp_path / "rank-gap")
    with _database(gap_store.root) as connection:
        connection.execute(
            """DELETE FROM selected_evidence
            WHERE run_id = ? AND tracklet_id = ? AND rank = 2""",
            (gap.preparing.run_id, gap.tracklet.tracklet_id),
        )
    _assert_error(lambda: gap_store.finalize_run(gap.committed), PortErrorCode.CONFLICT)

    _, selector_store, selected = _prepare(tmp_path / "selector")
    alternate = (
        BestFrameEvidenceSelector(limits=EvidenceSelectionLimits(2))
        .plan(
            selected.tracklet,
            selected.observations,
        )
        .intents[1]
    )
    encoded = dumps_evidence_intent(alternate)
    with _database(selector_store.root) as connection:
        connection.execute(
            """UPDATE selected_evidence SET observation_id = ?, source_id = ?,
            frame_id = ?, stream_index = ?, selector_name = ?, selector_version = ?,
            selector_configuration_sha256 = ?, intent_json = ?, intent_sha256 = ?
            WHERE run_id = ? AND tracklet_id = ? AND rank = 2""",
            (
                alternate.observation_id,
                alternate.source_id,
                alternate.frame_id,
                alternate.stream_index,
                alternate.selector.name,
                alternate.selector.version,
                alternate.selector.configuration_sha256,
                encoded,
                hashlib.sha256(encoded).hexdigest(),
                selected.preparing.run_id,
                selected.tracklet.tracklet_id,
            ),
        )
    _assert_error(lambda: selector_store.finalize_run(selected.committed), PortErrorCode.CONFLICT)
