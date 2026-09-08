# SPDX-License-Identifier: Apache-2.0
"""Private local SQLite metadata store for ingestion and perception records."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import NoReturn, cast

from visualworld import storage as _filesystem
from visualworld import world_store_v2 as _v2_schema
from visualworld.evidence import (
    EvidenceIntent,
    dumps_evidence_intent,
    loads_evidence_intent,
)
from visualworld.ingestion import (
    Artifact,
    EvidenceRef,
    FrameRef,
    Record,
    RecordValidationError,
    RunManifest,
    Source,
    dumps_record,
    loads_record,
)
from visualworld.perception import (
    Observation,
    PerceptionRecord,
    Tracklet,
    TrackPoint,
    dumps_perception_record,
    loads_perception_record,
)
from visualworld.ports import (
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    PortError,
    PortErrorCode,
    PortKind,
)
from visualworld.storage import (
    DEFAULT_LOCK_TIMEOUT_MS,
    ArtifactState,
    EvidenceWriterSession,
    LocalEvidenceStore,
    StageHandle,
)

DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_MAX_AUDIT_RECORDS = 4_096
WORLD_SCHEMA_VERSION = 2
WORLD_PROTOCOL_VERSION = 1

_DATABASE_NAME = "world.sqlite3"
_DATABASE_COMPANIONS = ("world.sqlite3-wal", "world.sqlite3-shm")
_UNEXPECTED_DATABASE_FILES = ("world.sqlite3-journal",)
_RECORD_ID = re.compile(r"(?:src|frm|evi|run)_[0-9a-f]{64}\Z")
_SOURCE_ID = re.compile(r"src_[0-9a-f]{64}\Z")
_FRAME_ID = re.compile(r"frm_[0-9a-f]{64}\Z")
_EVIDENCE_ID = re.compile(r"evi_[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"run_[0-9a-f]{64}\Z")
_OBSERVATION_ID = re.compile(r"obs_[0-9a-f]{64}\Z")
_TRACKLET_ID = re.compile(r"trk_[0-9a-f]{64}\Z")
_DELETION_ID = re.compile(r"del_[0-9a-f]{64}\Z")
_STAGING_NAME = re.compile(r"[0-9a-f]{32}\.part\Z")
_UNSIGNED_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_SIGNED_DECIMAL = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")
_CATEGORY = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


def _error(
    code: PortErrorCode,
    operation: str,
    *,
    retryable: bool = False,
) -> PortError:
    return PortError(code, PortKind.WORLD_STORE, operation, retryable=retryable)


def _fail(
    code: PortErrorCode,
    operation: str,
    *,
    retryable: bool = False,
) -> NoReturn:
    raise _error(code, operation, retryable=retryable) from None


def _translate_filesystem_error(error: PortError, operation: str) -> NoReturn:
    if error.port is PortKind.WORLD_STORE:
        raise error
    _fail(error.code, operation, retryable=error.retryable)


def _sqlite_error(error: sqlite3.Error, operation: str) -> NoReturn:
    code = getattr(error, "sqlite_errorcode", None)
    primary = code & 0xFF if type(code) is int else None
    if primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        _fail(PortErrorCode.TIMEOUT, operation, retryable=True)
    if primary in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_SCHEMA}:
        _fail(PortErrorCode.CORRUPT, operation)
    if isinstance(error, sqlite3.IntegrityError):
        _fail(PortErrorCode.CONFLICT, operation)
    _fail(PortErrorCode.STORAGE_FAILED, operation, retryable=primary == sqlite3.SQLITE_IOERR)


def _bounded_limit(value: object, operation: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_PORT_BATCH_ITEMS:
        _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
    return value


def _identifier(value: object, pattern: re.Pattern[str], operation: str) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    return value


def _deletion_id(value: object, operation: str) -> str:
    return _identifier(value, _DELETION_ID, operation)


def _signed_i64(value: object, operation: str) -> tuple[str, int]:
    if type(value) is not str or not _SIGNED_DECIMAL.fullmatch(value):
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    parsed = int(value)
    if not -(2**63) <= parsed <= 2**63 - 1:
        _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
    return value, parsed


def _record_identifier(record: Record) -> str:
    if isinstance(record, Source):
        return record.source_id
    if isinstance(record, FrameRef):
        return record.frame_id
    if isinstance(record, EvidenceRef):
        return record.evidence_id
    return record.run_id


def _canonical_record(value: object, operation: str) -> tuple[Record, bytes]:
    try:
        encoded = dumps_record(cast(Record, value))
        decoded = loads_record(encoded)
    except (AttributeError, RecordValidationError, TypeError, ValueError):
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    if type(decoded) is not type(value) or decoded != value:
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    return decoded, encoded


def _canonical_stage(value: object, operation: str) -> StageHandle:
    if type(value) is not StageHandle:
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    try:
        return StageHandle.from_mapping(value.to_mapping())
    except (AttributeError, TypeError, ValueError):
        _fail(PortErrorCode.INVALID_REQUEST, operation)


def _canonical_perception_record(
    value: object,
    operation: str,
) -> tuple[PerceptionRecord, bytes]:
    failed = False
    encoded: bytes | None = None
    decoded: PerceptionRecord | None = None
    try:
        if type(value) not in {Observation, Tracklet}:
            raise ValueError("unsupported perception record")
        encoded = dumps_perception_record(cast(PerceptionRecord, value))
        decoded = loads_perception_record(encoded)
    except BaseException:
        failed = True
    if failed or encoded is None or decoded is None:
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    if type(decoded) is not type(value) or dumps_perception_record(decoded) != encoded:
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    return decoded, encoded


def _canonical_evidence_intent(
    value: object,
    operation: str,
) -> tuple[EvidenceIntent, bytes]:
    failed = False
    encoded: bytes | None = None
    decoded: EvidenceIntent | None = None
    try:
        if type(value) is not EvidenceIntent:
            raise ValueError("unsupported evidence intent")
        encoded = dumps_evidence_intent(value)
        decoded = loads_evidence_intent(encoded)
    except BaseException:
        failed = True
    if failed or encoded is None or decoded is None:
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    if dumps_evidence_intent(decoded) != encoded:
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    return decoded, encoded


_SCHEMA_V1: tuple[str, ...] = (
    """CREATE TABLE schema_migrations (
        schema_version INTEGER NOT NULL PRIMARY KEY CHECK (schema_version > 0),
        migration_name TEXT NOT NULL UNIQUE,
        code_sha256 TEXT NOT NULL CHECK (length(code_sha256) = 64),
        applied_at_utc TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE sources (
        source_id TEXT NOT NULL PRIMARY KEY CHECK (length(source_id) = 68),
        schema_version INTEGER NOT NULL,
        identity_version INTEGER NOT NULL,
        record_json BLOB NOT NULL,
        record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64)
    ) STRICT""",
    """CREATE TABLE source_streams (
        source_id TEXT NOT NULL,
        stream_index INTEGER NOT NULL,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        rotation_degrees INTEGER NOT NULL,
        time_base_numerator TEXT NOT NULL,
        time_base_denominator TEXT NOT NULL,
        PRIMARY KEY (source_id, stream_index),
        FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE
    ) STRICT""",
    """CREATE TABLE frames (
        frame_id TEXT NOT NULL PRIMARY KEY CHECK (length(frame_id) = 68),
        source_id TEXT NOT NULL,
        stream_index INTEGER NOT NULL,
        decode_index TEXT NOT NULL,
        pts_value TEXT NOT NULL,
        pts_time_base_numerator TEXT NOT NULL,
        pts_time_base_denominator TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        identity_version INTEGER NOT NULL,
        record_json BLOB NOT NULL,
        record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
        UNIQUE (source_id, stream_index, decode_index),
        FOREIGN KEY (source_id, stream_index)
            REFERENCES source_streams(source_id, stream_index) ON DELETE CASCADE
    ) STRICT""",
    """CREATE TABLE artifact_catalog (
        digest TEXT NOT NULL PRIMARY KEY CHECK (length(digest) = 64),
        byte_count TEXT NOT NULL,
        media_type TEXT NOT NULL,
        layout_version INTEGER NOT NULL CHECK (layout_version = 1)
    ) STRICT""",
    """CREATE TABLE evidence (
        evidence_id TEXT NOT NULL PRIMARY KEY CHECK (length(evidence_id) = 68),
        frame_id TEXT NOT NULL,
        artifact_digest TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        identity_version INTEGER NOT NULL,
        record_json BLOB NOT NULL,
        record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
        FOREIGN KEY (frame_id) REFERENCES frames(frame_id) ON DELETE CASCADE,
        FOREIGN KEY (artifact_digest) REFERENCES artifact_catalog(digest)
    ) STRICT""",
    """CREATE TABLE artifact_references (
        evidence_id TEXT NOT NULL PRIMARY KEY,
        artifact_digest TEXT NOT NULL,
        FOREIGN KEY (evidence_id) REFERENCES evidence(evidence_id) ON DELETE CASCADE,
        FOREIGN KEY (artifact_digest) REFERENCES artifact_catalog(digest)
    ) STRICT""",
    """CREATE TABLE runs (
        run_id TEXT NOT NULL PRIMARY KEY CHECK (length(run_id) = 68),
        source_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('preparing', 'committed', 'failed', 'cancelled')),
        schema_version INTEGER NOT NULL,
        identity_version INTEGER NOT NULL,
        record_json BLOB NOT NULL,
        record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
        FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE
    ) STRICT""",
    """CREATE TABLE artifact_intents (
        run_id TEXT NOT NULL,
        staging_name TEXT NOT NULL,
        artifact_digest TEXT NOT NULL,
        byte_count TEXT NOT NULL,
        media_type TEXT NOT NULL,
        protocol_version INTEGER NOT NULL CHECK (protocol_version = 1),
        PRIMARY KEY (run_id, staging_name),
        UNIQUE (run_id, artifact_digest),
        FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
    ) STRICT""",
    """CREATE TABLE run_records (
        run_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        record_type TEXT NOT NULL CHECK (record_type IN ('frame', 'evidence')),
        PRIMARY KEY (run_id, record_id),
        FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
    ) STRICT""",
    """CREATE TABLE deletion_jobs (
        deletion_id TEXT NOT NULL PRIMARY KEY CHECK (length(deletion_id) = 68),
        root_kind TEXT CHECK (root_kind IS NULL OR root_kind = 'source'),
        root_id TEXT,
        protocol_version INTEGER NOT NULL CHECK (protocol_version = 1),
        state TEXT NOT NULL CHECK (state IN ('pending', 'metadata_purged', 'complete')),
        record_count INTEGER NOT NULL,
        artifact_count INTEGER NOT NULL,
        shared_retention_count INTEGER NOT NULL,
        completed_at_utc TEXT
    ) STRICT""",
    """CREATE TABLE deletion_closure (
        deletion_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        record_type TEXT NOT NULL CHECK (record_type IN ('source', 'frame', 'evidence', 'run')),
        PRIMARY KEY (deletion_id, record_id),
        FOREIGN KEY (deletion_id) REFERENCES deletion_jobs(deletion_id) ON DELETE CASCADE
    ) STRICT""",
    """CREATE TABLE deletion_artifacts (
        deletion_id TEXT NOT NULL,
        artifact_digest TEXT NOT NULL,
        delete_required INTEGER NOT NULL CHECK (delete_required IN (0, 1)),
        PRIMARY KEY (deletion_id, artifact_digest),
        FOREIGN KEY (deletion_id) REFERENCES deletion_jobs(deletion_id) ON DELETE CASCADE
    ) STRICT""",
    """CREATE INDEX frames_source_position
        ON frames(source_id, stream_index, length(decode_index), decode_index)""",
    """CREATE INDEX frames_exact_pts
        ON frames(
            source_id,
            stream_index,
            pts_value,
            pts_time_base_numerator,
            pts_time_base_denominator
        )""",
    "CREATE INDEX evidence_frame ON evidence(frame_id, evidence_id)",
    "CREATE INDEX artifact_references_digest ON artifact_references(artifact_digest)",
    "CREATE INDEX runs_source_state ON runs(source_id, state, run_id)",
    "CREATE INDEX artifact_intents_digest ON artifact_intents(artifact_digest, run_id)",
    "CREATE INDEX run_records_record ON run_records(record_id, run_id)",
    "CREATE INDEX deletion_jobs_state ON deletion_jobs(state, deletion_id)",
    "CREATE INDEX deletion_closure_record ON deletion_closure(record_id, deletion_id)",
)

_MIGRATION_V1_NAME = "v1_ingestion_metadata"
_MIGRATION_V1_CHECKSUM = hashlib.sha256("\0".join(_SCHEMA_V1).encode("utf-8")).hexdigest()
# Compatibility alias retained for v0.1 tests and external diagnostics.
_MIGRATION_CHECKSUM = _MIGRATION_V1_CHECKSUM


@dataclass(frozen=True, slots=True)
class WorldStoreStats:
    record_count: int
    artifact_count: int
    intent_count: int
    schema_version: int = WORLD_SCHEMA_VERSION
    observation_count: int = 0
    tracklet_count: int = 0
    selection_count: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.record_count) is not int
            or self.record_count < 0
            or type(self.artifact_count) is not int
            or self.artifact_count < 0
            or type(self.intent_count) is not int
            or self.intent_count < 0
            or type(self.schema_version) is not int
            or self.schema_version != WORLD_SCHEMA_VERSION
            or type(self.observation_count) is not int
            or self.observation_count < 0
            or type(self.tracklet_count) is not int
            or self.tracklet_count < 0
            or type(self.selection_count) is not int
            or self.selection_count < 0
        ):
            raise ValueError("invalid world store statistics")


@dataclass(frozen=True, slots=True)
class EvidenceSelectionLink:
    """Bind one persisted evidence intent rank to an existing EvidenceRef."""

    tracklet_id: str
    rank: int
    evidence_id: str

    def __post_init__(self) -> None:
        if (
            type(self.tracklet_id) is not str
            or not _TRACKLET_ID.fullmatch(self.tracklet_id)
            or type(self.rank) is not int
            or not 1 <= self.rank <= 8
            or type(self.evidence_id) is not str
            or not _EVIDENCE_ID.fullmatch(self.evidence_id)
        ):
            raise ValueError("invalid evidence selection link")


@dataclass(frozen=True, slots=True, repr=False)
class PersistedEvidenceSelection:
    """One metadata-only selected view and its optional materialized reference."""

    run_id: str
    intent: EvidenceIntent
    evidence: EvidenceRef | None = None

    def __post_init__(self) -> None:
        if (
            type(self.run_id) is not str
            or not _RUN_ID.fullmatch(self.run_id)
            or type(self.intent) is not EvidenceIntent
            or (self.evidence is not None and type(self.evidence) is not EvidenceRef)
        ):
            raise ValueError("invalid persisted evidence selection")
        intent = loads_evidence_intent(dumps_evidence_intent(self.intent))
        evidence = self.evidence
        if evidence is not None:
            try:
                owned_evidence = loads_record(dumps_record(evidence))
            except (RecordValidationError, TypeError, ValueError):
                raise ValueError("invalid persisted evidence selection") from None
            if not isinstance(owned_evidence, EvidenceRef):
                raise ValueError("invalid persisted evidence selection")
            evidence = owned_evidence
            if (
                evidence.frame_id != intent.frame_id
                or evidence.geometry != intent.geometry
                or evidence.kind != intent.kind
                or evidence.retention != intent.retention
            ):
                raise ValueError("inconsistent persisted evidence selection")
        object.__setattr__(self, "intent", intent)
        object.__setattr__(self, "evidence", evidence)

    def __repr__(self) -> str:
        return (
            f"PersistedEvidenceSelection(run_id={self.run_id!r}, "
            f"rank={self.intent.rank}, materialized={self.evidence is not None})"
        )


class DeletionState(StrEnum):
    PENDING = "pending"
    METADATA_PURGED = "metadata_purged"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True, repr=False)
class DeletionArtifact:
    artifact: Artifact
    delete_required: bool

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, Artifact) or type(self.delete_required) is not bool:
            raise ValueError("invalid deletion artifact")

    def __repr__(self) -> str:
        return f"DeletionArtifact(<redacted>, delete_required={self.delete_required!r})"


@dataclass(frozen=True, slots=True, repr=False)
class RunCleanupPlan:
    run_id: str
    artifacts: tuple[Artifact, ...]
    stages: tuple[StageHandle, ...]
    protocol_version: int = WORLD_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.run_id) is not str
            or not _RUN_ID.fullmatch(self.run_id)
            or not isinstance(self.artifacts, tuple)
            or not all(isinstance(item, Artifact) for item in self.artifacts)
            or len({item.sha256 for item in self.artifacts}) != len(self.artifacts)
            or not isinstance(self.stages, tuple)
            or not all(isinstance(item, StageHandle) for item in self.stages)
            or any(item.run_id != self.run_id for item in self.stages)
            or type(self.protocol_version) is not int
            or self.protocol_version != WORLD_PROTOCOL_VERSION
        ):
            raise ValueError("invalid run cleanup plan")

    def __repr__(self) -> str:
        return (
            f"RunCleanupPlan(run_id={self.run_id!r}, artifacts={len(self.artifacts)}, "
            f"stages={len(self.stages)}, protocol_version={self.protocol_version})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class DeletionPlan:
    deletion_id: str
    artifacts: tuple[DeletionArtifact, ...]
    stages: tuple[StageHandle, ...]
    run_ids: tuple[str, ...]
    record_count: int
    artifact_count: int
    shared_retention_count: int
    protocol_version: int = WORLD_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.deletion_id) is not str
            or not _DELETION_ID.fullmatch(self.deletion_id)
            or not isinstance(self.artifacts, tuple)
            or not all(isinstance(item, DeletionArtifact) for item in self.artifacts)
            or len({item.artifact.sha256 for item in self.artifacts}) != len(self.artifacts)
            or not isinstance(self.stages, tuple)
            or not all(isinstance(item, StageHandle) for item in self.stages)
            or not isinstance(self.run_ids, tuple)
            or not all(type(item) is str and _RUN_ID.fullmatch(item) for item in self.run_ids)
            or len(set(self.run_ids)) != len(self.run_ids)
            or any(item.run_id not in self.run_ids for item in self.stages)
            or any(
                type(value) is not int or value < 0
                for value in (
                    self.record_count,
                    self.artifact_count,
                    self.shared_retention_count,
                )
            )
            or self.artifact_count != sum(item.delete_required for item in self.artifacts)
            or self.shared_retention_count
            != sum(not item.delete_required for item in self.artifacts)
            or type(self.protocol_version) is not int
            or self.protocol_version != WORLD_PROTOCOL_VERSION
        ):
            raise ValueError("invalid deletion plan")

    def __repr__(self) -> str:
        return (
            f"DeletionPlan(deletion_id={self.deletion_id!r}, records={self.record_count}, "
            f"artifacts={self.artifact_count}, shared={self.shared_retention_count}, "
            f"runs={len(self.run_ids)}, protocol_version={self.protocol_version})"
        )


@dataclass(frozen=True, slots=True)
class DeletionStatus:
    deletion_id: str
    state: DeletionState
    record_count: int
    artifact_count: int
    shared_retention_count: int
    completed_at_utc: str | None
    protocol_version: int = WORLD_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.deletion_id) is not str
            or not _DELETION_ID.fullmatch(self.deletion_id)
            or not isinstance(self.state, DeletionState)
            or any(
                type(value) is not int or value < 0
                for value in (
                    self.record_count,
                    self.artifact_count,
                    self.shared_retention_count,
                )
            )
            or (
                self.state is DeletionState.COMPLETE
                and (type(self.completed_at_utc) is not str or not self.completed_at_utc)
            )
            or (self.state is not DeletionState.COMPLETE and self.completed_at_utc is not None)
            or type(self.protocol_version) is not int
            or self.protocol_version != WORLD_PROTOCOL_VERSION
        ):
            raise ValueError("invalid deletion status")


class LocalWorldStore:
    """SQLite/WAL implementation of the version-1 WorldStore port."""

    def __init__(
        self,
        root: Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
        max_audit_records: int = DEFAULT_MAX_AUDIT_RECORDS,
    ) -> None:
        if (
            not isinstance(root, Path)
            or not root.is_absolute()
            or type(busy_timeout_ms) is not int
            or not 1 <= busy_timeout_ms <= 60_000
            or type(lock_timeout_ms) is not int
            or not 1 <= lock_timeout_ms <= 60_000
            or type(max_audit_records) is not int
            or not 1 <= max_audit_records <= 1_000_000
        ):
            _fail(PortErrorCode.INVALID_REQUEST, "init")
        self._root = root
        self._database = root / _DATABASE_NAME
        self._busy_timeout_ms = busy_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms
        self._max_audit_records = max_audit_records
        self._poisoned = False
        self._retained_lock_streams: list[_filesystem.BinaryFile] = []
        self._descriptor = CapabilityDescriptor(
            PortKind.WORLD_STORE,
            "local-sqlite",
            "2",
            True,
            True,
        )
        self._initialize()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    def _migration_statements(self, version: int) -> tuple[str, ...]:
        if version == 1:
            return _SCHEMA_V1
        if version == 2:
            return _v2_schema.SCHEMA_V2
        raise ValueError("unsupported migration")

    @contextmanager
    def _root_descriptor(self, operation: str, *, create: bool) -> Iterator[int]:
        try:
            descriptor = _filesystem._open_root(self._root, operation, create=create)
        except PortError as error:
            _translate_filesystem_error(error, operation)
        try:
            yield descriptor
        finally:
            with suppress(OSError):
                os.close(descriptor)

    @contextmanager
    def _writer_lock(
        self,
        root_descriptor: int,
        operation: str,
        *,
        create: bool = False,
    ) -> Iterator[None]:
        if self._poisoned:
            _fail(PortErrorCode.STORAGE_FAILED, operation)
        try:
            with _filesystem._locked(
                root_descriptor,
                operation,
                exclusive=True,
                timeout_ms=self._lock_timeout_ms,
                poison=self._poison_lock,
                create=create,
            ):
                yield
        except PortError as error:
            _translate_filesystem_error(error, operation)

    def _poison_lock(self, stream: _filesystem.BinaryFile) -> None:
        self._poisoned = True
        self._retained_lock_streams.append(stream)

    def _validate_database_files(self, root_descriptor: int, operation: str) -> None:
        for name in _UNEXPECTED_DATABASE_FILES:
            try:
                os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                _fail(PortErrorCode.STORAGE_FAILED, operation)
            _fail(PortErrorCode.CORRUPT, operation)
        for name in (_DATABASE_NAME, *_DATABASE_COMPANIONS):
            try:
                metadata = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if name == _DATABASE_NAME:
                    _fail(PortErrorCode.CORRUPT, operation)
                continue
            except OSError:
                _fail(PortErrorCode.STORAGE_FAILED, operation)
            if not _filesystem._regular_metadata_valid(metadata, 0o600):
                _fail(PortErrorCode.CORRUPT, operation)

    def _prepare_database_file(self, root_descriptor: int, operation: str) -> None:
        stream: _filesystem.BinaryFile | None = None
        try:
            try:
                stream = _filesystem._open_file(root_descriptor, _DATABASE_NAME, "x+b", 0o600)
                os.fchmod(stream.fileno(), 0o600)
                os.fsync(stream.fileno())
                stream.close()
                os.fsync(root_descriptor)
            except FileExistsError:
                pass
            except OSError:
                _fail(PortErrorCode.UNSUPPORTED, operation)
        finally:
            if stream is not None and not stream.closed:
                with suppress(OSError):
                    stream.close()
        self._validate_database_files(root_descriptor, operation)

    def _configure_connection(
        self,
        connection: sqlite3.Connection,
        operation: str,
        *,
        initialize: bool,
        read_only: bool,
    ) -> None:
        try:
            connection.enable_load_extension(False)
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION, False)
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_WRITABLE_SCHEMA, False)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA recursive_triggers = OFF")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            if initialize:
                selected = connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if selected is None or str(selected[0]).lower() != "wal":
                    _fail(PortErrorCode.UNSUPPORTED, operation)
            journal = connection.execute("PRAGMA journal_mode").fetchone()
            settings = (
                connection.execute("PRAGMA foreign_keys").fetchone(),
                connection.execute("PRAGMA synchronous").fetchone(),
                connection.execute("PRAGMA secure_delete").fetchone(),
                connection.execute("PRAGMA trusted_schema").fetchone(),
                connection.execute("PRAGMA recursive_triggers").fetchone(),
                connection.execute("PRAGMA busy_timeout").fetchone(),
            )
            if (
                journal is None
                or str(journal[0]).lower() != "wal"
                or any(row is None for row in settings)
                or tuple(cast(tuple[object, ...], row)[0] for row in settings)
                != (1, 2, 1, 0, 0, self._busy_timeout_ms)
            ):
                _fail(PortErrorCode.CORRUPT, operation)
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
            if read_only:
                connection.execute("PRAGMA query_only = ON")
        except PortError:
            raise
        except sqlite3.Error as error:
            _sqlite_error(error, operation)

    @staticmethod
    def _authorizer(
        action: int,
        argument_one: str | None,
        argument_two: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        del database, trigger
        denied = {
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_CREATE_VTABLE,
            sqlite3.SQLITE_DROP_VTABLE,
        }
        if action in denied or (
            action == sqlite3.SQLITE_FUNCTION
            and isinstance(argument_one or argument_two, str)
            and cast(str, argument_one or argument_two).lower() == "load_extension"
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextmanager
    def _connection(
        self,
        root_descriptor: int,
        operation: str,
        *,
        initialize: bool = False,
        read_only: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        self._validate_database_files(root_descriptor, operation)
        connection: sqlite3.Connection | None = None
        target = self._database.as_uri() + ("?mode=ro" if read_only else "?mode=rw")
        try:
            connection = sqlite3.connect(
                target,
                uri=True,
                timeout=self._busy_timeout_ms / 1_000,
                autocommit=True,
            )
            self._configure_connection(
                connection,
                operation,
                initialize=initialize,
                read_only=read_only,
            )
            self._validate_database_files(root_descriptor, operation)
            yield connection
        except PortError:
            raise
        except sqlite3.Error as error:
            _sqlite_error(error, operation)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error as error:
                    _sqlite_error(error, operation)

    def _initialize(self) -> None:
        operation = "init"
        with (
            self._root_descriptor(operation, create=True) as root_descriptor,
            self._writer_lock(root_descriptor, operation, create=True),
        ):
            self._prepare_database_file(root_descriptor, operation)
            with self._connection(
                root_descriptor,
                operation,
                initialize=True,
            ) as connection:
                self._migrate_or_validate(connection, operation)
                try:
                    os.fsync(root_descriptor)
                except OSError:
                    _fail(PortErrorCode.UNSUPPORTED, operation)

    def _migrate_or_validate(
        self,
        connection: sqlite3.Connection,
        operation: str,
    ) -> None:
        try:
            user_version_row = connection.execute("PRAGMA user_version").fetchone()
            if user_version_row is None or type(user_version_row[0]) is not int:
                _fail(PortErrorCode.CORRUPT, operation)
            user_version = user_version_row[0]
            objects = connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            ).fetchone()
            if objects is None or type(objects[0]) is not int:
                _fail(PortErrorCode.CORRUPT, operation)
            if user_version > WORLD_SCHEMA_VERSION:
                _fail(PortErrorCode.UNSUPPORTED, operation)
            if user_version == 0:
                if objects[0] != 0:
                    _fail(PortErrorCode.CORRUPT, operation)
                self._apply_v1_migration(connection, operation)
                user_version = 1
            if user_version == 1:
                self._validate_schema_version(connection, operation, version=1)
                self._apply_v2_migration(connection, operation)
            elif user_version != WORLD_SCHEMA_VERSION:
                _fail(PortErrorCode.CORRUPT, operation)
            self._validate_schema(connection, operation)
        except PortError:
            raise
        except (OSError, sqlite3.Error) as error:
            if isinstance(error, sqlite3.Error):
                _sqlite_error(error, operation)
            _fail(PortErrorCode.STORAGE_FAILED, operation)

    def _apply_v1_migration(self, connection: sqlite3.Connection, operation: str) -> None:
        began = False
        try:
            connection.execute("BEGIN EXCLUSIVE")
            began = True
            for statement in self._migration_statements(1):
                connection.execute(statement)
            connection.execute(
                """INSERT INTO schema_migrations(
                    schema_version, migration_name, code_sha256, applied_at_utc
                ) VALUES (?, ?, ?, ?)""",
                (
                    1,
                    _MIGRATION_V1_NAME,
                    _MIGRATION_V1_CHECKSUM,
                    datetime.now(UTC).isoformat(timespec="microseconds"),
                ),
            )
            connection.execute("PRAGMA user_version = 1")
            connection.execute("COMMIT")
            began = False
        except (PortError, sqlite3.Error):
            if began:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
            raise

    def _apply_v2_migration(self, connection: sqlite3.Connection, operation: str) -> None:
        """Apply the additive v2 graph without rewriting v1 rows or pending markers."""

        began = False
        try:
            connection.execute("BEGIN EXCLUSIVE")
            began = True
            for statement in self._migration_statements(2):
                self._execute_v2_migration_step(connection, statement)
            self._execute_v2_migration_step(
                connection,
                """INSERT INTO schema_migrations(
                    schema_version, migration_name, code_sha256, applied_at_utc
                ) VALUES (?, ?, ?, ?)""",
                (
                    2,
                    _v2_schema.MIGRATION_V2_NAME,
                    _v2_schema.MIGRATION_V2_CHECKSUM,
                    datetime.now(UTC).isoformat(timespec="microseconds"),
                ),
            )
            self._execute_v2_migration_step(connection, "PRAGMA user_version = 2")
            self._execute_v2_migration_step(connection, "COMMIT")
            began = False
        except (PortError, sqlite3.Error):
            if began:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
            raise

    def _execute_v2_migration_step(
        self,
        connection: sqlite3.Connection,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor:
        return connection.execute(statement, parameters)

    def _validate_schema(self, connection: sqlite3.Connection, operation: str) -> None:
        self._validate_schema_version(connection, operation, version=WORLD_SCHEMA_VERSION)

    def _validate_schema_version(
        self,
        connection: sqlite3.Connection,
        operation: str,
        *,
        version: int,
    ) -> None:
        if version == 1:
            statements = _SCHEMA_V1
            expected_ledger = [(1, _MIGRATION_V1_NAME, _MIGRATION_V1_CHECKSUM)]
        elif version == WORLD_SCHEMA_VERSION:
            statements = (*_SCHEMA_V1, *_v2_schema.SCHEMA_V2)
            expected_ledger = [
                (1, _MIGRATION_V1_NAME, _MIGRATION_V1_CHECKSUM),
                (2, _v2_schema.MIGRATION_V2_NAME, _v2_schema.MIGRATION_V2_CHECKSUM),
            ]
        else:
            raise AssertionError("unsupported schema validation version")
        expected: dict[tuple[str, str], str] = {}
        for statement in statements:
            match = re.match(r"CREATE (TABLE|INDEX) ([a-z_]+)", statement)
            if match is None:
                raise AssertionError("unrecognized schema statement")
            expected[(match.group(1).lower(), match.group(2))] = statement
        try:
            actual_rows = connection.execute(
                """SELECT type, name, sql FROM sqlite_schema
                WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"""
            ).fetchall()
            actual = {
                (str(row[0]), str(row[1])): str(row[2]) for row in actual_rows if row[2] is not None
            }
            if actual != expected:
                _fail(PortErrorCode.CORRUPT, operation)
            ledger = connection.execute(
                """SELECT schema_version, migration_name, code_sha256
                FROM schema_migrations ORDER BY schema_version"""
            ).fetchall()
            if ledger != expected_ledger:
                _fail(PortErrorCode.CORRUPT, operation)
            user_version = connection.execute("PRAGMA user_version").fetchone()
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if user_version != (version,) or quick_check != ("ok",) or foreign_keys:
                _fail(PortErrorCode.CORRUPT, operation)
            connection.set_authorizer(self._authorizer)
        except PortError:
            raise
        except sqlite3.Error as error:
            _sqlite_error(error, operation)

    @contextmanager
    def _read_connection(self, operation: str) -> Iterator[sqlite3.Connection]:
        with (
            self._root_descriptor(operation, create=False) as root_descriptor,
            self._connection(
                root_descriptor,
                operation,
                read_only=True,
            ) as connection,
        ):
            self._validate_schema(connection, operation)
            began = False
            try:
                connection.execute("BEGIN")
                began = True
                yield connection
                connection.execute("COMMIT")
                began = False
            finally:
                if began:
                    with suppress(sqlite3.Error):
                        connection.execute("ROLLBACK")

    @contextmanager
    def _write_connection(self, operation: str) -> Iterator[sqlite3.Connection]:
        with (
            self._root_descriptor(operation, create=False) as root_descriptor,
            self._writer_lock(root_descriptor, operation),
            self._connection(root_descriptor, operation) as connection,
        ):
            self._validate_schema(connection, operation)
            yield connection

    def _evidence_session_descriptor(
        self,
        session: object,
        operation: str,
    ) -> int:
        if (
            type(session) is not EvidenceWriterSession
            or type(session._store) is not LocalEvidenceStore
            or session._store.root != self._root
        ):
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        try:
            descriptor = session._descriptor()
        except PortError:
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        try:
            with self._root_descriptor(operation, create=False) as root_descriptor:
                expected = os.fstat(root_descriptor)
                actual = os.fstat(descriptor)
        except OSError:
            _fail(PortErrorCode.STORAGE_FAILED, operation)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        return descriptor

    @contextmanager
    def _coordinated_write_connection(
        self,
        operation: str,
        evidence_session: EvidenceWriterSession | None,
    ) -> Iterator[sqlite3.Connection]:
        if evidence_session is None:
            with self._write_connection(operation) as connection:
                yield connection
            return
        root_descriptor = self._evidence_session_descriptor(evidence_session, operation)
        with self._connection(root_descriptor, operation) as connection:
            self._validate_schema(connection, operation)
            yield connection

    @contextmanager
    def _transaction(
        self,
        connection: sqlite3.Connection,
        operation: str,
    ) -> Iterator[None]:
        began = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            began = True
            yield
            connection.execute("COMMIT")
            began = False
        except BaseException:
            if began:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
            raise

    def commit(
        self,
        records: tuple[Record, ...],
        *,
        evidence_session: EvidenceWriterSession | None = None,
    ) -> None:
        operation = "commit"
        selected = self._select_records(records, operation)
        with self._coordinated_write_connection(operation, evidence_session) as connection:
            try:
                with self._transaction(connection, operation):
                    self._guard_committed_retries(connection, selected, operation)
                    self._write_records(connection, selected, operation)
                    runs = [record for record, _ in selected if isinstance(record, RunManifest)]
                    data = [
                        record
                        for record, _ in selected
                        if isinstance(record, (FrameRef, EvidenceRef))
                    ]
                    if data and len(runs) == 1:
                        self._associate_records(connection, runs[0].run_id, data, operation)
                    elif data and len(runs) > 1:
                        _fail(PortErrorCode.INVALID_REQUEST, operation)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)

    def _guard_committed_retries(
        self,
        connection: sqlite3.Connection,
        records: tuple[tuple[Record, bytes], ...],
        operation: str,
    ) -> None:
        data = [
            (record, encoded)
            for record, encoded in records
            if isinstance(record, (FrameRef, EvidenceRef))
        ]
        for run, encoded in records:
            if not isinstance(run, RunManifest) or run.state != "committed":
                continue
            existing = connection.execute(
                "SELECT state, record_json FROM runs WHERE run_id = ?",
                (run.run_id,),
            ).fetchone()
            if existing != ("committed", encoded):
                _fail(PortErrorCode.CONFLICT, operation)
            for record, record_json in data:
                if isinstance(record, FrameRef):
                    table, column, identifier, record_type = (
                        "frames",
                        "frame_id",
                        record.frame_id,
                        "frame",
                    )
                else:
                    table, column, identifier, record_type = (
                        "evidence",
                        "evidence_id",
                        record.evidence_id,
                        "evidence",
                    )
                if self._existing_json(connection, table, column, identifier) != record_json:
                    _fail(PortErrorCode.CONFLICT, operation)
                ownership = connection.execute(
                    """SELECT record_type FROM run_records
                    WHERE run_id = ? AND record_id = ?""",
                    (run.run_id, identifier),
                ).fetchone()
                if ownership != (record_type,):
                    _fail(PortErrorCode.CONFLICT, operation)

    def _select_records(
        self,
        records: object,
        operation: str,
    ) -> tuple[tuple[Record, bytes], ...]:
        if type(records) is not tuple or len(records) > MAX_PORT_BATCH_ITEMS:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        selected = tuple(_canonical_record(record, operation) for record in records)
        by_identifier: dict[str, bytes] = {}
        for record, encoded in selected:
            identifier = _record_identifier(record)
            existing = by_identifier.get(identifier)
            if existing is not None and existing != encoded:
                _fail(PortErrorCode.CONFLICT, operation)
            by_identifier[identifier] = encoded
        return selected

    def _write_records(
        self,
        connection: sqlite3.Connection,
        records: tuple[tuple[Record, bytes], ...],
        operation: str,
    ) -> None:
        source_ids = {
            record.source_id
            for record, _ in records
            if isinstance(record, (Source, FrameRef, RunManifest))
        }
        for record, _ in records:
            if not isinstance(record, EvidenceRef):
                continue
            row = connection.execute(
                "SELECT source_id FROM frames WHERE frame_id = ?",
                (record.frame_id,),
            ).fetchone()
            if row is not None:
                if type(row[0]) is not str or not _SOURCE_ID.fullmatch(row[0]):
                    _fail(PortErrorCode.CORRUPT, operation)
                source_ids.add(row[0])
        self._guard_sources_writable(connection, source_ids, operation)
        order = (Source, RunManifest, FrameRef, EvidenceRef)
        for record_type in order:
            for record, encoded in records:
                if isinstance(record, record_type):
                    self._write_record(connection, record, encoded, operation)

    @staticmethod
    def _guard_sources_writable(
        connection: sqlite3.Connection,
        source_ids: set[str],
        operation: str,
    ) -> None:
        for source_id in source_ids:
            pending = connection.execute(
                """SELECT 1 FROM deletion_jobs
                WHERE state = 'metadata_purged'
                   OR (root_kind = 'source' AND root_id = ? AND state = 'pending')
                LIMIT 1""",
                (source_id,),
            ).fetchone()
            if pending is not None:
                _fail(PortErrorCode.CONFLICT, operation)

    def _guard_run_writable(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        operation: str,
        *,
        missing_code: PortErrorCode = PortErrorCode.CONFLICT,
    ) -> None:
        row = connection.execute(
            "SELECT source_id FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            _fail(missing_code, operation)
        if type(row[0]) is not str or not _SOURCE_ID.fullmatch(row[0]):
            _fail(PortErrorCode.CORRUPT, operation)
        self._guard_sources_writable(connection, {row[0]}, operation)

    def _write_record(
        self,
        connection: sqlite3.Connection,
        record: Record,
        encoded: bytes,
        operation: str,
    ) -> None:
        digest = hashlib.sha256(encoded).hexdigest()
        if isinstance(record, Source):
            self._write_source(connection, record, encoded, digest, operation)
        elif isinstance(record, FrameRef):
            self._write_frame(connection, record, encoded, digest, operation)
        elif isinstance(record, EvidenceRef):
            self._write_evidence(connection, record, encoded, digest, operation)
        else:
            self._write_run(connection, record, encoded, digest, operation)

    @staticmethod
    def _existing_json(
        connection: sqlite3.Connection,
        table: str,
        identifier_name: str,
        identifier: str,
    ) -> bytes | None:
        statements = {
            ("sources", "source_id"): "SELECT record_json FROM sources WHERE source_id = ?",
            ("frames", "frame_id"): "SELECT record_json FROM frames WHERE frame_id = ?",
            ("evidence", "evidence_id"): ("SELECT record_json FROM evidence WHERE evidence_id = ?"),
            ("runs", "run_id"): "SELECT record_json FROM runs WHERE run_id = ?",
        }
        statement = statements[(table, identifier_name)]
        row = connection.execute(statement, (identifier,)).fetchone()
        if row is None:
            return None
        if type(row[0]) is not bytes:
            return b""
        return row[0]

    def _write_source(
        self,
        connection: sqlite3.Connection,
        record: Source,
        encoded: bytes,
        digest: str,
        operation: str,
    ) -> None:
        existing = self._existing_json(connection, "sources", "source_id", record.source_id)
        if existing is not None:
            if existing != encoded:
                _fail(PortErrorCode.CONFLICT, operation)
            self._verify_projection(connection, record, operation)
            return
        connection.execute(
            """INSERT INTO sources(
                source_id, schema_version, identity_version, record_json, record_sha256
            ) VALUES (?, ?, ?, ?, ?)""",
            (record.source_id, record.schema_version, record.identity_version, encoded, digest),
        )
        connection.executemany(
            """INSERT INTO source_streams(
                source_id, stream_index, width, height, rotation_degrees,
                time_base_numerator, time_base_denominator
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            tuple(
                (
                    record.source_id,
                    stream.stream_index,
                    stream.width,
                    stream.height,
                    stream.rotation_degrees,
                    stream.time_base.numerator,
                    stream.time_base.denominator,
                )
                for stream in record.streams
            ),
        )

    def _write_frame(
        self,
        connection: sqlite3.Connection,
        record: FrameRef,
        encoded: bytes,
        digest: str,
        operation: str,
    ) -> None:
        existing = self._existing_json(connection, "frames", "frame_id", record.frame_id)
        if existing is not None:
            if existing != encoded:
                _fail(PortErrorCode.CONFLICT, operation)
            self._verify_projection(connection, record, operation)
            return
        connection.execute(
            """INSERT INTO frames(
                frame_id, source_id, stream_index, decode_index, pts_value,
                pts_time_base_numerator, pts_time_base_denominator,
                schema_version, identity_version, record_json, record_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.frame_id,
                record.source_id,
                record.stream_index,
                record.decode_index,
                record.pts.value,
                record.pts.time_base.numerator,
                record.pts.time_base.denominator,
                record.schema_version,
                record.identity_version,
                encoded,
                digest,
            ),
        )

    def _write_evidence(
        self,
        connection: sqlite3.Connection,
        record: EvidenceRef,
        encoded: bytes,
        digest: str,
        operation: str,
    ) -> None:
        artifact = record.artifact
        catalog = connection.execute(
            "SELECT byte_count, media_type, layout_version FROM artifact_catalog WHERE digest = ?",
            (artifact.sha256,),
        ).fetchone()
        expected_catalog = (artifact.bytes, artifact.media_type, WORLD_PROTOCOL_VERSION)
        if catalog is None:
            connection.execute(
                """INSERT INTO artifact_catalog(
                    digest, byte_count, media_type, layout_version
                ) VALUES (?, ?, ?, ?)""",
                (artifact.sha256, *expected_catalog),
            )
        elif catalog != expected_catalog:
            _fail(PortErrorCode.CONFLICT, operation)
        existing = self._existing_json(
            connection,
            "evidence",
            "evidence_id",
            record.evidence_id,
        )
        if existing is not None:
            if existing != encoded:
                _fail(PortErrorCode.CONFLICT, operation)
            self._verify_projection(connection, record, operation)
            return
        connection.execute(
            """INSERT INTO evidence(
                evidence_id, frame_id, artifact_digest, schema_version,
                identity_version, record_json, record_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                record.evidence_id,
                record.frame_id,
                artifact.sha256,
                record.schema_version,
                record.identity_version,
                encoded,
                digest,
            ),
        )
        connection.execute(
            """INSERT INTO artifact_references(evidence_id, artifact_digest)
            VALUES (?, ?)""",
            (record.evidence_id, artifact.sha256),
        )

    def _write_run(
        self,
        connection: sqlite3.Connection,
        record: RunManifest,
        encoded: bytes,
        digest: str,
        operation: str,
    ) -> None:
        existing_row = connection.execute(
            "SELECT state, record_json FROM runs WHERE run_id = ?",
            (record.run_id,),
        ).fetchone()
        if existing_row is None:
            connection.execute(
                """INSERT INTO runs(
                    run_id, source_id, state, schema_version, identity_version,
                    record_json, record_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.run_id,
                    record.source_id,
                    record.state,
                    record.schema_version,
                    record.identity_version,
                    encoded,
                    digest,
                ),
            )
            return
        existing_state, existing_json = existing_row
        if type(existing_json) is not bytes:
            _fail(PortErrorCode.CORRUPT, operation)
        existing_record = self._decode_record(existing_json, operation)
        if not isinstance(existing_record, RunManifest):
            _fail(PortErrorCode.CORRUPT, operation)
        self._verify_projection(connection, existing_record, operation)
        if existing_json == encoded:
            return
        transitions = {
            "preparing": frozenset({"committed", "failed", "cancelled"}),
            "failed": frozenset({"preparing"}),
            "cancelled": frozenset({"preparing"}),
            "committed": frozenset(),
        }
        if type(existing_state) is not str or record.state not in transitions.get(
            existing_state,
            frozenset(),
        ):
            _fail(PortErrorCode.CONFLICT, operation)
        if (
            existing_record.run_id != record.run_id
            or existing_record.source_id != record.source_id
            or existing_record.identity_projection() != record.identity_projection()
        ):
            _fail(PortErrorCode.CORRUPT, operation)
        connection.execute(
            """UPDATE runs SET state = ?, record_json = ?, record_sha256 = ?
            WHERE run_id = ?""",
            (record.state, encoded, digest, record.run_id),
        )

    def _associate_records(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        records: list[FrameRef | EvidenceRef],
        operation: str,
    ) -> None:
        run = connection.execute(
            "SELECT source_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            _fail(PortErrorCode.CONFLICT, operation)
        run_source_id = run[0]
        for record in sorted(records, key=lambda item: isinstance(item, EvidenceRef)):
            if isinstance(record, FrameRef):
                if record.source_id != run_source_id:
                    _fail(PortErrorCode.CONFLICT, operation)
            else:
                frame = connection.execute(
                    "SELECT source_id FROM frames WHERE frame_id = ?",
                    (record.frame_id,),
                ).fetchone()
                frame_ownership = connection.execute(
                    """SELECT record_type FROM run_records
                    WHERE run_id = ? AND record_id = ?""",
                    (run_id, record.frame_id),
                ).fetchone()
                if frame != (run_source_id,) or frame_ownership != ("frame",):
                    _fail(PortErrorCode.CONFLICT, operation)
            identifier = record.frame_id if isinstance(record, FrameRef) else record.evidence_id
            record_type = "frame" if isinstance(record, FrameRef) else "evidence"
            existing = connection.execute(
                "SELECT record_type FROM run_records WHERE run_id = ? AND record_id = ?",
                (run_id, identifier),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO run_records(run_id, record_id, record_type) VALUES (?, ?, ?)",
                    (run_id, identifier, record_type),
                )
            elif existing != (record_type,):
                _fail(PortErrorCode.CORRUPT, operation)

    def commit_for_run(
        self,
        run_id: str,
        records: tuple[Record, ...],
        *,
        evidence_session: EvidenceWriterSession | None = None,
    ) -> None:
        """Commit one hidden metadata batch for a preparing run."""

        operation = "commit_for_run"
        selected_run = _identifier(run_id, _RUN_ID, operation)
        selected = self._select_records(records, operation)
        if any(isinstance(record, (Source, RunManifest)) for record, _ in selected):
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        with self._coordinated_write_connection(operation, evidence_session) as connection:
            try:
                with self._transaction(connection, operation):
                    self._guard_run_writable(connection, selected_run, operation)
                    state = connection.execute(
                        "SELECT state FROM runs WHERE run_id = ?",
                        (selected_run,),
                    ).fetchone()
                    if state != ("preparing",):
                        _fail(PortErrorCode.CONFLICT, operation)
                    self._write_records(connection, selected, operation)
                    self._associate_records(
                        connection,
                        selected_run,
                        [cast(FrameRef | EvidenceRef, record) for record, _ in selected],
                        operation,
                    )
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)

    def record_artifact_intents(
        self,
        run_id: str,
        stages: tuple[StageHandle, ...],
        *,
        evidence_session: EvidenceWriterSession | None = None,
    ) -> None:
        operation = "record_artifact_intents"
        selected_run = _identifier(run_id, _RUN_ID, operation)
        if type(stages) is not tuple or len(stages) > MAX_PORT_BATCH_ITEMS:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        selected = tuple(_canonical_stage(stage, operation) for stage in stages)
        if any(stage.run_id != selected_run for stage in selected):
            _fail(PortErrorCode.CONFLICT, operation)
        with self._coordinated_write_connection(operation, evidence_session) as connection:
            try:
                with self._transaction(connection, operation):
                    self._guard_run_writable(connection, selected_run, operation)
                    state = connection.execute(
                        "SELECT state FROM runs WHERE run_id = ?",
                        (selected_run,),
                    ).fetchone()
                    if state != ("preparing",):
                        _fail(PortErrorCode.CONFLICT, operation)
                    for stage in selected:
                        expected = (
                            stage.artifact.sha256,
                            stage.artifact.bytes,
                            stage.artifact.media_type,
                            stage.protocol_version,
                        )
                        existing = connection.execute(
                            """SELECT artifact_digest, byte_count, media_type, protocol_version
                            FROM artifact_intents WHERE run_id = ? AND staging_name = ?""",
                            (selected_run, stage.staging_name),
                        ).fetchone()
                        if existing is None:
                            connection.execute(
                                """INSERT INTO artifact_intents(
                                    run_id, staging_name, artifact_digest, byte_count,
                                    media_type, protocol_version
                                ) VALUES (?, ?, ?, ?, ?, ?)""",
                                (
                                    selected_run,
                                    stage.staging_name,
                                    *expected,
                                ),
                            )
                        elif existing != expected:
                            _fail(PortErrorCode.CONFLICT, operation)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)

    def list_artifact_intents(
        self,
        run_id: str,
        *,
        after_staging_name: str | None = None,
        limit: int = MAX_PORT_BATCH_ITEMS,
    ) -> tuple[StageHandle, ...]:
        operation = "list_artifact_intents"
        selected_run = _identifier(run_id, _RUN_ID, operation)
        selected_limit = _bounded_limit(limit, operation)
        after = ""
        if after_staging_name is not None:
            after = _identifier(after_staging_name, _STAGING_NAME, operation)
        with self._read_connection(operation) as connection:
            try:
                hidden = connection.execute(
                    "SELECT 1 FROM deletion_closure WHERE record_id = ? LIMIT 1",
                    (selected_run,),
                ).fetchone()
                if hidden is not None:
                    _fail(PortErrorCode.NOT_FOUND, operation)
                rows = connection.execute(
                    """SELECT staging_name, artifact_digest, byte_count,
                    media_type, protocol_version FROM artifact_intents
                    WHERE run_id = ? AND staging_name > ?
                    ORDER BY staging_name LIMIT ?""",
                    (selected_run, after, selected_limit),
                ).fetchall()
                try:
                    return tuple(
                        StageHandle(
                            selected_run,
                            row[0],
                            Artifact(row[1], row[2], row[3]),
                            row[4],
                        )
                        for row in rows
                    )
                except (TypeError, ValueError):
                    _fail(PortErrorCode.CORRUPT, operation)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def _select_perception_records(
        self,
        records: object,
        operation: str,
    ) -> tuple[tuple[PerceptionRecord, bytes], ...]:
        if type(records) is not tuple or len(records) > MAX_PORT_BATCH_ITEMS:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        selected = tuple(_canonical_perception_record(record, operation) for record in records)
        by_identifier: dict[str, bytes] = {}
        for record, encoded in selected:
            identifier = (
                record.observation_id if isinstance(record, Observation) else record.tracklet_id
            )
            existing = by_identifier.get(identifier)
            if existing is not None and existing != encoded:
                _fail(PortErrorCode.CONFLICT, operation)
            by_identifier[identifier] = encoded
        return selected

    @staticmethod
    def _existing_perception_json(
        connection: sqlite3.Connection,
        record: PerceptionRecord,
    ) -> bytes | None:
        if isinstance(record, Observation):
            row = connection.execute(
                "SELECT record_json FROM observations WHERE observation_id = ?",
                (record.observation_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT record_json FROM tracklets WHERE tracklet_id = ?",
                (record.tracklet_id,),
            ).fetchone()
        if row is None:
            return None
        return row[0] if type(row[0]) is bytes else b""

    def _validate_observation_frame(
        self,
        connection: sqlite3.Connection,
        record: Observation,
        operation: str,
        *,
        error_code: PortErrorCode = PortErrorCode.CONFLICT,
    ) -> None:
        row = connection.execute(
            """SELECT frame.source_id, frame.stream_index, frame.pts_value,
            frame.pts_time_base_numerator, frame.pts_time_base_denominator,
            stream.width, stream.height
            FROM frames AS frame
            JOIN source_streams AS stream
              ON stream.source_id = frame.source_id
             AND stream.stream_index = frame.stream_index
            WHERE frame.frame_id = ?""",
            (record.frame_id,),
        ).fetchone()
        if row is None:
            _fail(error_code, operation)
        expected = (
            record.source_id,
            record.stream_index,
            record.pts.value,
            record.pts.time_base.numerator,
            record.pts.time_base.denominator,
            record.geometry.source_width,
            record.geometry.source_height,
        )
        if row != expected:
            _fail(error_code, operation)

    def _write_observation(
        self,
        connection: sqlite3.Connection,
        record: Observation,
        encoded: bytes,
        operation: str,
    ) -> None:
        self._validate_observation_frame(connection, record, operation)
        existing = self._existing_perception_json(connection, record)
        if existing is not None:
            if existing != encoded:
                _fail(PortErrorCode.CONFLICT, operation)
            self._verify_perception_projection(connection, record, operation)
            return
        connection.execute(
            """INSERT INTO observations(
                observation_id, source_id, frame_id, stream_index, pts_value,
                pts_order, pts_time_base_numerator, pts_time_base_denominator,
                category, confidence_millionths, producer_name, producer_version,
                producer_configuration_sha256, schema_version, identity_version,
                record_json, record_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.observation_id,
                record.source_id,
                record.frame_id,
                record.stream_index,
                record.pts.value,
                int(record.pts.value),
                record.pts.time_base.numerator,
                record.pts.time_base.denominator,
                record.category,
                record.confidence_millionths,
                record.producer.name,
                record.producer.version,
                record.producer.configuration_sha256,
                record.schema_version,
                record.identity_version,
                encoded,
                hashlib.sha256(encoded).hexdigest(),
            ),
        )

    def _write_tracklet(
        self,
        connection: sqlite3.Connection,
        record: Tracklet,
        encoded: bytes,
        operation: str,
    ) -> None:
        existing = self._existing_perception_json(connection, record)
        if existing is not None:
            if existing != encoded:
                _fail(PortErrorCode.CONFLICT, operation)
            self._verify_perception_projection(connection, record, operation)
            return
        stream = connection.execute(
            """SELECT 1 FROM source_streams
            WHERE source_id = ? AND stream_index = ?""",
            (record.source_id, record.stream_index),
        ).fetchone()
        if stream is None:
            _fail(PortErrorCode.CONFLICT, operation)
        for point in record.points:
            row = connection.execute(
                "SELECT record_json FROM observations WHERE observation_id = ?",
                (point.observation_id,),
            ).fetchone()
            if row is None:
                _fail(PortErrorCode.CONFLICT, operation)
            observation = self._decode_perception_record(row[0], operation)
            if not isinstance(observation, Observation):
                _fail(PortErrorCode.CORRUPT, operation)
            self._verify_perception_projection(connection, observation, operation)
            if point != TrackPoint.from_observation(observation):
                _fail(PortErrorCode.CONFLICT, operation)
        connection.execute(
            """INSERT INTO tracklets(
                tracklet_id, source_id, stream_index, category, termination_reason,
                producer_name, producer_version, producer_configuration_sha256,
                start_pts_value, start_pts_order, start_pts_time_base_numerator,
                start_pts_time_base_denominator, end_pts_value, end_pts_order,
                end_pts_time_base_numerator, end_pts_time_base_denominator,
                point_count, schema_version, identity_version, record_json, record_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.tracklet_id,
                record.source_id,
                record.stream_index,
                record.category,
                record.termination_reason,
                record.producer.name,
                record.producer.version,
                record.producer.configuration_sha256,
                record.start_pts.value,
                int(record.start_pts.value),
                record.start_pts.time_base.numerator,
                record.start_pts.time_base.denominator,
                record.end_pts.value,
                int(record.end_pts.value),
                record.end_pts.time_base.numerator,
                record.end_pts.time_base.denominator,
                len(record.points),
                record.schema_version,
                record.identity_version,
                encoded,
                hashlib.sha256(encoded).hexdigest(),
            ),
        )
        connection.executemany(
            """INSERT INTO tracklet_points(
                tracklet_id, ordinal, observation_id, frame_id, pts_value, pts_order
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            tuple(
                (
                    record.tracklet_id,
                    ordinal,
                    point.observation_id,
                    point.frame_id,
                    point.pts.value,
                    int(point.pts.value),
                )
                for ordinal, point in enumerate(record.points)
            ),
        )

    def _associate_perception_records(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        records: tuple[tuple[PerceptionRecord, bytes], ...],
        operation: str,
    ) -> None:
        run = connection.execute(
            "SELECT source_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None or type(run[0]) is not str:
            _fail(PortErrorCode.CORRUPT, operation)
        run_source_id = run[0]
        for record, _ in records:
            if isinstance(record, Observation):
                if record.source_id != run_source_id:
                    _fail(PortErrorCode.CONFLICT, operation)
                frame_owned = connection.execute(
                    """SELECT 1 FROM run_records WHERE run_id = ?
                    AND record_id = ? AND record_type = 'frame'""",
                    (run_id, record.frame_id),
                ).fetchone()
                if frame_owned is None:
                    _fail(PortErrorCode.CONFLICT, operation)
                identifier = record.observation_id
                record_type = "observation"
            else:
                if record.source_id != run_source_id:
                    _fail(PortErrorCode.CONFLICT, operation)
                for point in record.points:
                    owned = connection.execute(
                        """SELECT 1 FROM perception_run_records
                        WHERE run_id = ? AND record_id = ?
                          AND record_type = 'observation'""",
                        (run_id, point.observation_id),
                    ).fetchone()
                    reused = connection.execute(
                        """SELECT 1 FROM perception_run_records AS owned
                        JOIN tracklet_points AS point
                          ON point.tracklet_id = owned.record_id
                        WHERE owned.run_id = ? AND owned.record_type = 'tracklet'
                          AND owned.record_id != ? AND point.observation_id = ? LIMIT 1""",
                        (run_id, record.tracklet_id, point.observation_id),
                    ).fetchone()
                    if owned is None or reused is not None:
                        _fail(PortErrorCode.CONFLICT, operation)
                identifier = record.tracklet_id
                record_type = "tracklet"
            existing = connection.execute(
                """SELECT record_type FROM perception_run_records
                WHERE run_id = ? AND record_id = ?""",
                (run_id, identifier),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO perception_run_records(run_id, record_id, record_type)
                    VALUES (?, ?, ?)""",
                    (run_id, identifier, record_type),
                )
            elif existing != (record_type,):
                _fail(PortErrorCode.CORRUPT, operation)

    def _write_evidence_intents(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        intents: tuple[tuple[EvidenceIntent, bytes], ...],
        operation: str,
    ) -> None:
        for intent, encoded in intents:
            tracklet_row = connection.execute(
                """SELECT record_json FROM tracklets AS tracklet
                JOIN perception_run_records AS owned
                  ON owned.record_id = tracklet.tracklet_id
                WHERE owned.run_id = ? AND owned.record_type = 'tracklet'
                  AND tracklet.tracklet_id = ?""",
                (run_id, intent.tracklet_id),
            ).fetchone()
            observation_row = connection.execute(
                """SELECT observation.record_json FROM observations AS observation
                JOIN perception_run_records AS owned
                  ON owned.record_id = observation.observation_id
                WHERE owned.run_id = ? AND owned.record_type = 'observation'
                  AND observation.observation_id = ?""",
                (run_id, intent.observation_id),
            ).fetchone()
            if tracklet_row is None or observation_row is None:
                _fail(PortErrorCode.CONFLICT, operation)
            tracklet = self._decode_perception_record(tracklet_row[0], operation)
            observation = self._decode_perception_record(observation_row[0], operation)
            if not isinstance(tracklet, Tracklet) or not isinstance(observation, Observation):
                _fail(PortErrorCode.CORRUPT, operation)
            point_index = intent.score.point_index
            if (
                point_index >= len(tracklet.points)
                or tracklet.points[point_index].observation_id != intent.observation_id
                or intent.score.point_count != len(tracklet.points)
                or intent.source_id != observation.source_id
                or intent.frame_id != observation.frame_id
                or intent.stream_index != observation.stream_index
                or intent.pts != observation.pts
                or intent.geometry != observation.geometry
                or intent.score.confidence_millionths != observation.confidence_millionths
            ):
                _fail(PortErrorCode.CONFLICT, operation)
            expected: tuple[object, ...] = (
                intent.observation_id,
                intent.source_id,
                intent.frame_id,
                intent.stream_index,
                intent.selector.name,
                intent.selector.version,
                intent.selector.configuration_sha256,
                encoded,
                hashlib.sha256(encoded).hexdigest(),
            )
            existing = connection.execute(
                """SELECT observation_id, source_id, frame_id, stream_index,
                selector_name, selector_version, selector_configuration_sha256,
                intent_json, intent_sha256 FROM selected_evidence
                WHERE run_id = ? AND tracklet_id = ? AND rank = ?""",
                (run_id, intent.tracklet_id, intent.rank),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO selected_evidence(
                        run_id, tracklet_id, rank, observation_id, source_id, frame_id,
                        stream_index, selector_name, selector_version,
                        selector_configuration_sha256, intent_json, intent_sha256, evidence_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                    (run_id, intent.tracklet_id, intent.rank, *expected),
                )
            elif existing != expected:
                _fail(PortErrorCode.CONFLICT, operation)

    def commit_perception_for_run(
        self,
        run_id: str,
        records: tuple[PerceptionRecord, ...] = (),
        evidence_intents: tuple[EvidenceIntent, ...] = (),
        *,
        evidence_session: EvidenceWriterSession | None = None,
    ) -> None:
        """Commit one hidden v2 metadata batch for a preparing run."""

        operation = "commit_perception_for_run"
        selected_run = _identifier(run_id, _RUN_ID, operation)
        selected_records = self._select_perception_records(records, operation)
        if type(evidence_intents) is not tuple or len(evidence_intents) > MAX_PORT_BATCH_ITEMS:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        selected_intents = tuple(
            _canonical_evidence_intent(intent, operation) for intent in evidence_intents
        )
        with self._coordinated_write_connection(operation, evidence_session) as connection:
            try:
                with self._transaction(connection, operation):
                    self._guard_run_writable(connection, selected_run, operation)
                    state = connection.execute(
                        "SELECT state FROM runs WHERE run_id = ?", (selected_run,)
                    ).fetchone()
                    if state != ("preparing",):
                        _fail(PortErrorCode.CONFLICT, operation)
                    source_ids = {record.source_id for record, _ in selected_records}
                    source_ids.update(intent.source_id for intent, _ in selected_intents)
                    self._guard_sources_writable(connection, source_ids, operation)
                    for record, encoded in selected_records:
                        if isinstance(record, Observation):
                            self._write_observation(connection, record, encoded, operation)
                    self._associate_perception_records(
                        connection,
                        selected_run,
                        tuple(
                            item for item in selected_records if isinstance(item[0], Observation)
                        ),
                        operation,
                    )
                    for record, encoded in selected_records:
                        if isinstance(record, Tracklet):
                            self._write_tracklet(connection, record, encoded, operation)
                    self._associate_perception_records(
                        connection,
                        selected_run,
                        tuple(item for item in selected_records if isinstance(item[0], Tracklet)),
                        operation,
                    )
                    self._write_evidence_intents(
                        connection,
                        selected_run,
                        selected_intents,
                        operation,
                    )
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)

    def link_selected_evidence(
        self,
        run_id: str,
        links: tuple[EvidenceSelectionLink, ...],
        *,
        evidence_session: EvidenceWriterSession | None = None,
    ) -> None:
        """Link selected intents to already persisted run-owned evidence."""

        operation = "link_selected_evidence"
        selected_run = _identifier(run_id, _RUN_ID, operation)
        if type(links) is not tuple or len(links) > MAX_PORT_BATCH_ITEMS:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        failed = False
        selected_links: tuple[EvidenceSelectionLink, ...] = ()
        try:
            selected_links = tuple(
                EvidenceSelectionLink(link.tracklet_id, link.rank, link.evidence_id)
                for link in links
                if type(link) is EvidenceSelectionLink
            )
        except BaseException:
            failed = True
        if failed:
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        if len(selected_links) != len(links):
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        with self._coordinated_write_connection(operation, evidence_session) as connection:
            try:
                with self._transaction(connection, operation):
                    self._guard_run_writable(connection, selected_run, operation)
                    state = connection.execute(
                        "SELECT state FROM runs WHERE run_id = ?", (selected_run,)
                    ).fetchone()
                    if state != ("preparing",):
                        _fail(PortErrorCode.CONFLICT, operation)
                    for link in selected_links:
                        selection = connection.execute(
                            """SELECT intent_json, evidence_id FROM selected_evidence
                            WHERE run_id = ? AND tracklet_id = ? AND rank = ?""",
                            (selected_run, link.tracklet_id, link.rank),
                        ).fetchone()
                        evidence_row = connection.execute(
                            """SELECT item.record_json FROM evidence AS item
                            JOIN run_records AS owned ON owned.record_id = item.evidence_id
                            WHERE owned.run_id = ? AND owned.record_type = 'evidence'
                              AND item.evidence_id = ?""",
                            (selected_run, link.evidence_id),
                        ).fetchone()
                        if selection is None or evidence_row is None:
                            _fail(PortErrorCode.CONFLICT, operation)
                        intent = self._verify_evidence_selection(
                            connection,
                            selected_run,
                            link.tracklet_id,
                            link.rank,
                            operation,
                            require_artifact_intent=True,
                        ).intent
                        evidence = self._decode_record(evidence_row[0], operation)
                        if not isinstance(evidence, EvidenceRef):
                            _fail(PortErrorCode.CORRUPT, operation)
                        self._verify_projection(connection, evidence, operation)
                        artifact_intent = connection.execute(
                            """SELECT 1 FROM artifact_intents WHERE run_id = ?
                            AND artifact_digest = ? AND byte_count = ? AND media_type = ?
                            AND protocol_version = ?""",
                            (
                                selected_run,
                                evidence.artifact.sha256,
                                evidence.artifact.bytes,
                                evidence.artifact.media_type,
                                WORLD_PROTOCOL_VERSION,
                            ),
                        ).fetchone()
                        if (
                            evidence.frame_id != intent.frame_id
                            or evidence.geometry != intent.geometry
                            or evidence.kind != intent.kind
                            or evidence.retention != intent.retention
                            or artifact_intent is None
                        ):
                            _fail(PortErrorCode.CONFLICT, operation)
                        if evidence_session is None:
                            _fail(PortErrorCode.INVALID_REQUEST, operation)
                        artifact_check = evidence_session.inspect((evidence.artifact,))[0]
                        if artifact_check.state is ArtifactState.CORRUPT:
                            _fail(PortErrorCode.CORRUPT, operation)
                        if artifact_check.state is not ArtifactState.VALID:
                            _fail(PortErrorCode.CONFLICT, operation)
                        if selection[1] is None:
                            connection.execute(
                                """UPDATE selected_evidence SET evidence_id = ?
                                WHERE run_id = ? AND tracklet_id = ? AND rank = ?""",
                                (
                                    link.evidence_id,
                                    selected_run,
                                    link.tracklet_id,
                                    link.rank,
                                ),
                            )
                        elif selection[1] != link.evidence_id:
                            _fail(PortErrorCode.CONFLICT, operation)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)

    def finalize_run(
        self,
        manifest: RunManifest,
        records: tuple[Record, ...] = (),
        *,
        evidence_session: EvidenceWriterSession | None = None,
    ) -> None:
        """Atomically publish a run after its intended artifacts are referenced."""

        operation = "finalize_run"
        selected_manifest, encoded_manifest = _canonical_record(manifest, operation)
        if not isinstance(selected_manifest, RunManifest) or selected_manifest.state != "committed":
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        selected = self._select_records(records, operation)
        if any(isinstance(record, (Source, RunManifest)) for record, _ in selected):
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        with self._coordinated_write_connection(operation, evidence_session) as connection:
            try:
                with self._transaction(connection, operation):
                    self._guard_run_writable(
                        connection,
                        selected_manifest.run_id,
                        operation,
                    )
                    existing = connection.execute(
                        "SELECT state, record_json FROM runs WHERE run_id = ?",
                        (selected_manifest.run_id,),
                    ).fetchone()
                    if existing is None:
                        _fail(PortErrorCode.CONFLICT, operation)
                    if existing[0] == "committed":
                        if existing[1] != encoded_manifest:
                            _fail(PortErrorCode.CONFLICT, operation)
                        self._guard_committed_retries(
                            connection,
                            (*selected, (selected_manifest, encoded_manifest)),
                            operation,
                        )
                    elif existing[0] != "preparing":
                        _fail(PortErrorCode.CONFLICT, operation)
                    self._write_records(connection, selected, operation)
                    self._associate_records(
                        connection,
                        selected_manifest.run_id,
                        [cast(FrameRef | EvidenceRef, record) for record, _ in selected],
                        operation,
                    )
                    missing = connection.execute(
                        """SELECT count(*) FROM artifact_intents AS intent
                        WHERE intent.run_id = ? AND NOT EXISTS (
                            SELECT 1 FROM run_records AS owned
                            JOIN artifact_references AS reference
                              ON reference.evidence_id = owned.record_id
                            JOIN artifact_catalog AS catalog
                              ON catalog.digest = reference.artifact_digest
                            WHERE owned.run_id = intent.run_id
                              AND owned.record_type = 'evidence'
                              AND reference.artifact_digest = intent.artifact_digest
                              AND catalog.byte_count = intent.byte_count
                              AND catalog.media_type = intent.media_type
                              AND catalog.layout_version = intent.protocol_version
                        )""",
                        (selected_manifest.run_id,),
                    ).fetchone()
                    if missing is None or missing[0] != 0:
                        _fail(PortErrorCode.CONFLICT, operation)
                    frame_count = connection.execute(
                        """SELECT count(*) FROM run_records
                        WHERE run_id = ? AND record_type = 'frame'""",
                        (selected_manifest.run_id,),
                    ).fetchone()
                    if (
                        frame_count is None
                        or selected_manifest.outputs is None
                        or frame_count[0] != int(selected_manifest.outputs.sample_count)
                    ):
                        _fail(PortErrorCode.CONFLICT, operation)
                    self._verify_perception_run(
                        connection,
                        selected_manifest.run_id,
                        operation,
                        require_publication=True,
                    )
                    linked_rows = connection.execute(
                        """SELECT item.record_json FROM selected_evidence AS selected
                        JOIN evidence AS item ON item.evidence_id = selected.evidence_id
                        WHERE selected.run_id = ? AND selected.evidence_id IS NOT NULL
                        ORDER BY selected.tracklet_id, selected.rank LIMIT ?""",
                        (selected_manifest.run_id, self._max_audit_records + 1),
                    ).fetchall()
                    if len(linked_rows) > self._max_audit_records:
                        _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
                    if linked_rows and evidence_session is None:
                        _fail(PortErrorCode.INVALID_REQUEST, operation)
                    linked_artifacts: list[Artifact] = []
                    for row in linked_rows:
                        linked_record = self._decode_record(row[0], operation)
                        if not isinstance(linked_record, EvidenceRef):
                            _fail(PortErrorCode.CORRUPT, operation)
                        linked_artifacts.append(linked_record.artifact)
                    if evidence_session is not None and linked_artifacts:
                        checks = evidence_session.inspect(tuple(linked_artifacts))
                        if any(check.state is ArtifactState.CORRUPT for check in checks):
                            _fail(PortErrorCode.CORRUPT, operation)
                        if any(check.state is not ArtifactState.VALID for check in checks):
                            _fail(PortErrorCode.CONFLICT, operation)
                    self._write_run(
                        connection,
                        selected_manifest,
                        encoded_manifest,
                        hashlib.sha256(encoded_manifest).hexdigest(),
                        operation,
                    )
                    connection.execute(
                        "DELETE FROM artifact_intents WHERE run_id = ?",
                        (selected_manifest.run_id,),
                    )
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)

    def pending_runs(
        self,
        *,
        after_run_id: str | None = None,
        limit: int = MAX_PORT_BATCH_ITEMS,
        actionable_only: bool = False,
    ) -> tuple[RunManifest, ...]:
        operation = "pending_runs"
        selected_limit = _bounded_limit(limit, operation)
        if type(actionable_only) is not bool:
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        after = ""
        if after_run_id is not None:
            after = _identifier(after_run_id, _RUN_ID, operation)
        with self._read_connection(operation) as connection:
            try:
                if actionable_only:
                    rows = connection.execute(
                        """SELECT record_json FROM runs
                        WHERE (
                            state = 'preparing'
                            OR (
                                state IN ('failed', 'cancelled')
                                AND (
                                    EXISTS (
                                        SELECT 1 FROM artifact_intents AS intent
                                        WHERE intent.run_id = runs.run_id
                                    )
                                    OR EXISTS (
                                        SELECT 1 FROM run_records AS owned
                                        WHERE owned.run_id = runs.run_id
                                    )
                                    OR EXISTS (
                                        SELECT 1 FROM perception_run_records AS owned
                                        WHERE owned.run_id = runs.run_id
                                    )
                                    OR EXISTS (
                                        SELECT 1 FROM selected_evidence AS selected
                                        WHERE selected.run_id = runs.run_id
                                    )
                                )
                            )
                        ) AND run_id > ?
                          AND NOT EXISTS (
                              SELECT 1 FROM deletion_closure AS hidden
                              WHERE hidden.record_id = runs.run_id
                                AND hidden.record_type = 'run'
                          )
                        ORDER BY run_id LIMIT ?""",
                        (after, selected_limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """SELECT record_json FROM runs
                        WHERE state IN ('preparing', 'failed', 'cancelled')
                          AND run_id > ?
                          AND NOT EXISTS (
                              SELECT 1 FROM deletion_closure AS hidden
                              WHERE hidden.record_id = runs.run_id
                                AND hidden.record_type = 'run'
                          )
                        ORDER BY run_id LIMIT ?""",
                        (after, selected_limit),
                    ).fetchall()
                result = tuple(self._decode_record(row[0], operation) for row in rows)
                if not all(isinstance(record, RunManifest) for record in result):
                    _fail(PortErrorCode.CORRUPT, operation)
                for record in result:
                    self._verify_projection(connection, record, operation)
                return cast(tuple[RunManifest, ...], result)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def _artifact_spec(
        self,
        connection: sqlite3.Connection,
        digest: str,
        operation: str,
    ) -> Artifact:
        rows = connection.execute(
            """SELECT byte_count, media_type, layout_version FROM artifact_catalog
            WHERE digest = ?
            UNION ALL
            SELECT byte_count, media_type, protocol_version FROM artifact_intents
            WHERE artifact_digest = ?""",
            (digest, digest),
        ).fetchall()
        if not rows:
            _fail(PortErrorCode.CORRUPT, operation)
        try:
            artifacts = tuple(Artifact(digest, row[0], row[1]) for row in rows)
        except (TypeError, ValueError):
            _fail(PortErrorCode.CORRUPT, operation)
        if any(row[2] != WORLD_PROTOCOL_VERSION for row in rows) or any(
            item != artifacts[0] for item in artifacts[1:]
        ):
            _fail(PortErrorCode.CORRUPT, operation)
        return artifacts[0]

    def _stages_for_run(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        operation: str,
    ) -> tuple[StageHandle, ...]:
        rows = connection.execute(
            """SELECT staging_name, artifact_digest, byte_count, media_type,
            protocol_version FROM artifact_intents WHERE run_id = ?
            ORDER BY staging_name LIMIT ?""",
            (run_id, self._max_audit_records + 1),
        ).fetchall()
        if len(rows) > self._max_audit_records:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        try:
            return tuple(
                StageHandle(run_id, row[0], Artifact(row[1], row[2], row[3]), row[4])
                for row in rows
            )
        except (TypeError, ValueError):
            _fail(PortErrorCode.CORRUPT, operation)
        raise AssertionError("unreachable")

    def run_cleanup_plan(
        self,
        run_id: str,
        *,
        evidence_session: EvidenceWriterSession | None = None,
    ) -> RunCleanupPlan:
        """Freeze the deterministic cleanup inputs for one incomplete run."""

        operation = "run_cleanup_plan"
        selected_run = _identifier(run_id, _RUN_ID, operation)
        with self._coordinated_write_connection(operation, evidence_session) as connection:
            try:
                hidden = connection.execute(
                    "SELECT 1 FROM deletion_closure WHERE record_id = ? LIMIT 1",
                    (selected_run,),
                ).fetchone()
                if hidden is not None:
                    _fail(PortErrorCode.NOT_FOUND, operation)
                state = connection.execute(
                    "SELECT state FROM runs WHERE run_id = ?", (selected_run,)
                ).fetchone()
                if state is None:
                    _fail(PortErrorCode.NOT_FOUND, operation)
                if state[0] == "committed":
                    _fail(PortErrorCode.CONFLICT, operation)
                if state[0] not in {"preparing", "failed", "cancelled"}:
                    _fail(PortErrorCode.CORRUPT, operation)
                stages = self._stages_for_run(connection, selected_run, operation)
                digest_rows = connection.execute(
                    """SELECT artifact_digest FROM artifact_intents WHERE run_id = ?
                    UNION
                    SELECT reference.artifact_digest
                    FROM run_records AS owned
                    JOIN artifact_references AS reference
                      ON reference.evidence_id = owned.record_id
                    WHERE owned.run_id = ? AND owned.record_type = 'evidence'
                    ORDER BY artifact_digest LIMIT ?""",
                    (selected_run, selected_run, self._max_audit_records + 1),
                ).fetchall()
                if len(digest_rows) > self._max_audit_records:
                    _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
                artifacts: list[Artifact] = []
                for row in digest_rows:
                    if type(row[0]) is not str:
                        _fail(PortErrorCode.CORRUPT, operation)
                    digest = row[0]
                    artifact = self._artifact_spec(connection, digest, operation)
                    surviving = connection.execute(
                        """SELECT 1 FROM artifact_references AS reference
                        WHERE reference.artifact_digest = ? AND NOT (
                            EXISTS (
                                SELECT 1 FROM run_records AS target
                                WHERE target.run_id = ?
                                  AND target.record_id = reference.evidence_id
                                  AND target.record_type = 'evidence'
                            ) AND NOT EXISTS (
                                SELECT 1 FROM run_records AS other
                                WHERE other.run_id != ?
                                  AND other.record_id = reference.evidence_id
                                  AND other.record_type = 'evidence'
                            )
                        ) LIMIT 1""",
                        (digest, selected_run, selected_run),
                    ).fetchone()
                    if surviving is None:
                        artifacts.append(artifact)
                return RunCleanupPlan(selected_run, tuple(artifacts), stages)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def finish_run_cleanup(
        self,
        run_id: str,
        *,
        evidence_session: EvidenceWriterSession | None = None,
    ) -> None:
        """Remove hidden run-owned metadata and leave a retryable failed marker."""

        operation = "finish_run_cleanup"
        selected_run = _identifier(run_id, _RUN_ID, operation)
        with self._coordinated_write_connection(operation, evidence_session) as connection:
            try:
                with self._transaction(connection, operation):
                    self._guard_run_writable(
                        connection,
                        selected_run,
                        operation,
                        missing_code=PortErrorCode.NOT_FOUND,
                    )
                    row = connection.execute(
                        "SELECT state, record_json FROM runs WHERE run_id = ?",
                        (selected_run,),
                    ).fetchone()
                    if row is None:
                        _fail(PortErrorCode.NOT_FOUND, operation)
                    if row[0] == "committed":
                        _fail(PortErrorCode.CONFLICT, operation)
                    run = self._decode_record(row[1], operation)
                    if not isinstance(run, RunManifest) or run.state != row[0]:
                        _fail(PortErrorCode.CORRUPT, operation)
                    evidence_rows = connection.execute(
                        """SELECT owned.record_id FROM run_records AS owned
                        WHERE owned.run_id = ? AND owned.record_type = 'evidence'
                          AND NOT EXISTS (
                            SELECT 1 FROM run_records AS other
                            WHERE other.run_id != owned.run_id
                              AND other.record_id = owned.record_id
                          ) LIMIT ?""",
                        (selected_run, self._max_audit_records + 1),
                    ).fetchall()
                    frame_rows = connection.execute(
                        """SELECT owned.record_id FROM run_records AS owned
                        WHERE owned.run_id = ? AND owned.record_type = 'frame'
                          AND NOT EXISTS (
                            SELECT 1 FROM run_records AS other
                            WHERE other.run_id != owned.run_id
                              AND other.record_id = owned.record_id
                          ) LIMIT ?""",
                        (selected_run, self._max_audit_records + 1),
                    ).fetchall()
                    if (
                        len(evidence_rows) > self._max_audit_records
                        or len(frame_rows) > self._max_audit_records
                    ):
                        _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
                    evidence_ids = tuple(row[0] for row in evidence_rows)
                    candidate_frames = tuple(row[0] for row in frame_rows)
                    if not all(type(value) is str for value in (*evidence_ids, *candidate_frames)):
                        _fail(PortErrorCode.CORRUPT, operation)
                    evidence_set = set(evidence_ids)
                    frame_ids = tuple(
                        frame_id
                        for frame_id in candidate_frames
                        if connection.execute(
                            "SELECT evidence_id FROM evidence WHERE frame_id = ? LIMIT 1",
                            (frame_id,),
                        ).fetchone()
                        is None
                        or all(
                            evidence_id in evidence_set
                            for (evidence_id,) in connection.execute(
                                "SELECT evidence_id FROM evidence WHERE frame_id = ?",
                                (frame_id,),
                            ).fetchall()
                        )
                    )
                    digest_rows = connection.execute(
                        """SELECT artifact_digest FROM artifact_intents WHERE run_id = ?
                        UNION
                        SELECT reference.artifact_digest
                        FROM run_records AS owned
                        JOIN artifact_references AS reference
                          ON reference.evidence_id = owned.record_id
                        WHERE owned.run_id = ? AND owned.record_type = 'evidence'""",
                        (selected_run, selected_run),
                    ).fetchall()
                    tracklet_rows = connection.execute(
                        """SELECT owned.record_id FROM perception_run_records AS owned
                        WHERE owned.run_id = ? AND owned.record_type = 'tracklet'
                          AND NOT EXISTS (
                            SELECT 1 FROM perception_run_records AS other
                            WHERE other.run_id != owned.run_id
                              AND other.record_id = owned.record_id
                          ) LIMIT ?""",
                        (selected_run, self._max_audit_records + 1),
                    ).fetchall()
                    observation_rows = connection.execute(
                        """SELECT owned.record_id FROM perception_run_records AS owned
                        WHERE owned.run_id = ? AND owned.record_type = 'observation'
                          AND NOT EXISTS (
                            SELECT 1 FROM perception_run_records AS other
                            WHERE other.run_id != owned.run_id
                              AND other.record_id = owned.record_id
                          ) LIMIT ?""",
                        (selected_run, self._max_audit_records + 1),
                    ).fetchall()
                    if (
                        len(tracklet_rows) > self._max_audit_records
                        or len(observation_rows) > self._max_audit_records
                    ):
                        _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
                    tracklet_ids = tuple(row[0] for row in tracklet_rows)
                    observation_ids = tuple(row[0] for row in observation_rows)
                    if not all(type(value) is str for value in (*tracklet_ids, *observation_ids)):
                        _fail(PortErrorCode.CORRUPT, operation)
                    connection.execute(
                        "DELETE FROM selected_evidence WHERE run_id = ?", (selected_run,)
                    )
                    connection.execute(
                        "DELETE FROM perception_run_records WHERE run_id = ?", (selected_run,)
                    )
                    connection.executemany(
                        "DELETE FROM tracklets WHERE tracklet_id = ?",
                        ((value,) for value in tracklet_ids),
                    )
                    for observation_id in observation_ids:
                        connection.execute(
                            """DELETE FROM observations WHERE observation_id = ?
                            AND NOT EXISTS (
                                SELECT 1 FROM tracklet_points
                                WHERE observation_id = ?
                            )""",
                            (observation_id, observation_id),
                        )
                    connection.execute("DELETE FROM run_records WHERE run_id = ?", (selected_run,))
                    connection.executemany(
                        "DELETE FROM evidence WHERE evidence_id = ?",
                        ((value,) for value in evidence_ids),
                    )
                    connection.executemany(
                        "DELETE FROM frames WHERE frame_id = ?",
                        ((value,) for value in frame_ids),
                    )
                    connection.execute(
                        "DELETE FROM artifact_intents WHERE run_id = ?", (selected_run,)
                    )
                    for (digest,) in digest_rows:
                        connection.execute(
                            """DELETE FROM artifact_catalog WHERE digest = ?
                            AND NOT EXISTS (
                                SELECT 1 FROM artifact_references
                                WHERE artifact_digest = ?
                            )""",
                            (digest, digest),
                        )
                    if run.state == "preparing":
                        failed = RunManifest.create(
                            run.source_id,
                            run.producers,
                            run.sampling,
                            "failed",
                        )
                        self._write_run(
                            connection,
                            failed,
                            dumps_record(failed),
                            hashlib.sha256(dumps_record(failed)).hexdigest(),
                            operation,
                        )
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)

    def unreferenced_artifacts(
        self,
        artifacts: tuple[Artifact, ...],
    ) -> tuple[Artifact, ...]:
        """Return exact catalog-free or unreferenced artifacts from a bounded batch."""

        operation = "unreferenced_artifacts"
        if type(artifacts) is not tuple or len(artifacts) > MAX_PORT_BATCH_ITEMS:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        try:
            selected = tuple(Artifact.from_mapping(item.to_mapping()) for item in artifacts)
        except (AttributeError, TypeError, ValueError):
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        with self._read_connection(operation) as connection:
            try:
                result: list[Artifact] = []
                for artifact in selected:
                    catalog = connection.execute(
                        """SELECT byte_count, media_type, layout_version
                        FROM artifact_catalog WHERE digest = ?""",
                        (artifact.sha256,),
                    ).fetchone()
                    if catalog is not None and catalog != (
                        artifact.bytes,
                        artifact.media_type,
                        WORLD_PROTOCOL_VERSION,
                    ):
                        _fail(PortErrorCode.CORRUPT, operation)
                    referenced = connection.execute(
                        """SELECT 1 FROM artifact_references
                        WHERE artifact_digest = ? LIMIT 1""",
                        (artifact.sha256,),
                    ).fetchone()
                    if referenced is None:
                        result.append(artifact)
                return tuple(result)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def _deletion_status_row(
        self,
        row: tuple[object, ...],
        operation: str,
    ) -> DeletionStatus:
        try:
            return DeletionStatus(
                deletion_id=cast(str, row[0]),
                state=DeletionState(cast(str, row[1])),
                record_count=cast(int, row[2]),
                artifact_count=cast(int, row[3]),
                shared_retention_count=cast(int, row[4]),
                completed_at_utc=cast(str | None, row[5]),
                protocol_version=cast(int, row[6]),
            )
        except (TypeError, ValueError):
            _fail(PortErrorCode.CORRUPT, operation)
        raise AssertionError("unreachable")

    def _deletion_status(
        self,
        connection: sqlite3.Connection,
        deletion_id: str,
        operation: str,
    ) -> DeletionStatus:
        row = connection.execute(
            """SELECT deletion_id, state, record_count, artifact_count,
            shared_retention_count, completed_at_utc, protocol_version
            FROM deletion_jobs WHERE deletion_id = ?""",
            (deletion_id,),
        ).fetchone()
        if row is None:
            _fail(PortErrorCode.NOT_FOUND, operation)
        return self._deletion_status_row(row, operation)

    def _source_closure(
        self,
        connection: sqlite3.Connection,
        source_id: str,
        operation: str,
    ) -> tuple[tuple[str, str], ...]:
        rows = connection.execute(
            """SELECT record_id, record_type FROM (
                SELECT ? AS record_id, 'source' AS record_type
                UNION ALL
                SELECT frame_id, 'frame' FROM frames WHERE source_id = ?
                UNION ALL
                SELECT item.evidence_id, 'evidence' FROM evidence AS item
                JOIN frames AS frame ON frame.frame_id = item.frame_id
                WHERE frame.source_id = ?
                UNION ALL
                SELECT run_id, 'run' FROM runs WHERE source_id = ?
            ) ORDER BY record_type, record_id LIMIT ?""",
            (
                source_id,
                source_id,
                source_id,
                source_id,
                self._max_audit_records + 1,
            ),
        ).fetchall()
        if len(rows) > self._max_audit_records:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        patterns = {
            "source": _SOURCE_ID,
            "frame": _FRAME_ID,
            "evidence": _EVIDENCE_ID,
            "run": _RUN_ID,
        }
        if any(
            type(record_id) is not str
            or record_type not in patterns
            or not patterns[record_type].fullmatch(record_id)
            for record_id, record_type in rows
        ):
            _fail(PortErrorCode.CORRUPT, operation)
        return cast(tuple[tuple[str, str], ...], tuple(rows))

    def _perception_source_closure(
        self,
        connection: sqlite3.Connection,
        source_id: str,
        operation: str,
    ) -> tuple[tuple[str, str], ...]:
        rows = connection.execute(
            """SELECT record_id, record_type FROM (
                SELECT observation_id AS record_id, 'observation' AS record_type
                FROM observations WHERE source_id = ?
                UNION ALL
                SELECT tracklet_id, 'tracklet' FROM tracklets WHERE source_id = ?
            ) ORDER BY record_type, record_id LIMIT ?""",
            (source_id, source_id, self._max_audit_records + 1),
        ).fetchall()
        if len(rows) > self._max_audit_records:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        patterns = {"observation": _OBSERVATION_ID, "tracklet": _TRACKLET_ID}
        if any(
            type(record_id) is not str
            or record_type not in patterns
            or not patterns[record_type].fullmatch(record_id)
            for record_id, record_type in rows
        ):
            _fail(PortErrorCode.CORRUPT, operation)
        return cast(tuple[tuple[str, str], ...], tuple(rows))

    def _load_deletion_plan(
        self,
        connection: sqlite3.Connection,
        deletion_id: str,
        operation: str,
    ) -> DeletionPlan:
        status = self._deletion_status(connection, deletion_id, operation)
        if status.state is not DeletionState.PENDING:
            _fail(PortErrorCode.CONFLICT, operation)
        job = connection.execute(
            """SELECT root_kind, root_id FROM deletion_jobs
            WHERE deletion_id = ?""",
            (deletion_id,),
        ).fetchone()
        if (
            job is None
            or job[0] != "source"
            or type(job[1]) is not str
            or not _SOURCE_ID.fullmatch(job[1])
        ):
            _fail(PortErrorCode.CORRUPT, operation)
        source_id = job[1]
        closure_rows = connection.execute(
            """SELECT record_id, record_type FROM deletion_closure
            WHERE deletion_id = ? ORDER BY record_type, record_id LIMIT ?""",
            (deletion_id, self._max_audit_records + 1),
        ).fetchall()
        perception_closure_rows = connection.execute(
            """SELECT record_id, record_type FROM perception_deletion_closure
            WHERE deletion_id = ? ORDER BY record_type, record_id LIMIT ?""",
            (deletion_id, self._max_audit_records + 1),
        ).fetchall()
        if (
            len(closure_rows) > self._max_audit_records
            or len(perception_closure_rows) > self._max_audit_records
            or len(closure_rows) + len(perception_closure_rows) > self._max_audit_records
        ):
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        expected_closure = set(self._source_closure(connection, source_id, operation))
        expected_perception_closure = set(
            self._perception_source_closure(connection, source_id, operation)
        )
        if (
            len(closure_rows) + len(perception_closure_rows) != status.record_count
            or set(closure_rows) != expected_closure
            or set(perception_closure_rows) != expected_perception_closure
        ):
            _fail(PortErrorCode.CORRUPT, operation)
        rows = connection.execute(
            """SELECT artifact_digest, delete_required FROM deletion_artifacts
            WHERE deletion_id = ? ORDER BY artifact_digest LIMIT ?""",
            (deletion_id, self._max_audit_records + 1),
        ).fetchall()
        if len(rows) > self._max_audit_records:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        expected_digest_rows = connection.execute(
            """SELECT reference.artifact_digest
                FROM artifact_references AS reference
                JOIN deletion_closure AS closure
                  ON closure.record_id = reference.evidence_id
                WHERE closure.deletion_id = ? AND closure.record_type = 'evidence'
                UNION
                SELECT intent.artifact_digest FROM artifact_intents AS intent
                JOIN deletion_closure AS closure ON closure.record_id = intent.run_id
                WHERE closure.deletion_id = ? AND closure.record_type = 'run'
                LIMIT ?""",
            (deletion_id, deletion_id, self._max_audit_records + 1),
        ).fetchall()
        if len(expected_digest_rows) > self._max_audit_records:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        if any(type(row[0]) is not str for row in expected_digest_rows):
            _fail(PortErrorCode.CORRUPT, operation)
        expected_digests = {row[0] for row in expected_digest_rows}
        if {row[0] for row in rows} != expected_digests:
            _fail(PortErrorCode.CORRUPT, operation)
        artifacts: list[DeletionArtifact] = []
        for digest, delete_required in rows:
            if type(digest) is not str or delete_required not in {0, 1}:
                _fail(PortErrorCode.CORRUPT, operation)
            surviving = connection.execute(
                """SELECT 1 FROM artifact_references AS reference
                WHERE reference.artifact_digest = ? AND NOT EXISTS (
                    SELECT 1 FROM deletion_closure AS closure
                    WHERE closure.deletion_id = ?
                      AND closure.record_id = reference.evidence_id
                      AND closure.record_type = 'evidence'
                ) LIMIT 1""",
                (digest, deletion_id),
            ).fetchone()
            expected_delete = surviving is None
            if (delete_required == 1) != expected_delete:
                _fail(PortErrorCode.CORRUPT, operation)
            artifacts.append(
                DeletionArtifact(
                    self._artifact_spec(connection, digest, operation),
                    expected_delete,
                )
            )
        stage_rows = connection.execute(
            """SELECT intent.run_id, intent.staging_name, intent.artifact_digest,
            intent.byte_count, intent.media_type, intent.protocol_version
            FROM artifact_intents AS intent
            JOIN deletion_closure AS closure ON closure.record_id = intent.run_id
            WHERE closure.deletion_id = ? AND closure.record_type = 'run'
            ORDER BY intent.run_id, intent.staging_name LIMIT ?""",
            (deletion_id, self._max_audit_records + 1),
        ).fetchall()
        if len(stage_rows) > self._max_audit_records:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        try:
            stages = tuple(
                StageHandle(row[0], row[1], Artifact(row[2], row[3], row[4]), row[5])
                for row in stage_rows
            )
            run_ids = tuple(
                record_id for record_id, record_type in closure_rows if record_type == "run"
            )
            return DeletionPlan(
                deletion_id,
                tuple(artifacts),
                stages,
                run_ids,
                status.record_count,
                status.artifact_count,
                status.shared_retention_count,
            )
        except (TypeError, ValueError):
            _fail(PortErrorCode.CORRUPT, operation)
        raise AssertionError("unreachable")

    def begin_source_deletion(
        self,
        source_id: str,
        deletion_id: str,
        *,
        evidence_session: EvidenceWriterSession | None = None,
    ) -> DeletionPlan:
        """Freeze one source closure and its reference-safe artifact decisions."""

        operation = "begin_source_deletion"
        selected_source = _identifier(source_id, _SOURCE_ID, operation)
        selected_deletion = _deletion_id(deletion_id, operation)
        with self._coordinated_write_connection(operation, evidence_session) as connection:
            try:
                with self._transaction(connection, operation):
                    existing = connection.execute(
                        """SELECT deletion_id FROM deletion_jobs
                        WHERE root_kind = 'source' AND root_id = ?
                          AND state = 'pending' LIMIT 1""",
                        (selected_source,),
                    ).fetchone()
                    if existing is not None:
                        if type(existing[0]) is not str:
                            _fail(PortErrorCode.CORRUPT, operation)
                        return self._load_deletion_plan(connection, existing[0], operation)
                    active = connection.execute(
                        """SELECT 1 FROM deletion_jobs
                        WHERE state != 'complete' LIMIT 1"""
                    ).fetchone()
                    if active is not None:
                        _fail(PortErrorCode.CONFLICT, operation)
                    collision = connection.execute(
                        "SELECT 1 FROM deletion_jobs WHERE deletion_id = ?",
                        (selected_deletion,),
                    ).fetchone()
                    if collision is not None:
                        _fail(PortErrorCode.CONFLICT, operation)
                    source = connection.execute(
                        "SELECT 1 FROM sources WHERE source_id = ?",
                        (selected_source,),
                    ).fetchone()
                    if source is None:
                        _fail(PortErrorCode.NOT_FOUND, operation)
                    hidden = connection.execute(
                        "SELECT 1 FROM deletion_closure WHERE record_id = ? LIMIT 1",
                        (selected_source,),
                    ).fetchone()
                    if hidden is not None:
                        _fail(PortErrorCode.CONFLICT, operation)
                    connection.execute(
                        """INSERT INTO deletion_jobs(
                            deletion_id, root_kind, root_id, protocol_version, state,
                            record_count, artifact_count, shared_retention_count,
                            completed_at_utc
                        ) VALUES (?, 'source', ?, ?, 'pending', 0, 0, 0, NULL)""",
                        (selected_deletion, selected_source, WORLD_PROTOCOL_VERSION),
                    )
                    closure = tuple(
                        (selected_deletion, record_id, record_type)
                        for record_id, record_type in self._source_closure(
                            connection,
                            selected_source,
                            operation,
                        )
                    )
                    connection.executemany(
                        """INSERT INTO deletion_closure(
                            deletion_id, record_id, record_type
                        ) VALUES (?, ?, ?)""",
                        closure,
                    )
                    perception_closure = tuple(
                        (selected_deletion, record_id, record_type)
                        for record_id, record_type in self._perception_source_closure(
                            connection,
                            selected_source,
                            operation,
                        )
                    )
                    connection.executemany(
                        """INSERT INTO perception_deletion_closure(
                            deletion_id, record_id, record_type
                        ) VALUES (?, ?, ?)""",
                        perception_closure,
                    )
                    digest_rows = connection.execute(
                        """SELECT reference.artifact_digest
                        FROM artifact_references AS reference
                        JOIN deletion_closure AS closure
                          ON closure.record_id = reference.evidence_id
                        WHERE closure.deletion_id = ? AND closure.record_type = 'evidence'
                        UNION
                        SELECT intent.artifact_digest FROM artifact_intents AS intent
                        JOIN deletion_closure AS closure ON closure.record_id = intent.run_id
                        WHERE closure.deletion_id = ? AND closure.record_type = 'run'
                        ORDER BY artifact_digest LIMIT ?""",
                        (selected_deletion, selected_deletion, self._max_audit_records + 1),
                    ).fetchall()
                    if len(digest_rows) > self._max_audit_records:
                        _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
                    decisions: list[tuple[str, int]] = []
                    for (digest,) in digest_rows:
                        if type(digest) is not str:
                            _fail(PortErrorCode.CORRUPT, operation)
                        self._artifact_spec(connection, digest, operation)
                        surviving = connection.execute(
                            """SELECT 1 FROM artifact_references AS reference
                            WHERE reference.artifact_digest = ? AND NOT EXISTS (
                                SELECT 1 FROM deletion_closure AS closure
                                WHERE closure.deletion_id = ?
                                  AND closure.record_id = reference.evidence_id
                                  AND closure.record_type = 'evidence'
                            ) LIMIT 1""",
                            (digest, selected_deletion),
                        ).fetchone()
                        decisions.append((digest, 0 if surviving is not None else 1))
                    connection.executemany(
                        """INSERT INTO deletion_artifacts(
                            deletion_id, artifact_digest, delete_required
                        ) VALUES (?, ?, ?)""",
                        (
                            (selected_deletion, digest, delete_required)
                            for digest, delete_required in decisions
                        ),
                    )
                    delete_count = sum(item[1] for item in decisions)
                    shared_count = len(decisions) - delete_count
                    connection.execute(
                        """UPDATE deletion_jobs SET record_count = ?, artifact_count = ?,
                        shared_retention_count = ? WHERE deletion_id = ?""",
                        (
                            len(closure) + len(perception_closure),
                            delete_count,
                            shared_count,
                            selected_deletion,
                        ),
                    )
                    return self._load_deletion_plan(connection, selected_deletion, operation)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def get_deletion_plan(self, deletion_id: str) -> DeletionPlan:
        operation = "get_deletion_plan"
        selected = _deletion_id(deletion_id, operation)
        with self._read_connection(operation) as connection:
            try:
                return self._load_deletion_plan(connection, selected, operation)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def pending_deletions(
        self,
        *,
        after_deletion_id: str | None = None,
        limit: int = MAX_PORT_BATCH_ITEMS,
    ) -> tuple[DeletionStatus, ...]:
        operation = "pending_deletions"
        selected_limit = _bounded_limit(limit, operation)
        after = ""
        if after_deletion_id is not None:
            after = _deletion_id(after_deletion_id, operation)
        with self._read_connection(operation) as connection:
            try:
                rows = connection.execute(
                    """SELECT deletion_id, state, record_count, artifact_count,
                    shared_retention_count, completed_at_utc, protocol_version
                    FROM deletion_jobs WHERE state != 'complete' AND deletion_id > ?
                    ORDER BY deletion_id LIMIT ?""",
                    (after, selected_limit),
                ).fetchall()
                return tuple(self._deletion_status_row(row, operation) for row in rows)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def purge_deletion_metadata(
        self,
        deletion_id: str,
        *,
        evidence_session: EvidenceWriterSession | None = None,
    ) -> DeletionStatus:
        """Purge a pending source closure after its required files are absent."""

        operation = "purge_deletion_metadata"
        selected = _deletion_id(deletion_id, operation)
        with self._coordinated_write_connection(operation, evidence_session) as connection:
            try:
                with self._transaction(connection, operation):
                    status = self._deletion_status(connection, selected, operation)
                    if status.state in {DeletionState.METADATA_PURGED, DeletionState.COMPLETE}:
                        return status
                    plan = self._load_deletion_plan(connection, selected, operation)
                    if evidence_session is None:
                        _fail(PortErrorCode.INVALID_REQUEST, operation)
                    required = tuple(
                        item.artifact for item in plan.artifacts if item.delete_required
                    )
                    checks = tuple(
                        evidence_session.inspect((artifact,))[0] for artifact in required
                    )
                    if any(check.state is ArtifactState.CORRUPT for check in checks):
                        _fail(PortErrorCode.CORRUPT, operation)
                    if any(check.state is not ArtifactState.MISSING for check in checks):
                        _fail(PortErrorCode.CONFLICT, operation)
                    root = connection.execute(
                        """SELECT root_kind, root_id FROM deletion_jobs
                        WHERE deletion_id = ?""",
                        (selected,),
                    ).fetchone()
                    if root is None or root[0] != "source" or not isinstance(root[1], str):
                        _fail(PortErrorCode.CORRUPT, operation)
                    digests = connection.execute(
                        """SELECT artifact_digest FROM deletion_artifacts
                        WHERE deletion_id = ? AND delete_required = 1""",
                        (selected,),
                    ).fetchall()
                    removed = connection.execute(
                        "DELETE FROM sources WHERE source_id = ?", (root[1],)
                    )
                    if removed.rowcount != 1:
                        _fail(PortErrorCode.CORRUPT, operation)
                    for (digest,) in digests:
                        connection.execute(
                            """DELETE FROM artifact_catalog WHERE digest = ?
                            AND NOT EXISTS (
                                SELECT 1 FROM artifact_references
                                WHERE artifact_digest = ?
                            )""",
                            (digest, digest),
                        )
                    connection.execute(
                        "DELETE FROM deletion_closure WHERE deletion_id = ?", (selected,)
                    )
                    connection.execute(
                        "DELETE FROM perception_deletion_closure WHERE deletion_id = ?",
                        (selected,),
                    )
                    connection.execute(
                        "DELETE FROM deletion_artifacts WHERE deletion_id = ?", (selected,)
                    )
                    connection.execute(
                        """UPDATE deletion_jobs SET root_kind = NULL, root_id = NULL,
                        state = 'metadata_purged' WHERE deletion_id = ?""",
                        (selected,),
                    )
                    return self._deletion_status(connection, selected, operation)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def complete_deletion(
        self,
        deletion_id: str,
        *,
        evidence_session: EvidenceWriterSession | None = None,
    ) -> DeletionStatus:
        """Truncate obsolete WAL content and write the reduced completion receipt."""

        operation = "complete_deletion"
        selected = _deletion_id(deletion_id, operation)
        with self._coordinated_write_connection(operation, evidence_session) as connection:
            try:
                status = self._deletion_status(connection, selected, operation)
                if status.state is DeletionState.COMPLETE:
                    return status
                if status.state is not DeletionState.METADATA_PURGED:
                    _fail(PortErrorCode.CONFLICT, operation)
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if (
                    checkpoint is None
                    or len(checkpoint) != 3
                    or any(type(value) is not int for value in checkpoint)
                    or checkpoint[0] != 0
                    or checkpoint[1] != 0
                    or checkpoint[2] != 0
                ):
                    _fail(PortErrorCode.TIMEOUT, operation, retryable=True)
                completed = datetime.now(UTC).isoformat(timespec="microseconds")
                with self._transaction(connection, operation):
                    connection.execute(
                        """UPDATE deletion_jobs SET state = 'complete',
                        completed_at_utc = ? WHERE deletion_id = ?
                          AND state = 'metadata_purged'""",
                        (completed, selected),
                    )
                return self._deletion_status(connection, selected, operation)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def deletion_status(self, deletion_id: str) -> DeletionStatus:
        operation = "deletion_status"
        selected = _deletion_id(deletion_id, operation)
        with self._read_connection(operation) as connection:
            try:
                return self._deletion_status(connection, selected, operation)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def get(self, record_id: str) -> Record:
        operation = "get"
        selected = _identifier(record_id, _RECORD_ID, operation)
        prefix = selected[:3]
        statements = {
            "src": "SELECT record_json FROM sources WHERE source_id = ?",
            "frm": "SELECT record_json FROM frames WHERE frame_id = ?",
            "evi": "SELECT record_json FROM evidence WHERE evidence_id = ?",
            "run": "SELECT record_json FROM runs WHERE run_id = ?",
        }
        with self._read_connection(operation) as connection:
            try:
                hidden = connection.execute(
                    "SELECT 1 FROM deletion_closure WHERE record_id = ? LIMIT 1",
                    (selected,),
                ).fetchone()
                row = connection.execute(statements[prefix], (selected,)).fetchone()
                if row is None or hidden is not None:
                    _fail(PortErrorCode.NOT_FOUND, operation)
                visible = self._record_visible(connection, prefix, selected, operation)
                if not visible:
                    _fail(PortErrorCode.NOT_FOUND, operation)
                record = self._decode_record(row[0], operation)
                if _record_identifier(record) != selected:
                    _fail(PortErrorCode.CORRUPT, operation)
                self._verify_projection(connection, record, operation)
                return record
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def _record_visible(
        self,
        connection: sqlite3.Connection,
        prefix: str,
        record_id: str,
        operation: str,
    ) -> bool:
        if prefix not in {"frm", "evi"}:
            return True
        if prefix == "frm":
            row = connection.execute(
                """SELECT
                NOT EXISTS (
                    SELECT 1 FROM run_records AS owned
                    WHERE owned.record_id = ?
                ) OR EXISTS (
                    SELECT 1 FROM run_records AS owned
                    JOIN runs AS run ON run.run_id = owned.run_id
                    WHERE owned.record_id = ? AND run.state = 'committed'
                )""",
                (record_id, record_id),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT (
                    NOT EXISTS (
                        SELECT 1 FROM run_records AS owned
                        WHERE owned.record_id = item.evidence_id
                    ) OR EXISTS (
                        SELECT 1 FROM run_records AS owned
                        JOIN runs AS run ON run.run_id = owned.run_id
                        WHERE owned.record_id = item.evidence_id
                          AND run.state = 'committed'
                    )
                ) AND (
                    NOT EXISTS (
                        SELECT 1 FROM run_records AS owned
                        WHERE owned.record_id = item.frame_id
                    ) OR EXISTS (
                        SELECT 1 FROM run_records AS owned
                        JOIN runs AS run ON run.run_id = owned.run_id
                        WHERE owned.record_id = item.frame_id
                          AND run.state = 'committed'
                    )
                ) FROM evidence AS item WHERE item.evidence_id = ?""",
                (record_id,),
            ).fetchone()
        value: object = None if row is None else row[0]
        if type(value) is not int or value not in {0, 1}:
            _fail(PortErrorCode.CORRUPT, operation)
        return value == 1

    def _committed_run(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        operation: str,
    ) -> RunManifest:
        row = connection.execute(
            """SELECT run.record_json FROM runs AS run
            WHERE run.run_id = ? AND run.state = 'committed'
              AND NOT EXISTS (
                SELECT 1 FROM deletion_closure AS hidden
                WHERE hidden.record_id = run.run_id
                  AND hidden.record_type = 'run'
              )""",
            (run_id,),
        ).fetchone()
        if row is None:
            _fail(PortErrorCode.NOT_FOUND, operation)
        record = self._decode_record(row[0], operation)
        if not isinstance(record, RunManifest) or record.state != "committed":
            _fail(PortErrorCode.CORRUPT, operation)
        self._verify_projection(connection, record, operation)
        return record

    def list_run_frames(
        self,
        run_id: str,
        *,
        after_stream_index: int | None = None,
        after_decode_index: str | None = None,
        limit: int,
    ) -> tuple[FrameRef, ...]:
        """Return only frames owned by one visible committed run."""

        operation = "list_run_frames"
        selected_run = _identifier(run_id, _RUN_ID, operation)
        selected_limit = _bounded_limit(limit, operation)
        after_stream = -1
        after = ""
        if (after_stream_index is None) != (after_decode_index is None):
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        if after_stream_index is not None and after_decode_index is not None:
            if type(after_stream_index) is not int or not 0 <= after_stream_index <= 2**31 - 1:
                _fail(PortErrorCode.INVALID_REQUEST, operation)
            if type(after_decode_index) is not str or not _UNSIGNED_DECIMAL.fullmatch(
                after_decode_index
            ):
                _fail(PortErrorCode.INVALID_REQUEST, operation)
            if len(after_decode_index) > 20 or int(after_decode_index) > 2**64 - 1:
                _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
            after_stream = after_stream_index
            after = after_decode_index
        with self._read_connection(operation) as connection:
            try:
                manifest = self._committed_run(connection, selected_run, operation)
                counts = connection.execute(
                    """SELECT
                    (
                        SELECT count(*) FROM run_records
                        WHERE run_id = ? AND record_type = 'frame'
                    ),
                    (
                        SELECT count(*) FROM run_records AS owned
                        JOIN frames AS frame ON frame.frame_id = owned.record_id
                        WHERE owned.run_id = ? AND owned.record_type = 'frame'
                          AND frame.source_id = ?
                          AND NOT EXISTS (
                            SELECT 1 FROM deletion_closure AS hidden
                            WHERE hidden.record_id = frame.frame_id
                              AND hidden.record_type = 'frame'
                          )
                    )""",
                    (selected_run, selected_run, manifest.source_id),
                ).fetchone()
                if (
                    counts is None
                    or len(counts) != 2
                    or any(type(value) is not int for value in counts)
                    or counts[0] != counts[1]
                    or manifest.outputs is None
                    or counts[0] != int(manifest.outputs.sample_count)
                ):
                    _fail(PortErrorCode.CORRUPT, operation)
                rows = connection.execute(
                    """SELECT frame.record_json FROM run_records AS owned
                    JOIN frames AS frame ON frame.frame_id = owned.record_id
                    WHERE owned.run_id = ? AND owned.record_type = 'frame'
                      AND frame.source_id = ?
                      AND (
                        frame.stream_index > ?
                        OR (
                            frame.stream_index = ?
                            AND (
                                length(frame.decode_index) > length(?)
                                OR (
                                    length(frame.decode_index) = length(?)
                                    AND frame.decode_index > ?
                                )
                            )
                        )
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM deletion_closure AS hidden
                        WHERE hidden.record_id = frame.frame_id
                          AND hidden.record_type = 'frame'
                      )
                    ORDER BY frame.stream_index,
                      length(frame.decode_index), frame.decode_index
                    LIMIT ?""",
                    (
                        selected_run,
                        manifest.source_id,
                        after_stream,
                        after_stream,
                        after,
                        after,
                        after,
                        selected_limit,
                    ),
                ).fetchall()
                records = tuple(self._decode_record(row[0], operation) for row in rows)
                if not all(
                    isinstance(record, FrameRef) and record.source_id == manifest.source_id
                    for record in records
                ):
                    _fail(PortErrorCode.CORRUPT, operation)
                for record in records:
                    self._verify_projection(connection, record, operation)
                return cast(tuple[FrameRef, ...], records)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def list_run_evidence(
        self,
        run_id: str,
        *,
        after_evidence_id: str | None = None,
        limit: int,
    ) -> tuple[EvidenceRef, ...]:
        """Return only evidence owned by one visible committed run."""

        operation = "list_run_evidence"
        selected_run = _identifier(run_id, _RUN_ID, operation)
        selected_limit = _bounded_limit(limit, operation)
        after = ""
        if after_evidence_id is not None:
            after = _identifier(after_evidence_id, _EVIDENCE_ID, operation)
        with self._read_connection(operation) as connection:
            try:
                manifest = self._committed_run(connection, selected_run, operation)
                counts = connection.execute(
                    """SELECT
                    (
                        SELECT count(*) FROM run_records
                        WHERE run_id = ? AND record_type = 'evidence'
                    ),
                    (
                        SELECT count(*) FROM run_records AS owned
                        JOIN evidence AS item ON item.evidence_id = owned.record_id
                        JOIN frames AS frame ON frame.frame_id = item.frame_id
                        WHERE owned.run_id = ? AND owned.record_type = 'evidence'
                          AND frame.source_id = ?
                          AND EXISTS (
                            SELECT 1 FROM run_records AS frame_owned
                            WHERE frame_owned.run_id = owned.run_id
                              AND frame_owned.record_id = item.frame_id
                              AND frame_owned.record_type = 'frame'
                          )
                          AND NOT EXISTS (
                            SELECT 1 FROM deletion_closure AS hidden
                            WHERE hidden.record_id IN (item.evidence_id, item.frame_id)
                          )
                    )""",
                    (selected_run, selected_run, manifest.source_id),
                ).fetchone()
                if (
                    counts is None
                    or len(counts) != 2
                    or any(type(value) is not int for value in counts)
                    or counts[0] != counts[1]
                ):
                    _fail(PortErrorCode.CORRUPT, operation)
                rows = connection.execute(
                    """SELECT item.record_json FROM run_records AS owned
                    JOIN evidence AS item ON item.evidence_id = owned.record_id
                    JOIN frames AS frame ON frame.frame_id = item.frame_id
                    WHERE owned.run_id = ? AND owned.record_type = 'evidence'
                      AND owned.record_id > ? AND frame.source_id = ?
                      AND EXISTS (
                        SELECT 1 FROM run_records AS frame_owned
                        WHERE frame_owned.run_id = owned.run_id
                          AND frame_owned.record_id = item.frame_id
                          AND frame_owned.record_type = 'frame'
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM deletion_closure AS hidden
                        WHERE hidden.record_id IN (item.evidence_id, item.frame_id)
                      )
                    ORDER BY owned.record_id LIMIT ?""",
                    (selected_run, after, manifest.source_id, selected_limit),
                ).fetchall()
                records = tuple(self._decode_record(row[0], operation) for row in rows)
                if not all(isinstance(record, EvidenceRef) for record in records):
                    _fail(PortErrorCode.CORRUPT, operation)
                for record in records:
                    self._verify_projection(connection, record, operation)
                return cast(tuple[EvidenceRef, ...], records)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def list_frames(
        self,
        source_id: str,
        *,
        stream_index: int,
        after_decode_index: str | None = None,
        limit: int,
    ) -> tuple[FrameRef, ...]:
        operation = "list_frames"
        selected_source = _identifier(source_id, _SOURCE_ID, operation)
        if type(stream_index) is not int or not 0 <= stream_index <= 2**31 - 1:
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        selected_limit = _bounded_limit(limit, operation)
        after = ""
        if after_decode_index is not None:
            if type(after_decode_index) is not str or not _UNSIGNED_DECIMAL.fullmatch(
                after_decode_index
            ):
                _fail(PortErrorCode.INVALID_REQUEST, operation)
            if len(after_decode_index) > 20 or int(after_decode_index) > 2**64 - 1:
                _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
            after = after_decode_index
        with self._read_connection(operation) as connection:
            try:
                stream = connection.execute(
                    """SELECT 1 FROM source_streams AS stream
                    WHERE stream.source_id = ? AND stream.stream_index = ?
                      AND NOT EXISTS (
                        SELECT 1 FROM deletion_closure AS hidden
                        WHERE hidden.record_id = stream.source_id
                      )""",
                    (selected_source, stream_index),
                ).fetchone()
                if stream is None:
                    _fail(PortErrorCode.NOT_FOUND, operation)
                rows = connection.execute(
                    """SELECT frame.record_json FROM frames AS frame
                    WHERE frame.source_id = ? AND frame.stream_index = ?
                      AND (
                        length(frame.decode_index) > length(?)
                        OR (
                            length(frame.decode_index) = length(?)
                            AND frame.decode_index > ?
                        )
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM deletion_closure AS hidden
                        WHERE hidden.record_id = frame.frame_id
                      )
                      AND (
                        NOT EXISTS (
                            SELECT 1 FROM run_records AS owned
                            WHERE owned.record_id = frame.frame_id
                        )
                        OR EXISTS (
                            SELECT 1 FROM run_records AS owned
                            JOIN runs AS run ON run.run_id = owned.run_id
                            WHERE owned.record_id = frame.frame_id
                              AND run.state = 'committed'
                        )
                      )
                    ORDER BY length(frame.decode_index), frame.decode_index
                    LIMIT ?""",
                    (
                        selected_source,
                        stream_index,
                        after,
                        after,
                        after,
                        selected_limit,
                    ),
                ).fetchall()
                records = tuple(self._decode_record(row[0], operation) for row in rows)
                if not all(isinstance(record, FrameRef) for record in records):
                    _fail(PortErrorCode.CORRUPT, operation)
                for record in records:
                    self._verify_projection(connection, record, operation)
                return cast(tuple[FrameRef, ...], records)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def list_evidence(self, frame_id: str, *, limit: int) -> tuple[EvidenceRef, ...]:
        operation = "list_evidence"
        selected_frame = _identifier(frame_id, _FRAME_ID, operation)
        selected_limit = _bounded_limit(limit, operation)
        with self._read_connection(operation) as connection:
            try:
                rows = connection.execute(
                    """SELECT item.record_json FROM evidence AS item
                    WHERE item.frame_id = ?
                      AND NOT EXISTS (
                        SELECT 1 FROM deletion_closure AS hidden
                        WHERE hidden.record_id = item.evidence_id
                      )
                      AND (
                        NOT EXISTS (
                            SELECT 1 FROM run_records AS owned
                            WHERE owned.record_id = item.evidence_id
                        )
                        OR EXISTS (
                            SELECT 1 FROM run_records AS owned
                            JOIN runs AS run ON run.run_id = owned.run_id
                            WHERE owned.record_id = item.evidence_id
                              AND run.state = 'committed'
                        )
                      )
                      AND (
                        NOT EXISTS (
                            SELECT 1 FROM run_records AS frame_owned
                            WHERE frame_owned.record_id = item.frame_id
                        )
                        OR EXISTS (
                            SELECT 1 FROM run_records AS frame_owned
                            JOIN runs AS frame_run ON frame_run.run_id = frame_owned.run_id
                            WHERE frame_owned.record_id = item.frame_id
                              AND frame_run.state = 'committed'
                        )
                      )
                    ORDER BY item.evidence_id LIMIT ?""",
                    (selected_frame, selected_limit),
                ).fetchall()
                records = tuple(self._decode_record(row[0], operation) for row in rows)
                if not all(isinstance(record, EvidenceRef) for record in records):
                    _fail(PortErrorCode.CORRUPT, operation)
                for record in records:
                    self._verify_projection(connection, record, operation)
                return cast(tuple[EvidenceRef, ...], records)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def _perception_record_visible(
        self,
        connection: sqlite3.Connection,
        record_id: str,
        record_type: str,
        source_id: str,
        operation: str,
    ) -> bool:
        row = connection.execute(
            """SELECT (
                EXISTS (
                    SELECT 1 FROM perception_run_records AS owned
                    JOIN runs AS run ON run.run_id = owned.run_id
                    WHERE owned.record_id = ? AND owned.record_type = ?
                      AND run.state = 'committed'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM deletion_jobs AS job
                    WHERE job.root_kind = 'source' AND job.root_id = ?
                      AND job.state = 'pending'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM perception_deletion_closure AS hidden
                    WHERE hidden.record_id = ? AND hidden.record_type = ?
                )
            )""",
            (record_id, record_type, source_id, record_id, record_type),
        ).fetchone()
        value: object = None if row is None else row[0]
        if type(value) is not int or value not in {0, 1}:
            _fail(PortErrorCode.CORRUPT, operation)
        return value == 1

    def get_perception(self, record_id: str) -> PerceptionRecord:
        """Get one visible committed Observation or Tracklet by typed identifier."""

        operation = "get_perception"
        if type(record_id) is not str:
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        if _OBSERVATION_ID.fullmatch(record_id):
            table, column, record_type = "observations", "observation_id", "observation"
        elif _TRACKLET_ID.fullmatch(record_id):
            table, column, record_type = "tracklets", "tracklet_id", "tracklet"
        else:
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        statements = {
            ("observations", "observation_id"): (
                "SELECT record_json, source_id FROM observations WHERE observation_id = ?"
            ),
            ("tracklets", "tracklet_id"): (
                "SELECT record_json, source_id FROM tracklets WHERE tracklet_id = ?"
            ),
        }
        with self._read_connection(operation) as connection:
            try:
                row = connection.execute(statements[(table, column)], (record_id,)).fetchone()
                if row is None or type(row[1]) is not str:
                    _fail(PortErrorCode.NOT_FOUND, operation)
                if not self._perception_record_visible(
                    connection, record_id, record_type, row[1], operation
                ):
                    _fail(PortErrorCode.NOT_FOUND, operation)
                record = self._decode_perception_record(row[0], operation)
                identifier = (
                    record.observation_id if isinstance(record, Observation) else record.tracklet_id
                )
                if identifier != record_id:
                    _fail(PortErrorCode.CORRUPT, operation)
                self._verify_perception_projection(connection, record, operation)
                return record
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def list_run_observations(
        self,
        run_id: str,
        *,
        stream_index: int,
        after_pts_value: str | None = None,
        after_observation_id: str | None = None,
        category: str | None = None,
        limit: int,
    ) -> tuple[Observation, ...]:
        """Page run-owned observations by exact source PTS and typed identity."""

        operation = "list_run_observations"
        selected_run = _identifier(run_id, _RUN_ID, operation)
        if type(stream_index) is not int or not 0 <= stream_index <= 2**31 - 1:
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        selected_limit = _bounded_limit(limit, operation)
        after_order = -(2**63)
        after_id = ""
        if (after_pts_value is None) != (after_observation_id is None):
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        if after_pts_value is not None and after_observation_id is not None:
            _, after_order = _signed_i64(after_pts_value, operation)
            after_id = _identifier(after_observation_id, _OBSERVATION_ID, operation)
        if category is not None and (
            type(category) is not str or not _CATEGORY.fullmatch(category)
        ):
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        with self._read_connection(operation) as connection:
            try:
                manifest = self._committed_run(connection, selected_run, operation)
                rows = connection.execute(
                    _v2_schema.LIST_RUN_OBSERVATIONS_SQL,
                    (
                        manifest.source_id,
                        stream_index,
                        category,
                        category,
                        after_order,
                        after_order,
                        after_id,
                        selected_run,
                        selected_limit,
                    ),
                ).fetchall()
                records = tuple(self._decode_perception_record(row[0], operation) for row in rows)
                if not all(isinstance(record, Observation) for record in records):
                    _fail(PortErrorCode.CORRUPT, operation)
                for record in records:
                    self._verify_perception_projection(connection, record, operation)
                return cast(tuple[Observation, ...], records)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def list_run_tracklets(
        self,
        run_id: str,
        *,
        stream_index: int,
        after_start_pts_value: str | None = None,
        after_tracklet_id: str | None = None,
        category: str | None = None,
        termination_reason: str | None = None,
        limit: int,
    ) -> tuple[Tracklet, ...]:
        """Page run-owned completed tracklets by exact start PTS and identity."""

        operation = "list_run_tracklets"
        selected_run = _identifier(run_id, _RUN_ID, operation)
        if type(stream_index) is not int or not 0 <= stream_index <= 2**31 - 1:
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        selected_limit = _bounded_limit(limit, operation)
        after_order = -(2**63)
        after_id = ""
        if (after_start_pts_value is None) != (after_tracklet_id is None):
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        if after_start_pts_value is not None and after_tracklet_id is not None:
            _, after_order = _signed_i64(after_start_pts_value, operation)
            after_id = _identifier(after_tracklet_id, _TRACKLET_ID, operation)
        for token in (category, termination_reason):
            if token is not None and (type(token) is not str or not _CATEGORY.fullmatch(token)):
                _fail(PortErrorCode.INVALID_REQUEST, operation)
        with self._read_connection(operation) as connection:
            try:
                manifest = self._committed_run(connection, selected_run, operation)
                rows = connection.execute(
                    _v2_schema.LIST_RUN_TRACKLETS_SQL,
                    (
                        manifest.source_id,
                        stream_index,
                        category,
                        category,
                        termination_reason,
                        termination_reason,
                        after_order,
                        after_order,
                        after_id,
                        selected_run,
                        selected_limit,
                    ),
                ).fetchall()
                records = tuple(self._decode_perception_record(row[0], operation) for row in rows)
                if not all(isinstance(record, Tracklet) for record in records):
                    _fail(PortErrorCode.CORRUPT, operation)
                for record in records:
                    self._verify_perception_projection(connection, record, operation)
                return cast(tuple[Tracklet, ...], records)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def list_tracklet_observations(
        self,
        tracklet_id: str,
        *,
        after_ordinal: int | None = None,
        limit: int,
    ) -> tuple[Observation, ...]:
        """Page a visible tracklet's exact ordered Observation membership."""

        operation = "list_tracklet_observations"
        selected_tracklet = _identifier(tracklet_id, _TRACKLET_ID, operation)
        selected_limit = _bounded_limit(limit, operation)
        after = -1
        if after_ordinal is not None:
            if type(after_ordinal) is not int or not 0 <= after_ordinal < 64:
                _fail(PortErrorCode.INVALID_REQUEST, operation)
            after = after_ordinal
        with self._read_connection(operation) as connection:
            try:
                row = connection.execute(
                    "SELECT source_id FROM tracklets WHERE tracklet_id = ?",
                    (selected_tracklet,),
                ).fetchone()
                if (
                    row is None
                    or type(row[0]) is not str
                    or not self._perception_record_visible(
                        connection, selected_tracklet, "tracklet", row[0], operation
                    )
                ):
                    _fail(PortErrorCode.NOT_FOUND, operation)
                rows = connection.execute(
                    _v2_schema.LIST_TRACKLET_OBSERVATIONS_SQL,
                    (selected_tracklet, after, selected_limit),
                ).fetchall()
                records = tuple(self._decode_perception_record(item[0], operation) for item in rows)
                if not all(isinstance(record, Observation) for record in records):
                    _fail(PortErrorCode.CORRUPT, operation)
                for record in records:
                    self._verify_perception_projection(connection, record, operation)
                return cast(tuple[Observation, ...], records)
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def list_selected_evidence(
        self,
        run_id: str,
        tracklet_id: str,
        *,
        after_rank: int | None = None,
        limit: int,
    ) -> tuple[PersistedEvidenceSelection, ...]:
        """Page one committed run's selected views in deterministic rank order."""

        operation = "list_selected_evidence"
        selected_run = _identifier(run_id, _RUN_ID, operation)
        selected_tracklet = _identifier(tracklet_id, _TRACKLET_ID, operation)
        selected_limit = _bounded_limit(limit, operation)
        after = 0
        if after_rank is not None:
            if type(after_rank) is not int or not 1 <= after_rank <= 8:
                _fail(PortErrorCode.INVALID_REQUEST, operation)
            after = after_rank
        with self._read_connection(operation) as connection:
            try:
                manifest = self._committed_run(connection, selected_run, operation)
                tracklet_row = connection.execute(
                    """SELECT tracklet.record_json FROM tracklets AS tracklet
                    JOIN perception_run_records AS owned
                      ON owned.record_id = tracklet.tracklet_id
                    WHERE owned.run_id = ? AND owned.record_type = 'tracklet'
                      AND tracklet.tracklet_id = ?""",
                    (selected_run, selected_tracklet),
                ).fetchone()
                if tracklet_row is None:
                    _fail(PortErrorCode.NOT_FOUND, operation)
                tracklet = self._decode_perception_record(tracklet_row[0], operation)
                if not isinstance(tracklet, Tracklet) or tracklet.source_id != manifest.source_id:
                    _fail(PortErrorCode.CORRUPT, operation)
                self._verify_perception_projection(connection, tracklet, operation)
                rows = connection.execute(
                    _v2_schema.LIST_SELECTED_EVIDENCE_RANKS_SQL,
                    (selected_run, selected_tracklet, after, selected_limit),
                ).fetchall()
                result = tuple(
                    self._verify_evidence_selection(
                        connection,
                        selected_run,
                        selected_tracklet,
                        row[0],
                        operation,
                        require_artifact_intent=False,
                    )
                    for row in rows
                )
                return result
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def _decode_perception_record(self, value: object, operation: str) -> PerceptionRecord:
        if type(value) is not bytes:
            _fail(PortErrorCode.CORRUPT, operation)
        record: PerceptionRecord | None = None
        with suppress(RecordValidationError, TypeError, ValueError):
            record = loads_perception_record(value)
        if record is None:
            _fail(PortErrorCode.CORRUPT, operation)
        return record

    def _decode_evidence_intent(self, value: object, operation: str) -> EvidenceIntent:
        if type(value) is not bytes:
            _fail(PortErrorCode.CORRUPT, operation)
        intent: EvidenceIntent | None = None
        with suppress(RecordValidationError, TypeError, ValueError):
            intent = loads_evidence_intent(value)
        if intent is None:
            _fail(PortErrorCode.CORRUPT, operation)
        return intent

    def _verify_perception_projection(
        self,
        connection: sqlite3.Connection,
        record: PerceptionRecord,
        operation: str,
    ) -> None:
        encoded = dumps_perception_record(record)
        digest = hashlib.sha256(encoded).hexdigest()
        if isinstance(record, Observation):
            row = connection.execute(
                """SELECT source_id, frame_id, stream_index, pts_value, pts_order,
                pts_time_base_numerator, pts_time_base_denominator, category,
                confidence_millionths, producer_name, producer_version,
                producer_configuration_sha256, schema_version, identity_version,
                record_json, record_sha256 FROM observations WHERE observation_id = ?""",
                (record.observation_id,),
            ).fetchone()
            observation_expected = (
                record.source_id,
                record.frame_id,
                record.stream_index,
                record.pts.value,
                int(record.pts.value),
                record.pts.time_base.numerator,
                record.pts.time_base.denominator,
                record.category,
                record.confidence_millionths,
                record.producer.name,
                record.producer.version,
                record.producer.configuration_sha256,
                record.schema_version,
                record.identity_version,
                encoded,
                digest,
            )
            if row != observation_expected:
                _fail(PortErrorCode.CORRUPT, operation)
            self._validate_observation_frame(
                connection,
                record,
                operation,
                error_code=PortErrorCode.CORRUPT,
            )
            return
        row = connection.execute(
            """SELECT source_id, stream_index, category, termination_reason,
            producer_name, producer_version, producer_configuration_sha256,
            start_pts_value, start_pts_order, start_pts_time_base_numerator,
            start_pts_time_base_denominator, end_pts_value, end_pts_order,
            end_pts_time_base_numerator, end_pts_time_base_denominator,
            point_count, schema_version, identity_version, record_json, record_sha256
            FROM tracklets WHERE tracklet_id = ?""",
            (record.tracklet_id,),
        ).fetchone()
        tracklet_expected = (
            record.source_id,
            record.stream_index,
            record.category,
            record.termination_reason,
            record.producer.name,
            record.producer.version,
            record.producer.configuration_sha256,
            record.start_pts.value,
            int(record.start_pts.value),
            record.start_pts.time_base.numerator,
            record.start_pts.time_base.denominator,
            record.end_pts.value,
            int(record.end_pts.value),
            record.end_pts.time_base.numerator,
            record.end_pts.time_base.denominator,
            len(record.points),
            record.schema_version,
            record.identity_version,
            encoded,
            digest,
        )
        points = connection.execute(
            """SELECT ordinal, observation_id, frame_id, pts_value, pts_order
            FROM tracklet_points WHERE tracklet_id = ? ORDER BY ordinal LIMIT 65""",
            (record.tracklet_id,),
        ).fetchall()
        expected_points = [
            (
                ordinal,
                point.observation_id,
                point.frame_id,
                point.pts.value,
                int(point.pts.value),
            )
            for ordinal, point in enumerate(record.points)
        ]
        if row != tracklet_expected or points != expected_points:
            _fail(PortErrorCode.CORRUPT, operation)
        for point in record.points:
            observation_row = connection.execute(
                "SELECT record_json FROM observations WHERE observation_id = ?",
                (point.observation_id,),
            ).fetchone()
            if observation_row is None:
                _fail(PortErrorCode.CORRUPT, operation)
            observation = self._decode_perception_record(observation_row[0], operation)
            if not isinstance(observation, Observation):
                _fail(PortErrorCode.CORRUPT, operation)
            self._verify_perception_projection(connection, observation, operation)
            if point != TrackPoint.from_observation(observation):
                _fail(PortErrorCode.CORRUPT, operation)

    def _verify_evidence_selection(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        tracklet_id: str,
        rank: object,
        operation: str,
        *,
        require_artifact_intent: bool,
    ) -> PersistedEvidenceSelection:
        if type(rank) is not int:
            _fail(PortErrorCode.CORRUPT, operation)
        row = connection.execute(
            """SELECT observation_id, source_id, frame_id, stream_index,
            selector_name, selector_version, selector_configuration_sha256,
            intent_json, intent_sha256, evidence_id FROM selected_evidence
            WHERE run_id = ? AND tracklet_id = ? AND rank = ?""",
            (run_id, tracklet_id, rank),
        ).fetchone()
        if row is None:
            _fail(PortErrorCode.CORRUPT, operation)
        intent = self._decode_evidence_intent(row[7], operation)
        expected_prefix = (
            intent.observation_id,
            intent.source_id,
            intent.frame_id,
            intent.stream_index,
            intent.selector.name,
            intent.selector.version,
            intent.selector.configuration_sha256,
            dumps_evidence_intent(intent),
            hashlib.sha256(dumps_evidence_intent(intent)).hexdigest(),
        )
        if row[:9] != expected_prefix or intent.tracklet_id != tracklet_id or intent.rank != rank:
            _fail(PortErrorCode.CORRUPT, operation)
        tracklet_row = connection.execute(
            "SELECT record_json FROM tracklets WHERE tracklet_id = ?",
            (tracklet_id,),
        ).fetchone()
        observation_row = connection.execute(
            "SELECT record_json FROM observations WHERE observation_id = ?",
            (intent.observation_id,),
        ).fetchone()
        ownership = connection.execute(
            """SELECT count(*) FROM perception_run_records WHERE run_id = ? AND (
                (record_id = ? AND record_type = 'tracklet') OR
                (record_id = ? AND record_type = 'observation')
            )""",
            (run_id, tracklet_id, intent.observation_id),
        ).fetchone()
        if tracklet_row is None or observation_row is None or ownership != (2,):
            _fail(PortErrorCode.CORRUPT, operation)
        tracklet = self._decode_perception_record(tracklet_row[0], operation)
        observation = self._decode_perception_record(observation_row[0], operation)
        if not isinstance(tracklet, Tracklet) or not isinstance(observation, Observation):
            _fail(PortErrorCode.CORRUPT, operation)
        self._verify_perception_projection(connection, tracklet, operation)
        self._verify_perception_projection(connection, observation, operation)
        point_index = intent.score.point_index
        if (
            point_index >= len(tracklet.points)
            or tracklet.points[point_index].observation_id != intent.observation_id
            or intent.score.point_count != len(tracklet.points)
            or intent.source_id != observation.source_id
            or intent.frame_id != observation.frame_id
            or intent.stream_index != observation.stream_index
            or intent.pts != observation.pts
            or intent.geometry != observation.geometry
            or intent.score.confidence_millionths != observation.confidence_millionths
        ):
            _fail(PortErrorCode.CORRUPT, operation)
        evidence: EvidenceRef | None = None
        evidence_id = row[9]
        if evidence_id is not None:
            if type(evidence_id) is not str or not _EVIDENCE_ID.fullmatch(evidence_id):
                _fail(PortErrorCode.CORRUPT, operation)
            evidence_row = connection.execute(
                """SELECT item.record_json FROM evidence AS item
                JOIN run_records AS owned ON owned.record_id = item.evidence_id
                WHERE owned.run_id = ? AND owned.record_type = 'evidence'
                  AND item.evidence_id = ?""",
                (run_id, evidence_id),
            ).fetchone()
            if evidence_row is None:
                _fail(PortErrorCode.CORRUPT, operation)
            decoded = self._decode_record(evidence_row[0], operation)
            if not isinstance(decoded, EvidenceRef):
                _fail(PortErrorCode.CORRUPT, operation)
            self._verify_projection(connection, decoded, operation)
            evidence = decoded
            if (
                evidence.frame_id != intent.frame_id
                or evidence.geometry != intent.geometry
                or evidence.kind != intent.kind
                or evidence.retention != intent.retention
            ):
                _fail(PortErrorCode.CORRUPT, operation)
            if require_artifact_intent:
                artifact_intent = connection.execute(
                    """SELECT 1 FROM artifact_intents WHERE run_id = ?
                    AND artifact_digest = ? AND byte_count = ? AND media_type = ?
                    AND protocol_version = ?""",
                    (
                        run_id,
                        evidence.artifact.sha256,
                        evidence.artifact.bytes,
                        evidence.artifact.media_type,
                        WORLD_PROTOCOL_VERSION,
                    ),
                ).fetchone()
                if artifact_intent is None:
                    _fail(PortErrorCode.CONFLICT, operation)
        try:
            return PersistedEvidenceSelection(run_id, intent, evidence)
        except (RecordValidationError, TypeError, ValueError):
            _fail(PortErrorCode.CORRUPT, operation)
        raise AssertionError("unreachable")

    def _verify_perception_run(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        operation: str,
        *,
        require_publication: bool,
    ) -> None:
        run = connection.execute(
            "SELECT source_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None or type(run[0]) is not str:
            _fail(PortErrorCode.CORRUPT, operation)
        rows = connection.execute(
            """SELECT record_id, record_type FROM perception_run_records
            WHERE run_id = ? ORDER BY record_type, record_id LIMIT ?""",
            (run_id, self._max_audit_records + 1),
        ).fetchall()
        if len(rows) > self._max_audit_records:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        point_count = connection.execute(
            """SELECT count(*) FROM (
                SELECT 1 FROM perception_run_records AS owned
                JOIN tracklet_points AS point ON point.tracklet_id = owned.record_id
                WHERE owned.run_id = ? AND owned.record_type = 'tracklet'
                LIMIT ?
            )""",
            (run_id, self._max_audit_records + 1),
        ).fetchone()
        if (
            point_count is None
            or type(point_count[0]) is not int
            or point_count[0] > self._max_audit_records
        ):
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        for record_id, record_type in rows:
            if record_type == "observation":
                row = connection.execute(
                    "SELECT record_json FROM observations WHERE observation_id = ?",
                    (record_id,),
                ).fetchone()
            elif record_type == "tracklet":
                row = connection.execute(
                    "SELECT record_json FROM tracklets WHERE tracklet_id = ?", (record_id,)
                ).fetchone()
            else:
                _fail(PortErrorCode.CORRUPT, operation)
            if row is None:
                _fail(PortErrorCode.CORRUPT, operation)
            record = self._decode_perception_record(row[0], operation)
            if record.source_id != run[0]:
                _fail(PortErrorCode.CORRUPT, operation)
            self._verify_perception_projection(connection, record, operation)
            if isinstance(record, Tracklet):
                for point in record.points:
                    owned = connection.execute(
                        """SELECT 1 FROM perception_run_records WHERE run_id = ?
                        AND record_id = ? AND record_type = 'observation'""",
                        (run_id, point.observation_id),
                    ).fetchone()
                    if owned is None:
                        _fail(PortErrorCode.CORRUPT, operation)
        reused = connection.execute(
            """SELECT point.observation_id FROM perception_run_records AS owned
            JOIN tracklet_points AS point ON point.tracklet_id = owned.record_id
            WHERE owned.run_id = ? AND owned.record_type = 'tracklet'
            GROUP BY point.observation_id HAVING count(*) > 1 LIMIT 1""",
            (run_id,),
        ).fetchone()
        if reused is not None:
            _fail(PortErrorCode.CORRUPT, operation)
        selection_rows = connection.execute(
            """SELECT tracklet_id, rank FROM selected_evidence WHERE run_id = ?
            ORDER BY tracklet_id, rank LIMIT ?""",
            (run_id, self._max_audit_records + 1),
        ).fetchall()
        if len(selection_rows) > self._max_audit_records:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        grouped: dict[str, list[int]] = {}
        selectors: dict[str, tuple[str, str, str]] = {}
        for tracklet_id, rank in selection_rows:
            selection = self._verify_evidence_selection(
                connection,
                run_id,
                tracklet_id,
                rank,
                operation,
                require_artifact_intent=require_publication,
            )
            grouped.setdefault(tracklet_id, []).append(rank)
            selector = selection.intent.selector
            selected_producer = (
                selector.name,
                selector.version,
                selector.configuration_sha256,
            )
            previous = selectors.setdefault(tracklet_id, selected_producer)
            if previous != selected_producer:
                _fail(
                    PortErrorCode.CONFLICT if require_publication else PortErrorCode.CORRUPT,
                    operation,
                )
        for ranks in grouped.values():
            if ranks != list(range(1, len(ranks) + 1)):
                _fail(
                    PortErrorCode.CONFLICT if require_publication else PortErrorCode.CORRUPT,
                    operation,
                )

    def _decode_record(self, value: object, operation: str) -> Record:
        if type(value) is not bytes:
            _fail(PortErrorCode.CORRUPT, operation)
        try:
            return loads_record(value)
        except (RecordValidationError, TypeError, ValueError):
            _fail(PortErrorCode.CORRUPT, operation)
        raise AssertionError("unreachable")

    def _verify_projection(
        self,
        connection: sqlite3.Connection,
        record: Record,
        operation: str,
    ) -> None:
        encoded = dumps_record(record)
        digest = hashlib.sha256(encoded).hexdigest()
        if isinstance(record, Source):
            row = connection.execute(
                """SELECT schema_version, identity_version, record_json, record_sha256
                FROM sources WHERE source_id = ?""",
                (record.source_id,),
            ).fetchone()
            streams = connection.execute(
                """SELECT stream_index, width, height, rotation_degrees,
                time_base_numerator, time_base_denominator FROM source_streams
                WHERE source_id = ? ORDER BY stream_index""",
                (record.source_id,),
            ).fetchall()
            expected_streams = sorted(
                (
                    stream.stream_index,
                    stream.width,
                    stream.height,
                    stream.rotation_degrees,
                    stream.time_base.numerator,
                    stream.time_base.denominator,
                )
                for stream in record.streams
            )
            if streams != expected_streams:
                _fail(PortErrorCode.CORRUPT, operation)
        elif isinstance(record, FrameRef):
            row = connection.execute(
                """SELECT source_id, stream_index, decode_index, pts_value,
                pts_time_base_numerator, pts_time_base_denominator,
                schema_version, identity_version, record_json, record_sha256
                FROM frames WHERE frame_id = ?""",
                (record.frame_id,),
            ).fetchone()
            expected_prefix: tuple[object, ...] = (
                record.source_id,
                record.stream_index,
                record.decode_index,
                record.pts.value,
                record.pts.time_base.numerator,
                record.pts.time_base.denominator,
            )
        elif isinstance(record, EvidenceRef):
            row = connection.execute(
                """SELECT frame_id, artifact_digest, schema_version, identity_version,
                record_json, record_sha256 FROM evidence WHERE evidence_id = ?""",
                (record.evidence_id,),
            ).fetchone()
            expected_prefix = (record.frame_id, record.artifact.sha256)
            catalog = connection.execute(
                """SELECT byte_count, media_type, layout_version FROM artifact_catalog
                WHERE digest = ?""",
                (record.artifact.sha256,),
            ).fetchone()
            reference = connection.execute(
                """SELECT artifact_digest FROM artifact_references
                WHERE evidence_id = ?""",
                (record.evidence_id,),
            ).fetchone()
            if catalog != (
                record.artifact.bytes,
                record.artifact.media_type,
                WORLD_PROTOCOL_VERSION,
            ) or reference != (record.artifact.sha256,):
                _fail(PortErrorCode.CORRUPT, operation)
        else:
            row = connection.execute(
                """SELECT source_id, state, schema_version, identity_version,
                record_json, record_sha256 FROM runs WHERE run_id = ?""",
                (record.run_id,),
            ).fetchone()
            expected_prefix = (record.source_id, record.state)
        common = (
            record.schema_version,
            record.identity_version,
            encoded,
            digest,
        )
        expected = common if isinstance(record, Source) else expected_prefix + common
        if row != expected:
            _fail(PortErrorCode.CORRUPT, operation)

    def verify(self) -> WorldStoreStats:
        """Validate bounded canonical records, projections, and reference edges."""

        operation = "verify"
        with self._read_connection(operation) as connection:
            try:
                table_queries = (
                    "SELECT record_json FROM sources ORDER BY source_id LIMIT ?",
                    "SELECT record_json FROM frames ORDER BY frame_id LIMIT ?",
                    "SELECT record_json FROM evidence ORDER BY evidence_id LIMIT ?",
                    "SELECT record_json FROM runs ORDER BY run_id LIMIT ?",
                )
                rows: list[tuple[object, ...]] = []
                remaining = self._max_audit_records + 1
                for query in table_queries:
                    selected = connection.execute(query, (remaining,)).fetchall()
                    rows.extend(selected)
                    remaining = self._max_audit_records + 1 - len(rows)
                    if remaining <= 0:
                        break
                if len(rows) > self._max_audit_records:
                    _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
                for row in rows:
                    record = self._decode_record(row[0], operation)
                    self._verify_projection(connection, record, operation)
                perception_rows: list[tuple[object, ...]] = []
                for query in (
                    "SELECT record_json FROM observations ORDER BY observation_id LIMIT ?",
                    "SELECT record_json FROM tracklets ORDER BY tracklet_id LIMIT ?",
                ):
                    remaining = self._max_audit_records + 1 - len(rows) - len(perception_rows)
                    if remaining <= 0:
                        break
                    perception_rows.extend(connection.execute(query, (remaining,)).fetchall())
                if len(rows) + len(perception_rows) > self._max_audit_records:
                    _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
                for row in perception_rows:
                    perception_record = self._decode_perception_record(row[0], operation)
                    self._verify_perception_projection(connection, perception_record, operation)
                self._verify_coordination(connection, operation)
                artifacts = connection.execute(
                    """SELECT count(*) FROM (
                        SELECT 1 FROM artifact_catalog LIMIT ?
                    )""",
                    (self._max_audit_records + 1,),
                ).fetchone()
                intents = connection.execute(
                    """SELECT count(*) FROM (
                        SELECT 1 FROM artifact_intents LIMIT ?
                    )""",
                    (self._max_audit_records + 1,),
                ).fetchone()
                perception_counts = connection.execute(
                    """SELECT
                    (SELECT count(*) FROM observations),
                    (SELECT count(*) FROM tracklets),
                    (SELECT count(*) FROM selected_evidence)"""
                ).fetchone()
                if (
                    artifacts is None
                    or intents is None
                    or perception_counts is None
                    or artifacts[0] > self._max_audit_records
                    or intents[0] > self._max_audit_records
                    or any(
                        type(value) is not int or value > self._max_audit_records
                        for value in perception_counts
                    )
                ):
                    _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
                if type(artifacts[0]) is not int or type(intents[0]) is not int:
                    _fail(PortErrorCode.CORRUPT, operation)
                return WorldStoreStats(
                    len(rows) + len(perception_rows),
                    artifacts[0],
                    intents[0],
                    observation_count=perception_counts[0],
                    tracklet_count=perception_counts[1],
                    selection_count=perception_counts[2],
                )
            except PortError:
                raise
            except sqlite3.Error as error:
                _sqlite_error(error, operation)
        raise AssertionError("unreachable")

    def _verify_coordination(
        self,
        connection: sqlite3.Connection,
        operation: str,
    ) -> None:
        counts = connection.execute(
            """SELECT
                (SELECT count(*) FROM (
                    SELECT 1 FROM run_records LIMIT ?
                )),
                (SELECT count(*) FROM (
                    SELECT 1 FROM artifact_intents LIMIT ?
                )),
                (SELECT count(*) FROM (
                    SELECT 1 FROM perception_run_records LIMIT ?
                )),
                (SELECT count(*) FROM (
                    SELECT 1 FROM selected_evidence LIMIT ?
                )),
                (SELECT count(*) FROM (
                    SELECT 1 FROM tracklet_points LIMIT ?
                ))""",
            (
                self._max_audit_records + 1,
                self._max_audit_records + 1,
                self._max_audit_records + 1,
                self._max_audit_records + 1,
                self._max_audit_records + 1,
            ),
        ).fetchone()
        if counts is None or any(
            type(value) is not int or value > self._max_audit_records for value in counts
        ):
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        invalid_ownership = connection.execute(
            """SELECT count(*) FROM run_records AS owned
            JOIN runs AS run ON run.run_id = owned.run_id
            LEFT JOIN frames AS frame
              ON owned.record_type = 'frame' AND frame.frame_id = owned.record_id
            LEFT JOIN evidence AS item
              ON owned.record_type = 'evidence' AND item.evidence_id = owned.record_id
            LEFT JOIN frames AS evidence_frame ON evidence_frame.frame_id = item.frame_id
            WHERE (
                owned.record_type = 'frame'
                AND (frame.frame_id IS NULL OR frame.source_id != run.source_id)
            ) OR (
                owned.record_type = 'evidence'
                AND (
                    item.evidence_id IS NULL
                    OR evidence_frame.source_id != run.source_id
                    OR NOT EXISTS (
                        SELECT 1 FROM run_records AS frame_owned
                        WHERE frame_owned.run_id = owned.run_id
                          AND frame_owned.record_id = item.frame_id
                          AND frame_owned.record_type = 'frame'
                    )
                )
            )"""
        ).fetchone()
        if invalid_ownership is None or invalid_ownership[0] != 0:
            _fail(PortErrorCode.CORRUPT, operation)
        orphaned_perception = connection.execute(
            """SELECT 1 WHERE
                EXISTS (
                    SELECT 1 FROM observations AS observation
                    WHERE NOT EXISTS (
                        SELECT 1 FROM perception_run_records AS owned
                        WHERE owned.record_id = observation.observation_id
                          AND owned.record_type = 'observation'
                    )
                )
                OR EXISTS (
                    SELECT 1 FROM tracklets AS tracklet
                    WHERE NOT EXISTS (
                        SELECT 1 FROM perception_run_records AS owned
                        WHERE owned.record_id = tracklet.tracklet_id
                          AND owned.record_type = 'tracklet'
                    )
                )"""
        ).fetchone()
        if orphaned_perception is not None:
            _fail(PortErrorCode.CORRUPT, operation)
        run_rows = connection.execute(
            "SELECT run_id FROM runs ORDER BY run_id LIMIT ?",
            (self._max_audit_records + 1,),
        ).fetchall()
        if len(run_rows) > self._max_audit_records:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        for (run_id,) in run_rows:
            if type(run_id) is not str or not _RUN_ID.fullmatch(run_id):
                _fail(PortErrorCode.CORRUPT, operation)
            self._verify_perception_run(
                connection,
                run_id,
                operation,
                require_publication=False,
            )
        rows = connection.execute(
            """SELECT intent.run_id, intent.staging_name, intent.artifact_digest,
            intent.byte_count, intent.media_type, intent.protocol_version, run.state
            FROM artifact_intents AS intent
            JOIN runs AS run ON run.run_id = intent.run_id
            ORDER BY intent.run_id, intent.staging_name"""
        ).fetchall()
        try:
            for row in rows:
                if row[6] not in {"preparing", "failed", "cancelled"}:
                    _fail(PortErrorCode.CORRUPT, operation)
                StageHandle(
                    row[0],
                    row[1],
                    Artifact(row[2], row[3], row[4]),
                    row[5],
                )
        except (TypeError, ValueError):
            _fail(PortErrorCode.CORRUPT, operation)
        deletion_rows = connection.execute(
            """SELECT deletion_id, state, record_count, artifact_count,
            shared_retention_count, completed_at_utc, protocol_version,
            root_kind, root_id FROM deletion_jobs ORDER BY deletion_id LIMIT ?""",
            (self._max_audit_records + 1,),
        ).fetchall()
        if len(deletion_rows) > self._max_audit_records:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        overlap = connection.execute(
            """SELECT 1 WHERE EXISTS (
                SELECT 1 FROM deletion_closure GROUP BY record_id
                HAVING count(*) > 1
            ) OR EXISTS (
                SELECT 1 FROM perception_deletion_closure GROUP BY record_id
                HAVING count(*) > 1
            )"""
        ).fetchone()
        if overlap is not None:
            _fail(PortErrorCode.CORRUPT, operation)
        record_queries = {
            "source": "SELECT 1 FROM sources WHERE source_id = ?",
            "frame": "SELECT 1 FROM frames WHERE frame_id = ?",
            "evidence": "SELECT 1 FROM evidence WHERE evidence_id = ?",
            "run": "SELECT 1 FROM runs WHERE run_id = ?",
            "observation": "SELECT 1 FROM observations WHERE observation_id = ?",
            "tracklet": "SELECT 1 FROM tracklets WHERE tracklet_id = ?",
        }
        for row in deletion_rows:
            status = self._deletion_status_row(row[:7], operation)
            root_kind, root_id = row[7], row[8]
            closure = connection.execute(
                """SELECT record_id, record_type FROM deletion_closure
                WHERE deletion_id = ? ORDER BY record_id LIMIT ?""",
                (status.deletion_id, self._max_audit_records + 1),
            ).fetchall()
            perception_closure = connection.execute(
                """SELECT record_id, record_type FROM perception_deletion_closure
                WHERE deletion_id = ? ORDER BY record_id LIMIT ?""",
                (status.deletion_id, self._max_audit_records + 1),
            ).fetchall()
            artifacts = connection.execute(
                """SELECT artifact_digest, delete_required FROM deletion_artifacts
                WHERE deletion_id = ? ORDER BY artifact_digest LIMIT ?""",
                (status.deletion_id, self._max_audit_records + 1),
            ).fetchall()
            if (
                len(closure) > self._max_audit_records
                or len(perception_closure) > self._max_audit_records
                or len(closure) + len(perception_closure) > self._max_audit_records
                or len(artifacts) > self._max_audit_records
            ):
                _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
            if status.state is DeletionState.PENDING:
                self._load_deletion_plan(connection, status.deletion_id, operation)
                if (
                    root_kind != "source"
                    or type(root_id) is not str
                    or not _SOURCE_ID.fullmatch(root_id)
                    or (root_id, "source") not in closure
                    or status.record_count != len(closure) + len(perception_closure)
                    or status.artifact_count
                    != sum(delete_required == 1 for _, delete_required in artifacts)
                    or status.shared_retention_count
                    != sum(delete_required == 0 for _, delete_required in artifacts)
                ):
                    _fail(PortErrorCode.CORRUPT, operation)
                for record_id, record_type in (*closure, *perception_closure):
                    if type(record_id) is not str or record_type not in record_queries:
                        _fail(PortErrorCode.CORRUPT, operation)
                    if (
                        connection.execute(
                            record_queries[record_type],
                            (record_id,),
                        ).fetchone()
                        is None
                    ):
                        _fail(PortErrorCode.CORRUPT, operation)
                for digest, delete_required in artifacts:
                    if (
                        type(digest) is not str
                        or delete_required not in {0, 1}
                        or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    ):
                        _fail(PortErrorCode.CORRUPT, operation)
                    self._artifact_spec(connection, digest, operation)
            elif (
                root_kind is not None
                or root_id is not None
                or closure
                or perception_closure
                or artifacts
            ):
                _fail(PortErrorCode.CORRUPT, operation)


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_MAX_AUDIT_RECORDS",
    "WORLD_PROTOCOL_VERSION",
    "WORLD_SCHEMA_VERSION",
    "DeletionArtifact",
    "DeletionPlan",
    "DeletionState",
    "DeletionStatus",
    "EvidenceSelectionLink",
    "LocalWorldStore",
    "PersistedEvidenceSelection",
    "RunCleanupPlan",
    "WorldStoreStats",
]
