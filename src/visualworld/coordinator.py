# SPDX-License-Identifier: Apache-2.0
"""Crash-safe version-1 orchestration for manual original-pixel evidence."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar

from visualworld import __version__
from visualworld.geometry import Box, CropError, extract_rgb24_crop, source_geometry
from visualworld.ingestion import (
    Artifact,
    EvidenceRef,
    FrameRef,
    Producer,
    RecordValidationError,
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    dumps_record,
)
from visualworld.ports import (
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    FrameSampler,
    PortError,
    PortErrorCode,
    PortKind,
    VideoSource,
)
from visualworld.storage import (
    ArtifactState,
    EvidenceWriterSession,
    InventoryEntry,
    InventoryKind,
    LocalEvidenceStore,
)
from visualworld.world_store import (
    DeletionPlan,
    DeletionState,
    DeletionStatus,
    LocalWorldStore,
    RunCleanupPlan,
)

COORDINATION_PROTOCOL_VERSION = 1
MAX_COORDINATOR_ITEMS = MAX_PORT_BATCH_ITEMS
MAX_FRAME_BYTES = 512 * 1024 * 1024
MAX_COORDINATOR_BYTES = 512 * 1024 * 1024
MAX_RECOVERY_RUNS = 4_096

_FRAME_ID = re.compile(r"frm_[0-9a-f]{64}\Z")
_SOURCE_ID = re.compile(r"src_[0-9a-f]{64}\Z")
_DELETION_ID = re.compile(r"del_[0-9a-f]{64}\Z")


class CoordinatorErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    OPERATION_FAILED = "operation_failed"


class CoordinatorStage(StrEnum):
    PROBE = "probe"
    SAMPLE = "sample"
    CROP = "crop"
    PREPARE = "prepare"
    STAGE = "stage"
    RECORD_INTENTS = "record_intents"
    PROMOTE = "promote"
    RECORD_METADATA = "record_metadata"
    FINALIZE = "finalize"
    VERIFY = "verify"
    RECOVER_RUN = "recover_run"
    RECOVER_DELETION = "recover_deletion"
    CLEAN_STAGING = "clean_staging"
    REPAIR_ORPHANS = "repair_orphans"
    DELETE_PENDING = "delete_pending"
    DELETE_ARTIFACTS = "delete_artifacts"
    DELETE_METADATA = "delete_metadata"
    DELETE_CHECKPOINT = "delete_checkpoint"


class EventStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CommitBoundary(StrEnum):
    PREPARED = "prepared"
    STAGED = "staged"
    INTENTS_RECORDED = "intents_recorded"
    ARTIFACTS_PROMOTED = "artifacts_promoted"
    FRAME_METADATA_RECORDED = "frame_metadata_recorded"
    EVIDENCE_METADATA_RECORDED = "evidence_metadata_recorded"
    RUN_COMMITTED = "run_committed"
    DELETION_PENDING = "deletion_pending"
    DELETION_ARTIFACTS_REMOVED = "deletion_artifacts_removed"
    DELETION_METADATA_PURGED = "deletion_metadata_purged"
    DELETION_CHECKPOINTED = "deletion_checkpointed"


class IngestionDisposition(StrEnum):
    COMMITTED = "committed"
    ALREADY_COMMITTED = "already_committed"


class CoordinatorError(RuntimeError):
    """Stable orchestration failure that never includes private adapter details."""

    def __init__(
        self,
        code: CoordinatorErrorCode,
        stage: CoordinatorStage,
        *,
        retryable: bool = False,
    ) -> None:
        if (
            not isinstance(code, CoordinatorErrorCode)
            or not isinstance(stage, CoordinatorStage)
            or type(retryable) is not bool
        ):
            raise ValueError("invalid coordinator error")
        self.code = code
        self.stage = stage
        self.retryable = retryable
        super().__init__(f"{code.value} at coordinator.{stage.value}")


@dataclass(frozen=True, slots=True)
class IngestionConfig:
    sampling: Sampling
    stream_index: int = 0
    max_candidates: int = MAX_COORDINATOR_ITEMS
    max_frame_bytes: int = MAX_FRAME_BYTES
    max_total_frame_bytes: int = MAX_COORDINATOR_BYTES
    protocol_version: int = COORDINATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sampling, Sampling)
            or type(self.stream_index) is not int
            or not 0 <= self.stream_index <= 2**31 - 1
            or type(self.max_candidates) is not int
            or not 1 <= self.max_candidates <= MAX_COORDINATOR_ITEMS
            or type(self.max_frame_bytes) is not int
            or not 3 <= self.max_frame_bytes <= MAX_FRAME_BYTES
            or type(self.max_total_frame_bytes) is not int
            or not 3 <= self.max_total_frame_bytes <= MAX_COORDINATOR_BYTES
            or self.max_frame_bytes > self.max_total_frame_bytes
            or type(self.protocol_version) is not int
            or self.protocol_version != COORDINATION_PROTOCOL_VERSION
        ):
            raise ValueError("invalid version-1 ingestion config")


@dataclass(frozen=True, slots=True, repr=False)
class ManualEvidenceInput:
    frame_id: str
    frame_rgb24: bytes = field(repr=False)
    box_xyxy: Box
    measurement: str = "measured"

    def __post_init__(self) -> None:
        if (
            type(self.frame_id) is not str
            or not _FRAME_ID.fullmatch(self.frame_id)
            or type(self.frame_rgb24) is not bytes
            or not self.frame_rgb24
            or len(self.frame_rgb24) > MAX_FRAME_BYTES
            or not isinstance(self.box_xyxy, tuple)
            or len(self.box_xyxy) != 4
            or any(type(value) is not int for value in self.box_xyxy)
            or self.measurement
            not in {"measured", "calibrated", "estimated", "inferred", "unknown"}
        ):
            raise ValueError("invalid manual evidence input")

    def __repr__(self) -> str:
        return (
            f"ManualEvidenceInput(frame_id={self.frame_id!r}, "
            f"box_xyxy={self.box_xyxy!r}, measurement={self.measurement!r}, "
            "frame_rgb24=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class CoordinatorEvent:
    stage: CoordinatorStage
    status: EventStatus
    duration_ns: int
    item_count: int
    run_id: str | None = None
    deletion_id: str | None = None
    protocol_version: int = COORDINATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.stage, CoordinatorStage)
            or not isinstance(self.status, EventStatus)
            or type(self.duration_ns) is not int
            or self.duration_ns < 1
            or type(self.item_count) is not int
            or self.item_count < 0
            or (self.run_id is not None and not re.fullmatch(r"run_[0-9a-f]{64}", self.run_id))
            or (self.deletion_id is not None and not _DELETION_ID.fullmatch(self.deletion_id))
            or type(self.protocol_version) is not int
            or self.protocol_version != COORDINATION_PROTOCOL_VERSION
        ):
            raise ValueError("invalid coordinator event")


@dataclass(frozen=True, slots=True)
class IngestionResult:
    manifest: RunManifest
    frames: tuple[FrameRef, ...]
    evidence: tuple[EvidenceRef, ...]
    disposition: IngestionDisposition
    events: tuple[CoordinatorEvent, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.manifest, RunManifest)
            or self.manifest.state != "committed"
            or not isinstance(self.frames, tuple)
            or not all(isinstance(item, FrameRef) for item in self.frames)
            or not isinstance(self.evidence, tuple)
            or not all(isinstance(item, EvidenceRef) for item in self.evidence)
            or len(self.frames) != len(self.evidence)
            or not isinstance(self.disposition, IngestionDisposition)
            or not isinstance(self.events, tuple)
            or not all(isinstance(item, CoordinatorEvent) for item in self.events)
        ):
            raise ValueError("invalid ingestion result")


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    runs_cleaned: int
    deletions_completed: int
    staging_entries_removed: int
    integrity_issues: int
    events: tuple[CoordinatorEvent, ...]

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not int or value < 0
                for value in (
                    self.runs_cleaned,
                    self.deletions_completed,
                    self.staging_entries_removed,
                    self.integrity_issues,
                )
            )
            or not isinstance(self.events, tuple)
            or not all(isinstance(item, CoordinatorEvent) for item in self.events)
        ):
            raise ValueError("invalid recovery report")


@dataclass(frozen=True, slots=True)
class RepairReport:
    recovery: RecoveryReport
    orphan_artifacts_removed: int
    integrity_issues: int
    events: tuple[CoordinatorEvent, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.recovery, RecoveryReport)
            or type(self.orphan_artifacts_removed) is not int
            or self.orphan_artifacts_removed < 0
            or type(self.integrity_issues) is not int
            or self.integrity_issues < 0
            or not isinstance(self.events, tuple)
            or not all(isinstance(item, CoordinatorEvent) for item in self.events)
        ):
            raise ValueError("invalid repair report")


EventSink = Callable[[CoordinatorEvent], None]
FaultHook = Callable[[CommitBoundary, str], None]
_T = TypeVar("_T")


def new_deletion_id() -> str:
    """Return an application-generated opaque version-1 deletion identifier."""

    return "del_" + secrets.token_hex(32)


def sample_index_bytes(frames: tuple[FrameRef, ...]) -> bytes:
    """Encode the ordered sample index as canonical version-1 JSON lines."""

    if not isinstance(frames, tuple) or not all(isinstance(item, FrameRef) for item in frames):
        raise ValueError("invalid sample index")
    return b"".join(dumps_record(frame) + b"\n" for frame in frames)


def _batches[T](values: tuple[T, ...]) -> tuple[tuple[T, ...], ...]:
    return tuple(
        values[index : index + MAX_PORT_BATCH_ITEMS]
        for index in range(0, len(values), MAX_PORT_BATCH_ITEMS)
    )


class IngestionCoordinator:
    """Coordinate deterministic local ingestion, recovery, repair, and deletion."""

    def __init__(
        self,
        evidence_store: LocalEvidenceStore,
        world_store: LocalWorldStore,
        *,
        event_sink: EventSink | None = None,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        if (
            type(evidence_store) is not LocalEvidenceStore
            or type(world_store) is not LocalWorldStore
            or evidence_store.root != world_store.root
            or (event_sink is not None and not callable(event_sink))
            or not callable(clock_ns)
        ):
            raise ValueError("coordinator requires matching local version-1 stores")
        self._evidence = evidence_store
        self._world = world_store
        self._event_sink = event_sink
        self._clock_ns = clock_ns

    def _emit(self, events: list[CoordinatorEvent], event: CoordinatorEvent) -> None:
        events.append(event)
        if self._event_sink is not None:
            with suppress(Exception):
                self._event_sink(event)

    @staticmethod
    def _map_error(error: Exception, stage: CoordinatorStage) -> CoordinatorError:
        if isinstance(error, CoordinatorError):
            return CoordinatorError(error.code, error.stage, retryable=error.retryable)
        if isinstance(error, PortError):
            if error.code in {PortErrorCode.INVALID_REQUEST, PortErrorCode.LIMIT_EXCEEDED}:
                code = CoordinatorErrorCode.INVALID_REQUEST
            elif error.code in {PortErrorCode.CONFLICT, PortErrorCode.NOT_FOUND}:
                code = CoordinatorErrorCode.CONFLICT
            elif error.code is PortErrorCode.CORRUPT:
                code = CoordinatorErrorCode.CORRUPT
            else:
                code = CoordinatorErrorCode.OPERATION_FAILED
            return CoordinatorError(code, stage, retryable=error.retryable)
        if isinstance(error, (CropError, RecordValidationError, TypeError, ValueError)):
            return CoordinatorError(CoordinatorErrorCode.INVALID_REQUEST, stage)
        return CoordinatorError(CoordinatorErrorCode.OPERATION_FAILED, stage)

    def _optional_world_call(
        self,
        operation: Callable[[], _T],
        stage: CoordinatorStage,
    ) -> _T | None:
        mapped: CoordinatorError | None = None
        try:
            return operation()
        except PortError as error:
            if error.code is PortErrorCode.NOT_FOUND:
                return None
            mapped = self._map_error(error, stage)
        if mapped is not None:
            raise mapped from None
        raise AssertionError("unreachable")

    def _step(
        self,
        events: list[CoordinatorEvent],
        stage: CoordinatorStage,
        operation: Callable[[], _T],
        *,
        item_count: int,
        run_id: str | None = None,
        deletion_id: str | None = None,
    ) -> _T:
        started = self._clock_ns()
        mapped: CoordinatorError | None = None
        try:
            result = operation()
        except Exception as error:
            elapsed = max(1, self._clock_ns() - started)
            self._emit(
                events,
                CoordinatorEvent(
                    stage,
                    EventStatus.FAILED,
                    elapsed,
                    item_count,
                    run_id,
                    deletion_id,
                ),
            )
            mapped = self._map_error(error, stage)
        if mapped is not None:
            raise mapped from None
        elapsed = max(1, self._clock_ns() - started)
        self._emit(
            events,
            CoordinatorEvent(
                stage,
                EventStatus.SUCCEEDED,
                elapsed,
                item_count,
                run_id,
                deletion_id,
            ),
        )
        return result

    @staticmethod
    def _boundary(hook: FaultHook | None, boundary: CommitBoundary, identifier: str) -> None:
        if hook is not None:
            hook(boundary, identifier)

    @staticmethod
    def _validate_adapter(adapter: object, port: PortKind) -> None:
        descriptor_error = False
        try:
            descriptor = getattr(adapter, "descriptor", None)
        except Exception:
            descriptor_error = True
            descriptor = None
        if descriptor_error:
            raise CoordinatorError(
                CoordinatorErrorCode.INVALID_REQUEST,
                CoordinatorStage.PROBE
                if port is PortKind.VIDEO_SOURCE
                else CoordinatorStage.SAMPLE,
            ) from None
        if (
            not isinstance(descriptor, CapabilityDescriptor)
            or descriptor.port is not port
            or not descriptor.deterministic
            or not descriptor.offline
            or descriptor.contract_version != 1
            or descriptor.allowed_effects
        ):
            raise CoordinatorError(
                CoordinatorErrorCode.INVALID_REQUEST,
                (
                    CoordinatorStage.PROBE
                    if port is PortKind.VIDEO_SOURCE
                    else CoordinatorStage.SAMPLE
                ),
            )

    @staticmethod
    def _producer(
        name: str,
        version: str,
        configuration: dict[str, object],
    ) -> Producer:
        encoded = json.dumps(
            configuration,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return Producer(name, version, hashlib.sha256(encoded).hexdigest())

    def _prepare_records(
        self,
        source: Source,
        selected: tuple[FrameRef, ...],
        manual: tuple[ManualEvidenceInput, ...],
        config: IngestionConfig,
        video_source: VideoSource,
        sampler: FrameSampler,
        events: list[CoordinatorEvent],
    ) -> tuple[RunManifest, RunManifest, tuple[EvidenceRef, ...], tuple[bytes, ...]]:
        by_id = {item.frame_id: item for item in manual}
        if len(by_id) != len(manual) or set(by_id) != {frame.frame_id for frame in selected}:
            raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.CROP)
        if sum(len(item.frame_rgb24) for item in manual) > config.max_total_frame_bytes:
            raise CoordinatorError(
                CoordinatorErrorCode.INVALID_REQUEST,
                CoordinatorStage.CROP,
            )
        streams = {stream.stream_index: stream for stream in source.streams}

        def crop_all() -> tuple[tuple[EvidenceRef, ...], tuple[bytes, ...]]:
            evidence: list[EvidenceRef] = []
            contents: list[bytes] = []
            for frame in selected:
                stream = streams[frame.stream_index]
                item = by_id[frame.frame_id]
                expected_bytes = stream.width * stream.height * 3
                if (
                    expected_bytes > config.max_frame_bytes
                    or len(item.frame_rgb24) != expected_bytes
                ):
                    raise ValueError("frame byte count mismatch")
                geometry = source_geometry(
                    stream.width,
                    stream.height,
                    item.box_xyxy,
                    measurement=item.measurement,
                )
                crop = extract_rgb24_crop(
                    item.frame_rgb24,
                    stream.width,
                    stream.height,
                    geometry,
                )
                evidence.append(EvidenceRef.create(frame.frame_id, crop.artifact(), geometry))
                contents.append(crop.pixels)
            return tuple(evidence), tuple(contents)

        evidence, contents = self._step(
            events,
            CoordinatorStage.CROP,
            crop_all,
            item_count=len(selected),
        )
        evidence_by_frame = {item.frame_id: item for item in evidence}
        video_descriptor = video_source.descriptor
        sampler_descriptor = sampler.descriptor
        ordered_frames = [frame.frame_id for frame in selected]
        producers = (
            self._producer(
                video_descriptor.implementation,
                video_descriptor.implementation_version,
                {
                    "candidate_stream": config.stream_index,
                    "protocol_version": config.protocol_version,
                    "source_id": source.source_id,
                },
            ),
            self._producer(
                sampler_descriptor.implementation,
                sampler_descriptor.implementation_version,
                {
                    "protocol_version": config.protocol_version,
                    "sampling": config.sampling.to_mapping(),
                    "selected_frame_ids": ordered_frames,
                },
            ),
            self._producer(
                "visualworld.manual-region",
                __version__,
                {
                    "protocol_version": config.protocol_version,
                    "regions": [
                        {
                            "artifact_sha256": evidence_by_frame[frame.frame_id].artifact.sha256,
                            "box_xyxy": list(by_id[frame.frame_id].box_xyxy),
                            "frame_id": frame.frame_id,
                            "measurement": by_id[frame.frame_id].measurement,
                        }
                        for frame in selected
                    ],
                },
            ),
        )
        preparing = RunManifest.create(source.source_id, producers, config.sampling, "preparing")
        index = sample_index_bytes(selected)
        committed = RunManifest.create(
            source.source_id,
            producers,
            config.sampling,
            "committed",
            RunOutputs(str(len(selected)), hashlib.sha256(index).hexdigest()),
        )
        return preparing, committed, evidence, contents

    def ingest(
        self,
        video_source: VideoSource,
        sampler: FrameSampler,
        config: IngestionConfig,
        manual: tuple[ManualEvidenceInput, ...],
        *,
        fault_hook: FaultHook | None = None,
    ) -> IngestionResult:
        """Run one bounded deterministic ingestion with original RGB24 crops."""

        if (
            not isinstance(config, IngestionConfig)
            or not isinstance(manual, tuple)
            or len(manual) > MAX_COORDINATOR_ITEMS
            or not all(isinstance(item, ManualEvidenceInput) for item in manual)
            or (fault_hook is not None and not callable(fault_hook))
        ):
            raise CoordinatorError(
                CoordinatorErrorCode.INVALID_REQUEST,
                CoordinatorStage.PROBE,
            )
        self._validate_adapter(video_source, PortKind.VIDEO_SOURCE)
        self._validate_adapter(sampler, PortKind.FRAME_SAMPLER)
        self.recover()
        events: list[CoordinatorEvent] = []
        source = self._step(
            events,
            CoordinatorStage.PROBE,
            video_source.probe,
            item_count=1,
        )
        if not isinstance(source, Source) or config.stream_index not in {
            stream.stream_index for stream in source.streams
        }:
            raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.PROBE)
        candidates = self._step(
            events,
            CoordinatorStage.PROBE,
            lambda: video_source.read_frames(
                stream_index=config.stream_index,
                limit=config.max_candidates,
            ),
            item_count=config.max_candidates,
        )
        if (
            not isinstance(candidates, tuple)
            or len(candidates) > config.max_candidates
            or not all(
                isinstance(frame, FrameRef)
                and frame.source_id == source.source_id
                and frame.stream_index == config.stream_index
                for frame in candidates
            )
        ):
            raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.PROBE)
        if len(candidates) == config.max_candidates and candidates:
            more = self._step(
                events,
                CoordinatorStage.PROBE,
                lambda: video_source.read_frames(
                    stream_index=config.stream_index,
                    after_decode_index=candidates[-1].decode_index,
                    limit=1,
                ),
                item_count=1,
            )
            if (
                not isinstance(more, tuple)
                or len(more) > 1
                or not all(
                    isinstance(frame, FrameRef)
                    and frame.source_id == source.source_id
                    and frame.stream_index == config.stream_index
                    and int(frame.decode_index) > int(candidates[-1].decode_index)
                    for frame in more
                )
            ):
                raise CoordinatorError(
                    CoordinatorErrorCode.CONFLICT,
                    CoordinatorStage.PROBE,
                )
            if more:
                raise CoordinatorError(
                    CoordinatorErrorCode.INVALID_REQUEST,
                    CoordinatorStage.PROBE,
                )
        selected = self._step(
            events,
            CoordinatorStage.SAMPLE,
            lambda: sampler.sample(source, candidates, config.sampling),
            item_count=len(candidates),
        )
        candidate_ids = {frame.frame_id for frame in candidates}
        if (
            not isinstance(selected, tuple)
            or len(selected) > MAX_COORDINATOR_ITEMS
            or not all(isinstance(frame, FrameRef) for frame in selected)
            or len({frame.frame_id for frame in selected}) != len(selected)
            or not {frame.frame_id for frame in selected}.issubset(candidate_ids)
        ):
            raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.SAMPLE)
        preparing, committed, evidence, contents = self._prepare_records(
            source,
            selected,
            manual,
            config,
            video_source,
            sampler,
            events,
        )
        run_id = preparing.run_id
        unique_artifacts: dict[str, tuple[Artifact, bytes]] = {}
        for item, content in zip(evidence, contents, strict=True):
            existing_artifact = unique_artifacts.get(item.artifact.sha256)
            if existing_artifact is None:
                unique_artifacts[item.artifact.sha256] = (item.artifact, content)
            elif existing_artifact != (item.artifact, content):
                raise CoordinatorError(
                    CoordinatorErrorCode.CORRUPT,
                    CoordinatorStage.STAGE,
                )
        staged_entry_count = len(unique_artifacts) + bool(unique_artifacts)
        staged_bytes = sum(int(artifact.bytes) for artifact, _ in unique_artifacts.values())
        with self._evidence.writer_session() as session:
            existing = self._optional_world_call(
                lambda: self._world.get(run_id),
                CoordinatorStage.VERIFY,
            )
            if existing is not None:
                if existing == committed:
                    self._verify_existing(session, committed, selected, evidence, events)
                    return IngestionResult(
                        committed,
                        selected,
                        evidence,
                        IngestionDisposition.ALREADY_COMMITTED,
                        tuple(events),
                    )
                if not isinstance(existing, RunManifest) or existing.state not in {
                    "failed",
                    "cancelled",
                }:
                    raise CoordinatorError(
                        CoordinatorErrorCode.CONFLICT,
                        CoordinatorStage.PREPARE,
                    )
            if (
                staged_entry_count > self._evidence.max_inventory_entries
                or staged_bytes > self._evidence.max_inventory_bytes
            ):
                raise CoordinatorError(
                    CoordinatorErrorCode.INVALID_REQUEST,
                    CoordinatorStage.STAGE,
                )
            self._step(
                events,
                CoordinatorStage.PREPARE,
                lambda: self._world.commit(
                    (source, preparing),
                    evidence_session=session,
                ),
                item_count=2,
                run_id=run_id,
            )
            self._boundary(fault_hook, CommitBoundary.PREPARED, run_id)
            stages = self._step(
                events,
                CoordinatorStage.STAGE,
                lambda: tuple(
                    session.stage(run_id, artifact, content)
                    for artifact, content in unique_artifacts.values()
                ),
                item_count=len(unique_artifacts),
                run_id=run_id,
            )
            self._boundary(fault_hook, CommitBoundary.STAGED, run_id)

            def record_intents() -> None:
                for batch in _batches(stages):
                    self._world.record_artifact_intents(
                        run_id,
                        batch,
                        evidence_session=session,
                    )

            self._step(
                events,
                CoordinatorStage.RECORD_INTENTS,
                record_intents,
                item_count=len(stages),
                run_id=run_id,
            )
            self._boundary(fault_hook, CommitBoundary.INTENTS_RECORDED, run_id)
            self._step(
                events,
                CoordinatorStage.PROMOTE,
                lambda: tuple(session.commit_stage(stage) for stage in stages),
                item_count=len(stages),
                run_id=run_id,
            )
            self._boundary(fault_hook, CommitBoundary.ARTIFACTS_PROMOTED, run_id)

            def record_metadata() -> None:
                for frame_batch in _batches(selected):
                    self._world.commit_for_run(
                        run_id,
                        frame_batch,
                        evidence_session=session,
                    )
                    self._boundary(
                        fault_hook,
                        CommitBoundary.FRAME_METADATA_RECORDED,
                        run_id,
                    )
                for evidence_batch in _batches(evidence):
                    self._world.commit_for_run(
                        run_id,
                        evidence_batch,
                        evidence_session=session,
                    )
                    self._boundary(
                        fault_hook,
                        CommitBoundary.EVIDENCE_METADATA_RECORDED,
                        run_id,
                    )

            self._step(
                events,
                CoordinatorStage.RECORD_METADATA,
                record_metadata,
                item_count=len(selected) + len(evidence),
                run_id=run_id,
            )
            self._step(
                events,
                CoordinatorStage.FINALIZE,
                lambda: self._world.finalize_run(committed, evidence_session=session),
                item_count=1,
                run_id=run_id,
            )
            self._boundary(fault_hook, CommitBoundary.RUN_COMMITTED, run_id)
            self._verify_existing(session, committed, selected, evidence, events)
        return IngestionResult(
            committed,
            selected,
            evidence,
            IngestionDisposition.COMMITTED,
            tuple(events),
        )

    def _verify_existing(
        self,
        session: EvidenceWriterSession,
        manifest: RunManifest,
        frames: tuple[FrameRef, ...],
        evidence: tuple[EvidenceRef, ...],
        events: list[CoordinatorEvent],
    ) -> None:
        def verify() -> None:
            if self._world.get(manifest.run_id) != manifest:
                raise ValueError("manifest mismatch")
            for frame, item in zip(frames, evidence, strict=True):
                if self._world.get(frame.frame_id) != frame:
                    raise ValueError("frame mismatch")
                if self._world.get(item.evidence_id) != item:
                    raise ValueError("evidence mismatch")
            checks = tuple(session.inspect((item.artifact,))[0] for item in evidence)
            if any(check.state is not ArtifactState.VALID for check in checks):
                raise PortError(
                    PortErrorCode.CORRUPT,
                    PortKind.EVIDENCE_STORE,
                    "inspect",
                )

        self._step(
            events,
            CoordinatorStage.VERIFY,
            verify,
            item_count=1 + len(frames) + len(evidence),
            run_id=manifest.run_id,
        )

    def _all_pending_runs(self) -> tuple[RunManifest, ...]:
        result: list[RunManifest] = []
        after: str | None = None
        while True:
            page = self._world.pending_runs(
                after_run_id=after,
                limit=MAX_PORT_BATCH_ITEMS,
                actionable_only=True,
            )
            if len(result) + len(page) > MAX_RECOVERY_RUNS:
                raise CoordinatorError(
                    CoordinatorErrorCode.INVALID_REQUEST,
                    CoordinatorStage.RECOVER_RUN,
                )
            result.extend(page)
            if len(page) < MAX_PORT_BATCH_ITEMS:
                return tuple(result)
            after = page[-1].run_id

    def _all_pending_deletions(self) -> tuple[DeletionStatus, ...]:
        result: list[DeletionStatus] = []
        after: str | None = None
        while True:
            page = self._world.pending_deletions(
                after_deletion_id=after,
                limit=MAX_PORT_BATCH_ITEMS,
            )
            result.extend(page)
            if len(page) < MAX_PORT_BATCH_ITEMS:
                return tuple(result)
            after = page[-1].deletion_id

    @staticmethod
    def _inventory(session: EvidenceWriterSession) -> tuple[InventoryEntry, ...]:
        result: list[InventoryEntry] = []
        after: str | None = None
        while True:
            page = session.inventory(after=after, limit=MAX_PORT_BATCH_ITEMS)
            result.extend(page.entries)
            if page.next_after is None:
                return tuple(result)
            after = page.next_after

    @staticmethod
    def _staging_inventory(session: EvidenceWriterSession) -> tuple[InventoryEntry, ...]:
        result: list[InventoryEntry] = []
        after: str | None = None
        while True:
            page = session.staging_inventory(after=after, limit=MAX_PORT_BATCH_ITEMS)
            result.extend(page.entries)
            if page.next_after is None:
                return tuple(result)
            after = page.next_after

    def _remove_deletion_files(
        self,
        session: EvidenceWriterSession,
        plan: DeletionPlan,
    ) -> None:
        run_ids = set(plan.run_ids)
        for entry in self._staging_inventory(session):
            if (
                entry.kind is InventoryKind.STAGED
                and entry.stage is not None
                and entry.stage.run_id in run_ids
            ):
                session.discard_stage(entry.stage)
            elif (
                entry.kind in {InventoryKind.INCOMPLETE, InventoryKind.EMPTY_RUN}
                and entry.cleanup is not None
                and entry.cleanup.run_id in run_ids
            ):
                session.discard_incomplete(entry.cleanup)
            elif entry.kind in {InventoryKind.CORRUPT, InventoryKind.INVALID}:
                raise PortError(
                    PortErrorCode.CORRUPT,
                    PortKind.EVIDENCE_STORE,
                    "inventory",
                )
        for item in plan.artifacts:
            if item.delete_required:
                session.delete_artifact(item.artifact)

    def recover(self) -> RecoveryReport:
        """Resume durable pending operations and clean proven staging remnants."""

        events: list[CoordinatorEvent] = []
        runs_cleaned = 0
        deletions_completed = 0
        staging_removed = 0
        integrity_issues = 0
        with self._evidence.writer_session() as session:
            deletions = self._all_pending_deletions()
            for status in deletions:

                def recover_deletion(status: DeletionStatus = status) -> None:
                    if status.state is DeletionState.PENDING:
                        plan = self._world.get_deletion_plan(status.deletion_id)
                        self._remove_deletion_files(session, plan)
                        self._world.purge_deletion_metadata(
                            status.deletion_id,
                            evidence_session=session,
                        )
                    self._world.complete_deletion(
                        status.deletion_id,
                        evidence_session=session,
                    )

                self._step(
                    events,
                    CoordinatorStage.RECOVER_DELETION,
                    recover_deletion,
                    item_count=status.artifact_count,
                    deletion_id=status.deletion_id,
                )
                deletions_completed += 1
            for manifest in self._all_pending_runs():
                plan = self._world.run_cleanup_plan(
                    manifest.run_id,
                    evidence_session=session,
                )
                if manifest.state != "preparing" and not plan.artifacts and not plan.stages:
                    continue

                def recover_run(
                    plan: RunCleanupPlan = plan,
                    manifest: RunManifest = manifest,
                ) -> None:
                    for stage in plan.stages:
                        session.discard_stage(stage)
                    for artifact in plan.artifacts:
                        session.delete_artifact(artifact)
                    self._world.finish_run_cleanup(
                        manifest.run_id,
                        evidence_session=session,
                    )

                self._step(
                    events,
                    CoordinatorStage.RECOVER_RUN,
                    recover_run,
                    item_count=len(plan.artifacts) + len(plan.stages),
                    run_id=manifest.run_id,
                )
                runs_cleaned += 1
            inventory = self._staging_inventory(session)

            def clean_staging() -> None:
                nonlocal staging_removed, integrity_issues
                for entry in inventory:
                    if entry.kind is InventoryKind.STAGED and entry.stage is not None:
                        session.discard_stage(entry.stage)
                        staging_removed += 1
                    elif entry.kind in {InventoryKind.INCOMPLETE, InventoryKind.EMPTY_RUN}:
                        if entry.cleanup is None:
                            raise ValueError("missing cleanup capability")
                        session.discard_incomplete(entry.cleanup)
                        staging_removed += 1
                    elif entry.kind in {InventoryKind.CORRUPT, InventoryKind.INVALID}:
                        integrity_issues += 1

            self._step(
                events,
                CoordinatorStage.CLEAN_STAGING,
                clean_staging,
                item_count=len(inventory),
            )
        return RecoveryReport(
            runs_cleaned,
            deletions_completed,
            staging_removed,
            integrity_issues,
            tuple(events),
        )

    def repair(self) -> RepairReport:
        """Explicitly remove valid unreferenced CAS artifacts after recovery."""

        recovery = self.recover()
        events: list[CoordinatorEvent] = []
        removed = 0
        issues = recovery.integrity_issues
        with self._evidence.writer_session() as session:
            inventory = self._inventory(session)
            artifacts = tuple(
                entry.artifact
                for entry in inventory
                if entry.kind is InventoryKind.ARTIFACT and entry.artifact is not None
            )
            unreferenced: list[Artifact] = []
            for batch in _batches(artifacts):
                unreferenced.extend(self._world.unreferenced_artifacts(batch))

            def remove_orphans() -> None:
                nonlocal removed
                for artifact in unreferenced:
                    session.delete_artifact(artifact)
                    removed += 1

            self._step(
                events,
                CoordinatorStage.REPAIR_ORPHANS,
                remove_orphans,
                item_count=len(unreferenced),
            )
            current_issues = sum(
                entry.kind in {InventoryKind.CORRUPT, InventoryKind.INVALID} for entry in inventory
            )
            issues = max(issues, current_issues)
        return RepairReport(recovery, removed, issues, tuple(events))

    def delete_source(
        self,
        source_id: str,
        *,
        deletion_id: str | None = None,
        fault_hook: FaultHook | None = None,
    ) -> DeletionStatus:
        """Delete one managed source through the durable four-phase protocol."""

        selected = new_deletion_id() if deletion_id is None else deletion_id
        if (
            type(source_id) is not str
            or not _SOURCE_ID.fullmatch(source_id)
            or type(selected) is not str
            or not _DELETION_ID.fullmatch(selected)
            or (fault_hook is not None and not callable(fault_hook))
        ):
            raise CoordinatorError(
                CoordinatorErrorCode.INVALID_REQUEST,
                CoordinatorStage.DELETE_PENDING,
            )
        events: list[CoordinatorEvent] = []
        with self._evidence.writer_session() as session:
            status = self._optional_world_call(
                lambda: self._world.deletion_status(selected),
                CoordinatorStage.DELETE_PENDING,
            )
            if status is not None:
                if status.state is DeletionState.COMPLETE:
                    self._require_source_absent(source_id)
                    return status
                if status.state is DeletionState.METADATA_PURGED:
                    self._require_source_absent(source_id)
                    completed = self._step(
                        events,
                        CoordinatorStage.DELETE_CHECKPOINT,
                        lambda: self._world.complete_deletion(
                            selected,
                            evidence_session=session,
                        ),
                        item_count=1,
                        deletion_id=selected,
                    )
                    self._boundary(
                        fault_hook,
                        CommitBoundary.DELETION_CHECKPOINTED,
                        selected,
                    )
                    return completed
            plan = self._step(
                events,
                CoordinatorStage.DELETE_PENDING,
                lambda: self._world.begin_source_deletion(
                    source_id,
                    selected,
                    evidence_session=session,
                ),
                item_count=1,
                deletion_id=selected,
            )
            selected = plan.deletion_id
            self._boundary(fault_hook, CommitBoundary.DELETION_PENDING, selected)
            self._step(
                events,
                CoordinatorStage.DELETE_ARTIFACTS,
                lambda: self._remove_deletion_files(session, plan),
                item_count=plan.artifact_count + len(plan.stages),
                deletion_id=selected,
            )
            self._boundary(
                fault_hook,
                CommitBoundary.DELETION_ARTIFACTS_REMOVED,
                selected,
            )
            self._step(
                events,
                CoordinatorStage.DELETE_METADATA,
                lambda: self._world.purge_deletion_metadata(
                    selected,
                    evidence_session=session,
                ),
                item_count=plan.record_count,
                deletion_id=selected,
            )
            self._boundary(
                fault_hook,
                CommitBoundary.DELETION_METADATA_PURGED,
                selected,
            )
            completed = self._step(
                events,
                CoordinatorStage.DELETE_CHECKPOINT,
                lambda: self._world.complete_deletion(
                    selected,
                    evidence_session=session,
                ),
                item_count=1,
                deletion_id=selected,
            )
            self._boundary(
                fault_hook,
                CommitBoundary.DELETION_CHECKPOINTED,
                selected,
            )
            return completed

    def _require_source_absent(self, source_id: str) -> None:
        record = self._optional_world_call(
            lambda: self._world.get(source_id),
            CoordinatorStage.DELETE_PENDING,
        )
        if record is None:
            return
        raise CoordinatorError(
            CoordinatorErrorCode.CONFLICT,
            CoordinatorStage.DELETE_PENDING,
        )


__all__ = [
    "COORDINATION_PROTOCOL_VERSION",
    "MAX_COORDINATOR_BYTES",
    "MAX_COORDINATOR_ITEMS",
    "MAX_FRAME_BYTES",
    "MAX_RECOVERY_RUNS",
    "CommitBoundary",
    "CoordinatorError",
    "CoordinatorErrorCode",
    "CoordinatorEvent",
    "CoordinatorStage",
    "EventSink",
    "EventStatus",
    "FaultHook",
    "IngestionConfig",
    "IngestionCoordinator",
    "IngestionDisposition",
    "IngestionResult",
    "ManualEvidenceInput",
    "RecoveryReport",
    "RepairReport",
    "new_deletion_id",
    "sample_index_bytes",
]
