# Local WorldStore validation

- Issue: V01-15
- Result: pass on the required CPU-LITE profile
- Evidence: [machine-readable receipt](local-world-store-cpu-lite-receipt.json)

## Implemented metadata contract

`LocalWorldStore` is the first production implementation of the stable
`WorldStore` commit/get/frame-list/evidence-list port. It uses the standard
library SQLite adapter and the `world.sqlite3` WAL layout accepted by ADR-0005;
the database, WAL/SHM companions when present, and shared `writer.lock` are
private effective-UID-owned regular files under a mode-0700 absolute root.
Source video and artifact bytes never enter SQLite.

Schema version 1 uses typed `STRICT` tables for sources and streams, frames,
evidence, runs, the artifact catalog/reference graph, artifact intents,
run-record visibility edges, and deletion-job closure state. Canonical
ADR-0004 JSON is authoritative. Indexed identifiers, stream/decode position,
exact PTS components, run state, and artifact descriptors are derived in the
same transaction and rechecked on writes, reads, reopen, and bounded
verification. The schema migration ledger records an ordered name, source-code
checksum, UTC application time, and version mirrored by `PRAGMA user_version`;
the complete expected SQLite schema is compared on every connection. Newer
versions are refused and mismatched ledgers, schema objects, projections,
references, or canonical bytes fail as redacted corruption.

Writes use `BEGIN IMMEDIATE`, `foreign_keys=ON`, `journal_mode=WAL`,
`synchronous=FULL`, `secure_delete=ON`, bounded busy handling, and the same
exclusive kernel `writer.lock` as `LocalEvidenceStore`. Reads use short
read-only WAL connections. Preparing-run record batches are associated and
hidden from both point and list reads; stage handles are persisted as exact,
bounded-page-recoverable artifact intents; finalization requires every intent
to match an owned catalog/reference edge, clears intents, and changes the
canonical run marker to committed in one transaction. Exact retries are no-ops,
invalid state changes conflict, and preparing, failed, or cancelled runs remain
discoverable through bounded stable pages for the future recovery coordinator.

The mutation extensions accept an active `EvidenceWriterSession` bound to the
same root. This lets the next coordinator perform stage, intent, CAS promotion,
reference, and run-publication steps under one lock without receiving a raw
filesystem descriptor or SQLite connection. Issue #16 now uses these boundaries
for durable run cleanup, reference-aware source-deletion closure, metadata
purge, and reduced completion receipts; this baseline remains focused on the
300-record WorldStore transaction/index workload.

## Acceptance result

Contract tests cover all four record types, stable numeric ordering through the
unsigned 64-bit decode-index range, atomic foreign-key/position failure,
idempotence and conflicting content, reopen, hidden preparing records, intent
retry/collision/pagination, finalization completeness, pending-run recovery
pagination, bounded verification, shared-lock contention, bounded SQLite busy
behavior, and a real composed EvidenceStore/WorldStore writer session.

Migration/security tests cover complete rollback and retry, newer/negative
version refusal, ledger and schema drift, malformed database bytes, canonical
JSON/hash/projection/reference corruption, foreign-key corruption, unknown
unversioned schema, missing files, root/database/sidecar symlink, hard-link and
mode attacks, unexpected rollback journals, poisoned locks, mutated domain and
stage objects, raw backend failure redaction, and pending deletion visibility.
The product exposes no SQL/connection method; statements are fixed and
parameterized, `executescript` is not used, extension loading and double-quoted
string literals are disabled, defensive/untrusted-schema modes are enabled, and
the connection authorizer denies `ATTACH`, `DETACH`, virtual tables, and
`load_extension`.

## CPU-LITE baseline

The generated workload represents 60 seconds at 5 FPS: one 1920x1080 source,
one run, 300 frames with exact millisecond time-base PTS, 300 evidence records,
and 300 unique artifact descriptors. Five intent transactions took 10.6 ms; ten
hidden record transactions took 76.0 ms; finalization took 25.7 ms; and an
idempotent finalization retry took 2.6 ms. Five frame-index pages took 47.7 ms,
300 point evidence lookups took 581.5 ms, reopen took 2.0 ms, and complete
canonical/projection verification took 58.3 ms.

The live SQLite/WAL set occupied 1,523,712 logical and allocated bytes across
four regular files. Peak process RSS was 28,958,720 bytes, below the 2 GiB
profile bound. These single-run figures establish correctness and resource
provenance, not a cross-machine optimization claim.

## Security and portability boundary

The root and coordinator are trusted same-principal application components.
Kernel locking serializes cooperating processes; it does not confine a hostile
same-UID process that ignores the lock, mutates the namespace, reads memory, or
inherits descriptors across `fork`. Adapters must be created after worker
processes and no fork may occur during a writer session. WAL is supported only
on the approved local Linux x86_64 and macOS arm64 filesystems; it must be moved
or backed up together with any present `-wal` state. No network service,
third-party runtime dependency, media, model, dataset, or license change is
introduced.

Version 1 performs bounded full verification (default 4,096 records) and opens
a short connection per public call. Persistent read pools, incremental audits,
arbitrary queries, future world-model tables, and database optimization are
deliberately deferred until the vertical slice proves their need.
