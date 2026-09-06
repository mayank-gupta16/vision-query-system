# SPDX-License-Identifier: Apache-2.0
"""Private local staged filesystem CAS for version-1 evidence artifacts."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import os
import re
import secrets
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NoReturn, cast

from visualworld.ingestion import Artifact
from visualworld.ports import (
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    PortError,
    PortErrorCode,
    PortKind,
)

type BinaryFile = io.FileIO

DEFAULT_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_INVENTORY_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_INVENTORY_ENTRIES = 4_096
DEFAULT_LOCK_TIMEOUT_MS = 5_000
_CHUNK_BYTES = 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"run_[0-9a-f]{64}\Z")
_STAGING_NAME = re.compile(r"[0-9a-f]{32}\.part\Z")
_SHARD = re.compile(r"[0-9a-f]{2}\Z")
_INVENTORY_TOKEN = re.compile(r"inv_[0-9a-f]{64}\Z")
_PROBE_SOURCE = ".visualworld-probe.part"
_PROBE_DESTINATION = ".visualworld-probe.ready"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
_STRUCTURE_ERRNOS = frozenset({errno.EACCES, errno.EISDIR, errno.ELOOP, errno.ENOTDIR, errno.EPERM})


def _error(
    code: PortErrorCode,
    operation: str,
    *,
    retryable: bool = False,
) -> PortError:
    return PortError(code, PortKind.EVIDENCE_STORE, operation, retryable=retryable)


def _fail(
    code: PortErrorCode,
    operation: str,
    *,
    retryable: bool = False,
) -> NoReturn:
    raise _error(code, operation, retryable=retryable) from None


def _run_id(value: object, operation: str) -> str:
    if type(value) is not str or not _RUN_ID.fullmatch(value):
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    return value


def _digest(value: object, operation: str) -> str:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    return value


def _canonical_artifact(value: object) -> Artifact:
    if (
        type(value) is not Artifact
        or type(value.sha256) is not str
        or type(value.bytes) is not str
        or type(value.media_type) is not str
    ):
        raise ValueError("invalid artifact")
    return Artifact(value.sha256, value.bytes, value.media_type)


def _artifact(value: object, operation: str) -> Artifact:
    try:
        return _canonical_artifact(value)
    except (TypeError, ValueError):
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    raise AssertionError("unreachable")


def _staging_name(value: object, operation: str) -> str:
    if type(value) is not str or not _STAGING_NAME.fullmatch(value):
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    return value


class CommitDisposition(StrEnum):
    PROMOTED = "promoted"
    DEDUPLICATED = "deduplicated"
    ALREADY_COMMITTED = "already_committed"


class DeleteDisposition(StrEnum):
    DELETED = "deleted"
    ALREADY_ABSENT = "already_absent"


class ArtifactState(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    CORRUPT = "corrupt"


class InventoryKind(StrEnum):
    ARTIFACT = "artifact"
    CORRUPT = "corrupt"
    STAGED = "staged"
    INCOMPLETE = "incomplete"
    EMPTY_RUN = "empty_run"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True, repr=False)
class StageHandle:
    run_id: str
    staging_name: str
    artifact: Artifact
    protocol_version: int = 1

    def __post_init__(self) -> None:
        try:
            _canonical_artifact(self.artifact)
        except (TypeError, ValueError):
            raise ValueError("invalid stage handle") from None
        if (
            type(self.run_id) is not str
            or not _RUN_ID.fullmatch(self.run_id)
            or type(self.staging_name) is not str
            or not _STAGING_NAME.fullmatch(self.staging_name)
            or type(self.protocol_version) is not int
            or self.protocol_version != 1
        ):
            raise ValueError("invalid stage handle")

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact": self.artifact.to_mapping(),
            "protocol_version": self.protocol_version,
            "run_id": self.run_id,
            "staging_name": self.staging_name,
        }

    def __repr__(self) -> str:
        return "StageHandle(<redacted>)"

    @classmethod
    def from_mapping(cls, value: object) -> StageHandle:
        if not isinstance(value, dict) or set(value) != {
            "artifact",
            "protocol_version",
            "run_id",
            "staging_name",
        }:
            raise ValueError("invalid stage handle")
        try:
            return cls(
                run_id=value["run_id"],
                staging_name=value["staging_name"],
                artifact=Artifact.from_mapping(value["artifact"]),
                protocol_version=value["protocol_version"],
            )
        except (TypeError, ValueError):
            raise ValueError("invalid stage handle") from None


@dataclass(frozen=True, slots=True, repr=False)
class StagingCleanupHandle:
    """Opaque identity for explicit cleanup of one incomplete stage or empty run."""

    run_id: str
    staging_name: str | None
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    protocol_version: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.run_id) is not str
            or not _RUN_ID.fullmatch(self.run_id)
            or (
                self.staging_name is not None
                and (
                    type(self.staging_name) is not str
                    or not _STAGING_NAME.fullmatch(self.staging_name)
                )
            )
            or type(self.device) is not int
            or self.device < 0
            or type(self.inode) is not int
            or self.inode < 0
            or type(self.size) is not int
            or self.size < 0
            or type(self.modified_ns) is not int
            or self.modified_ns < 0
            or type(self.changed_ns) is not int
            or self.changed_ns < 0
            or type(self.protocol_version) is not int
            or self.protocol_version != 1
        ):
            raise ValueError("invalid staging cleanup handle")

    def to_mapping(self) -> dict[str, object]:
        return {
            "device": self.device,
            "inode": self.inode,
            "size": self.size,
            "modified_ns": self.modified_ns,
            "changed_ns": self.changed_ns,
            "protocol_version": self.protocol_version,
            "run_id": self.run_id,
            "staging_name": self.staging_name,
        }

    def __repr__(self) -> str:
        return "StagingCleanupHandle(<redacted>)"

    @classmethod
    def from_mapping(cls, value: object) -> StagingCleanupHandle:
        if not isinstance(value, dict) or set(value) != {
            "device",
            "inode",
            "size",
            "modified_ns",
            "changed_ns",
            "protocol_version",
            "run_id",
            "staging_name",
        }:
            raise ValueError("invalid staging cleanup handle")
        try:
            return cls(
                run_id=value["run_id"],
                staging_name=value["staging_name"],
                device=value["device"],
                inode=value["inode"],
                size=value["size"],
                modified_ns=value["modified_ns"],
                changed_ns=value["changed_ns"],
                protocol_version=value["protocol_version"],
            )
        except (TypeError, ValueError):
            raise ValueError("invalid staging cleanup handle") from None


def _stage_handle(value: object, operation: str) -> StageHandle:
    if type(value) is not StageHandle:
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    selected_run = _run_id(value.run_id, operation)
    selected_name = _staging_name(value.staging_name, operation)
    selected_artifact = _artifact(value.artifact, operation)
    if type(value.protocol_version) is not int or value.protocol_version != 1:
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    return StageHandle(selected_run, selected_name, selected_artifact, 1)


def _cleanup_handle(value: object, operation: str) -> StagingCleanupHandle:
    if type(value) is not StagingCleanupHandle:
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    try:
        return StagingCleanupHandle(
            value.run_id,
            value.staging_name,
            value.device,
            value.inode,
            value.size,
            value.modified_ns,
            value.changed_ns,
            value.protocol_version,
        )
    except (TypeError, ValueError):
        _fail(PortErrorCode.INVALID_REQUEST, operation)
    raise AssertionError("unreachable")


@dataclass(frozen=True, slots=True, repr=False)
class CommitResult:
    artifact: Artifact
    disposition: CommitDisposition

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, Artifact) or not isinstance(
            self.disposition, CommitDisposition
        ):
            raise ValueError("invalid commit result")

    def __repr__(self) -> str:
        return f"CommitResult(<redacted>, disposition={self.disposition!r})"


@dataclass(frozen=True, slots=True, repr=False)
class ArtifactCheck:
    artifact: Artifact
    state: ArtifactState

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, Artifact) or not isinstance(self.state, ArtifactState):
            raise ValueError("invalid artifact check")

    def __repr__(self) -> str:
        return f"ArtifactCheck(<redacted>, state={self.state!r})"


@dataclass(frozen=True, slots=True, repr=False)
class InventoryEntry:
    token: str
    kind: InventoryKind
    artifact: Artifact | None = None
    stage: StageHandle | None = None
    cleanup: StagingCleanupHandle | None = None

    def __post_init__(self) -> None:
        if type(self.token) is not str or not _INVENTORY_TOKEN.fullmatch(self.token):
            raise ValueError("invalid inventory token")
        if not isinstance(self.kind, InventoryKind):
            raise ValueError("invalid inventory kind")
        valid_shape = (
            (
                self.kind is InventoryKind.ARTIFACT
                and isinstance(self.artifact, Artifact)
                and self.stage is None
                and self.cleanup is None
            )
            or (
                self.kind is InventoryKind.STAGED
                and self.artifact is None
                and isinstance(self.stage, StageHandle)
                and self.cleanup is None
            )
            or (
                self.kind is InventoryKind.INCOMPLETE
                and self.artifact is None
                and self.stage is None
                and isinstance(self.cleanup, StagingCleanupHandle)
                and self.cleanup.staging_name is not None
            )
            or (
                self.kind is InventoryKind.EMPTY_RUN
                and self.artifact is None
                and self.stage is None
                and isinstance(self.cleanup, StagingCleanupHandle)
                and self.cleanup.staging_name is None
            )
            or (
                self.kind in {InventoryKind.CORRUPT, InventoryKind.INVALID}
                and self.artifact is None
                and self.stage is None
                and self.cleanup is None
            )
        )
        if not valid_shape:
            raise ValueError("invalid inventory entry")

    def __repr__(self) -> str:
        return f"InventoryEntry(<redacted>, kind={self.kind!r})"


@dataclass(frozen=True, slots=True, repr=False)
class InventoryPage:
    entries: tuple[InventoryEntry, ...]
    next_after: str | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.entries, tuple)
            or len(self.entries) > MAX_PORT_BATCH_ITEMS
            or not all(isinstance(entry, InventoryEntry) for entry in self.entries)
            or (
                self.next_after is not None
                and (
                    not isinstance(self.next_after, str)
                    or not _INVENTORY_TOKEN.fullmatch(self.next_after)
                )
            )
        ):
            raise ValueError("invalid inventory page")

    def __repr__(self) -> str:
        cursor = "<redacted>" if self.next_after is not None else "None"
        return f"InventoryPage(entries={len(self.entries)}, next_after={cursor})"


@dataclass(frozen=True, slots=True)
class _VerifiedFile:
    artifact: Artifact
    content: bytes | None
    inode: tuple[int, int]


@dataclass(slots=True)
class _InventoryBudget:
    maximum_entries: int
    maximum_bytes: int
    entries: int = 0
    bytes: int = 0

    def account(self, operation: str, *, size: int = 0, entry: bool = True) -> None:
        if entry:
            self.entries += 1
        self.bytes += max(0, size)
        if self.entries > self.maximum_entries or self.bytes > self.maximum_bytes:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)


@dataclass(frozen=True, slots=True)
class _ScannedEntry:
    name: str
    metadata: os.stat_result | None


def _scan_directory(
    descriptor: int,
    budget: _InventoryBudget,
    operation: str,
) -> tuple[_ScannedEntry, ...]:
    entries: list[_ScannedEntry] = []
    try:
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                budget.account(operation)
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    metadata = None
                entries.append(_ScannedEntry(entry.name, metadata))
    except OSError:
        _fail(PortErrorCode.STORAGE_FAILED, operation)
    return tuple(entries)


def _close_descriptors(descriptors: list[int]) -> None:
    for descriptor in reversed(descriptors):
        with suppress(OSError):
            os.close(descriptor)


def _directory_metadata_valid(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )


def _regular_metadata_valid(metadata: os.stat_result, mode: int) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == mode
    )


def _cleanup_identity_matches(
    cleanup: StagingCleanupHandle,
    metadata: os.stat_result,
) -> bool:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    ) == (
        cleanup.device,
        cleanup.inode,
        cleanup.size,
        cleanup.modified_ns,
        cleanup.changed_ns,
    )


def _open_directory(
    parent_descriptor: int,
    name: str,
    operation: str,
    *,
    create: bool,
    missing: PortErrorCode = PortErrorCode.CORRUPT,
) -> tuple[int, bool]:
    created = False
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            pass
        except OSError:
            _fail(PortErrorCode.STORAGE_FAILED, operation)
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    except FileNotFoundError:
        _fail(missing, operation)
    except OSError as error:
        _fail(
            (
                PortErrorCode.CORRUPT
                if error.errno in _STRUCTURE_ERRNOS
                else PortErrorCode.STORAGE_FAILED
            ),
            operation,
        )
    try:
        metadata = os.fstat(descriptor)
        parent_metadata = os.fstat(parent_descriptor)
    except OSError:
        with suppress(OSError):
            os.close(descriptor)
        _fail(PortErrorCode.STORAGE_FAILED, operation)
    if created:
        try:
            os.fchmod(descriptor, 0o700)
            metadata = os.fstat(descriptor)
        except OSError:
            with suppress(OSError):
                os.close(descriptor)
            _fail(
                PortErrorCode.UNSUPPORTED if operation == "init" else PortErrorCode.STORAGE_FAILED,
                operation,
            )
    if not _directory_metadata_valid(metadata) or metadata.st_dev != parent_metadata.st_dev:
        with suppress(OSError):
            os.close(descriptor)
        _fail(PortErrorCode.CORRUPT, operation)
    if create:
        try:
            os.fsync(descriptor)
            os.fsync(parent_descriptor)
        except OSError:
            with suppress(OSError):
                os.close(descriptor)
            _fail(
                PortErrorCode.UNSUPPORTED if operation == "init" else PortErrorCode.STORAGE_FAILED,
                operation,
            )
    return descriptor, created


@contextmanager
def _directory_chain(
    parent_descriptor: int,
    names: tuple[str, ...],
    operation: str,
    *,
    create: bool = False,
    missing: PortErrorCode = PortErrorCode.CORRUPT,
) -> Iterator[int]:
    descriptors: list[int] = []
    current = parent_descriptor
    try:
        for name in names:
            current, _ = _open_directory(
                current,
                name,
                operation,
                create=create,
                missing=missing,
            )
            descriptors.append(current)
        yield current
    finally:
        _close_descriptors(descriptors)


def _open_root(path: Path, operation: str, *, create: bool) -> int:
    if not isinstance(path, Path) or not path.is_absolute() or path.name in {"", ".", ".."}:
        _fail(PortErrorCode.UNSUPPORTED, operation)
    parent_descriptor: int | None = None
    descriptor: int | None = None
    created = False
    try:
        parent_before = path.parent.lstat()
        parent_descriptor = os.open(path.parent, _DIRECTORY_FLAGS)
        parent_opened = os.fstat(parent_descriptor)
    except OSError:
        if parent_descriptor is not None:
            _close_descriptors([parent_descriptor])
        _fail(PortErrorCode.UNSUPPORTED, operation)
    active_parent = parent_descriptor
    if (
        stat.S_ISLNK(parent_before.st_mode)
        or not stat.S_ISDIR(parent_opened.st_mode)
        or (parent_before.st_dev, parent_before.st_ino)
        != (parent_opened.st_dev, parent_opened.st_ino)
    ):
        _close_descriptors([active_parent])
        _fail(PortErrorCode.UNSUPPORTED, operation)
    try:
        if create:
            try:
                os.mkdir(path.name, 0o700, dir_fd=active_parent)
                created = True
            except FileExistsError:
                pass
            except OSError:
                _fail(PortErrorCode.UNSUPPORTED, operation)
        try:
            before = os.stat(path.name, dir_fd=active_parent, follow_symlinks=False)
            descriptor = os.open(path.name, _DIRECTORY_FLAGS, dir_fd=active_parent)
            opened = os.fstat(descriptor)
        except OSError:
            _fail(PortErrorCode.UNSUPPORTED, operation)
        if created:
            try:
                os.fchmod(descriptor, 0o700)
                opened = os.fstat(descriptor)
            except OSError:
                _fail(PortErrorCode.UNSUPPORTED, operation)
        if (
            stat.S_ISLNK(before.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not _directory_metadata_valid(opened)
        ):
            _fail(PortErrorCode.UNSUPPORTED, operation)
        if create:
            try:
                # Always sync on initialization so a retry completes a root mkdir
                # whose prior parent sync failed after the namespace change.
                os.fsync(active_parent)
            except OSError:
                _fail(PortErrorCode.UNSUPPORTED, operation)
        return descriptor
    except BaseException:
        if descriptor is not None:
            _close_descriptors([descriptor])
        raise
    finally:
        _close_descriptors([active_parent])


def _open_lock(root_descriptor: int, operation: str, *, create: bool) -> BinaryFile:
    created = False
    stream: BinaryFile | None = None
    try:
        if create:
            try:
                stream = _open_file(root_descriptor, "writer.lock", "x+b", 0o600)
                created = True
            except FileExistsError:
                stream = _open_file(root_descriptor, "writer.lock", "r+b", 0)
        else:
            stream = _open_file(root_descriptor, "writer.lock", "r+b", 0)
        metadata = os.fstat(stream.fileno())
        if created:
            os.fchmod(stream.fileno(), 0o600)
            metadata = os.fstat(stream.fileno())
        if create:
            os.fsync(stream.fileno())
    except OSError:
        if stream is not None and not stream.closed:
            with suppress(OSError):
                stream.close()
        _fail(PortErrorCode.UNSUPPORTED if create else PortErrorCode.CORRUPT, operation)
    if stream is None:
        _fail(PortErrorCode.UNSUPPORTED if create else PortErrorCode.CORRUPT, operation)
    if not _regular_metadata_valid(metadata, 0o600):
        if not stream.closed:
            with suppress(OSError):
                stream.close()
        _fail(PortErrorCode.UNSUPPORTED if create else PortErrorCode.CORRUPT, operation)
    return stream


def _close_lock_owner(
    stream: BinaryFile,
    operation: str,
    poison: Callable[[BinaryFile], None],
) -> None:
    try:
        stream.close()
    except BaseException as error:
        if not stream.closed:
            with suppress(BaseException):
                stream.close()
        if not stream.closed:
            poison(stream)
        if isinstance(error, OSError):
            _fail(PortErrorCode.STORAGE_FAILED, operation)
        raise


@contextmanager
def _locked(
    root_descriptor: int,
    operation: str,
    *,
    exclusive: bool,
    timeout_ms: int,
    poison: Callable[[BinaryFile], None],
    create: bool = False,
) -> Iterator[None]:
    stream = _open_lock(root_descriptor, operation, create=create)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic_ns() + timeout_ms * 1_000_000
    try:
        while True:
            try:
                fcntl.flock(stream.fileno(), mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic_ns() >= deadline:
                    _fail(PortErrorCode.TIMEOUT, operation, retryable=True)
                time.sleep(0.01)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    if time.monotonic_ns() >= deadline:
                        _fail(PortErrorCode.TIMEOUT, operation, retryable=True)
                    time.sleep(0.01)
                    continue
                _fail(PortErrorCode.STORAGE_FAILED, operation)
        yield
    finally:
        _close_lock_owner(stream, operation, poison)


def _open_file(
    parent_descriptor: int,
    name: str,
    mode: str,
    permissions: int,
) -> BinaryFile:
    def opener(selected_name: str, flags: int) -> int:
        return os.open(
            selected_name,
            flags | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            permissions,
            dir_fd=parent_descriptor,
        )

    return io.FileIO(name, mode, opener=opener)


def _write_all(stream: BinaryFile, content: bytes) -> str:
    digest = hashlib.sha256()
    view = memoryview(content)
    offset = 0
    while offset < len(view):
        end = min(len(view), offset + _CHUNK_BYTES)
        written = stream.write(view[offset:end])
        if written is None or written <= 0:
            raise OSError("short write")
        digest.update(view[offset : offset + written])
        offset += written
    return digest.hexdigest()


def _unlink_if_owned(
    parent_descriptor: int,
    name: str,
    inode: tuple[int, int] | None,
) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISREG(metadata.st_mode) and (
            inode is None or (metadata.st_dev, metadata.st_ino) == inode
        ):
            os.unlink(name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
    except OSError:
        pass


def _verify_file(
    parent_descriptor: int,
    name: str,
    digest_name: str | None,
    expected_artifact: Artifact | None,
    operation: str,
    maximum: int,
    *,
    content: bool,
) -> _VerifiedFile | None:
    try:
        stream = _open_file(parent_descriptor, name, "rb", 0)
    except FileNotFoundError:
        return None
    except OSError as error:
        _fail(
            (
                PortErrorCode.CORRUPT
                if error.errno in _STRUCTURE_ERRNOS
                else PortErrorCode.STORAGE_FAILED
            ),
            operation,
        )
    try:
        metadata = os.fstat(stream.fileno())
        if (
            not _regular_metadata_valid(metadata, 0o400)
            or metadata.st_size > maximum
            or (expected_artifact is not None and metadata.st_size != int(expected_artifact.bytes))
        ):
            _fail(PortErrorCode.CORRUPT, operation)
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if content else None
        total = 0
        while True:
            chunk = stream.read(min(_CHUNK_BYTES, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                _fail(PortErrorCode.CORRUPT, operation)
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
        actual_digest = digest.hexdigest()
        if total != metadata.st_size or (digest_name is not None and actual_digest != digest_name):
            _fail(PortErrorCode.CORRUPT, operation)
        verified_artifact = Artifact(
            actual_digest,
            str(total),
            (
                expected_artifact.media_type
                if expected_artifact is not None
                else "application/vnd.visualworld.rgb24"
            ),
        )
        result = _VerifiedFile(
            verified_artifact,
            b"".join(chunks) if chunks is not None else None,
            (metadata.st_dev, metadata.st_ino),
        )
        try:
            stream.close()
        except OSError:
            _fail(PortErrorCode.STORAGE_FAILED, operation)
        return result
    except OSError:
        _fail(PortErrorCode.STORAGE_FAILED, operation)
    finally:
        if not stream.closed:
            with suppress(OSError):
                stream.close()


def _files_equal(
    left_parent: int,
    left_name: str,
    right_parent: int,
    right_name: str,
    expected_bytes: int,
    operation: str,
) -> bool:
    left: BinaryFile | None = None
    right: BinaryFile | None = None
    try:
        left = _open_file(left_parent, left_name, "rb", 0)
        right = _open_file(right_parent, right_name, "rb", 0)
        compared = 0
        while compared < expected_bytes:
            requested = min(_CHUNK_BYTES, expected_bytes - compared)
            left_chunk = left.read(requested)
            right_chunk = right.read(requested)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                _fail(PortErrorCode.CORRUPT, operation)
            compared += len(left_chunk)
        if left.read(1) or right.read(1):
            _fail(PortErrorCode.CORRUPT, operation)
        left.close()
        right.close()
        return True
    except OSError:
        _fail(PortErrorCode.STORAGE_FAILED, operation)
    finally:
        for stream in (right, left):
            if stream is not None and not stream.closed:
                with suppress(OSError):
                    stream.close()


def _inventory_token(kind: InventoryKind, identity: bytes) -> str:
    digest = hashlib.sha256(kind.value.encode("ascii") + b"\0" + identity).hexdigest()
    return "inv_" + digest


def _invalid_entry(kind: InventoryKind, identity: bytes) -> InventoryEntry:
    return InventoryEntry(_inventory_token(kind, identity), kind)


class LocalEvidenceStore:
    """Local version-1 staged CAS implementing the stable EvidenceStore port.

    The configured root is trusted application state. It is created when absent,
    must be absolute, private, and owned by the effective UID, and same-principal
    mutations must use the shared kernel writer lock.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_payload_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        max_inventory_entries: int = DEFAULT_MAX_INVENTORY_ENTRIES,
        max_inventory_bytes: int = DEFAULT_MAX_INVENTORY_BYTES,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    ) -> None:
        if (
            not isinstance(root, Path)
            or not root.is_absolute()
            or type(max_payload_bytes) is not int
            or not 0 <= max_payload_bytes <= 2**63 - 1
            or type(max_inventory_entries) is not int
            or not 1 <= max_inventory_entries <= 1_000_000
            or type(max_inventory_bytes) is not int
            or not 0 <= max_inventory_bytes <= 2**63 - 1
            or type(lock_timeout_ms) is not int
            or not 1 <= lock_timeout_ms <= 60_000
        ):
            _fail(PortErrorCode.INVALID_REQUEST, "init")
        self._root = root
        self._maximum = max_payload_bytes
        self._inventory_maximum_entries = max_inventory_entries
        self._inventory_maximum_bytes = max_inventory_bytes
        self._lock_timeout_ms = lock_timeout_ms
        self._poisoned = False
        self._retained_lock_streams: list[BinaryFile] = []
        self._active_writer_session: EvidenceWriterSession | None = None
        self._descriptor = CapabilityDescriptor(
            PortKind.EVIDENCE_STORE,
            "local-cas",
            "1",
            True,
            True,
            max_payload_bytes=max_payload_bytes,
        )
        self._initialize()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    def _initialize(self) -> None:
        operation = "init"
        root_descriptor = _open_root(self._root, operation, create=True)
        try:
            with _locked(
                root_descriptor,
                operation,
                exclusive=True,
                timeout_ms=self._lock_timeout_ms,
                poison=self._poison_lock,
                create=True,
            ):
                with (
                    _directory_chain(
                        root_descriptor,
                        ("artifacts", "v1"),
                        operation,
                        create=True,
                    ) as artifacts_v1,
                    _directory_chain(
                        artifacts_v1,
                        ("sha256",),
                        operation,
                        create=True,
                    ) as artifacts,
                    _directory_chain(
                        root_descriptor,
                        ("staging", "v1"),
                        operation,
                        create=True,
                    ) as staging,
                ):
                    if os.fstat(artifacts).st_dev != os.fstat(staging).st_dev:
                        _fail(PortErrorCode.UNSUPPORTED, operation)
                    self._probe(staging, artifacts_v1)
                try:
                    os.fsync(root_descriptor)
                except OSError:
                    _fail(PortErrorCode.UNSUPPORTED, operation)
        finally:
            with suppress(OSError):
                os.close(root_descriptor)

    def _probe(self, staging: int, artifacts_v1: int) -> None:
        operation = "init"
        for descriptor, name in (
            (staging, _PROBE_SOURCE),
            (artifacts_v1, _PROBE_DESTINATION),
        ):
            try:
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                _fail(PortErrorCode.UNSUPPORTED, operation)
            if not _regular_metadata_valid(metadata, 0o600):
                _fail(PortErrorCode.UNSUPPORTED, operation)
            try:
                os.unlink(name, dir_fd=descriptor)
                os.fsync(descriptor)
            except OSError:
                _fail(PortErrorCode.UNSUPPORTED, operation)
        stream: BinaryFile | None = None
        moved = False
        try:
            stream = _open_file(staging, _PROBE_SOURCE, "xb", 0o600)
            os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno())
            stream.close()
            os.fsync(staging)
            os.rename(
                _PROBE_SOURCE,
                _PROBE_DESTINATION,
                src_dir_fd=staging,
                dst_dir_fd=artifacts_v1,
            )
            moved = True
            os.fsync(staging)
            os.fsync(artifacts_v1)
            os.unlink(_PROBE_DESTINATION, dir_fd=artifacts_v1)
            moved = False
            os.fsync(artifacts_v1)
        except (OSError, NotImplementedError):
            _fail(PortErrorCode.UNSUPPORTED, operation)
        finally:
            if stream is not None and not stream.closed:
                with suppress(OSError):
                    stream.close()
            _unlink_if_owned(
                artifacts_v1 if moved else staging,
                _PROBE_DESTINATION if moved else _PROBE_SOURCE,
                None,
            )

    @contextmanager
    def _operation(self, operation: str, *, exclusive: bool) -> Iterator[int]:
        if self._poisoned:
            _fail(PortErrorCode.STORAGE_FAILED, operation)
        root_descriptor = _open_root(self._root, operation, create=False)
        try:
            with _locked(
                root_descriptor,
                operation,
                exclusive=exclusive,
                timeout_ms=self._lock_timeout_ms,
                poison=self._poison_lock,
            ):
                yield root_descriptor
        finally:
            with suppress(OSError):
                os.close(root_descriptor)

    def _poison_lock(self, stream: BinaryFile) -> None:
        self._poisoned = True
        self._retained_lock_streams.append(stream)

    def _validate_put(self, artifact: object, content: object, operation: str) -> Artifact:
        selected = _artifact(artifact, operation)
        if type(content) is not bytes:
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        if len(content) > self._maximum or int(selected.bytes) > self._maximum:
            _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
        if (
            len(content) != int(selected.bytes)
            or hashlib.sha256(content).hexdigest() != selected.sha256
        ):
            _fail(PortErrorCode.CONFLICT, operation)
        return selected

    def stage(self, run_id: str, artifact: Artifact, content: bytes) -> StageHandle:
        operation = "stage"
        with self._operation(operation, exclusive=True) as root_descriptor:
            return self._stage_locked(root_descriptor, run_id, artifact, content, operation)

    def _stage_locked(
        self,
        root_descriptor: int,
        run_id: object,
        artifact: object,
        content: object,
        operation: str,
    ) -> StageHandle:
        selected_run = _run_id(run_id, operation)
        selected_artifact = self._validate_put(artifact, content, operation)
        selected_content = cast(bytes, content)
        with (
            _directory_chain(root_descriptor, ("staging", "v1"), operation) as staging,
            _directory_chain(
                staging,
                (selected_run,),
                operation,
                create=True,
            ) as run_directory,
        ):
            for _ in range(8):
                name = secrets.token_hex(16) + ".part"
                stream: BinaryFile | None = None
                created = False
                inode: tuple[int, int] | None = None
                completed = False
                try:
                    try:
                        stream = _open_file(run_directory, name, "xb", 0o600)
                        created = True
                        metadata = os.fstat(stream.fileno())
                        inode = (metadata.st_dev, metadata.st_ino)
                    except FileExistsError:
                        continue
                    except OSError:
                        _fail(PortErrorCode.STORAGE_FAILED, operation)
                    try:
                        digest = _write_all(stream, selected_content)
                        if digest != selected_artifact.sha256:
                            _fail(PortErrorCode.CONFLICT, operation)
                        os.fchmod(stream.fileno(), 0o400)
                        os.fsync(stream.fileno())
                        stream.close()
                        os.fsync(run_directory)
                    except OSError:
                        _fail(PortErrorCode.STORAGE_FAILED, operation)
                    completed = True
                    return StageHandle(selected_run, name, selected_artifact)
                finally:
                    if stream is not None and not stream.closed:
                        with suppress(OSError):
                            stream.close()
                    if created and not completed:
                        _unlink_if_owned(run_directory, name, inode)
                        with suppress(OSError):
                            self._remove_empty_run(staging, run_directory, selected_run)
            _fail(PortErrorCode.STORAGE_FAILED, operation)
        raise AssertionError("unreachable")

    def _cas_file(
        self,
        root_descriptor: int,
        digest: str,
        expected_artifact: Artifact | None,
        operation: str,
        *,
        content: bool,
        create: bool,
    ) -> tuple[_VerifiedFile | None, int | None, list[int]]:
        descriptors: list[int] = []
        current = root_descriptor
        try:
            for name in ("artifacts", "v1", "sha256"):
                current, _ = _open_directory(current, name, operation, create=False)
                descriptors.append(current)
            for shard in (digest[:2], digest[2:4]):
                try:
                    current, _ = _open_directory(
                        current,
                        shard,
                        operation,
                        create=create,
                        missing=PortErrorCode.NOT_FOUND,
                    )
                except PortError as error:
                    if error.code is PortErrorCode.NOT_FOUND:
                        return None, None, descriptors
                    raise
                descriptors.append(current)
            return (
                _verify_file(
                    current,
                    digest,
                    digest,
                    expected_artifact,
                    operation,
                    self._maximum,
                    content=content,
                ),
                current,
                descriptors,
            )
        except BaseException:
            _close_descriptors(descriptors)
            raise

    def commit_stage(self, stage: StageHandle) -> CommitResult:
        operation = "commit_stage"
        with self._operation(operation, exclusive=True) as root_descriptor:
            return self._commit_stage_locked(root_descriptor, stage, operation)

    def _commit_stage_locked(
        self,
        root_descriptor: int,
        stage: object,
        operation: str,
    ) -> CommitResult:
        selected = _stage_handle(stage, operation)
        with _directory_chain(root_descriptor, ("staging", "v1"), operation) as staging:
            run_directory: int | None = None
            try:
                try:
                    run_directory, _ = _open_directory(
                        staging,
                        selected.run_id,
                        operation,
                        create=False,
                        missing=PortErrorCode.NOT_FOUND,
                    )
                except PortError as error:
                    if error.code is not PortErrorCode.NOT_FOUND:
                        raise
                staged = (
                    _verify_file(
                        run_directory,
                        selected.staging_name,
                        selected.artifact.sha256,
                        selected.artifact,
                        operation,
                        self._maximum,
                        content=False,
                    )
                    if run_directory is not None
                    else None
                )
                final, final_parent, descriptors = self._cas_file(
                    root_descriptor,
                    selected.artifact.sha256,
                    selected.artifact,
                    operation,
                    content=False,
                    create=staged is not None,
                )
                try:
                    if staged is None:
                        if final is None:
                            _fail(PortErrorCode.NOT_FOUND, operation)
                        try:
                            os.fsync(cast(int, final_parent))
                            self._sync_stage_absence(
                                staging,
                                run_directory,
                                selected.run_id,
                            )
                        except OSError:
                            _fail(PortErrorCode.STORAGE_FAILED, operation)
                        return CommitResult(
                            selected.artifact,
                            CommitDisposition.ALREADY_COMMITTED,
                        )
                    active_run = cast(int, run_directory)
                    if final is not None:
                        if not _files_equal(
                            cast(int, final_parent),
                            selected.artifact.sha256,
                            active_run,
                            selected.staging_name,
                            int(selected.artifact.bytes),
                            operation,
                        ):
                            _fail(PortErrorCode.CONFLICT, operation)
                        os.unlink(selected.staging_name, dir_fd=active_run)
                        os.fsync(active_run)
                        os.fsync(cast(int, final_parent))
                        self._remove_empty_run(staging, active_run, selected.run_id)
                        return CommitResult(
                            selected.artifact,
                            CommitDisposition.DEDUPLICATED,
                        )
                    destination = cast(int, final_parent)
                    os.rename(
                        selected.staging_name,
                        selected.artifact.sha256,
                        src_dir_fd=active_run,
                        dst_dir_fd=destination,
                    )
                    os.fsync(active_run)
                    os.fsync(destination)
                    self._remove_empty_run(staging, active_run, selected.run_id)
                    return CommitResult(selected.artifact, CommitDisposition.PROMOTED)
                except OSError:
                    _fail(PortErrorCode.STORAGE_FAILED, operation)
                finally:
                    _close_descriptors(descriptors)
            finally:
                if run_directory is not None:
                    with suppress(OSError):
                        os.close(run_directory)
        raise AssertionError("unreachable")

    def _remove_empty_run(self, staging: int, run_directory: int, run_id: str) -> None:
        try:
            os.rmdir(run_id, dir_fd=staging)
            os.fsync(staging)
        except OSError as error:
            if error.errno == errno.ENOENT:
                os.fsync(staging)
            elif error.errno != errno.ENOTEMPTY:
                raise

    def _sync_stage_absence(
        self,
        staging: int,
        run_directory: int | None,
        run_id: str,
    ) -> None:
        if run_directory is None:
            os.fsync(staging)
            return
        os.fsync(run_directory)
        self._remove_empty_run(staging, run_directory, run_id)

    def put(self, artifact: Artifact, content: bytes) -> Artifact:
        operation = "put"
        selected = self._validate_put(artifact, content, operation)
        run_id = "run_" + secrets.token_hex(32)
        with self._operation(operation, exclusive=True) as root_descriptor:
            stage = self._stage_locked(root_descriptor, run_id, selected, content, operation)
            return self._commit_stage_locked(root_descriptor, stage, operation).artifact

    @contextmanager
    def writer_session(self) -> Iterator[EvidenceWriterSession]:
        """Hold the store lock across coordinator-owned metadata and CAS steps."""

        with self._operation("writer_session", exclusive=True) as root_descriptor:
            session = EvidenceWriterSession(self, root_descriptor)
            if self._active_writer_session is not None:
                _fail(PortErrorCode.STORAGE_FAILED, "writer_session")
            self._active_writer_session = session
            try:
                yield session
            finally:
                session._deactivate()

    def get(self, digest: str) -> bytes:
        operation = "get"
        selected_digest = _digest(digest, operation)
        with self._operation(operation, exclusive=False) as root_descriptor:
            final, _, descriptors = self._cas_file(
                root_descriptor,
                selected_digest,
                None,
                operation,
                content=True,
                create=False,
            )
            try:
                if final is None:
                    _fail(PortErrorCode.NOT_FOUND, operation)
                return cast(bytes, final.content)
            finally:
                _close_descriptors(descriptors)

    def inspect(self, artifacts: tuple[Artifact, ...]) -> tuple[ArtifactCheck, ...]:
        operation = "inspect"
        with self._operation(operation, exclusive=False) as root_descriptor:
            return self._inspect_locked(root_descriptor, artifacts, operation)

    def _inspect_locked(
        self,
        root_descriptor: int,
        artifacts: object,
        operation: str,
    ) -> tuple[ArtifactCheck, ...]:
        if type(artifacts) is not tuple or len(artifacts) > MAX_PORT_BATCH_ITEMS:
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        selected_artifacts = tuple(_artifact(artifact, operation) for artifact in artifacts)
        checks: list[ArtifactCheck] = []
        checked_bytes = 0
        for artifact in selected_artifacts:
            checked_bytes += int(artifact.bytes)
            if checked_bytes > self._inventory_maximum_bytes:
                _fail(PortErrorCode.LIMIT_EXCEEDED, operation)
            try:
                final, _, descriptors = self._cas_file(
                    root_descriptor,
                    artifact.sha256,
                    artifact,
                    operation,
                    content=False,
                    create=False,
                )
                try:
                    state = ArtifactState.VALID if final is not None else ArtifactState.MISSING
                finally:
                    _close_descriptors(descriptors)
            except PortError as error:
                if error.code is PortErrorCode.CORRUPT:
                    state = ArtifactState.CORRUPT
                else:
                    raise
            checks.append(ArtifactCheck(artifact, state))
        return tuple(checks)

    def _inventory_cas(
        self,
        root_descriptor: int,
        budget: _InventoryBudget,
    ) -> list[InventoryEntry]:
        operation = "inventory"
        entries: list[InventoryEntry] = []
        with _directory_chain(root_descriptor, ("artifacts", "v1", "sha256"), operation) as root:
            for first_entry in _scan_directory(root, budget, operation):
                first_identity = os.fsencode(first_entry.name)
                if (
                    not _SHARD.fullmatch(first_entry.name)
                    or first_entry.metadata is None
                    or not stat.S_ISDIR(first_entry.metadata.st_mode)
                ):
                    entries.append(_invalid_entry(InventoryKind.INVALID, b"a1\0" + first_identity))
                    continue
                try:
                    first, _ = _open_directory(root, first_entry.name, operation, create=False)
                except PortError as error:
                    if error.code is not PortErrorCode.CORRUPT:
                        raise
                    entries.append(_invalid_entry(InventoryKind.INVALID, b"a1\0" + first_identity))
                    continue
                try:
                    for second_entry in _scan_directory(first, budget, operation):
                        second_identity = first_identity + b"/" + os.fsencode(second_entry.name)
                        if (
                            not _SHARD.fullmatch(second_entry.name)
                            or second_entry.metadata is None
                            or not stat.S_ISDIR(second_entry.metadata.st_mode)
                        ):
                            entries.append(
                                _invalid_entry(InventoryKind.INVALID, b"a2\0" + second_identity)
                            )
                            continue
                        try:
                            second, _ = _open_directory(
                                first,
                                second_entry.name,
                                operation,
                                create=False,
                            )
                        except PortError as error:
                            if error.code is not PortErrorCode.CORRUPT:
                                raise
                            entries.append(
                                _invalid_entry(InventoryKind.INVALID, b"a2\0" + second_identity)
                            )
                            continue
                        try:
                            for file_entry in _scan_directory(second, budget, operation):
                                name = file_entry.name
                                identity = second_identity + b"/" + os.fsencode(name)
                                if not _DIGEST.fullmatch(name) or not name.startswith(
                                    first_entry.name + second_entry.name
                                ):
                                    entries.append(
                                        _invalid_entry(InventoryKind.INVALID, b"af\0" + identity)
                                    )
                                    continue
                                metadata = file_entry.metadata
                                if metadata is None:
                                    entries.append(
                                        _invalid_entry(InventoryKind.INVALID, b"af\0" + identity)
                                    )
                                    continue
                                budget.account(operation, size=metadata.st_size, entry=False)
                                artifact = Artifact(name, str(metadata.st_size))
                                try:
                                    verified = _verify_file(
                                        second,
                                        name,
                                        name,
                                        artifact,
                                        operation,
                                        self._maximum,
                                        content=False,
                                    )
                                except PortError as error:
                                    if error.code is not PortErrorCode.CORRUPT:
                                        raise
                                    verified = None
                                if verified is None:
                                    entries.append(
                                        _invalid_entry(InventoryKind.CORRUPT, b"af\0" + identity)
                                    )
                                else:
                                    entries.append(
                                        InventoryEntry(
                                            _inventory_token(InventoryKind.ARTIFACT, name.encode()),
                                            InventoryKind.ARTIFACT,
                                            artifact=artifact,
                                        )
                                    )
                        finally:
                            with suppress(OSError):
                                os.close(second)
                finally:
                    with suppress(OSError):
                        os.close(first)
        return entries

    def _inventory_staging(
        self,
        root_descriptor: int,
        budget: _InventoryBudget,
    ) -> list[InventoryEntry]:
        operation = "inventory"
        entries: list[InventoryEntry] = []
        with _directory_chain(root_descriptor, ("staging", "v1"), operation) as root:
            for run_entry in _scan_directory(root, budget, operation):
                run_identity = os.fsencode(run_entry.name)
                if (
                    not _RUN_ID.fullmatch(run_entry.name)
                    or run_entry.metadata is None
                    or not stat.S_ISDIR(run_entry.metadata.st_mode)
                ):
                    entries.append(_invalid_entry(InventoryKind.INVALID, b"sr\0" + run_identity))
                    continue
                try:
                    run, _ = _open_directory(root, run_entry.name, operation, create=False)
                except PortError as error:
                    if error.code is not PortErrorCode.CORRUPT:
                        raise
                    entries.append(_invalid_entry(InventoryKind.INVALID, b"sr\0" + run_identity))
                    continue
                try:
                    file_entries = _scan_directory(run, budget, operation)
                    if not file_entries:
                        try:
                            run_metadata = os.fstat(run)
                        except OSError:
                            _fail(PortErrorCode.STORAGE_FAILED, operation)
                        cleanup = StagingCleanupHandle(
                            run_entry.name,
                            None,
                            run_metadata.st_dev,
                            run_metadata.st_ino,
                            run_metadata.st_size,
                            run_metadata.st_mtime_ns,
                            run_metadata.st_ctime_ns,
                        )
                        entries.append(
                            InventoryEntry(
                                _inventory_token(InventoryKind.EMPTY_RUN, run_identity),
                                InventoryKind.EMPTY_RUN,
                                cleanup=cleanup,
                            )
                        )
                    for file_entry in file_entries:
                        identity = run_identity + b"/" + os.fsencode(file_entry.name)
                        metadata = file_entry.metadata
                        if metadata is None:
                            entries.append(
                                _invalid_entry(InventoryKind.INVALID, b"sf\0" + identity)
                            )
                            continue
                        budget.account(operation, size=metadata.st_size, entry=False)
                        if not _STAGING_NAME.fullmatch(
                            file_entry.name
                        ) or not _regular_metadata_valid(metadata, stat.S_IMODE(metadata.st_mode)):
                            entries.append(
                                _invalid_entry(InventoryKind.INVALID, b"sf\0" + identity)
                            )
                        elif stat.S_IMODE(metadata.st_mode) == 0o600:
                            cleanup = StagingCleanupHandle(
                                run_entry.name,
                                file_entry.name,
                                metadata.st_dev,
                                metadata.st_ino,
                                metadata.st_size,
                                metadata.st_mtime_ns,
                                metadata.st_ctime_ns,
                            )
                            entries.append(
                                InventoryEntry(
                                    _inventory_token(InventoryKind.INCOMPLETE, identity),
                                    InventoryKind.INCOMPLETE,
                                    cleanup=cleanup,
                                )
                            )
                        elif stat.S_IMODE(metadata.st_mode) == 0o400:
                            try:
                                verified = _verify_file(
                                    run,
                                    file_entry.name,
                                    None,
                                    None,
                                    operation,
                                    self._maximum,
                                    content=False,
                                )
                            except PortError as error:
                                if error.code is not PortErrorCode.CORRUPT:
                                    raise
                                entries.append(
                                    _invalid_entry(InventoryKind.CORRUPT, b"sf\0" + identity)
                                )
                                continue
                            if verified is None:
                                entries.append(
                                    _invalid_entry(InventoryKind.CORRUPT, b"sf\0" + identity)
                                )
                                continue
                            artifact = verified.artifact
                            stage = StageHandle(run_entry.name, file_entry.name, artifact)
                            entries.append(
                                InventoryEntry(
                                    _inventory_token(InventoryKind.STAGED, identity),
                                    InventoryKind.STAGED,
                                    stage=stage,
                                )
                            )
                        else:
                            entries.append(
                                _invalid_entry(InventoryKind.INVALID, b"sf\0" + identity)
                            )
                finally:
                    with suppress(OSError):
                        os.close(run)
        return entries

    def inventory(
        self,
        *,
        after: str | None = None,
        limit: int = MAX_PORT_BATCH_ITEMS,
    ) -> InventoryPage:
        operation = "inventory"
        with self._operation(operation, exclusive=False) as root_descriptor:
            return self._inventory_locked(root_descriptor, after, limit, operation)

    def _inventory_locked(
        self,
        root_descriptor: int,
        after: object,
        limit: object,
        operation: str,
    ) -> InventoryPage:
        if (
            (
                after is not None
                and (type(after) is not str or not _INVENTORY_TOKEN.fullmatch(after))
            )
            or type(limit) is not int
            or not 1 <= limit <= MAX_PORT_BATCH_ITEMS
        ):
            _fail(PortErrorCode.INVALID_REQUEST, operation)
        budget = _InventoryBudget(
            self._inventory_maximum_entries,
            self._inventory_maximum_bytes,
        )
        entries = sorted(
            [
                *self._inventory_cas(root_descriptor, budget),
                *self._inventory_staging(root_descriptor, budget),
            ],
            key=lambda entry: entry.token,
        )
        start = 0
        if after is not None:
            tokens = [entry.token for entry in entries]
            try:
                start = tokens.index(after) + 1
            except ValueError:
                _fail(PortErrorCode.INVALID_REQUEST, operation)
        selected = tuple(entries[start : start + limit])
        next_after = selected[-1].token if start + len(selected) < len(entries) else None
        return InventoryPage(selected, next_after)

    def delete_artifact(self, artifact: Artifact) -> DeleteDisposition:
        operation = "delete_artifact"
        with self._operation(operation, exclusive=True) as root_descriptor:
            return self._delete_artifact_locked(root_descriptor, artifact, operation)

    def _delete_artifact_locked(
        self,
        root_descriptor: int,
        artifact: object,
        operation: str,
    ) -> DeleteDisposition:
        selected = _artifact(artifact, operation)
        final, parent, descriptors = self._cas_file(
            root_descriptor,
            selected.sha256,
            selected,
            operation,
            content=False,
            create=False,
        )
        try:
            if final is None:
                if parent is not None:
                    try:
                        os.fsync(parent)
                    except OSError:
                        _fail(PortErrorCode.STORAGE_FAILED, operation)
                return DeleteDisposition.ALREADY_ABSENT
            if parent is None:
                _fail(PortErrorCode.CORRUPT, operation)
            try:
                current = os.stat(
                    selected.sha256,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) != final.inode:
                    _fail(PortErrorCode.CORRUPT, operation)
                os.unlink(selected.sha256, dir_fd=parent)
                os.fsync(parent)
            except OSError:
                _fail(PortErrorCode.STORAGE_FAILED, operation)
            return DeleteDisposition.DELETED
        finally:
            _close_descriptors(descriptors)

    def discard_stage(self, stage: StageHandle) -> DeleteDisposition:
        operation = "discard_stage"
        with self._operation(operation, exclusive=True) as root_descriptor:
            return self._discard_stage_locked(root_descriptor, stage, operation)

    def _discard_stage_locked(
        self,
        root_descriptor: int,
        stage: object,
        operation: str,
    ) -> DeleteDisposition:
        selected = _stage_handle(stage, operation)
        with _directory_chain(root_descriptor, ("staging", "v1"), operation) as staging:
            try:
                run, _ = _open_directory(
                    staging,
                    selected.run_id,
                    operation,
                    create=False,
                    missing=PortErrorCode.NOT_FOUND,
                )
            except PortError as error:
                if error.code is PortErrorCode.NOT_FOUND:
                    try:
                        os.fsync(staging)
                    except OSError:
                        _fail(PortErrorCode.STORAGE_FAILED, operation)
                    return DeleteDisposition.ALREADY_ABSENT
                raise
            try:
                verified = _verify_file(
                    run,
                    selected.staging_name,
                    selected.artifact.sha256,
                    selected.artifact,
                    operation,
                    self._maximum,
                    content=False,
                )
                if verified is None:
                    try:
                        self._sync_stage_absence(staging, run, selected.run_id)
                    except OSError:
                        _fail(PortErrorCode.STORAGE_FAILED, operation)
                    return DeleteDisposition.ALREADY_ABSENT
                try:
                    current = os.stat(
                        selected.staging_name,
                        dir_fd=run,
                        follow_symlinks=False,
                    )
                    if (current.st_dev, current.st_ino) != verified.inode:
                        _fail(PortErrorCode.CORRUPT, operation)
                    os.unlink(selected.staging_name, dir_fd=run)
                    os.fsync(run)
                    self._remove_empty_run(staging, run, selected.run_id)
                except OSError:
                    _fail(PortErrorCode.STORAGE_FAILED, operation)
                return DeleteDisposition.DELETED
            finally:
                with suppress(OSError):
                    os.close(run)

    def discard_incomplete(self, cleanup: StagingCleanupHandle) -> DeleteDisposition:
        """Discard an inventoried crash remnant after coordinator authorization."""

        operation = "discard_incomplete"
        with self._operation(operation, exclusive=True) as root_descriptor:
            return self._discard_incomplete_locked(root_descriptor, cleanup, operation)

    def _discard_incomplete_locked(
        self,
        root_descriptor: int,
        cleanup: object,
        operation: str,
    ) -> DeleteDisposition:
        selected = _cleanup_handle(cleanup, operation)
        with _directory_chain(root_descriptor, ("staging", "v1"), operation) as staging:
            try:
                run, _ = _open_directory(
                    staging,
                    selected.run_id,
                    operation,
                    create=False,
                    missing=PortErrorCode.NOT_FOUND,
                )
            except PortError as error:
                if error.code is PortErrorCode.NOT_FOUND:
                    try:
                        os.fsync(staging)
                    except OSError:
                        _fail(PortErrorCode.STORAGE_FAILED, operation)
                    return DeleteDisposition.ALREADY_ABSENT
                raise
            try:
                if selected.staging_name is None:
                    try:
                        metadata = os.fstat(run)
                        with os.scandir(run) as iterator:
                            occupied = next(iterator, None) is not None
                    except OSError:
                        _fail(PortErrorCode.STORAGE_FAILED, operation)
                    if occupied:
                        _fail(PortErrorCode.CONFLICT, operation)
                    if not _cleanup_identity_matches(selected, metadata):
                        _fail(PortErrorCode.CORRUPT, operation)
                    try:
                        os.rmdir(selected.run_id, dir_fd=staging)
                        os.fsync(staging)
                    except OSError:
                        _fail(PortErrorCode.STORAGE_FAILED, operation)
                    return DeleteDisposition.DELETED

                try:
                    metadata = os.stat(
                        selected.staging_name,
                        dir_fd=run,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    try:
                        self._sync_stage_absence(staging, run, selected.run_id)
                    except OSError:
                        _fail(PortErrorCode.STORAGE_FAILED, operation)
                    return DeleteDisposition.ALREADY_ABSENT
                except OSError:
                    _fail(PortErrorCode.STORAGE_FAILED, operation)
                if not _regular_metadata_valid(
                    metadata,
                    0o600,
                ) or not _cleanup_identity_matches(selected, metadata):
                    _fail(PortErrorCode.CORRUPT, operation)
                try:
                    os.unlink(selected.staging_name, dir_fd=run)
                    os.fsync(run)
                    self._remove_empty_run(staging, run, selected.run_id)
                except OSError:
                    _fail(PortErrorCode.STORAGE_FAILED, operation)
                return DeleteDisposition.DELETED
            finally:
                with suppress(OSError):
                    os.close(run)


@dataclass(slots=True, repr=False)
class EvidenceWriterSession:
    """Exclusive store session for the cross-store coordinator protocol."""

    _store: LocalEvidenceStore
    _root_descriptor: int | None

    def __repr__(self) -> str:
        return (
            "EvidenceWriterSession(<active>)"
            if self._root_descriptor is not None
            else ("EvidenceWriterSession(<closed>)")
        )

    def _descriptor(self) -> int:
        if self._root_descriptor is None or self._store._active_writer_session is not self:
            _fail(PortErrorCode.INVALID_REQUEST, "writer_session")
        return self._root_descriptor

    def _deactivate(self) -> None:
        if self._store._active_writer_session is self:
            self._store._active_writer_session = None
        self._root_descriptor = None

    def stage(self, run_id: str, artifact: Artifact, content: bytes) -> StageHandle:
        return self._store._stage_locked(
            self._descriptor(),
            run_id,
            artifact,
            content,
            "stage",
        )

    def commit_stage(self, stage: StageHandle) -> CommitResult:
        return self._store._commit_stage_locked(
            self._descriptor(),
            stage,
            "commit_stage",
        )

    def delete_artifact(self, artifact: Artifact) -> DeleteDisposition:
        return self._store._delete_artifact_locked(
            self._descriptor(),
            artifact,
            "delete_artifact",
        )

    def discard_stage(self, stage: StageHandle) -> DeleteDisposition:
        return self._store._discard_stage_locked(
            self._descriptor(),
            stage,
            "discard_stage",
        )

    def discard_incomplete(self, cleanup: StagingCleanupHandle) -> DeleteDisposition:
        return self._store._discard_incomplete_locked(
            self._descriptor(),
            cleanup,
            "discard_incomplete",
        )

    def inspect(self, artifacts: tuple[Artifact, ...]) -> tuple[ArtifactCheck, ...]:
        return self._store._inspect_locked(
            self._descriptor(),
            artifacts,
            "inspect",
        )

    def inventory(
        self,
        *,
        after: str | None = None,
        limit: int = MAX_PORT_BATCH_ITEMS,
    ) -> InventoryPage:
        return self._store._inventory_locked(
            self._descriptor(),
            after,
            limit,
            "inventory",
        )


__all__ = [
    "DEFAULT_LOCK_TIMEOUT_MS",
    "DEFAULT_MAX_ARTIFACT_BYTES",
    "DEFAULT_MAX_INVENTORY_BYTES",
    "DEFAULT_MAX_INVENTORY_ENTRIES",
    "ArtifactCheck",
    "ArtifactState",
    "CommitDisposition",
    "CommitResult",
    "DeleteDisposition",
    "EvidenceWriterSession",
    "InventoryEntry",
    "InventoryKind",
    "InventoryPage",
    "LocalEvidenceStore",
    "StageHandle",
    "StagingCleanupHandle",
]
