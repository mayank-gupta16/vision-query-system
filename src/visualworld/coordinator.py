# SPDX-License-Identifier: Apache-2.0
"""Crash-safe version-1 orchestration for manual original-pixel evidence."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from typing import Protocol, TypeVar

from visualworld import __version__
from visualworld.evidence import EvidenceIntent, EvidencePlanResult, dumps_evidence_intent
from visualworld.geometry import Box, CropError, extract_rgb24_crop, source_geometry
from visualworld.ingestion import (
    Artifact,
    EvidenceRef,
    FrameRef,
    Producer,
    Rational,
    RecordValidationError,
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    dumps_record,
)
from visualworld.perception import (
    FrameDiscontinuity,
    Observation,
    Tracklet,
    TrackPoint,
    dumps_perception_record,
)
from visualworld.ports import (
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    DetectionResult,
    Detector,
    FrameSampler,
    PerceptionResultState,
    PortError,
    PortErrorCode,
    PortKind,
    VideoSource,
)
from visualworld.sampling import ResumableFrameSampler, SamplingCursor, SamplingPage
from visualworld.storage import (
    ArtifactState,
    EvidenceWriterSession,
    InventoryEntry,
    InventoryKind,
    LocalEvidenceStore,
)
from visualworld.tracking import ResumableTracker, TrackingCursor, TrackingPage
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
    LIMIT_EXCEEDED = "limit_exceeded"


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
    DETECT = "detect"
    DISCONTINUITY = "discontinuity"
    TRACK = "track"
    SELECT = "select"
    PERCEPTION_METADATA = "perception_metadata"


class EventStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CommitBoundary(StrEnum):
    PREPARED = "prepared"
    STAGED = "staged"
    INTENTS_RECORDED = "intents_recorded"
    ARTIFACTS_PROMOTED = "artifacts_promoted"
    FRAME_METADATA_RECORDED = "frame_metadata_recorded"
    OBSERVATIONS_RECORDED = "observations_recorded"
    TRACKLETS_AND_INTENTS_RECORDED = "tracklets_and_intents_recorded"
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
    def _validate_adapter(adapter: object, port: PortKind) -> CapabilityDescriptor:
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
            type(descriptor) is not CapabilityDescriptor
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
        return descriptor

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
        video_descriptor: CapabilityDescriptor,
        sampler_descriptor: CapabilityDescriptor,
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
        video_descriptor = self._validate_adapter(video_source, PortKind.VIDEO_SOURCE)
        sampler_descriptor = self._validate_adapter(sampler, PortKind.FRAME_SAMPLER)
        self.recover()
        events: list[CoordinatorEvent] = []
        source = self._step(
            events,
            CoordinatorStage.PROBE,
            lambda: video_source.probe(),
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
            video_descriptor,
            sampler_descriptor,
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
            if len(result) + len(page) > MAX_RECOVERY_RUNS:
                raise CoordinatorError(
                    CoordinatorErrorCode.LIMIT_EXCEEDED,
                    CoordinatorStage.RECOVER_DELETION,
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
            if len(result) + len(page.entries) > MAX_RECOVERY_RUNS:
                raise CoordinatorError(
                    CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.REPAIR_ORPHANS
                )
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
            if len(result) + len(page.entries) > MAX_RECOVERY_RUNS:
                raise CoordinatorError(
                    CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.CLEAN_STAGING
                )
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


PERCEPTION_COORDINATION_PROTOCOL_VERSION = 1
MAX_PERCEPTION_PAGES = 4_096
MAX_PERCEPTION_SAMPLES = 4_096
MAX_PERCEPTION_EVENTS = MAX_PERCEPTION_PAGES * 6 + MAX_PERCEPTION_SAMPLES + 8
MAX_PERCEPTION_EVENT_ITEMS = MAX_PERCEPTION_SAMPLES * 3
MAX_PERCEPTION_DURATION_NS = 86_400_000_000_000


class _PerceptionCancelled(RuntimeError):
    """Internal control flow for a redacted cancelled result."""

    def __init__(
        self,
        stage: CoordinatorStage,
        started_ns: int,
        item_count: int,
        run_id: str | None,
    ) -> None:
        self.stage = stage
        self.started_ns = started_ns
        self.item_count = item_count
        self.run_id = run_id


class _Mappable(Protocol):
    def to_mapping(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class FrameDiscontinuityResult:
    """Pixel-free discontinuity facts for one exact sampled-frame page."""

    state: PerceptionResultState
    discontinuities: tuple[FrameDiscontinuity, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.state) is not PerceptionResultState
            or type(self.discontinuities) is not tuple
            or len(self.discontinuities) > MAX_PORT_BATCH_ITEMS
            or not all(type(item) is FrameDiscontinuity for item in self.discontinuities)
        ):
            raise ValueError("invalid discontinuity result")
        for item in self.discontinuities:
            FrameDiscontinuity.__post_init__(item)
        if self.state is PerceptionResultState.COMPLETE:
            if self.reason is not None:
                raise ValueError("complete discontinuity result has a reason")
        elif (
            self.discontinuities
            or type(self.reason) is not str
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.reason)
        ):
            raise ValueError("incomplete discontinuity result is invalid")

    @classmethod
    def complete(cls, discontinuities: tuple[FrameDiscontinuity, ...]) -> FrameDiscontinuityResult:
        return cls(PerceptionResultState.COMPLETE, discontinuities)


class DiscontinuityProvider(Protocol):
    """Pixel-free page scorer; ``None`` resets replayed-run context."""

    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    @property
    def producer(self) -> Producer: ...

    def score(
        self, source: Source, previous_selected: FrameRef | None, frames: tuple[FrameRef, ...]
    ) -> FrameDiscontinuityResult: ...


class EvidencePlanner(Protocol):
    """Metadata-only selector seam; #87 owns crop materialization."""

    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    @property
    def producer(self) -> Producer: ...

    def plan(
        self, tracklet: Tracklet, observations: tuple[Observation, ...]
    ) -> EvidencePlanResult: ...


@dataclass(frozen=True, slots=True)
class PerceptionConfig:
    """Bounded fixed-5-FPS configuration for one v0.2 perception run.

    ``max_metadata_bytes`` covers canonical source, peak run-manifest, and
    perception-graph record bytes; storage-engine overhead is outside it.
    """

    page_candidates: int = MAX_PORT_BATCH_ITEMS
    max_pages: int = MAX_PERCEPTION_PAGES
    max_samples: int = MAX_PERCEPTION_SAMPLES
    max_candidates: int = MAX_PERCEPTION_SAMPLES
    max_observations: int = MAX_PERCEPTION_SAMPLES
    max_tracklets: int = MAX_PERCEPTION_SAMPLES
    max_intents: int = MAX_PERCEPTION_SAMPLES
    max_metadata_bytes: int = MAX_COORDINATOR_BYTES
    max_duration_ns: int = 60_000_000_000
    stream_index: int = 0
    protocol_version: int = PERCEPTION_COORDINATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.page_candidates) is not int
            or not 2 <= self.page_candidates <= MAX_PORT_BATCH_ITEMS
            or type(self.max_pages) is not int
            or not 1 <= self.max_pages <= MAX_PERCEPTION_PAGES
            or type(self.max_samples) is not int
            or not 1 <= self.max_samples <= MAX_PERCEPTION_SAMPLES
            or type(self.max_candidates) is not int
            or not 1 <= self.max_candidates <= MAX_PERCEPTION_SAMPLES
            or type(self.max_observations) is not int
            or not 0 <= self.max_observations <= MAX_PERCEPTION_SAMPLES
            or type(self.max_tracklets) is not int
            or not 0 <= self.max_tracklets <= MAX_PERCEPTION_SAMPLES
            or type(self.max_intents) is not int
            or not 0 <= self.max_intents <= MAX_PERCEPTION_SAMPLES
            or type(self.max_metadata_bytes) is not int
            or not 0 <= self.max_metadata_bytes <= MAX_COORDINATOR_BYTES
            or type(self.max_duration_ns) is not int
            or not 1 <= self.max_duration_ns <= MAX_PERCEPTION_DURATION_NS
            or type(self.stream_index) is not int
            or not 0 <= self.stream_index <= 2**31 - 1
            or type(self.protocol_version) is not int
            or self.protocol_version != PERCEPTION_COORDINATION_PROTOCOL_VERSION
        ):
            raise ValueError("invalid perception configuration")


class PerceptionDisposition(StrEnum):
    COMMITTED = "committed"
    ALREADY_COMMITTED = "already_committed"


@dataclass(frozen=True, slots=True)
class PerceptionEvent:
    """Bounded v0.2 timing and count record, with no adapter diagnostics."""

    stage: CoordinatorStage
    status: EventStatus
    duration_ns: int
    item_count: int
    run_id: str | None = None
    protocol_version: int = PERCEPTION_COORDINATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.stage) is not CoordinatorStage
            or type(self.status) is not EventStatus
            or type(self.duration_ns) is not int
            or not 1 <= self.duration_ns <= MAX_PERCEPTION_DURATION_NS
            or type(self.item_count) is not int
            or not 0 <= self.item_count <= MAX_PERCEPTION_EVENT_ITEMS
            or (self.run_id is not None and not re.fullmatch(r"run_[0-9a-f]{64}", self.run_id))
            or type(self.protocol_version) is not int
            or self.protocol_version != PERCEPTION_COORDINATION_PROTOCOL_VERSION
        ):
            raise ValueError("invalid perception event")


PerceptionEventSink = Callable[[PerceptionEvent], None]


@dataclass(frozen=True, slots=True)
class PerceptionRunResult:
    """Public pixel-free result; incomplete runs never expose partial graph values."""

    state: PerceptionResultState
    manifest: RunManifest | None = None
    frames: tuple[FrameRef, ...] = ()
    observations: tuple[Observation, ...] = ()
    tracklets: tuple[Tracklet, ...] = ()
    intents: tuple[EvidenceIntent, ...] = ()
    disposition: PerceptionDisposition | None = None
    events: tuple[PerceptionEvent, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        complete = self.state is PerceptionResultState.COMPLETE
        if (
            type(self.state) is not PerceptionResultState
            or type(self.frames) is not tuple
            or type(self.observations) is not tuple
            or type(self.tracklets) is not tuple
            or type(self.intents) is not tuple
            or type(self.events) is not tuple
            or len(self.frames) > MAX_PERCEPTION_SAMPLES
            or len(self.observations) > MAX_PERCEPTION_SAMPLES
            or len(self.tracklets) > MAX_PERCEPTION_SAMPLES
            or len(self.intents) > MAX_PERCEPTION_SAMPLES
            or not all(type(item) is FrameRef for item in self.frames)
            or not all(type(item) is Observation for item in self.observations)
            or not all(type(item) is Tracklet for item in self.tracklets)
            or not all(type(item) is EvidenceIntent for item in self.intents)
            or not all(type(item) is PerceptionEvent for item in self.events)
            or len(self.events) > MAX_PERCEPTION_EVENTS
        ):
            raise ValueError("invalid perception run result")
        if complete:
            if type(self.manifest) is not RunManifest or self.manifest.state != "committed":
                raise ValueError("complete perception result requires a committed manifest")
            if type(self.disposition) is not PerceptionDisposition:
                raise ValueError("complete perception result requires a disposition")
            if self.reason is not None:
                raise ValueError("complete perception result has a reason")
        elif (
            self.manifest is not None
            or self.frames
            or self.observations
            or self.tracklets
            or self.intents
            or self.disposition is not None
            or type(self.reason) is not str
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.reason)
        ):
            raise ValueError("incomplete perception result contains outputs")


class PerceptionCoordinator:
    """Compose bounded pixel-free perception pages into one atomic schema-v2 run."""

    def __init__(
        self,
        evidence_store: LocalEvidenceStore,
        world_store: LocalWorldStore,
        *,
        event_sink: PerceptionEventSink | None = None,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        if (
            type(evidence_store) is not LocalEvidenceStore
            or type(world_store) is not LocalWorldStore
            or evidence_store.root != world_store.root
            or (event_sink is not None and not callable(event_sink))
            or not callable(clock_ns)
        ):
            raise ValueError("perception coordinator requires matching local stores")
        self._evidence = evidence_store
        self._world = world_store
        self._event_sink = event_sink
        self._clock_ns = clock_ns
        # Reuse the v0.1 recovery protocol for hidden preparing runs.  The
        # coordinator never serializes page cursors: a retry starts clean and
        # deterministically replays the source from its opaque zero cursor.
        self._recovery = IngestionCoordinator(evidence_store, world_store)

    @staticmethod
    def _descriptor(adapter: object, port: PortKind) -> CapabilityDescriptor:
        try:
            descriptor = getattr(adapter, "descriptor", None)
        except Exception:
            raise CoordinatorError(
                CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.PROBE
            ) from None
        if (
            type(descriptor) is not CapabilityDescriptor
            or descriptor.port is not port
            or not descriptor.deterministic
            or not descriptor.offline
            or descriptor.allowed_effects
        ):
            raise CoordinatorError(CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.PROBE)
        return descriptor

    @staticmethod
    def _producer(role: str, descriptor: CapabilityDescriptor) -> Producer:
        configuration = json.dumps(
            {
                "implementation": descriptor.implementation,
                "implementation_version": descriptor.implementation_version,
                "max_batch_items": descriptor.max_batch_items,
                "max_payload_bytes": descriptor.max_payload_bytes,
                "protocol_version": PERCEPTION_COORDINATION_PROTOCOL_VERSION,
                "role": role,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return Producer(
            f"visualworld.perception-{role}",
            "1",
            hashlib.sha256(configuration).hexdigest(),
        )

    @staticmethod
    def _adapter_producer(adapter: object, stage: CoordinatorStage) -> Producer:
        try:
            producer = getattr(adapter, "producer", None)
        except Exception:
            raise CoordinatorError(CoordinatorErrorCode.INVALID_REQUEST, stage) from None
        if type(producer) is not Producer:
            raise CoordinatorError(CoordinatorErrorCode.INVALID_REQUEST, stage)
        try:
            Producer.__post_init__(producer)
        except (TypeError, ValueError):
            raise CoordinatorError(CoordinatorErrorCode.INVALID_REQUEST, stage) from None
        return producer

    @staticmethod
    def _config_producer(config: PerceptionConfig) -> Producer:
        encoded = json.dumps(
            {
                "max_candidates": config.max_candidates,
                "max_duration_ns": config.max_duration_ns,
                "max_intents": config.max_intents,
                "max_metadata_bytes": config.max_metadata_bytes,
                "max_observations": config.max_observations,
                "max_pages": config.max_pages,
                "max_samples": config.max_samples,
                "max_tracklets": config.max_tracklets,
                "page_candidates": config.page_candidates,
                "protocol_version": config.protocol_version,
                "stream_index": config.stream_index,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return Producer("visualworld.perception-config", "1", hashlib.sha256(encoded).hexdigest())

    def _now(self) -> int:
        try:
            value = self._clock_ns()
        except Exception:
            raise CoordinatorError(
                CoordinatorErrorCode.OPERATION_FAILED, CoordinatorStage.PROBE
            ) from None
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise CoordinatorError(CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.PROBE)
        return value

    def _emit(
        self,
        events: list[PerceptionEvent],
        stage: CoordinatorStage,
        status: EventStatus,
        started_ns: int,
        item_count: int,
        run_id: str | None = None,
    ) -> None:
        event = PerceptionEvent(
            stage,
            status,
            min(MAX_PERCEPTION_DURATION_NS, max(1, self._now() - started_ns)),
            item_count,
            run_id,
        )
        events.append(event)
        if self._event_sink is not None:
            with suppress(Exception):
                self._event_sink(event)

    def _expired(self, started_ns: int, config: PerceptionConfig) -> bool:
        return self._now() - started_ns > config.max_duration_ns

    @staticmethod
    def _is_cancelled(cancelled: threading.Event | None) -> bool:
        if cancelled is None:
            return False
        try:
            value = threading.Event.is_set(cancelled)
        except Exception:
            raise CoordinatorError(
                CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.PROBE
            ) from None
        if type(value) is not bool:
            raise CoordinatorError(CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.PROBE)
        return value

    def _raise_if_cancelled(
        self,
        cancelled: threading.Event | None,
        stage: CoordinatorStage,
        started_ns: int,
        run_id: str | None,
        item_count: int = 0,
    ) -> None:
        if self._is_cancelled(cancelled):
            raise _PerceptionCancelled(stage, started_ns, item_count, run_id)

    def _adapter_call(
        self,
        events: list[PerceptionEvent],
        stage: CoordinatorStage,
        started_ns: int,
        item_count: int,
        run_id: str | None,
        operation: Callable[[], _T],
    ) -> _T:
        """Map untrusted adapter failures to the redacted coordinator surface."""

        try:
            return operation()
        except _PerceptionCancelled:
            raise
        except CoordinatorError:
            raise
        except PortError as error:
            if error.code is PortErrorCode.CANCELLED:
                raise _PerceptionCancelled(stage, started_ns, item_count, run_id) from None
            code = {
                PortErrorCode.LIMIT_EXCEEDED: CoordinatorErrorCode.LIMIT_EXCEEDED,
                PortErrorCode.INVALID_REQUEST: CoordinatorErrorCode.INVALID_REQUEST,
                PortErrorCode.NOT_FOUND: CoordinatorErrorCode.CONFLICT,
                PortErrorCode.CONFLICT: CoordinatorErrorCode.CONFLICT,
                PortErrorCode.CORRUPT: CoordinatorErrorCode.CORRUPT,
            }.get(error.code, CoordinatorErrorCode.OPERATION_FAILED)
            self._emit(events, stage, EventStatus.FAILED, started_ns, item_count, run_id)
            raise CoordinatorError(code, stage, retryable=error.retryable) from None
        except Exception:
            self._emit(events, stage, EventStatus.FAILED, started_ns, item_count, run_id)
            raise CoordinatorError(CoordinatorErrorCode.OPERATION_FAILED, stage) from None

    @staticmethod
    def _incomplete(
        state: PerceptionResultState,
        reason: str,
        events: tuple[PerceptionEvent, ...] = (),
    ) -> PerceptionRunResult:
        return PerceptionRunResult(state, events=events, reason=reason)

    @staticmethod
    def _validate_page(
        source: Source, stream_index: int, page: object, maximum: int
    ) -> tuple[FrameRef, ...]:
        if (
            type(page) is not tuple
            or len(page) > maximum
            or not all(type(frame) is FrameRef for frame in page)
            or len({frame.frame_id for frame in page}) != len(page)
            or any(
                frame.source_id != source.source_id or frame.stream_index != stream_index
                for frame in page
            )
            or any(
                int(current.decode_index) <= int(previous.decode_index)
                for previous, current in pairwise(page)
            )
        ):
            raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.PROBE)
        stream = next(item for item in source.streams if item.stream_index == stream_index)
        try:
            for frame in page:
                FrameRef.__post_init__(frame)
                if frame.pts.time_base != stream.time_base or (
                    frame.duration is not None and frame.duration.time_base != stream.time_base
                ):
                    raise ValueError
        except (TypeError, ValueError):
            raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.PROBE) from None
        return page

    @staticmethod
    def _payload_size(values: tuple[_Mappable, ...]) -> int:
        mappings: list[object] = []
        try:
            for value in values:
                mappings.append(value.to_mapping())
            return len(
                json.dumps(
                    mappings,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            )
        except Exception:
            raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.VERIFY) from None

    @classmethod
    def _check_payload(
        cls,
        descriptor: CapabilityDescriptor,
        stage: CoordinatorStage,
        values: tuple[_Mappable, ...],
    ) -> None:
        maximum = descriptor.max_payload_bytes
        if maximum is not None and cls._payload_size(values) > maximum:
            raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, stage)

    @staticmethod
    def _metadata_size(
        value: Source | RunManifest | FrameRef | Observation | Tracklet | EvidenceIntent,
    ) -> int:
        try:
            if type(value) is Source:
                return len(dumps_record(value))
            if type(value) is RunManifest:
                return len(dumps_record(value))
            if type(value) is FrameRef:
                return len(dumps_record(value))
            if type(value) is Observation:
                return len(dumps_perception_record(value))
            if type(value) is Tracklet:
                return len(dumps_perception_record(value))
            if type(value) is EvidenceIntent:
                return len(dumps_evidence_intent(value))
        except (TypeError, ValueError, RecordValidationError):
            pass
        raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.VERIFY)

    @staticmethod
    def _observation_key(value: Observation) -> tuple[int, str]:
        return int(value.pts.value), value.observation_id

    @staticmethod
    def _tracklet_key(value: Tracklet) -> tuple[int, str]:
        return int(value.points[0].pts.value), value.tracklet_id

    def _verify_persisted(
        self,
        manifest: RunManifest,
        frames: tuple[FrameRef, ...],
        observations: tuple[Observation, ...],
        tracklets: tuple[Tracklet, ...],
        intents: tuple[EvidenceIntent, ...],
        source: Source,
        config: PerceptionConfig,
    ) -> None:
        """Read the committed graph through public bounded store APIs."""

        try:
            if self._world.get(manifest.run_id) != manifest:
                raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.VERIFY)
            stored_frames: list[FrameRef] = []
            frame_after: tuple[int, str] | None = None
            while True:
                frame_page = self._world.list_run_frames(
                    manifest.run_id,
                    after_stream_index=None if frame_after is None else frame_after[0],
                    after_decode_index=None if frame_after is None else frame_after[1],
                    limit=MAX_PORT_BATCH_ITEMS,
                )
                if len(stored_frames) + len(frame_page) > config.max_samples:
                    raise CoordinatorError(
                        CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.VERIFY
                    )
                stored_frames.extend(frame_page)
                if len(frame_page) < MAX_PORT_BATCH_ITEMS:
                    break
                last_frame = frame_page[-1]
                frame_after = (last_frame.stream_index, last_frame.decode_index)
            stored_observations: list[Observation] = []
            stored_tracklets: list[Tracklet] = []
            for stream in source.streams:
                observation_after: tuple[str, str] | None = None
                while True:
                    observation_page = self._world.list_run_observations(
                        manifest.run_id,
                        stream_index=stream.stream_index,
                        after_pts_value=None if observation_after is None else observation_after[0],
                        after_observation_id=None
                        if observation_after is None
                        else observation_after[1],
                        limit=MAX_PORT_BATCH_ITEMS,
                    )
                    if len(stored_observations) + len(observation_page) > config.max_observations:
                        raise CoordinatorError(
                            CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.VERIFY
                        )
                    stored_observations.extend(observation_page)
                    if len(observation_page) < MAX_PORT_BATCH_ITEMS:
                        break
                    last_observation = observation_page[-1]
                    observation_after = (
                        last_observation.pts.value,
                        last_observation.observation_id,
                    )
                tracklet_after: tuple[str, str] | None = None
                while True:
                    tracklet_page = self._world.list_run_tracklets(
                        manifest.run_id,
                        stream_index=stream.stream_index,
                        after_start_pts_value=None if tracklet_after is None else tracklet_after[0],
                        after_tracklet_id=None if tracklet_after is None else tracklet_after[1],
                        limit=MAX_PORT_BATCH_ITEMS,
                    )
                    if len(stored_tracklets) + len(tracklet_page) > config.max_tracklets:
                        raise CoordinatorError(
                            CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.VERIFY
                        )
                    stored_tracklets.extend(tracklet_page)
                    if len(tracklet_page) < MAX_PORT_BATCH_ITEMS:
                        break
                    last_tracklet = tracklet_page[-1]
                    tracklet_after = (
                        last_tracklet.points[0].pts.value,
                        last_tracklet.tracklet_id,
                    )
            if (
                tuple(stored_frames)
                != tuple(
                    sorted(frames, key=lambda item: (item.stream_index, int(item.decode_index)))
                )
                or len({item.observation_id for item in stored_observations})
                != len(stored_observations)
                or tuple(stored_observations)
                != tuple(sorted(observations, key=self._observation_key))
                or len({item.tracklet_id for item in stored_tracklets}) != len(stored_tracklets)
                or tuple(stored_tracklets) != tuple(sorted(tracklets, key=self._tracklet_key))
            ):
                raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.VERIFY)
            stored_intents: list[EvidenceIntent] = []
            for tracklet in sorted(tracklets, key=self._tracklet_key):
                after_rank: int | None = None
                while True:
                    selection_page = self._world.list_selected_evidence(
                        manifest.run_id,
                        tracklet.tracklet_id,
                        after_rank=after_rank,
                        limit=8,
                    )
                    if len(stored_intents) + len(selection_page) > config.max_intents:
                        raise CoordinatorError(
                            CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.VERIFY
                        )
                    stored_intents.extend(selection.intent for selection in selection_page)
                    if len(selection_page) < 8:
                        break
                    after_rank = selection_page[-1].intent.rank
            expected_intents = tuple(
                intent
                for tracklet in sorted(tracklets, key=self._tracklet_key)
                for intent in sorted(
                    (item for item in intents if item.tracklet_id == tracklet.tracklet_id),
                    key=lambda item: item.rank,
                )
            )
            if (
                len({(item.tracklet_id, item.rank) for item in stored_intents})
                != len(stored_intents)
                or tuple(stored_intents) != expected_intents
            ):
                raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.VERIFY)
            persisted_bytes = (
                self._metadata_size(source)
                + self._metadata_size(manifest)
                + sum(self._metadata_size(item) for item in stored_frames)
                + sum(self._metadata_size(item) for item in stored_observations)
                + sum(self._metadata_size(item) for item in stored_tracklets)
                + sum(self._metadata_size(item) for item in stored_intents)
            )
            if persisted_bytes > config.max_metadata_bytes:
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.VERIFY)
        except CoordinatorError:
            raise
        except PortError as error:
            code = {
                PortErrorCode.LIMIT_EXCEEDED: CoordinatorErrorCode.LIMIT_EXCEEDED,
                PortErrorCode.INVALID_REQUEST: CoordinatorErrorCode.INVALID_REQUEST,
                PortErrorCode.NOT_FOUND: CoordinatorErrorCode.CONFLICT,
                PortErrorCode.CONFLICT: CoordinatorErrorCode.CONFLICT,
                PortErrorCode.CORRUPT: CoordinatorErrorCode.CORRUPT,
            }.get(error.code, CoordinatorErrorCode.OPERATION_FAILED)
            raise CoordinatorError(code, CoordinatorStage.VERIFY) from None

    def run(
        self,
        video_source: VideoSource,
        sampler: ResumableFrameSampler,
        detector: Detector,
        tracker: ResumableTracker,
        discontinuities: DiscontinuityProvider,
        selector: EvidencePlanner,
        config: PerceptionConfig,
        *,
        cancelled: threading.Event | None = None,
        fault_hook: FaultHook | None = None,
    ) -> PerceptionRunResult:
        """Run deterministic pages; #87 owns pixels and EvidenceRef materialization."""

        events: list[PerceptionEvent] = []
        run_started_ns = self._now()
        if (
            type(config) is not PerceptionConfig
            or (cancelled is not None and type(cancelled) is not threading.Event)
            or (fault_hook is not None and not callable(fault_hook))
        ):
            raise CoordinatorError(CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.PROBE)
        if self._is_cancelled(cancelled):
            self._emit(
                events,
                CoordinatorStage.PROBE,
                EventStatus.CANCELLED,
                run_started_ns,
                0,
            )
            return self._incomplete(PerceptionResultState.UNKNOWN, "cancelled", tuple(events))
        try:
            return self._execute(
                video_source,
                sampler,
                detector,
                tracker,
                discontinuities,
                selector,
                config,
                cancelled=cancelled,
                fault_hook=fault_hook,
                events=events,
                run_started_ns=run_started_ns,
            )
        except _PerceptionCancelled as cancellation:
            self._emit(
                events,
                cancellation.stage,
                EventStatus.CANCELLED,
                cancellation.started_ns,
                cancellation.item_count,
                cancellation.run_id,
            )
            return self._incomplete(PerceptionResultState.UNKNOWN, "cancelled", tuple(events))

    def _execute(
        self,
        video_source: VideoSource,
        sampler: ResumableFrameSampler,
        detector: Detector,
        tracker: ResumableTracker,
        discontinuities: DiscontinuityProvider,
        selector: EvidencePlanner,
        config: PerceptionConfig,
        *,
        cancelled: threading.Event | None,
        fault_hook: FaultHook | None,
        events: list[PerceptionEvent],
        run_started_ns: int,
    ) -> PerceptionRunResult:
        started_ns = self._now()
        video_descriptor = self._descriptor(video_source, PortKind.VIDEO_SOURCE)
        sampler_descriptor = self._descriptor(sampler, PortKind.FRAME_SAMPLER)
        detector_descriptor = self._descriptor(detector, PortKind.DETECTOR)
        tracker_descriptor = self._descriptor(tracker, PortKind.TRACKER)
        selector_descriptor = self._descriptor(selector, PortKind.EVIDENCE_SELECTOR)
        discontinuity_descriptor = self._descriptor(discontinuities, PortKind.FRAME_DISCONTINUITY)
        detector_producer = self._adapter_producer(detector, CoordinatorStage.DETECT)
        tracker_producer = self._adapter_producer(tracker, CoordinatorStage.TRACK)
        selector_producer = self._adapter_producer(selector, CoordinatorStage.SELECT)
        discontinuity_producer = self._adapter_producer(
            discontinuities, CoordinatorStage.DISCONTINUITY
        )
        if (
            config.page_candidates > video_descriptor.max_batch_items
            or config.page_candidates > sampler_descriptor.max_batch_items
        ):
            raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.PROBE)
        self._raise_if_cancelled(cancelled, CoordinatorStage.RECOVER_RUN, started_ns, None)
        self._recovery.recover()
        if self._expired(run_started_ns, config):
            raise CoordinatorError(
                CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.RECOVER_RUN
            )
        started_ns = self._now()
        self._raise_if_cancelled(cancelled, CoordinatorStage.PROBE, started_ns, None)
        source = self._adapter_call(
            events,
            CoordinatorStage.PROBE,
            started_ns,
            0,
            None,
            video_source.probe,
        )
        self._raise_if_cancelled(cancelled, CoordinatorStage.PROBE, started_ns, None)
        if self._expired(run_started_ns, config):
            self._emit(events, CoordinatorStage.PROBE, EventStatus.FAILED, started_ns, 0)
            raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.PROBE)
        if type(source) is not Source:
            raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.PROBE)
        try:
            Source.__post_init__(source)
            stream_indexes = {stream.stream_index for stream in source.streams}
        except (TypeError, ValueError, RecordValidationError):
            raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.PROBE) from None
        if config.stream_index not in stream_indexes:
            raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.PROBE)
        sampling = Sampling(Rational("5", "1"))
        producers = (
            *(
                self._producer(role, descriptor)
                for role, descriptor in (
                    ("video", video_descriptor),
                    ("sampler", sampler_descriptor),
                    ("detector", detector_descriptor),
                    ("tracker", tracker_descriptor),
                    ("selector", selector_descriptor),
                    ("discontinuity", discontinuity_descriptor),
                )
            ),
            detector_producer,
            tracker_producer,
            selector_producer,
            discontinuity_producer,
            self._config_producer(config),
        )
        preparing = RunManifest.create(source.source_id, producers, sampling, "preparing")
        preparing_metadata_bytes = self._metadata_size(preparing)
        metadata_bytes = self._metadata_size(source) + preparing_metadata_bytes
        if metadata_bytes > config.max_metadata_bytes:
            raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.PROBE)
        self._emit(
            events, CoordinatorStage.PROBE, EventStatus.SUCCEEDED, started_ns, 1, preparing.run_id
        )
        sampled: list[FrameRef] = []
        selected_frame_ids: set[str] = set()
        observations: list[Observation] = []
        tracklets: list[Tracklet] = []
        intents: list[EvidenceIntent] = []
        by_observation_id: dict[str, Observation] = {}
        tracklet_ids: set[str] = set()
        tracked_observation_ids: set[str] = set()
        intent_keys: set[tuple[str, int]] = set()
        sample_cursor = None
        tracker_cursor = None
        previous_candidate: FrameRef | None = None
        previous_selected: FrameRef | None = None
        after: str | None = None
        page_count = 0
        candidate_count = 0
        seen_candidate_ids: set[str] = set()
        started_ns = self._now()
        self._raise_if_cancelled(cancelled, CoordinatorStage.PROBE, started_ns, preparing.run_id)
        raw = self._validate_page(
            source,
            config.stream_index,
            self._adapter_call(
                events,
                CoordinatorStage.PROBE,
                started_ns,
                config.page_candidates,
                preparing.run_id,
                lambda: video_source.read_frames(
                    stream_index=config.stream_index, limit=config.page_candidates
                ),
            ),
            config.page_candidates,
        )
        self._raise_if_cancelled(
            cancelled,
            CoordinatorStage.PROBE,
            started_ns,
            preparing.run_id,
            len(raw),
        )
        self._check_payload(video_descriptor, CoordinatorStage.PROBE, raw)
        if self._expired(run_started_ns, config):
            self._emit(
                events,
                CoordinatorStage.PROBE,
                EventStatus.FAILED,
                started_ns,
                len(raw),
                preparing.run_id,
            )
            raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.PROBE)
        self._emit(
            events,
            CoordinatorStage.PROBE,
            EventStatus.SUCCEEDED,
            started_ns,
            len(raw),
            preparing.run_id,
        )
        while raw:
            if self._expired(run_started_ns, config):
                self._emit(events, CoordinatorStage.SAMPLE, EventStatus.FAILED, started_ns, 0)
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.SAMPLE)
            page_count += 1
            if page_count > config.max_pages:
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.PROBE)
            if candidate_count + len(raw) > config.max_candidates:
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.PROBE)
            if seen_candidate_ids.intersection(frame.frame_id for frame in raw) or (
                previous_candidate is not None
                and int(raw[0].decode_index) <= int(previous_candidate.decode_index)
            ):
                raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.PROBE)
            candidate_count += len(raw)
            seen_candidate_ids.update(frame.frame_id for frame in raw)
            after = raw[-1].decode_index
            read_limit = config.page_candidates - 1
            started_ns = self._now()
            self._raise_if_cancelled(
                cancelled, CoordinatorStage.PROBE, started_ns, preparing.run_id
            )
            following = self._validate_page(
                source,
                config.stream_index,
                self._adapter_call(
                    events,
                    CoordinatorStage.PROBE,
                    started_ns,
                    read_limit,
                    preparing.run_id,
                    lambda after=after, read_limit=read_limit: video_source.read_frames(  # type: ignore[misc]
                        stream_index=config.stream_index,
                        after_decode_index=after,
                        # Every resumed sampler call includes one exact overlap.
                        # Reserve its slot before asking the source for new items.
                        limit=read_limit,
                    ),
                ),
                read_limit,
            )
            self._raise_if_cancelled(
                cancelled,
                CoordinatorStage.PROBE,
                started_ns,
                preparing.run_id,
                len(following),
            )
            self._check_payload(video_descriptor, CoordinatorStage.PROBE, following)
            if self._expired(run_started_ns, config):
                self._emit(
                    events,
                    CoordinatorStage.PROBE,
                    EventStatus.FAILED,
                    started_ns,
                    len(following),
                    preparing.run_id,
                )
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.PROBE)
            self._emit(
                events,
                CoordinatorStage.PROBE,
                EventStatus.SUCCEEDED,
                started_ns,
                len(following),
                preparing.run_id,
            )
            candidates = raw if previous_candidate is None else (previous_candidate, *raw)

            def sample_page(
                candidates: tuple[FrameRef, ...] = candidates,
                sample_cursor: SamplingCursor | None = sample_cursor,
                following: tuple[FrameRef, ...] = following,
            ) -> SamplingPage:
                return sampler.sample_page(
                    source,
                    candidates,
                    sampling,
                    cursor=sample_cursor,
                    end_of_stream=not following,
                    cancelled=cancelled,
                )

            self._check_payload(sampler_descriptor, CoordinatorStage.SAMPLE, (source, *candidates))
            started_ns = self._now()
            self._raise_if_cancelled(
                cancelled, CoordinatorStage.SAMPLE, started_ns, preparing.run_id
            )
            page: SamplingPage = self._adapter_call(
                events,
                CoordinatorStage.SAMPLE,
                started_ns,
                len(candidates),
                preparing.run_id,
                sample_page,
            )
            self._raise_if_cancelled(
                cancelled,
                CoordinatorStage.SAMPLE,
                started_ns,
                preparing.run_id,
                len(candidates),
            )
            if type(page) is not SamplingPage:
                raise CoordinatorError(
                    CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.SAMPLE
                )
            try:
                SamplingPage.__post_init__(page)
            except (TypeError, ValueError):
                raise CoordinatorError(
                    CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.SAMPLE
                ) from None
            selected = page.frames
            if len(sampled) + len(selected) > config.max_samples:
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.SAMPLE)
            candidate_order = {frame.frame_id: index for index, frame in enumerate(candidates)}
            if (
                page.finished is not (not following)
                or page.cursor is None
                or any(frame not in candidates for frame in selected)
                or tuple(candidate_order[frame.frame_id] for frame in selected)
                != tuple(sorted(candidate_order[frame.frame_id] for frame in selected))
                or selected_frame_ids.intersection(frame.frame_id for frame in selected)
            ):
                raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.SAMPLE)
            self._check_payload(sampler_descriptor, CoordinatorStage.SAMPLE, selected)
            if self._expired(run_started_ns, config):
                self._emit(
                    events,
                    CoordinatorStage.SAMPLE,
                    EventStatus.FAILED,
                    started_ns,
                    len(selected),
                    preparing.run_id,
                )
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.SAMPLE)
            self._emit(
                events,
                CoordinatorStage.SAMPLE,
                EventStatus.SUCCEEDED,
                started_ns,
                len(selected),
                preparing.run_id,
            )
            sample_cursor = page.cursor
            selected_frame_ids.update(frame.frame_id for frame in selected)
            selected_metadata_bytes = sum(self._metadata_size(frame) for frame in selected)
            if metadata_bytes + selected_metadata_bytes > config.max_metadata_bytes:
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.SAMPLE)
            if len(selected) > detector_descriptor.max_batch_items:
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.DETECT)
            self._check_payload(detector_descriptor, CoordinatorStage.DETECT, (source, *selected))
            started_ns = self._now()
            self._raise_if_cancelled(
                cancelled, CoordinatorStage.DETECT, started_ns, preparing.run_id
            )
            detection: DetectionResult = self._adapter_call(
                events,
                CoordinatorStage.DETECT,
                started_ns,
                len(selected),
                preparing.run_id,
                lambda selected=selected: detector.detect(source, selected),  # type: ignore[misc]
            )
            self._raise_if_cancelled(
                cancelled,
                CoordinatorStage.DETECT,
                started_ns,
                preparing.run_id,
                len(selected),
            )
            if type(detection) is not DetectionResult:
                self._emit(
                    events,
                    CoordinatorStage.DETECT,
                    EventStatus.FAILED,
                    started_ns,
                    len(selected),
                    preparing.run_id,
                )
                raise CoordinatorError(
                    CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.DETECT
                )
            try:
                DetectionResult.__post_init__(detection)
            except (TypeError, ValueError):
                self._emit(
                    events,
                    CoordinatorStage.DETECT,
                    EventStatus.FAILED,
                    started_ns,
                    len(selected),
                    preparing.run_id,
                )
                raise CoordinatorError(
                    CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.DETECT
                ) from None
            self._check_payload(
                detector_descriptor, CoordinatorStage.DETECT, detection.observations
            )
            if self._expired(run_started_ns, config):
                self._emit(
                    events,
                    CoordinatorStage.DETECT,
                    EventStatus.FAILED,
                    started_ns,
                    len(selected),
                    preparing.run_id,
                )
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.DETECT)
            self._emit(
                events,
                CoordinatorStage.DETECT,
                EventStatus.SUCCEEDED
                if detection.state is PerceptionResultState.COMPLETE
                else EventStatus.FAILED,
                started_ns,
                len(selected),
                preparing.run_id,
            )
            if detection.state is not PerceptionResultState.COMPLETE:
                return self._incomplete(
                    detection.state, detection.reason or "detection_unavailable", tuple(events)
                )
            if any(observation.category != "vehicle" for observation in detection.observations):
                return self._incomplete(
                    PerceptionResultState.UNSUPPORTED, "category_unsupported", tuple(events)
                )
            selected_by_id = {frame.frame_id: frame for frame in selected}
            if len(observations) + len(detection.observations) > config.max_observations:
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.DETECT)
            stream = next(
                item for item in source.streams if item.stream_index == config.stream_index
            )
            if any(
                observation.source_id != source.source_id
                or observation.stream_index != config.stream_index
                or observation.frame_id not in selected_by_id
                or observation.pts != selected_by_id[observation.frame_id].pts
                or observation.geometry.source_width != stream.width
                or observation.geometry.source_height != stream.height
                or observation.producer != detector_producer
                or observation.observation_id in by_observation_id
                for observation in detection.observations
            ):
                raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.DETECT)
            observation_metadata_bytes = sum(
                self._metadata_size(observation) for observation in detection.observations
            )
            if (
                metadata_bytes + selected_metadata_bytes + observation_metadata_bytes
                > config.max_metadata_bytes
            ):
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.DETECT)
            by_observation_id.update(
                {observation.observation_id: observation for observation in detection.observations}
            )

            def score_page(
                previous_selected: FrameRef | None = previous_selected,
                selected: tuple[FrameRef, ...] = selected,
            ) -> FrameDiscontinuityResult:
                return discontinuities.score(source, previous_selected, selected)

            if len(selected) > discontinuity_descriptor.max_batch_items:
                raise CoordinatorError(
                    CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.DISCONTINUITY
                )
            self._check_payload(
                discontinuity_descriptor,
                CoordinatorStage.DISCONTINUITY,
                (
                    source,
                    *((previous_selected,) if previous_selected is not None else ()),
                    *selected,
                ),
            )
            started_ns = self._now()
            self._raise_if_cancelled(
                cancelled, CoordinatorStage.DISCONTINUITY, started_ns, preparing.run_id
            )
            score_result = self._adapter_call(
                events,
                CoordinatorStage.DISCONTINUITY,
                started_ns,
                len(selected),
                preparing.run_id,
                score_page,
            )
            self._raise_if_cancelled(
                cancelled,
                CoordinatorStage.DISCONTINUITY,
                started_ns,
                preparing.run_id,
                len(selected),
            )
            if type(score_result) is not FrameDiscontinuityResult:
                self._emit(
                    events,
                    CoordinatorStage.DISCONTINUITY,
                    EventStatus.FAILED,
                    started_ns,
                    len(selected),
                    preparing.run_id,
                )
                raise CoordinatorError(
                    CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.DISCONTINUITY
                )
            try:
                FrameDiscontinuityResult.__post_init__(score_result)
            except (TypeError, ValueError):
                self._emit(
                    events,
                    CoordinatorStage.DISCONTINUITY,
                    EventStatus.FAILED,
                    started_ns,
                    len(selected),
                    preparing.run_id,
                )
                raise CoordinatorError(
                    CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.DISCONTINUITY
                ) from None
            self._check_payload(
                discontinuity_descriptor,
                CoordinatorStage.DISCONTINUITY,
                score_result.discontinuities,
            )
            if self._expired(run_started_ns, config):
                self._emit(
                    events,
                    CoordinatorStage.DISCONTINUITY,
                    EventStatus.FAILED,
                    started_ns,
                    len(selected),
                    preparing.run_id,
                )
                raise CoordinatorError(
                    CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.DISCONTINUITY
                )
            self._emit(
                events,
                CoordinatorStage.DISCONTINUITY,
                EventStatus.SUCCEEDED
                if score_result.state is PerceptionResultState.COMPLETE
                else EventStatus.FAILED,
                started_ns,
                len(selected),
                preparing.run_id,
            )
            if score_result.state is not PerceptionResultState.COMPLETE:
                return self._incomplete(
                    score_result.state, score_result.reason or "scores_unavailable", tuple(events)
                )
            if tuple(
                (item.source_id, item.frame_id, item.stream_index, item.pts)
                for item in score_result.discontinuities
            ) != tuple(
                (frame.source_id, frame.frame_id, frame.stream_index, frame.pts)
                for frame in selected
            ):
                raise CoordinatorError(
                    CoordinatorErrorCode.CONFLICT, CoordinatorStage.DISCONTINUITY
                )

            def track_page(
                selected: tuple[FrameRef, ...] = selected,
                detection: DetectionResult = detection,
                score_result: FrameDiscontinuityResult = score_result,
                tracker_cursor: TrackingCursor | None = tracker_cursor,
                following: tuple[FrameRef, ...] = following,
            ) -> TrackingPage:
                return tracker.track_page(
                    source,
                    selected,
                    detection.observations,
                    discontinuities=score_result.discontinuities,
                    cursor=tracker_cursor,
                    end_of_stream=not following,
                    cancelled=cancelled,
                )

            if len(selected) > tracker_descriptor.max_batch_items:
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.TRACK)
            self._check_payload(
                tracker_descriptor,
                CoordinatorStage.TRACK,
                (source, *selected, *detection.observations, *score_result.discontinuities),
            )
            started_ns = self._now()
            self._raise_if_cancelled(
                cancelled, CoordinatorStage.TRACK, started_ns, preparing.run_id
            )
            tracked: TrackingPage = self._adapter_call(
                events,
                CoordinatorStage.TRACK,
                started_ns,
                len(selected),
                preparing.run_id,
                track_page,
            )
            self._raise_if_cancelled(
                cancelled,
                CoordinatorStage.TRACK,
                started_ns,
                preparing.run_id,
                len(selected),
            )
            if type(tracked) is not TrackingPage:
                self._emit(
                    events,
                    CoordinatorStage.TRACK,
                    EventStatus.FAILED,
                    started_ns,
                    len(selected),
                    preparing.run_id,
                )
                raise CoordinatorError(CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.TRACK)
            try:
                TrackingPage.__post_init__(tracked)
            except (TypeError, ValueError):
                self._emit(
                    events,
                    CoordinatorStage.TRACK,
                    EventStatus.FAILED,
                    started_ns,
                    len(selected),
                    preparing.run_id,
                )
                raise CoordinatorError(
                    CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.TRACK
                ) from None
            self._check_payload(tracker_descriptor, CoordinatorStage.TRACK, tracked.tracklets)
            if self._expired(run_started_ns, config):
                self._emit(
                    events,
                    CoordinatorStage.TRACK,
                    EventStatus.FAILED,
                    started_ns,
                    len(selected),
                    preparing.run_id,
                )
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.TRACK)
            self._emit(
                events,
                CoordinatorStage.TRACK,
                EventStatus.SUCCEEDED
                if tracked.state is PerceptionResultState.COMPLETE
                else EventStatus.FAILED,
                started_ns,
                len(selected),
                preparing.run_id,
            )
            if tracked.state is not PerceptionResultState.COMPLETE:
                return self._incomplete(
                    tracked.state, tracked.reason or "tracking_unavailable", tuple(events)
                )
            tracker_cursor = tracked.cursor
            if len(tracklets) + len(tracked.tracklets) > config.max_tracklets:
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.TRACK)
            page_point_ids = [
                point.observation_id for tracklet in tracked.tracklets for point in tracklet.points
            ]
            if (
                tracked.finished is not (not following)
                or tracker_cursor is None
                or tracklet_ids.intersection(tracklet.tracklet_id for tracklet in tracked.tracklets)
                or tracked_observation_ids.intersection(page_point_ids)
                or len(page_point_ids) != len(set(page_point_ids))
                or any(
                    tracklet.source_id != source.source_id
                    or tracklet.stream_index != config.stream_index
                    or tracklet.category != "vehicle"
                    or tracklet.producer != tracker_producer
                    or any(
                        point.observation_id not in by_observation_id
                        or point
                        != TrackPoint.from_observation(by_observation_id[point.observation_id])
                        for point in tracklet.points
                    )
                    for tracklet in tracked.tracklets
                )
            ):
                raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.TRACK)
            page_point_id_set = set(page_point_ids)
            active_point_ids = {
                point.observation_id
                for active in tracker_cursor.active_tracks
                for point in active.points
            }
            all_completed_point_ids = tracked_observation_ids | page_point_id_set
            if active_point_ids.intersection(
                all_completed_point_ids
            ) or active_point_ids | all_completed_point_ids != set(by_observation_id):
                raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.TRACK)
            tracklet_metadata_bytes = sum(
                self._metadata_size(tracklet) for tracklet in tracked.tracklets
            )
            if (
                metadata_bytes
                + selected_metadata_bytes
                + observation_metadata_bytes
                + tracklet_metadata_bytes
                > config.max_metadata_bytes
            ):
                raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.TRACK)
            metadata_bytes += (
                selected_metadata_bytes + observation_metadata_bytes + tracklet_metadata_bytes
            )
            tracklet_ids.update(tracklet.tracklet_id for tracklet in tracked.tracklets)
            tracked_observation_ids.update(page_point_id_set)
            sampled.extend(selected)
            observations.extend(detection.observations)
            for tracklet in tracked.tracklets:
                selected_observations = tuple(
                    by_observation_id[point.observation_id] for point in tracklet.points
                )

                def plan_tracklet(
                    tracklet: Tracklet = tracklet,
                    selected_observations: tuple[Observation, ...] = selected_observations,
                ) -> EvidencePlanResult:
                    return selector.plan(tracklet, selected_observations)

                if len(selected_observations) > selector_descriptor.max_batch_items:
                    raise CoordinatorError(
                        CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.SELECT
                    )
                self._check_payload(
                    selector_descriptor,
                    CoordinatorStage.SELECT,
                    (tracklet, *selected_observations),
                )
                started_ns = self._now()
                self._raise_if_cancelled(
                    cancelled, CoordinatorStage.SELECT, started_ns, preparing.run_id
                )
                plan: EvidencePlanResult = self._adapter_call(
                    events,
                    CoordinatorStage.SELECT,
                    started_ns,
                    len(selected_observations),
                    preparing.run_id,
                    plan_tracklet,
                )
                self._raise_if_cancelled(
                    cancelled,
                    CoordinatorStage.SELECT,
                    started_ns,
                    preparing.run_id,
                    len(selected_observations),
                )
                if type(plan) is not EvidencePlanResult:
                    self._emit(
                        events,
                        CoordinatorStage.SELECT,
                        EventStatus.FAILED,
                        started_ns,
                        len(selected_observations),
                        preparing.run_id,
                    )
                    raise CoordinatorError(
                        CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.SELECT
                    )
                try:
                    EvidencePlanResult.__post_init__(plan)
                except (TypeError, ValueError):
                    self._emit(
                        events,
                        CoordinatorStage.SELECT,
                        EventStatus.FAILED,
                        started_ns,
                        len(selected_observations),
                        preparing.run_id,
                    )
                    raise CoordinatorError(
                        CoordinatorErrorCode.INVALID_REQUEST, CoordinatorStage.SELECT
                    ) from None
                self._check_payload(selector_descriptor, CoordinatorStage.SELECT, plan.intents)
                if self._expired(run_started_ns, config):
                    self._emit(
                        events,
                        CoordinatorStage.SELECT,
                        EventStatus.FAILED,
                        started_ns,
                        len(selected_observations),
                        preparing.run_id,
                    )
                    raise CoordinatorError(
                        CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.SELECT
                    )
                self._emit(
                    events,
                    CoordinatorStage.SELECT,
                    EventStatus.SUCCEEDED
                    if plan.state is PerceptionResultState.COMPLETE
                    else EventStatus.FAILED,
                    started_ns,
                    len(selected_observations),
                    preparing.run_id,
                )
                if plan.state is not PerceptionResultState.COMPLETE:
                    return self._incomplete(
                        plan.state, plan.reason or "selection_unavailable", tuple(events)
                    )
                if len(intents) + len(plan.intents) > config.max_intents:
                    raise CoordinatorError(
                        CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.SELECT
                    )
                if not plan.intents:
                    raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.SELECT)
                page_intent_keys = {(intent.tracklet_id, intent.rank) for intent in plan.intents}
                if len(page_intent_keys) != len(plan.intents) or intent_keys.intersection(
                    page_intent_keys
                ):
                    raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.SELECT)
                for intent in plan.intents:
                    observation = by_observation_id.get(intent.observation_id)
                    if (
                        observation is None
                        or intent.tracklet_id != tracklet.tracklet_id
                        or intent.source_id != observation.source_id
                        or intent.frame_id != observation.frame_id
                        or intent.stream_index != observation.stream_index
                        or intent.pts != observation.pts
                        or intent.geometry != observation.geometry
                        or intent.score.confidence_millionths != observation.confidence_millionths
                        or intent.selector != selector_producer
                        or intent.observation_id
                        not in {point.observation_id for point in tracklet.points}
                    ):
                        raise CoordinatorError(
                            CoordinatorErrorCode.CONFLICT, CoordinatorStage.SELECT
                        )
                intent_metadata_bytes = sum(self._metadata_size(intent) for intent in plan.intents)
                if metadata_bytes + intent_metadata_bytes > config.max_metadata_bytes:
                    raise CoordinatorError(
                        CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.SELECT
                    )
                metadata_bytes += intent_metadata_bytes
                intent_keys.update(page_intent_keys)
                tracklets.append(tracklet)
                intents.extend(plan.intents)
            previous_candidate = raw[-1]
            if selected:
                previous_selected = selected[-1]
            raw = following
        started_ns = self._now()
        self._raise_if_cancelled(cancelled, CoordinatorStage.FINALIZE, started_ns, preparing.run_id)
        if self._expired(run_started_ns, config):
            raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.FINALIZE)
        reprobed = self._adapter_call(
            events,
            CoordinatorStage.PROBE,
            started_ns,
            0,
            preparing.run_id,
            video_source.probe,
        )
        self._raise_if_cancelled(cancelled, CoordinatorStage.PROBE, started_ns, preparing.run_id)
        if self._expired(run_started_ns, config):
            self._emit(
                events,
                CoordinatorStage.PROBE,
                EventStatus.FAILED,
                started_ns,
                0,
                preparing.run_id,
            )
            raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.PROBE)
        if reprobed != source:
            raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.PROBE)
        index = sample_index_bytes(tuple(sampled))
        committed = RunManifest.create(
            source.source_id,
            producers,
            sampling,
            "committed",
            RunOutputs(str(len(sampled)), hashlib.sha256(index).hexdigest()),
        )
        committed_metadata_bytes = self._metadata_size(committed)
        metadata_bytes += max(0, committed_metadata_bytes - preparing_metadata_bytes)
        if metadata_bytes > config.max_metadata_bytes:
            raise CoordinatorError(CoordinatorErrorCode.LIMIT_EXCEEDED, CoordinatorStage.FINALIZE)
        try:
            existing = self._world.get(preparing.run_id)
        except PortError as error:
            if error.code is not PortErrorCode.NOT_FOUND:
                code = {
                    PortErrorCode.LIMIT_EXCEEDED: CoordinatorErrorCode.LIMIT_EXCEEDED,
                    PortErrorCode.INVALID_REQUEST: CoordinatorErrorCode.INVALID_REQUEST,
                    PortErrorCode.CONFLICT: CoordinatorErrorCode.CONFLICT,
                    PortErrorCode.CORRUPT: CoordinatorErrorCode.CORRUPT,
                }.get(error.code, CoordinatorErrorCode.OPERATION_FAILED)
                raise CoordinatorError(code, CoordinatorStage.VERIFY) from None
            existing = None
        if existing is not None:
            if existing == committed:
                self._verify_persisted(
                    committed,
                    tuple(sampled),
                    tuple(observations),
                    tuple(tracklets),
                    tuple(intents),
                    source,
                    config,
                )
                return PerceptionRunResult(
                    state=PerceptionResultState.COMPLETE,
                    manifest=committed,
                    frames=tuple(sampled),
                    observations=tuple(observations),
                    tracklets=tuple(tracklets),
                    intents=tuple(intents),
                    disposition=PerceptionDisposition.ALREADY_COMMITTED,
                    events=tuple(events),
                )
            if (
                type(existing) is not RunManifest
                or existing.state not in {"failed", "cancelled"}
                or existing.run_id != preparing.run_id
                or existing.source_id != preparing.source_id
                or existing.producers != preparing.producers
                or existing.sampling != preparing.sampling
                or existing.outputs is not None
            ):
                raise CoordinatorError(CoordinatorErrorCode.CONFLICT, CoordinatorStage.FINALIZE)
        started_ns = self._now()
        self._raise_if_cancelled(
            cancelled, CoordinatorStage.PERCEPTION_METADATA, started_ns, preparing.run_id
        )
        try:
            with self._evidence.writer_session() as session:
                self._world.commit((source, preparing), evidence_session=session)
                IngestionCoordinator._boundary(
                    fault_hook, CommitBoundary.PREPARED, preparing.run_id
                )
                self._raise_if_cancelled(
                    cancelled,
                    CoordinatorStage.PERCEPTION_METADATA,
                    started_ns,
                    preparing.run_id,
                )
                if self._expired(run_started_ns, config):
                    raise CoordinatorError(
                        CoordinatorErrorCode.LIMIT_EXCEEDED,
                        CoordinatorStage.PERCEPTION_METADATA,
                    )
                for batch in _batches(tuple(sampled)):
                    self._world.commit_for_run(preparing.run_id, batch, evidence_session=session)
                IngestionCoordinator._boundary(
                    fault_hook, CommitBoundary.FRAME_METADATA_RECORDED, preparing.run_id
                )
                self._raise_if_cancelled(
                    cancelled,
                    CoordinatorStage.PERCEPTION_METADATA,
                    started_ns,
                    preparing.run_id,
                    len(sampled),
                )
                if self._expired(run_started_ns, config):
                    raise CoordinatorError(
                        CoordinatorErrorCode.LIMIT_EXCEEDED,
                        CoordinatorStage.PERCEPTION_METADATA,
                    )
                for observation_batch in _batches(tuple(observations)):
                    self._world.commit_perception_for_run(
                        preparing.run_id, observation_batch, evidence_session=session
                    )
                IngestionCoordinator._boundary(
                    fault_hook, CommitBoundary.OBSERVATIONS_RECORDED, preparing.run_id
                )
                self._raise_if_cancelled(
                    cancelled,
                    CoordinatorStage.PERCEPTION_METADATA,
                    started_ns,
                    preparing.run_id,
                    len(observations),
                )
                if self._expired(run_started_ns, config):
                    raise CoordinatorError(
                        CoordinatorErrorCode.LIMIT_EXCEEDED,
                        CoordinatorStage.PERCEPTION_METADATA,
                    )
                for tracklet in tracklets:
                    tracklet_intents = tuple(
                        intent for intent in intents if intent.tracklet_id == tracklet.tracklet_id
                    )
                    self._world.commit_perception_for_run(
                        preparing.run_id,
                        (tracklet,),
                        tracklet_intents,
                        evidence_session=session,
                    )
                IngestionCoordinator._boundary(
                    fault_hook,
                    CommitBoundary.TRACKLETS_AND_INTENTS_RECORDED,
                    preparing.run_id,
                )
                self._raise_if_cancelled(
                    cancelled,
                    CoordinatorStage.PERCEPTION_METADATA,
                    started_ns,
                    preparing.run_id,
                    len(tracklets) + len(intents),
                )
                if self._expired(run_started_ns, config):
                    raise CoordinatorError(
                        CoordinatorErrorCode.LIMIT_EXCEEDED,
                        CoordinatorStage.PERCEPTION_METADATA,
                    )
                self._world.finalize_run(committed, evidence_session=session)
                IngestionCoordinator._boundary(
                    fault_hook, CommitBoundary.RUN_COMMITTED, preparing.run_id
                )
        except _PerceptionCancelled:
            raise
        except CoordinatorError:
            self._emit(
                events,
                CoordinatorStage.PERCEPTION_METADATA,
                EventStatus.FAILED,
                started_ns,
                0,
                preparing.run_id,
            )
            raise
        except PortError as error:
            code = {
                PortErrorCode.LIMIT_EXCEEDED: CoordinatorErrorCode.LIMIT_EXCEEDED,
                PortErrorCode.INVALID_REQUEST: CoordinatorErrorCode.INVALID_REQUEST,
                PortErrorCode.NOT_FOUND: CoordinatorErrorCode.CONFLICT,
                PortErrorCode.CONFLICT: CoordinatorErrorCode.CONFLICT,
                PortErrorCode.CORRUPT: CoordinatorErrorCode.CORRUPT,
            }.get(error.code, CoordinatorErrorCode.OPERATION_FAILED)
            self._emit(
                events,
                CoordinatorStage.PERCEPTION_METADATA,
                EventStatus.FAILED,
                started_ns,
                0,
                preparing.run_id,
            )
            raise CoordinatorError(code, CoordinatorStage.PERCEPTION_METADATA) from None
        self._emit(
            events,
            CoordinatorStage.PERCEPTION_METADATA,
            EventStatus.SUCCEEDED,
            started_ns,
            len(observations) + len(tracklets) + len(intents),
            committed.run_id,
        )
        self._verify_persisted(
            committed,
            tuple(sampled),
            tuple(observations),
            tuple(tracklets),
            tuple(intents),
            source,
            config,
        )
        return PerceptionRunResult(
            state=PerceptionResultState.COMPLETE,
            manifest=committed,
            frames=tuple(sampled),
            observations=tuple(observations),
            tracklets=tuple(tracklets),
            intents=tuple(intents),
            disposition=PerceptionDisposition.COMMITTED,
            events=tuple(events),
        )


__all__ = [
    "COORDINATION_PROTOCOL_VERSION",
    "MAX_COORDINATOR_BYTES",
    "MAX_COORDINATOR_ITEMS",
    "MAX_FRAME_BYTES",
    "MAX_PERCEPTION_PAGES",
    "MAX_PERCEPTION_SAMPLES",
    "MAX_RECOVERY_RUNS",
    "PERCEPTION_COORDINATION_PROTOCOL_VERSION",
    "CommitBoundary",
    "CoordinatorError",
    "CoordinatorErrorCode",
    "CoordinatorEvent",
    "CoordinatorStage",
    "DiscontinuityProvider",
    "EventSink",
    "EventStatus",
    "EvidencePlanner",
    "FaultHook",
    "FrameDiscontinuityResult",
    "IngestionConfig",
    "IngestionCoordinator",
    "IngestionDisposition",
    "IngestionResult",
    "ManualEvidenceInput",
    "PerceptionConfig",
    "PerceptionCoordinator",
    "PerceptionDisposition",
    "PerceptionEvent",
    "PerceptionEventSink",
    "PerceptionRunResult",
    "RecoveryReport",
    "RepairReport",
    "new_deletion_id",
    "sample_index_bytes",
]
