# ADR-0005: Local metadata, artifacts, recovery, and deletion

- Status: Accepted
- Date: 2026-09-06
- Deciders: repository maintainers
- Issue: [#6](https://github.com/mayank-gupta16/vision-query-system/issues/6)

## Context

VisualWorld must commit durable metadata and original-pixel evidence without
placing video blobs in a database. A process or machine may stop between the
metadata and filesystem steps, so no single cross-store transaction is
available. Recovery and privacy deletion therefore need an explicit protocol.

The v0.1 store is local and single-machine. Distributed, remote, vector, model,
and analytics stores are outside this decision.

## Decision

### Store layout and boundaries

Use the CPython standard-library
[`sqlite3`](https://docs.python.org/3/library/sqlite3.html) adapter for one local
metadata database and a separate SHA-256 content-addressed filesystem (CAS) for
artifact bytes:

```text
STORE_ROOT/                         mode 0700
  writer.lock                      kernel advisory lock, mode 0600
  world.sqlite3                    metadata only, mode 0600
  world.sqlite3-wal                SQLite-managed persistent state when present
  world.sqlite3-shm                reconstructable SQLite coordination state
  artifacts/v1/sha256/ab/cd/HASH   immutable regular artifact, mode 0400
  staging/v1/RUN_ID/RANDOM.part    incomplete artifact, mode 0600
```

`HASH` is exactly 64 lowercase hexadecimal characters; `ab` and `cd` are its
first two and next two characters. Paths are constructed only from validated
digests and application-generated names. Store operations reject symlinks,
non-regular files, traversal, and roots that do not keep staging and artifacts
on the same local filesystem. Source videos remain caller-controlled and are
never copied into `world.sqlite3`.

Initialization probes a temporary staging file for exclusive creation, durable
file/directory sync, and same-filesystem atomic rename. A root lacking any
required primitive fails with a structured unsupported-store error.

Every mutation holds the kernel-enforced exclusive `writer.lock`; file contents
or a recorded PID are never treated as proof of ownership. Read-only operations
may run concurrently through SQLite, while repair obtains the same exclusive
lock before changing either store.

SQLite uses write-ahead logging with `foreign_keys=ON`, `synchronous=FULL`,
`secure_delete=ON`, one application writer, short read transactions, and bounded
busy handling. Every connection verifies these settings. Startup must verify
that `journal_mode=WAL` was actually selected; otherwise the adapter returns a
structured unsupported-store error. WAL is local-filesystem-only, and the
database must never be copied or separated from a present `-wal` file. The
reconstructable `-shm` file is still private managed state while present. The
adapter uses fixed parameterized statements, never accepts model/user SQL, and
never enables extension loading or `ATTACH`.

The metadata schema has typed record tables for ADR-0004. Each stores validated
canonical JSON plus only the typed columns needed for primary keys, foreign keys,
state, exact PTS lookup, and source/frame/artifact lookup. It also has these
minimum coordination concepts:

- a run row whose `preparing`, `committed`, `failed`, or `cancelled` state is the
  ingestion commit marker;
- an artifact catalog containing digest, byte count, media type, and layout
  version, never artifact bytes;
- artifact intents associating each staged digest and application-generated
  staging name with its preparing run before the CAS rename;
- explicit artifact-reference edges from their owning evidence records;
- a deletion job with a random application-generated ID, root object, protocol
  version, state, and counts; and
- a migration ledger described below.

Canonical JSON is authoritative; indexed columns are deterministic projections
derived in the same transaction. Writes and audits reject any projection/record
mismatch rather than choosing one representation silently.

Queries expose outputs only from `committed` runs and exclude every object under
a pending deletion job.

### Ingestion commit protocol

The coordinator owns cross-store consistency; neither store pretends to be a
distributed transaction manager.

```mermaid
sequenceDiagram
    participant C as Coordinator
    participant M as SQLite metadata
    participant S as Staging filesystem
    participant A as Artifact CAS
    C->>M: BEGIN IMMEDIATE; upsert run=preparing; COMMIT
    C->>S: create secure .part; stream bytes + SHA-256; fsync
    C->>M: BEGIN IMMEDIATE; record artifact intents; COMMIT
    C->>A: verify existing digest or atomic same-filesystem rename; fsync directories
    C->>M: BEGIN IMMEDIATE; insert records/catalog/refs; clear intents; run=committed; COMMIT
    C->>S: remove empty run staging directory
```

For every artifact, the writer computes digest and byte count while streaming to
a new exclusive temporary regular file. It verifies the expected digest and byte
count, changes the completed file to mode 0400, and fsyncs it. The coordinator
then commits an artifact intent containing the run, digest, size, and staging
name. If the final digest path already exists, it must be a regular mode-0400
file with the same size and digest; then the temporary file is removed.
Otherwise the single writer atomically renames the file within the same
filesystem and fsyncs the affected directories. Only after every artifact is
durable does the final SQLite transaction add references, clear the intents,
and mark the run `committed`.

Equivalent retries use the same ADR-0004 `run_id`. A committed equivalent run is
a no-op after integrity checks. A preparing/failed/cancelled run is recovered or
cleaned and retried; it never appears as partial success.

Crash outcomes are deterministic:

| Crash boundary | Durable state | Recovery |
| --- | --- | --- |
| Before preparing commit | No run | Retry from the beginning |
| After preparing commit | Hidden preparing run | Resume or mark failed |
| During staging write, before intent | Run-scoped `.part` may exist | Remove after proving no live writer, then retry |
| After intent, before CAS rename | Intent plus staged file | Verify and finish rename, or clean both |
| After CAS rename, before final metadata commit | Intent plus unreferenced CAS artifact | Verify; reuse on retry or remove with its run |
| During final SQLite transaction | Transaction rolls back; artifacts may be orphaned | Same audit/retry path |
| After final SQLite commit | Complete committed run | Integrity-check and return existing run |

Startup processes pending runs and deletions before serving normal reads. It
does not delete a young staging file based on age alone; it must first prove the
single writer is not active and that no committed reference needs the file.

### Audit and repair

The store exposes dry-run audit separately from explicit repair:

- a referenced artifact that is absent, non-regular, wrong-sized, or hash-mismatched
  makes the owning evidence unavailable and reports store corruption; it is never
  silently regenerated or trusted by filename;
- a valid CAS artifact with no reference is an orphan and may be reused by a
  matching retry or deleted by repair;
- a staging file with no live preparing run is incomplete and may be deleted;
- an artifact intent makes a staged or unreferenced CAS file attributable to its
  preparing run and therefore to source-scoped recovery and deletion;
- a preparing run is resumed idempotently or marked failed after its staging and
  orphan state is reconciled; and
- a pending deletion is resumed before the affected records become visible.

Audits use bounded streaming hashes, `lstat`-style no-follow checks, and only the
known versioned directory shape. Repair never imports unknown files into metadata.
Automatic startup repair is limited to deterministic pending-operation cleanup;
destructive orphan removal requires the explicit repair command or the owning
retry/deletion protocol.

### Cascade deletion protocol

Deletion follows reference edges rather than filenames and completes through
explicit durable phases around filesystem removal:

```mermaid
sequenceDiagram
    participant C as Coordinator
    participant M as SQLite metadata
    participant A as Artifact CAS
    C->>M: BEGIN IMMEDIATE; record deletion=pending and closure; COMMIT
    Note over M: affected objects are now hidden
    C->>A: unlink artifacts with no surviving refs; fsync directories
    C->>M: BEGIN IMMEDIATE; remove refs/metadata; redact job; state=metadata_purged; COMMIT
    C->>M: close readers; wal_checkpoint(TRUNCATE); verify success
    C->>M: BEGIN IMMEDIATE; mark reduced receipt complete; COMMIT
```

The first transaction computes and freezes the source/run/frame/evidence closure,
marks it pending deletion, and prevents new references. The coordinator unlinks
only artifacts whose complete reference set is inside that closure; content still
authorized through another source remains and is counted as shared retention.
Staging and orphan files associated with the closure are included. Any unlink
failure leaves the job pending and hidden for retry.

After every required unlink and directory sync succeeds, one transaction removes
the reference edges and metadata using foreign-key cascades, redacts the job's
identifiers, and sets `metadata_purged`. With no other readers, the coordinator
then requires a successful `wal_checkpoint(TRUNCATE)` before a final transaction
marks the reduced receipt complete. SQLite's documented
[`secure_delete` and checkpoint behavior](https://www.sqlite.org/pragma.html)
therefore applies both to freed database content and the WAL. Version 1 creates
no FTS or other virtual tables that can retain separate shadow content.

The retained completion receipt contains only deletion ID, completion time,
aggregate counts, and shared-retention count—not source, run, frame, evidence,
path, or artifact identifiers. Logs use the same reduced receipt.

A crash before the first commit changes nothing. A crash after it leaves hidden
pending data. A crash during unlink may leave a subset of files, and a crash
before metadata purge may leave metadata pointing to already deleted files, but
the pending marker keeps it invisible and recovery resumes the same idempotent
job. A crash after metadata purge resumes the required WAL truncation using the
identifier-free job state. Completion is recorded only after the managed live
store no longer contains uniquely referenced derivatives or obsolete WAL data.

| Deletion crash boundary | Durable state | Recovery |
| --- | --- | --- |
| Before pending commit | No deletion | Retry from the beginning |
| After pending commit, before unlink | Complete files and hidden closure | Resume unlink |
| During unlink | Hidden closure and a subset of files | Resume idempotent unlink |
| After unlink, before metadata purge | Hidden metadata may reference removed files | Purge metadata |
| After `metadata_purged`, before WAL truncation | Identifier-free job; obsolete WAL may remain | Run and verify truncating checkpoint |
| After WAL truncation, before completion | Identifier-free job with clean WAL | Mark reduced receipt complete |
| After completion | Reduced receipt only | Return existing receipt |

Deletion covers the managed store and registered future caches/indexes/exports.
It cannot promise physical secure erase from SSD wear leveling, snapshots,
external backups, or the caller's independently retained source; those require
separate disclosed retention controls. Deduplicated content retained by another
authorized source is reported, not misrepresented as erased.

### Versions and migrations

Track three independent versions:

1. ADR-0004 record `schema_version` and `identity_version`;
2. SQLite schema version in an authoritative `schema_migrations` ledger with
   ordered version, migration name, code checksum, and applied time; and
3. artifact layout and coordination `protocol_version`, both initially `1`.

`PRAGMA user_version` mirrors the latest ledger version; a mismatch is an
integrity error, not permission to guess. An application refuses a store newer
than it supports. Forward SQLite migrations use reviewed, checksummed,
deterministic code in one exclusive transaction after pending operations are
recovered. They validate before and after and roll back completely on failure.
Automatic downgrade is unsupported.

Schema version 2 is a strictly additive metadata migration: it creates no
derived rows and does not rewrite version-1 canonical records or coordination
markers. Initialization may therefore apply this migration while a version-1
preparing run or deletion job exists, validate the exact two-entry ledger and
schema, and immediately resume the unchanged recovery protocol. A future
migration that transforms existing state must retain the original
recovery-before-migration ordering. Injected interruption at every version-2
DDL, ledger, `user_version`, and commit boundary must reopen as an exact readable
version-1 or validated version-2 store; retry then converges on version 2.

Version 1 permits only transactional metadata migrations. A future artifact
layout change must copy to a new versioned tree, verify every digest, atomically
switch catalog references, and remove the old tree only after an explicit audit;
it must not destructively rename the only artifact copy.

### Estimated overhead

For artifact payload size `B`, a new artifact costs one streaming SHA-256 pass,
one `B`-byte staging write, one file sync, and one same-filesystem rename without
a second payload copy. A deduplicated artifact also verifies the existing
`B`-byte file before discarding its staged copy. Each run has three durable
SQLite transactions—prepare, batched artifact intents, and final records/refs—
plus metadata/index page writes; WAL can temporarily hold approximately the
changed page volume until checkpointing.

Using ADR-0004's 60-second, 5 FPS example, 300 frame/evidence pairs contain
about 319 KiB of canonical JSON. Primary/foreign-key and source/frame/artifact
lookup keys add about 0.1 MiB before B-tree page overhead: roughly 348 key bytes
per frame/evidence pair times 300. Thus 0.5–1.0 MiB is a conservative planning
range for the metadata database contribution. Artifact payloads dominate.
`secure_delete=ON` and deletion checkpointing intentionally add delete I/O.
Issues #14 and #15 must replace these estimates with measured disk, hash, write,
transaction, deletion, and recovery costs on CPU-LITE; no optimization gate is
introduced here.

## Alternatives

- Storing artifacts as SQLite BLOBs was rejected because large pixels/video
  would inflate transactions, WAL, backup, and deletion work and violate the
  explicit metadata-only database boundary.
- JSON files for metadata were rejected because multi-record commits, indexes,
  migrations, referential integrity, and crash recovery would have to be rebuilt.
- PostgreSQL, DuckDB, LMDB, and custom embedded databases were rejected for the
  v0.1 local slice because they add services or dependencies without improving
  the required single-machine transaction boundary.
- One archive/bundle per run was rejected because deduplication, random evidence
  access, incremental recovery, and selective deletion become harder.
- SQLite rollback-journal mode remains viable, but WAL is selected for local
  concurrent readers while retaining one writer. SQLite documents WAL commits
  and recovery in its [WAL guide](https://www.sqlite.org/wal.html) and atomic
  transactions in its [atomic-commit guide](https://www.sqlite.org/atomiccommit.html).

## Consequences

The first store needs no new Python package or external service. SQLite provides
the metadata transaction boundary; content addressing provides artifact
integrity and deduplication; explicit markers make every cross-store crash state
auditable and repairable.

The coordinator, not SQLite alone, must implement the ordered fsync/rename/
transaction and deletion protocols. Orphans can exist after a crash until startup
recovery, and WAL sidecars must be preserved with the database. The store is not
supported on network filesystems. Evidence/artifact bytes remain sensitive even
when unnamed, so permissions, retention, audit, and deletion apply to staging and
orphan files as well as committed artifacts.
