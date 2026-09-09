# SPDX-License-Identifier: Apache-2.0
"""Vertical, crash-recovery, deletion, and privacy tests for issue #16."""

from __future__ import annotations

import hashlib
import sqlite3
import traceback
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import cast

import pytest

import visualworld.coordinator as coordinator_module
from visualworld.coordinator import (
    CommitBoundary,
    CoordinatorError,
    CoordinatorErrorCode,
    CoordinatorEvent,
    CoordinatorStage,
    EventStatus,
    FaultHook,
    IngestionConfig,
    IngestionCoordinator,
    IngestionDisposition,
    IngestionResult,
    ManualEvidenceInput,
    RecoveryReport,
    RepairReport,
    new_deletion_id,
    sample_index_bytes,
)
from visualworld.ingestion import (
    Artifact,
    EvidenceRef,
    Fingerprint,
    FrameRef,
    MediaTime,
    Rational,
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
)
from visualworld.ports import (
    CapabilityDescriptor,
    FakeFrameSampler,
    FakeVideoSource,
    PortError,
    PortErrorCode,
    PortKind,
    VideoSource,
)
from visualworld.storage import InventoryKind, LocalEvidenceStore, StageHandle
from visualworld.world_store import (
    DeletionArtifact,
    DeletionPlan,
    DeletionState,
    DeletionStatus,
    LocalWorldStore,
    RunCleanupPlan,
)


class SimulatedCrash(BaseException):
    pass


def _fixture(
    marker: bytes = b"source-one",
) -> tuple[Source, tuple[FrameRef, ...], bytes]:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(hashlib.sha256(marker).hexdigest(), str(len(marker))),
        (SourceStream(0, 2, 2, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(
            source.source_id,
            0,
            str(index),
            MediaTime(str(index * 200), time_base),
        )
        for index in range(2)
    )
    pixels = bytes(range(12))
    return source, frames, pixels


def _coordinator(root: Path) -> tuple[IngestionCoordinator, LocalEvidenceStore, LocalWorldStore]:
    evidence = LocalEvidenceStore(root, max_payload_bytes=1024)
    world = LocalWorldStore(root)
    return IngestionCoordinator(evidence, world), evidence, world


def _ingest(
    coordinator: IngestionCoordinator,
    source: Source,
    frames: tuple[FrameRef, ...],
    pixels: bytes,
    *,
    fault_hook: FaultHook | None = None,
) -> IngestionResult:
    video = FakeVideoSource(source, frames)
    sampler = FakeFrameSampler((frames[1].frame_id,))
    config = IngestionConfig(Sampling(Rational("5", "1")), max_frame_bytes=12)
    manual = (ManualEvidenceInput(frames[1].frame_id, pixels, (1, 0, 2, 2)),)
    return coordinator.ingest(video, sampler, config, manual, fault_hook=fault_hook)


def test_fake_vertical_run_is_deterministic_retrievable_and_redacted(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    events: list[CoordinatorEvent] = []
    evidence_one = LocalEvidenceStore(tmp_path / "one", max_payload_bytes=1024)
    world_one = LocalWorldStore(tmp_path / "one")
    coordinator_one = IngestionCoordinator(evidence_one, world_one, event_sink=events.append)

    first = _ingest(coordinator_one, source, frames, pixels)
    retry = _ingest(coordinator_one, source, frames, pixels)
    coordinator_two, evidence_two, _ = _coordinator(tmp_path / "two")
    repeated = _ingest(coordinator_two, source, frames, pixels)

    expected_crop = pixels[3:6] + pixels[9:12]
    assert first.disposition is IngestionDisposition.COMMITTED
    assert retry.disposition is IngestionDisposition.ALREADY_COMMITTED
    assert first.manifest == retry.manifest == repeated.manifest
    assert first.evidence == retry.evidence == repeated.evidence
    assert first.frames == (frames[1],)
    assert evidence_one.get(first.evidence[0].artifact.sha256) == expected_crop
    assert evidence_two.get(first.evidence[0].artifact.sha256) == expected_crop
    assert world_one.get(first.manifest.run_id) == first.manifest
    assert world_one.get(first.evidence[0].evidence_id) == first.evidence[0]
    assert all(event.status is EventStatus.SUCCEEDED for event in first.events)
    rendered = repr(first.events) + repr(events) + repr(asdict(first.events[0]))
    assert repr(pixels) not in rendered
    assert repr(expected_crop) not in rendered
    assert "frame_rgb24" not in rendered


def test_duplicate_crop_artifact_is_staged_once_and_pixels_bind_manifest(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, evidence, world = _coordinator(tmp_path / "store")
    video = FakeVideoSource(source, frames)
    sampler = FakeFrameSampler(tuple(frame.frame_id for frame in frames))
    config = IngestionConfig(Sampling(Rational("5", "1")), max_frame_bytes=12)
    manual = tuple(ManualEvidenceInput(frame.frame_id, pixels, (0, 0, 2, 2)) for frame in frames)

    first = coordinator.ingest(video, sampler, config, manual)
    retried = coordinator.ingest(video, sampler, config, manual)

    assert first.manifest == retried.manifest
    assert len({item.artifact.sha256 for item in first.evidence}) == 1
    assert world.verify().artifact_count == 1
    assert evidence.get(first.evidence[0].artifact.sha256) == pixels

    changed_pixels = bytes(reversed(pixels))
    changed = coordinator.ingest(
        video,
        sampler,
        config,
        tuple(
            ManualEvidenceInput(frame.frame_id, changed_pixels, (0, 0, 2, 2)) for frame in frames
        ),
    )
    assert changed.manifest.run_id != first.manifest.run_id
    assert changed.evidence[0].artifact.sha256 != first.evidence[0].artifact.sha256


def test_ingest_preflights_recovery_budget_and_legacy_retry_inspects_incrementally(
    tmp_path: Path,
) -> None:
    source, frames, pixels = _fixture()
    root = tmp_path / "store"
    video = FakeVideoSource(source, frames)
    sampler = FakeFrameSampler(tuple(frame.frame_id for frame in frames))
    config = IngestionConfig(
        Sampling(Rational("5", "1")),
        max_frame_bytes=12,
        max_total_frame_bytes=24,
    )
    manual = (
        ManualEvidenceInput(frames[0].frame_id, pixels, (0, 0, 2, 2)),
        ManualEvidenceInput(frames[1].frame_id, bytes(reversed(pixels)), (0, 0, 2, 2)),
    )
    constrained_evidence = LocalEvidenceStore(
        root,
        max_payload_bytes=1024,
        max_inventory_bytes=12,
    )
    world = LocalWorldStore(root)
    constrained = IngestionCoordinator(constrained_evidence, world)

    with pytest.raises(CoordinatorError) as rejected:
        constrained.ingest(video, sampler, config, manual)
    assert rejected.value.stage is CoordinatorStage.STAGE
    assert world.verify().record_count == 0
    assert constrained_evidence.inventory().entries == ()

    admitted_evidence = LocalEvidenceStore(
        root,
        max_payload_bytes=1024,
        max_inventory_bytes=24,
    )
    admitted = IngestionCoordinator(admitted_evidence, world)
    committed = admitted.ingest(video, sampler, config, manual)
    retried = constrained.ingest(video, sampler, config, manual)

    assert retried.disposition is IngestionDisposition.ALREADY_COMMITTED
    assert retried.manifest == committed.manifest


@pytest.mark.parametrize(
    "boundary",
    [
        CommitBoundary.PREPARED,
        CommitBoundary.STAGED,
        CommitBoundary.INTENTS_RECORDED,
        CommitBoundary.ARTIFACTS_PROMOTED,
        CommitBoundary.FRAME_METADATA_RECORDED,
        CommitBoundary.EVIDENCE_METADATA_RECORDED,
        CommitBoundary.RUN_COMMITTED,
    ],
)
def test_crash_at_every_ingestion_boundary_retries_without_partial_success(
    tmp_path: Path,
    boundary: CommitBoundary,
) -> None:
    source, frames, pixels = _fixture(boundary.value.encode("ascii"))
    coordinator, evidence, world = _coordinator(tmp_path / boundary.value)

    def crash(selected: CommitBoundary, _: str) -> None:
        if selected is boundary:
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        _ingest(coordinator, source, frames, pixels, fault_hook=crash)

    if boundary is not CommitBoundary.RUN_COMMITTED:
        assert world.list_frames(source.source_id, stream_index=0, limit=64) == ()
    result = _ingest(coordinator, source, frames, pixels)

    assert result.manifest.state == "committed"
    assert world.list_frames(source.source_id, stream_index=0, limit=64) == result.frames
    assert evidence.get(result.evidence[0].artifact.sha256) == pixels[3:6] + pixels[9:12]
    inventory = evidence.inventory()
    assert {entry.kind for entry in inventory.entries} == {InventoryKind.ARTIFACT}


def test_explicit_repair_removes_only_unreferenced_artifacts(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, evidence, _ = _coordinator(tmp_path / "store")
    result = _ingest(coordinator, source, frames, pixels)
    orphans = tuple(
        (
            content,
            Artifact(hashlib.sha256(content).hexdigest(), str(len(content))),
        )
        for index in range(65)
        for content in (f"orphan-{index}".encode("ascii"),)
    )
    for content, orphan in orphans:
        evidence.put(orphan, content)

    report = coordinator.repair()

    assert report.orphan_artifacts_removed == 65
    assert report.integrity_issues == 0
    assert evidence.get(result.evidence[0].artifact.sha256) == pixels[3:6] + pixels[9:12]
    with pytest.raises(PortError) as raised:
        evidence.get(orphans[-1][1].sha256)
    assert raised.value.code is PortErrorCode.NOT_FOUND


def test_source_deletion_is_complete_and_receipt_is_reduced(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, evidence, world = _coordinator(tmp_path / "store")
    result = _ingest(coordinator, source, frames, pixels)
    deletion_id = "del_" + "1" * 64

    receipt = coordinator.delete_source(source.source_id, deletion_id=deletion_id)

    assert receipt.state is DeletionState.COMPLETE
    assert receipt.record_count == 4
    assert receipt.artifact_count == 1
    assert receipt.shared_retention_count == 0
    assert source.source_id not in repr(receipt)
    assert result.manifest.run_id not in repr(receipt)
    with pytest.raises(PortError, match="not_found"):
        world.get(source.source_id)
    with pytest.raises(PortError, match="not_found"):
        evidence.get(result.evidence[0].artifact.sha256)
    assert world.deletion_status(deletion_id) == receipt
    world.verify()


def test_source_deletion_retains_cross_source_deduplicated_evidence(tmp_path: Path) -> None:
    coordinator, evidence, world = _coordinator(tmp_path / "store")
    source_one, frames_one, pixels = _fixture(b"one")
    source_two, frames_two, _ = _fixture(b"two")
    first = _ingest(coordinator, source_one, frames_one, pixels)
    second = _ingest(coordinator, source_two, frames_two, pixels)
    assert first.evidence[0].artifact == second.evidence[0].artifact

    retained = coordinator.delete_source(
        source_one.source_id,
        deletion_id="del_" + "2" * 64,
    )

    assert retained.artifact_count == 0
    assert retained.shared_retention_count == 1
    assert evidence.get(second.evidence[0].artifact.sha256) == pixels[3:6] + pixels[9:12]
    assert world.get(second.evidence[0].evidence_id) == second.evidence[0]

    removed = coordinator.delete_source(
        source_two.source_id,
        deletion_id="del_" + "3" * 64,
    )
    assert removed.artifact_count == 1
    assert removed.shared_retention_count == 0
    with pytest.raises(PortError, match="not_found"):
        evidence.get(second.evidence[0].artifact.sha256)


def test_source_deletion_verifies_more_than_one_port_batch(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, evidence, world = _coordinator(tmp_path / "store")
    result = _ingest(coordinator, source, frames, pixels)
    extras: list[EvidenceRef] = []
    for index in range(64):
        content = f"extra-artifact-{index}".encode("ascii")
        artifact = Artifact(hashlib.sha256(content).hexdigest(), str(len(content)))
        evidence.put(artifact, content)
        extras.append(
            EvidenceRef.create(
                result.frames[0].frame_id,
                artifact,
                result.evidence[0].geometry,
            )
        )
    world.commit(tuple(extras))

    receipt = coordinator.delete_source(
        source.source_id,
        deletion_id="del_" + "e" * 64,
    )

    assert receipt.state is DeletionState.COMPLETE
    assert receipt.artifact_count == 65
    with pytest.raises(PortError, match="not_found"):
        evidence.get(extras[-1].artifact.sha256)


@pytest.mark.parametrize(
    "boundary",
    [
        CommitBoundary.DELETION_PENDING,
        CommitBoundary.DELETION_ARTIFACTS_REMOVED,
        CommitBoundary.DELETION_METADATA_PURGED,
        CommitBoundary.DELETION_CHECKPOINTED,
    ],
)
def test_deletion_retries_from_every_durable_boundary(
    tmp_path: Path,
    boundary: CommitBoundary,
) -> None:
    source, frames, pixels = _fixture(boundary.value.encode("ascii"))
    coordinator, evidence, world = _coordinator(tmp_path / boundary.value)
    result = _ingest(coordinator, source, frames, pixels)
    deletion_id = "del_" + hashlib.sha256(boundary.value.encode("ascii")).hexdigest()

    def crash(selected: CommitBoundary, _: str) -> None:
        if selected is boundary:
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        coordinator.delete_source(
            source.source_id,
            deletion_id=deletion_id,
            fault_hook=crash,
        )

    receipt = coordinator.delete_source(source.source_id, deletion_id=deletion_id)
    assert receipt.state is DeletionState.COMPLETE
    with pytest.raises(PortError, match="not_found"):
        world.get(source.source_id)
    with pytest.raises(PortError, match="not_found"):
        evidence.get(result.evidence[0].artifact.sha256)


def test_recovery_resumes_pending_deletion_without_source_input(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, _, world = _coordinator(tmp_path / "store")
    _ingest(coordinator, source, frames, pixels)
    deletion_id = "del_" + "4" * 64

    def crash(selected: CommitBoundary, _: str) -> None:
        if selected is CommitBoundary.DELETION_PENDING:
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        coordinator.delete_source(
            source.source_id,
            deletion_id=deletion_id,
            fault_hook=crash,
        )

    report = coordinator.recover()
    assert report.deletions_completed == 1
    assert world.deletion_status(deletion_id).state is DeletionState.COMPLETE


def test_invalid_pixel_payload_returns_structured_redacted_error(tmp_path: Path) -> None:
    source, frames, _ = _fixture()
    coordinator, _, _ = _coordinator(tmp_path / "store")
    secret = b"private-pixels"

    with pytest.raises(CoordinatorError) as raised:
        _ingest(coordinator, source, frames, secret)

    assert raised.value.code is CoordinatorErrorCode.INVALID_REQUEST
    assert repr(secret) not in str(raised.value)
    assert raised.value.__cause__ is None


def test_coordinator_value_objects_validate_and_redact(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, evidence, world = _coordinator(tmp_path / "store")
    result = _ingest(coordinator, source, frames, pixels)
    event = result.events[0]
    deletion = DeletionArtifact(result.evidence[0].artifact, True)
    run_plan = RunCleanupPlan(result.manifest.run_id, (), ())
    delete_plan = DeletionPlan("del_" + "5" * 64, (deletion,), (), (), 1, 1, 0)
    status = DeletionStatus(
        "del_" + "5" * 64,
        DeletionState.PENDING,
        1,
        1,
        0,
        None,
    )

    assert "redacted" in repr(ManualEvidenceInput(frames[0].frame_id, pixels, (0, 0, 1, 1)))
    assert "redacted" in repr(deletion)
    assert "artifacts=0" in repr(run_plan)
    assert "artifacts=1" in repr(delete_plan)
    assert status.state is DeletionState.PENDING
    assert sample_index_bytes((frames[0],)).endswith(b"\n")
    assert new_deletion_id().startswith("del_")
    assert len(new_deletion_id()) == 68

    invalid_factories = (
        lambda: IngestionConfig(cast(Sampling, object())),
        lambda: IngestionConfig(result.manifest.sampling, protocol_version=2),
        lambda: IngestionConfig(
            result.manifest.sampling,
            max_frame_bytes=12,
            max_total_frame_bytes=11,
        ),
        lambda: ManualEvidenceInput("bad", b"x", (0, 0, 1, 1)),
        lambda: ManualEvidenceInput(frames[0].frame_id, pixels, (0, 0, 1, 1), "bad"),
        lambda: CoordinatorEvent(CoordinatorStage.PROBE, EventStatus.SUCCEEDED, 0, 0),
        lambda: CoordinatorEvent(
            CoordinatorStage.PROBE,
            EventStatus.SUCCEEDED,
            1,
            0,
            protocol_version=2,
        ),
        lambda: IngestionResult(
            result.manifest,
            result.frames,
            (),
            IngestionDisposition.COMMITTED,
            (),
        ),
        lambda: IngestionResult(
            result.manifest,
            result.frames,
            result.evidence,
            IngestionDisposition.COMMITTED,
            cast(tuple[CoordinatorEvent, ...], (object(),)),
        ),
        lambda: RecoveryReport(-1, 0, 0, 0, ()),
        lambda: RecoveryReport(0, 0, 0, 0, cast(tuple[CoordinatorEvent, ...], (object(),))),
        lambda: RepairReport(cast(RecoveryReport, object()), 0, 0, ()),
        lambda: RepairReport(
            RecoveryReport(0, 0, 0, 0, ()),
            0,
            0,
            cast(tuple[CoordinatorEvent, ...], (object(),)),
        ),
        lambda: DeletionArtifact(cast(Artifact, object()), True),
        lambda: DeletionArtifact(result.evidence[0].artifact, cast(bool, 1)),
        lambda: RunCleanupPlan("bad", (), ()),
        lambda: RunCleanupPlan(result.manifest.run_id, (), (), protocol_version=2),
        lambda: DeletionPlan("bad", (), (), (), 0, 0, 0),
        lambda: DeletionPlan(
            "del_" + "5" * 64,
            (deletion,),
            (),
            (),
            1,
            1,
            0,
            protocol_version=2,
        ),
        lambda: DeletionStatus("bad", DeletionState.PENDING, 0, 0, 0, None),
        lambda: DeletionStatus(
            "del_" + "5" * 64,
            DeletionState.PENDING,
            0,
            0,
            0,
            None,
            protocol_version=2,
        ),
        lambda: sample_index_bytes(cast(tuple[FrameRef, ...], [])),
        lambda: CoordinatorError(
            cast(CoordinatorErrorCode, object()),
            CoordinatorStage.PROBE,
        ),
        lambda: IngestionCoordinator(evidence, LocalWorldStore(tmp_path / "other")),
    )
    for factory in invalid_factories:
        with pytest.raises(ValueError):
            factory()
    assert event.duration_ns > 0
    assert world.verify().record_count == 4


def test_coordinator_maps_adapter_failures_and_rejects_invalid_adapters(tmp_path: Path) -> None:
    coordinator, _, _ = _coordinator(tmp_path / "store")
    errors = (
        (PortErrorCode.INVALID_REQUEST, CoordinatorErrorCode.INVALID_REQUEST),
        (PortErrorCode.CONFLICT, CoordinatorErrorCode.CONFLICT),
        (PortErrorCode.CORRUPT, CoordinatorErrorCode.CORRUPT),
        (PortErrorCode.STORAGE_FAILED, CoordinatorErrorCode.OPERATION_FAILED),
    )
    for port_code, expected in errors:
        mapped = coordinator._map_error(
            PortError(port_code, PortKind.VIDEO_SOURCE, "probe"),
            CoordinatorStage.PROBE,
        )
        assert mapped.code is expected
    assert (
        coordinator._map_error(RuntimeError("private"), CoordinatorStage.PROBE).code
        is CoordinatorErrorCode.OPERATION_FAILED
    )
    original = CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.PROBE)
    assert coordinator._map_error(original, CoordinatorStage.SAMPLE) is not original
    with pytest.raises(CoordinatorError):
        coordinator._validate_adapter(object(), PortKind.VIDEO_SOURCE)

    class ExplodingDescriptor:
        @property
        def descriptor(self) -> object:
            raise RuntimeError("private descriptor detail")

    with pytest.raises(CoordinatorError) as exploding:
        coordinator._validate_adapter(ExplodingDescriptor(), PortKind.VIDEO_SOURCE)
    assert exploding.value.code is CoordinatorErrorCode.INVALID_REQUEST
    assert exploding.value.__context__ is None

    class DescriptorSubclass(CapabilityDescriptor):
        pass

    base = FakeVideoSource(*_fixture()[:2]).descriptor
    subclass = DescriptorSubclass(
        base.port,
        base.implementation,
        base.implementation_version,
        base.deterministic,
        base.offline,
    )

    class SubclassDescriptorAdapter:
        @property
        def descriptor(self) -> CapabilityDescriptor:
            return subclass

    with pytest.raises(CoordinatorError) as subclassed:
        coordinator._validate_adapter(SubclassDescriptorAdapter(), PortKind.VIDEO_SOURCE)
    assert subclassed.value.code is CoordinatorErrorCode.INVALID_REQUEST


def test_adapter_coordinator_error_chain_is_rebuilt_without_private_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, frames, _ = _fixture()
    coordinator, _, _ = _coordinator(tmp_path / "store")
    video = FakeVideoSource(source, frames)
    private_detail = "private-adapter-detail"

    def leaky_probe() -> Source:
        try:
            raise RuntimeError(private_detail)
        except RuntimeError as cause:
            raise CoordinatorError(
                CoordinatorErrorCode.OPERATION_FAILED,
                CoordinatorStage.PROBE,
            ) from cause

    monkeypatch.setattr(video, "probe", leaky_probe)
    with pytest.raises(CoordinatorError) as raised:
        coordinator.ingest(
            video,
            FakeFrameSampler(()),
            IngestionConfig(Sampling(Rational("5", "1")), max_frame_bytes=12),
            (),
        )

    rendered = "".join(traceback.format_exception(raised.value))
    assert private_detail not in rendered
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_validated_adapter_descriptors_are_snapshotted_once(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, _, _ = _coordinator(tmp_path / "store")
    delegate = FakeVideoSource(source, frames)

    class OneShotDescriptorVideo:
        def __init__(self) -> None:
            self.descriptor_reads = 0

        @property
        def descriptor(self) -> CapabilityDescriptor:
            self.descriptor_reads += 1
            if self.descriptor_reads > 1:
                raise RuntimeError("private repeated descriptor detail")
            return delegate.descriptor

        def probe(self) -> Source:
            return delegate.probe()

        def read_frames(
            self,
            *,
            stream_index: int,
            after_decode_index: str | None = None,
            limit: int = 64,
        ) -> tuple[FrameRef, ...]:
            return delegate.read_frames(
                stream_index=stream_index,
                after_decode_index=after_decode_index,
                limit=limit,
            )

    video = OneShotDescriptorVideo()
    result = coordinator.ingest(
        video,
        FakeFrameSampler((frames[1].frame_id,)),
        IngestionConfig(Sampling(Rational("5", "1")), max_frame_bytes=12),
        (ManualEvidenceInput(frames[1].frame_id, pixels, (1, 0, 2, 2)),),
    )

    assert result.manifest.state == "committed"
    assert video.descriptor_reads == 1


def test_adapter_method_lookup_errors_are_sanitized(tmp_path: Path) -> None:
    source, frames, _ = _fixture()
    coordinator, _, _ = _coordinator(tmp_path / "store")
    delegate = FakeVideoSource(source, frames)
    private_detail = "private-method-lookup-detail"

    class ExplodingProbeLookup:
        @property
        def descriptor(self) -> CapabilityDescriptor:
            return delegate.descriptor

        @property
        def probe(self) -> object:
            raise RuntimeError(private_detail)

    with pytest.raises(CoordinatorError) as raised:
        coordinator.ingest(
            cast(VideoSource, ExplodingProbeLookup()),
            FakeFrameSampler(()),
            IngestionConfig(Sampling(Rational("5", "1")), max_frame_bytes=12),
            (),
        )

    rendered = "".join(traceback.format_exception(raised.value))
    assert private_detail not in rendered
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_ingest_rejects_overflow_missing_regions_and_bad_stream(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, _, _ = _coordinator(tmp_path / "store")
    sampling = Sampling(Rational("5", "1"))

    with pytest.raises(CoordinatorError) as overflow:
        coordinator.ingest(
            FakeVideoSource(source, frames),
            FakeFrameSampler((frames[0].frame_id,)),
            IngestionConfig(sampling, max_candidates=1, max_frame_bytes=12),
            (ManualEvidenceInput(frames[0].frame_id, pixels, (0, 0, 1, 1)),),
        )
    assert overflow.value.code is CoordinatorErrorCode.INVALID_REQUEST

    with pytest.raises(CoordinatorError) as aggregate:
        coordinator.ingest(
            FakeVideoSource(source, frames),
            FakeFrameSampler(tuple(frame.frame_id for frame in frames)),
            IngestionConfig(
                sampling,
                max_frame_bytes=12,
                max_total_frame_bytes=12,
            ),
            tuple(ManualEvidenceInput(frame.frame_id, pixels, (0, 0, 1, 1)) for frame in frames),
        )
    assert aggregate.value.stage is CoordinatorStage.CROP

    with pytest.raises(CoordinatorError) as missing:
        coordinator.ingest(
            FakeVideoSource(source, frames),
            FakeFrameSampler((frames[0].frame_id,)),
            IngestionConfig(sampling, max_frame_bytes=12),
            (),
        )
    assert missing.value.stage is CoordinatorStage.CROP

    with pytest.raises(CoordinatorError) as stream:
        coordinator.ingest(
            FakeVideoSource(source, frames),
            FakeFrameSampler((frames[0].frame_id,)),
            IngestionConfig(sampling, stream_index=1, max_frame_bytes=12),
            (),
        )
    assert stream.value.stage is CoordinatorStage.PROBE


def test_event_sink_failure_does_not_change_commit(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    evidence = LocalEvidenceStore(tmp_path / "store", max_payload_bytes=1024)
    world = LocalWorldStore(tmp_path / "store")

    def broken_sink(_: CoordinatorEvent) -> None:
        raise RuntimeError("logging unavailable")

    result = _ingest(
        IngestionCoordinator(evidence, world, event_sink=broken_sink),
        source,
        frames,
        pixels,
    )
    assert result.manifest.state == "committed"


def test_world_coordination_extension_rejects_invalid_phases(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, evidence, world = _coordinator(tmp_path / "store")
    result = _ingest(coordinator, source, frames, pixels)
    missing_run = "run_" + "f" * 64

    for operation in (world.run_cleanup_plan, world.finish_run_cleanup):
        with pytest.raises(PortError) as missing:
            operation(missing_run)
        assert missing.value.code is PortErrorCode.NOT_FOUND
        with pytest.raises(PortError) as committed:
            operation(result.manifest.run_id)
        assert committed.value.code is PortErrorCode.CONFLICT

    assert world.unreferenced_artifacts((result.evidence[0].artifact,)) == ()
    with pytest.raises(PortError) as too_many:
        world.unreferenced_artifacts((result.evidence[0].artifact,) * 65)
    assert too_many.value.code is PortErrorCode.LIMIT_EXCEEDED
    with pytest.raises(PortError) as invalid:
        world.unreferenced_artifacts(cast(tuple[Artifact, ...], (object(),)))
    assert invalid.value.code is PortErrorCode.INVALID_REQUEST
    assert evidence.get(result.evidence[0].artifact.sha256)


def test_world_deletion_phases_are_idempotent_and_auditable(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, evidence, world = _coordinator(tmp_path / "store")
    result = _ingest(coordinator, source, frames, pixels)
    deletion_id = "del_" + "6" * 64

    with evidence.writer_session() as session:
        plan = world.begin_source_deletion(
            source.source_id,
            deletion_id,
            evidence_session=session,
        )
        repeated = world.begin_source_deletion(
            source.source_id,
            "del_" + "7" * 64,
            evidence_session=session,
        )
        assert repeated == plan
        source_two, _, _ = _fixture(b"overlapping-delete")
        world.commit((source_two,), evidence_session=session)
        with pytest.raises(PortError) as overlapping:
            world.begin_source_deletion(
                source_two.source_id,
                "del_" + "d" * 64,
                evidence_session=session,
            )
        assert overlapping.value.code is PortErrorCode.CONFLICT
        assert world.get_deletion_plan(deletion_id) == plan
        assert world.pending_deletions() == (world.deletion_status(deletion_id),)
        assert world.pending_deletions(after_deletion_id=deletion_id) == ()
        world.verify()
        with pytest.raises(PortError) as premature:
            world.complete_deletion(deletion_id, evidence_session=session)
        assert premature.value.code is PortErrorCode.CONFLICT
        with pytest.raises(PortError) as files_present:
            world.purge_deletion_metadata(deletion_id, evidence_session=session)
        assert files_present.value.code is PortErrorCode.CONFLICT
        for item in plan.artifacts:
            if item.delete_required:
                session.delete_artifact(item.artifact)
        purged = world.purge_deletion_metadata(deletion_id, evidence_session=session)
        assert purged.state is DeletionState.METADATA_PURGED
        assert world.purge_deletion_metadata(deletion_id, evidence_session=session) == purged
        with pytest.raises(PortError) as no_plan:
            world.get_deletion_plan(deletion_id)
        assert no_plan.value.code is PortErrorCode.CONFLICT
        completed = world.complete_deletion(deletion_id, evidence_session=session)
        assert world.complete_deletion(deletion_id, evidence_session=session) == completed
    assert completed.state is DeletionState.COMPLETE
    assert world.pending_deletions() == ()
    assert world.deletion_status(deletion_id) == completed
    with pytest.raises(PortError):
        evidence.get(result.evidence[0].artifact.sha256)


def test_world_deletion_rejects_invalid_ids_missing_sources_and_collisions(tmp_path: Path) -> None:
    source, _, _ = _fixture()
    _, evidence, world = _coordinator(tmp_path / "store")
    with pytest.raises(PortError) as invalid:
        world.begin_source_deletion(source.source_id, "bad")
    assert invalid.value.code is PortErrorCode.INVALID_REQUEST
    with pytest.raises(PortError) as missing:
        world.begin_source_deletion(source.source_id, "del_" + "8" * 64)
    assert missing.value.code is PortErrorCode.NOT_FOUND
    with pytest.raises(PortError):
        world.deletion_status("del_" + "9" * 64)
    with pytest.raises(PortError):
        world.pending_deletions(limit=0)
    assert evidence.inventory().entries == ()


def test_pending_deletion_hides_recovery_reads_and_rejects_all_source_writes(
    tmp_path: Path,
) -> None:
    source, frames, pixels = _fixture()
    coordinator, _, world = _coordinator(tmp_path / "store")
    committed = _ingest(coordinator, source, frames, pixels)
    sampling = Sampling(Rational("4", "1"))
    preparing = RunManifest.create(source.source_id, (), sampling, "preparing")
    world.commit((preparing,))
    world.commit_for_run(
        preparing.run_id,
        (committed.frames[0], committed.evidence[0]),
    )
    stage = StageHandle(
        preparing.run_id,
        "f" * 32 + ".part",
        committed.evidence[0].artifact,
    )
    world.record_artifact_intents(preparing.run_id, (stage,))
    deletion_id = "del_" + "f" * 64
    plan = world.begin_source_deletion(source.source_id, deletion_id)
    new_frame = FrameRef.create(
        source.source_id,
        0,
        "2",
        MediaTime("400", source.streams[0].time_base),
    )
    content = b"new-private-evidence"
    artifact = Artifact(hashlib.sha256(content).hexdigest(), str(len(content)))
    new_evidence = EvidenceRef.create(
        committed.frames[0].frame_id,
        artifact,
        committed.evidence[0].geometry,
    )
    new_run = RunManifest.create(
        source.source_id,
        (),
        Sampling(Rational("3", "1")),
        "preparing",
    )
    final = RunManifest.create(
        source.source_id,
        (),
        sampling,
        "committed",
        RunOutputs(
            "1",
            hashlib.sha256(sample_index_bytes((committed.frames[0],))).hexdigest(),
        ),
    )

    operations: tuple[Callable[[], object], ...] = (
        lambda: world.commit((source,)),
        lambda: world.commit((new_frame,)),
        lambda: world.commit((new_evidence,)),
        lambda: world.commit((new_run,)),
        lambda: world.commit_for_run(preparing.run_id, (new_frame,)),
        lambda: world.record_artifact_intents(preparing.run_id, (stage,)),
        lambda: world.finalize_run(final),
        lambda: world.finish_run_cleanup(preparing.run_id),
    )
    for operation in operations:
        with pytest.raises(PortError) as blocked:
            operation()
        assert blocked.value.code is PortErrorCode.CONFLICT

    assert preparing not in world.pending_runs()
    with pytest.raises(PortError) as hidden_intents:
        world.list_artifact_intents(preparing.run_id)
    assert hidden_intents.value.code is PortErrorCode.NOT_FOUND
    with pytest.raises(PortError) as hidden_cleanup:
        world.run_cleanup_plan(preparing.run_id)
    assert hidden_cleanup.value.code is PortErrorCode.NOT_FOUND
    assert world.get_deletion_plan(deletion_id) == plan
    world.verify()


def test_shared_run_cleanup_preserves_records_and_artifact(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, evidence, world = _coordinator(tmp_path / "store")
    committed = _ingest(coordinator, source, frames, pixels)
    second_sampling = Sampling(Rational("4", "1"))
    preparing = RunManifest.create(source.source_id, (), second_sampling, "preparing")
    failed = RunManifest.create(source.source_id, (), Sampling(Rational("3", "1")), "failed")
    world.commit((preparing, failed))
    world.commit_for_run(preparing.run_id, (committed.frames[0], committed.evidence[0]))

    plan = world.run_cleanup_plan(preparing.run_id)
    assert plan.artifacts == ()
    world.finish_run_cleanup(preparing.run_id)
    world.finish_run_cleanup(failed.run_id)

    assert world.get(committed.frames[0].frame_id) == committed.frames[0]
    assert world.get(committed.evidence[0].evidence_id) == committed.evidence[0]
    assert evidence.get(committed.evidence[0].artifact.sha256)


def test_cleanup_detects_conflicting_intent_catalog_metadata(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, _, world = _coordinator(tmp_path / "store")
    committed = _ingest(coordinator, source, frames, pixels)
    preparing = RunManifest.create(
        source.source_id,
        (),
        Sampling(Rational("4", "1")),
        "preparing",
    )
    world.commit((preparing,))
    conflicting = Artifact(
        committed.evidence[0].artifact.sha256,
        str(int(committed.evidence[0].artifact.bytes) + 1),
    )
    world.record_artifact_intents(
        preparing.run_id,
        (StageHandle(preparing.run_id, "0" * 32 + ".part", conflicting),),
    )

    with pytest.raises(PortError) as raised:
        world.run_cleanup_plan(preparing.run_id)
    assert raised.value.code is PortErrorCode.CORRUPT


@pytest.mark.parametrize(
    "crash_boundary",
    [CommitBoundary.STAGED, CommitBoundary.INTENTS_RECORDED],
)
def test_deletion_of_pending_run_removes_all_its_staging(
    tmp_path: Path,
    crash_boundary: CommitBoundary,
) -> None:
    source, frames, pixels = _fixture()
    coordinator, evidence, world = _coordinator(tmp_path / "store")

    def crash(boundary: CommitBoundary, _: str) -> None:
        if boundary is crash_boundary:
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        _ingest(coordinator, source, frames, pixels, fault_hook=crash)
    assert any(entry.kind is InventoryKind.STAGED for entry in evidence.inventory().entries)

    receipt = coordinator.delete_source(
        source.source_id,
        deletion_id="del_" + "a" * 64,
    )
    assert receipt.state is DeletionState.COMPLETE
    assert evidence.inventory().entries == ()
    with pytest.raises(PortError):
        world.get(source.source_id)


def test_recovery_completes_already_purged_deletion(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, _, world = _coordinator(tmp_path / "store")
    _ingest(coordinator, source, frames, pixels)
    deletion_id = "del_" + "b" * 64

    def crash(boundary: CommitBoundary, _: str) -> None:
        if boundary is CommitBoundary.DELETION_METADATA_PURGED:
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        coordinator.delete_source(
            source.source_id,
            deletion_id=deletion_id,
            fault_hook=crash,
        )

    assert world.deletion_status(deletion_id).state is DeletionState.METADATA_PURGED
    with pytest.raises(PortError) as frozen:
        world.commit((source,))
    assert frozen.value.code is PortErrorCode.CONFLICT
    world.verify()

    report = coordinator.recover()
    assert report.deletions_completed == 1
    assert world.deletion_status(deletion_id).state is DeletionState.COMPLETE
    with pytest.raises(PortError, match="not_found"):
        world.get(source.source_id)


def test_verify_existing_rejects_record_drift_and_missing_cas(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, evidence_store, world = _coordinator(tmp_path / "store")
    result = _ingest(coordinator, source, frames, pixels)
    different_manifest = RunManifest.create(
        result.manifest.source_id,
        result.manifest.producers,
        result.manifest.sampling,
        "committed",
        RunOutputs("1", "f" * 64),
    )
    different_frame = FrameRef.create(
        result.frames[0].source_id,
        result.frames[0].stream_index,
        result.frames[0].decode_index,
        result.frames[0].pts,
        key_frame=True,
    )
    different_evidence = EvidenceRef(
        result.evidence[0].evidence_id,
        result.evidence[0].frame_id,
        Artifact(
            result.evidence[0].artifact.sha256,
            str(int(result.evidence[0].artifact.bytes) + 1),
        ),
        result.evidence[0].geometry,
    )
    assert different_frame.frame_id == result.frames[0].frame_id
    assert different_frame != result.frames[0]
    assert different_evidence.evidence_id == result.evidence[0].evidence_id
    assert different_evidence != result.evidence[0]

    with evidence_store.writer_session() as session:
        with pytest.raises(CoordinatorError):
            coordinator._verify_existing(
                session, different_manifest, result.frames, result.evidence, []
            )
        with pytest.raises(CoordinatorError):
            coordinator._verify_existing(
                session,
                result.manifest,
                (different_frame,),
                result.evidence,
                [],
            )
        with pytest.raises(CoordinatorError):
            coordinator._verify_existing(
                session,
                result.manifest,
                result.frames,
                (different_evidence,),
                [],
            )
        session.delete_artifact(result.evidence[0].artifact)
        with pytest.raises(CoordinatorError) as missing:
            coordinator._verify_existing(
                session,
                result.manifest,
                result.frames,
                result.evidence,
                [],
            )
        assert missing.value.code is CoordinatorErrorCode.CORRUPT
    assert world.get(result.manifest.run_id) == result.manifest


def test_invalid_ingest_and_delete_shapes_fail_before_effects(tmp_path: Path) -> None:
    source, frames, _ = _fixture()
    coordinator, _, world = _coordinator(tmp_path / "store")
    with pytest.raises(CoordinatorError):
        coordinator.ingest(
            FakeVideoSource(source, frames),
            FakeFrameSampler(()),
            cast(IngestionConfig, object()),
            (),
        )
    with pytest.raises(CoordinatorError):
        coordinator.delete_source(source.source_id, deletion_id="bad")
    assert world.verify().record_count == 0


def test_malformed_adapter_results_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, frames, pixels = _fixture()
    sampling = Sampling(Rational("5", "1"))
    manual = (ManualEvidenceInput(frames[0].frame_id, pixels, (0, 0, 1, 1)),)

    coordinator, _, _ = _coordinator(tmp_path / "candidates")
    video = FakeVideoSource(source, frames)
    monkeypatch.setattr(
        video,
        "read_frames",
        lambda **_arguments: cast(tuple[FrameRef, ...], (object(),)),
    )
    with pytest.raises(CoordinatorError) as bad_candidates:
        coordinator.ingest(
            video,
            FakeFrameSampler(()),
            IngestionConfig(sampling, max_frame_bytes=12),
            (),
        )
    assert bad_candidates.value.stage is CoordinatorStage.PROBE

    coordinator, _, _ = _coordinator(tmp_path / "samples")
    sampler = FakeFrameSampler((frames[0].frame_id,))
    monkeypatch.setattr(
        sampler,
        "sample",
        lambda *_arguments: (frames[0], frames[0]),
    )
    with pytest.raises(CoordinatorError) as bad_samples:
        coordinator.ingest(
            FakeVideoSource(source, frames),
            sampler,
            IngestionConfig(sampling, max_frame_bytes=12),
            manual,
        )
    assert bad_samples.value.stage is CoordinatorStage.SAMPLE

    coordinator, _, _ = _coordinator(tmp_path / "too-many-candidates")
    video = FakeVideoSource(source, frames)
    monkeypatch.setattr(video, "read_frames", lambda **_arguments: (frames[0],) * 65)
    with pytest.raises(CoordinatorError) as too_many:
        coordinator.ingest(
            video,
            FakeFrameSampler(()),
            IngestionConfig(sampling, max_frame_bytes=12),
            (),
        )
    assert too_many.value.stage is CoordinatorStage.PROBE


def test_exact_candidate_limit_without_overflow_continues(tmp_path: Path) -> None:
    source, frames, pixels = _fixture()
    coordinator, _, _ = _coordinator(tmp_path / "store")
    result = coordinator.ingest(
        FakeVideoSource(source, frames),
        FakeFrameSampler((frames[0].frame_id,)),
        IngestionConfig(
            Sampling(Rational("5", "1")),
            max_candidates=2,
            max_frame_bytes=12,
        ),
        (ManualEvidenceInput(frames[0].frame_id, pixels, (0, 0, 1, 1)),),
    )
    assert result.manifest.state == "committed"


def test_malformed_overflow_probe_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, frames, pixels = _fixture()
    coordinator, _, _ = _coordinator(tmp_path / "store")
    video = FakeVideoSource(source, frames)
    original = video.read_frames

    def malformed_read(
        *,
        stream_index: int,
        after_decode_index: str | None = None,
        limit: int = 64,
    ) -> tuple[FrameRef, ...] | list[FrameRef]:
        if after_decode_index is not None:
            return []
        return original(
            stream_index=stream_index,
            after_decode_index=after_decode_index,
            limit=limit,
        )

    monkeypatch.setattr(video, "read_frames", malformed_read)
    with pytest.raises(CoordinatorError) as malformed:
        coordinator.ingest(
            video,
            FakeFrameSampler((frames[0].frame_id,)),
            IngestionConfig(
                Sampling(Rational("5", "1")),
                max_candidates=2,
                max_frame_bytes=12,
            ),
            (ManualEvidenceInput(frames[0].frame_id, pixels, (0, 0, 1, 1)),),
        )
    assert malformed.value.stage is CoordinatorStage.PROBE


def test_completed_deletion_id_cannot_be_reused_for_another_source(tmp_path: Path) -> None:
    source, frames, pixels = _fixture(b"first")
    coordinator, _, world = _coordinator(tmp_path / "store")
    _ingest(coordinator, source, frames, pixels)
    deletion_id = "del_" + "c" * 64
    completed = coordinator.delete_source(source.source_id, deletion_id=deletion_id)
    assert world.purge_deletion_metadata(deletion_id) == completed

    source_two, _, _ = _fixture(b"second")
    world.commit((source_two,))
    with pytest.raises(CoordinatorError) as coordinated_collision:
        coordinator.delete_source(source_two.source_id, deletion_id=deletion_id)
    assert coordinated_collision.value.code is CoordinatorErrorCode.CONFLICT
    with pytest.raises(PortError) as collision:
        world.begin_source_deletion(source_two.source_id, deletion_id)
    assert collision.value.code is PortErrorCode.CONFLICT


@pytest.mark.parametrize("tamper", ["closure", "decision"])
def test_deletion_plan_tampering_prevents_purge(tmp_path: Path, tamper: str) -> None:
    source, frames, pixels = _fixture(tamper.encode("ascii"))
    coordinator, evidence, world = _coordinator(tmp_path / tamper)
    _ingest(coordinator, source, frames, pixels)
    deletion_id = "del_" + hashlib.sha256(tamper.encode("ascii")).hexdigest()

    def crash(boundary: CommitBoundary, _: str) -> None:
        if boundary is CommitBoundary.DELETION_PENDING:
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        coordinator.delete_source(
            source.source_id,
            deletion_id=deletion_id,
            fault_hook=crash,
        )
    with closing(sqlite3.connect(world.root / "world.sqlite3", autocommit=True)) as connection:
        if tamper == "closure":
            connection.execute(
                """DELETE FROM deletion_closure WHERE deletion_id = ?
                AND record_type = 'evidence'""",
                (deletion_id,),
            )
        else:
            connection.execute(
                """UPDATE deletion_artifacts SET delete_required = 0
                WHERE deletion_id = ?""",
                (deletion_id,),
            )
            connection.execute(
                """UPDATE deletion_jobs SET artifact_count = 0,
                shared_retention_count = 1 WHERE deletion_id = ?""",
                (deletion_id,),
            )

    with pytest.raises(PortError) as plan:
        world.get_deletion_plan(deletion_id)
    assert plan.value.code is PortErrorCode.CORRUPT
    with evidence.writer_session() as session, pytest.raises(PortError) as purge:
        world.purge_deletion_metadata(deletion_id, evidence_session=session)
    assert purge.value.code is PortErrorCode.CORRUPT


def test_recovery_cleans_incomplete_staging_and_reports_invalid_inventory(tmp_path: Path) -> None:
    coordinator, evidence, _ = _coordinator(tmp_path / "store")
    run_id = "run_" + "d" * 64
    run_directory = evidence.root / "staging" / "v1" / run_id
    run_directory.mkdir(mode=0o700)
    incomplete = run_directory / ("e" * 32 + ".part")
    incomplete.write_bytes(b"incomplete")
    incomplete.chmod(0o600)

    report = coordinator.recover()
    assert report.staging_entries_removed == 1
    assert not run_directory.exists()

    invalid = evidence.root / "artifacts" / "v1" / "sha256" / "invalid"
    invalid.mkdir(mode=0o700)
    reported = coordinator.repair()
    assert reported.integrity_issues == 1


def test_recovery_pages_through_staging_and_reports_invalid_staging(tmp_path: Path) -> None:
    coordinator, evidence, _ = _coordinator(tmp_path / "store")
    run_id = "run_" + "e" * 64
    run_directory = evidence.root / "staging" / "v1" / run_id
    run_directory.mkdir(mode=0o700)
    for index in range(65):
        incomplete = run_directory / (f"{index:032x}.part")
        incomplete.write_bytes(b"incomplete")
        incomplete.chmod(0o600)

    report = coordinator.recover()
    assert report.staging_entries_removed == 65
    assert not run_directory.exists()

    invalid = evidence.root / "staging" / "v1" / "invalid"
    invalid.mkdir(mode=0o700)
    reported = coordinator.recover()
    assert reported.integrity_issues == 1
    assert reported.staging_entries_removed == 0


def test_pending_operation_pagination_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _, _ = _fixture()
    coordinator, _, world = _coordinator(tmp_path / "store")
    manifest = RunManifest.create(
        source.source_id,
        (),
        Sampling(Rational("1", "1")),
        "preparing",
    )
    run_calls: list[tuple[str | None, int, bool]] = []

    def pending_runs(
        *,
        after_run_id: str | None = None,
        limit: int = 64,
        actionable_only: bool = False,
    ) -> tuple[RunManifest, ...]:
        run_calls.append((after_run_id, limit, actionable_only))
        return (manifest,) * (64 if after_run_id is None else 1)

    monkeypatch.setattr(world, "pending_runs", pending_runs)
    monkeypatch.setattr(coordinator_module, "MAX_RECOVERY_RUNS", 64)
    with pytest.raises(CoordinatorError) as bounded:
        coordinator._all_pending_runs()
    assert bounded.value.code is CoordinatorErrorCode.INVALID_REQUEST
    assert run_calls == [(None, 64, True), (manifest.run_id, 64, True)]

    statuses = tuple(
        DeletionStatus(
            "del_" + f"{index:064x}",
            DeletionState.PENDING,
            0,
            0,
            0,
            None,
        )
        for index in range(65)
    )
    deletion_calls: list[str | None] = []

    def pending_deletions(
        *,
        after_deletion_id: str | None = None,
        limit: int = 64,
    ) -> tuple[DeletionStatus, ...]:
        deletion_calls.append(after_deletion_id)
        return statuses[:64] if after_deletion_id is None else statuses[64:]

    monkeypatch.setattr(world, "pending_deletions", pending_deletions)
    with pytest.raises(CoordinatorError) as deletion_bounded:
        coordinator._all_pending_deletions()
    assert deletion_bounded.value.code is CoordinatorErrorCode.LIMIT_EXCEEDED
    assert deletion_calls == [None, statuses[63].deletion_id]


def test_pending_run_recovery_excludes_already_clean_failures(tmp_path: Path) -> None:
    source, _, _ = _fixture()
    coordinator, _, world = _coordinator(tmp_path / "store")
    world.commit((source,))
    failed = tuple(
        RunManifest.create(
            source.source_id,
            (),
            Sampling(Rational(str(index), "1")),
            "failed",
        )
        for index in range(1, 65)
    )
    world.commit(failed)

    report = coordinator.recover()
    assert report.runs_cleaned == 0
    assert len(world.pending_runs()) == 64
    assert world.pending_runs(actionable_only=True) == ()


def test_world_lookup_errors_and_conflicts_are_mapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, frames, pixels = _fixture()
    coordinator, _, world = _coordinator(tmp_path / "corrupt")

    def corrupt_get(_: str) -> Source:
        raise PortError(PortErrorCode.CORRUPT, PortKind.WORLD_STORE, "get")

    monkeypatch.setattr(world, "get", corrupt_get)
    with pytest.raises(CoordinatorError) as corrupt:
        _ingest(coordinator, source, frames, pixels)
    assert corrupt.value.code is CoordinatorErrorCode.CORRUPT

    coordinator, _, world = _coordinator(tmp_path / "conflict")
    monkeypatch.setattr(world, "get", lambda _identifier: source)
    with pytest.raises(CoordinatorError) as conflict:
        _ingest(coordinator, source, frames, pixels)
    assert conflict.value.code is CoordinatorErrorCode.CONFLICT
