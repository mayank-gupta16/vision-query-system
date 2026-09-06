# SPDX-License-Identifier: Apache-2.0
"""Private local SQLite metadata store for version-1 ingestion records."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, cast

from visualworld import storage as _filesystem
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
from visualworld.ports import (
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    PortError,
    PortErrorCode,
    PortKind,
)
from visualworld.storage import (
    DEFAULT_LOCK_TIMEOUT_MS,
    EvidenceWriterSession,
    LocalEvidenceStore,
    StageHandle,
)

DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_MAX_AUDIT_RECORDS = 4_096
WORLD_SCHEMA_VERSION = 1
WORLD_PROTOCOL_VERSION = 1

_DATABASE_NAME = "world.sqlite3"
_DATABASE_COMPANIONS = ("world.sqlite3-wal", "world.sqlite3-shm")
_UNEXPECTED_DATABASE_FILES = ("world.sqlite3-journal",)
_RECORD_ID = re.compile(r"(?:src|frm|evi|run)_[0-9a-f]{64}\Z")
_SOURCE_ID = re.compile(r"src_[0-9a-f]{64}\Z")
_FRAME_ID = re.compile(r"frm_[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"run_[0-9a-f]{64}\Z")
_STAGING_NAME = re.compile(r"[0-9a-f]{32}\.part\Z")
_UNSIGNED_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)\Z")


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

_MIGRATION_NAME = "v1_ingestion_metadata"
_MIGRATION_CHECKSUM = hashlib.sha256("\0".join(_SCHEMA_V1).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WorldStoreStats:
    record_count: int
    artifact_count: int
    intent_count: int
    schema_version: int = WORLD_SCHEMA_VERSION

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
        ):
            raise ValueError("invalid world store statistics")


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
            "1",
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
        if version != WORLD_SCHEMA_VERSION:
            raise ValueError("unsupported migration")
        return _SCHEMA_V1

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
            for statement in self._migration_statements(WORLD_SCHEMA_VERSION):
                connection.execute(statement)
            connection.execute(
                """INSERT INTO schema_migrations(
                    schema_version, migration_name, code_sha256, applied_at_utc
                ) VALUES (?, ?, ?, ?)""",
                (
                    WORLD_SCHEMA_VERSION,
                    _MIGRATION_NAME,
                    _MIGRATION_CHECKSUM,
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

    def _validate_schema(self, connection: sqlite3.Connection, operation: str) -> None:
        expected: dict[tuple[str, str], str] = {}
        for statement in _SCHEMA_V1:
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
            if ledger != [(WORLD_SCHEMA_VERSION, _MIGRATION_NAME, _MIGRATION_CHECKSUM)]:
                _fail(PortErrorCode.CORRUPT, operation)
            user_version = connection.execute("PRAGMA user_version").fetchone()
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if user_version != (WORLD_SCHEMA_VERSION,) or quick_check != ("ok",) or foreign_keys:
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
            yield connection

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
        order = (Source, RunManifest, FrameRef, EvidenceRef)
        for record_type in order:
            for record, encoded in records:
                if isinstance(record, record_type):
                    self._write_record(connection, record, encoded, operation)

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
        limit: int = MAX_PORT_BATCH_ITEMS,
    ) -> tuple[StageHandle, ...]:
        operation = "list_artifact_intents"
        selected_run = _identifier(run_id, _RUN_ID, operation)
        selected_limit = _bounded_limit(limit, operation)
        with self._read_connection(operation) as connection:
            try:
                rows = connection.execute(
                    """SELECT staging_name, artifact_digest, byte_count,
                    media_type, protocol_version FROM artifact_intents
                    WHERE run_id = ? ORDER BY staging_name LIMIT ?""",
                    (selected_run, selected_limit),
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

    def pending_runs(self, *, limit: int = MAX_PORT_BATCH_ITEMS) -> tuple[RunManifest, ...]:
        operation = "pending_runs"
        selected_limit = _bounded_limit(limit, operation)
        with self._read_connection(operation) as connection:
            try:
                rows = connection.execute(
                    """SELECT record_json FROM runs
                    WHERE state IN ('preparing', 'failed', 'cancelled')
                    ORDER BY run_id LIMIT ?""",
                    (selected_limit,),
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
                self._verify_coordination(connection, operation)
                artifacts = connection.execute("SELECT count(*) FROM artifact_catalog").fetchone()
                intents = connection.execute("SELECT count(*) FROM artifact_intents").fetchone()
                if artifacts is None or intents is None:
                    _fail(PortErrorCode.CORRUPT, operation)
                return WorldStoreStats(len(rows), artifacts[0], intents[0])
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


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_MAX_AUDIT_RECORDS",
    "WORLD_PROTOCOL_VERSION",
    "WORLD_SCHEMA_VERSION",
    "LocalWorldStore",
    "WorldStoreStats",
]
