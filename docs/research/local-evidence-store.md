# Local EvidenceStore validation

- Issue: V01-14
- Result: pass on the required CPU-LITE profile
- Evidence: [machine-readable receipt](local-evidence-store-cpu-lite-receipt.json)

## Implemented storage contract

`LocalEvidenceStore` is the first production implementation of the stable
`EvidenceStore` `put`/`get` port. It stores artifact bytes in the version-1
SHA-256 CAS accepted by ADR-0005:

```text
STORE_ROOT/                         mode 0700
  writer.lock                      mode 0600
  artifacts/v1/sha256/ab/cd/HASH   immutable regular file, mode 0400
  staging/v1/RUN_ID/RANDOM.part    incomplete 0600; completed 0400
```

The adapter also exposes coordinator-facing staged write, idempotent promotion,
discard, integrity inspection, paginated inventory, metadata-fingerprint-bound
cleanup handles for incomplete files and empty run directories, and low-level
deletion operations.
A writer session holds the one kernel advisory lock across staging, future
SQLite intent work, CAS promotion, cleanup, and metadata completion. The adapter
does not infer references or delete orphans: the WorldStore/coordinator remains
the reference authority and must classify inventory results and prove that no
live/preparing run owns a remnant before authorizing cleanup or repair.

Initialization pins an absolute effective-UID-owned private root, durably syncs
the root entry through its trusted parent, creates only the fixed versioned
layout, and probes exclusive creation, file/directory sync, and same-filesystem
descriptor-relative rename. Existing objects with unexpected type, owner, mode,
link count, device, or symlink behavior fail closed. Mutations stage through an
exclusively created random file, hash and count the bytes, change it to read-only,
sync it, and either verify/deduplicate an existing CAS file or atomically rename
and sync both directories. A retry after a rename, unlink, or run-directory
removal re-syncs every affected existing parent before reporting idempotent
success.

All artifact and path-derived scalars must be exact built-in values. Traversal,
string/bytes subclass tricks, symlinks, FIFOs, hard links, hash/size conflicts,
corrupt destinations, and forged or mutated stage/cleanup handles are rejected
with structured redacted port errors. Reads take a shared lock; mutations and
repair primitives take the exclusive lock. A lock-owner close failure cannot be
reported as success; an ambiguously retained lock poisons that adapter instance.
The writer session can inspect and inventory under that same exclusive lock
before an authorized repair.

Inventory is dry-run only and streams hashes in 1 MiB chunks. In version 1,
pagination bounds returned results, not traversal work: every page rescans and
rehashes the complete known tree before selecting a page. The default whole-scan
support envelope is 4,096 discovered directory entries and 1 GiB of artifact or
stage bytes. Larger stores must explicitly raise those constructor budgets and
pay the repeated scan cost; an incremental persisted audit cursor is deferred.

## Acceptance result

Contract and adversarial tests cover layout and permissions, stable port
compatibility, staged and direct writes, repeated commit/discard/delete,
deduplication, synthetic same-hash/different-byte collision handling,
interrupted writes and rename/unlink durability recovery, lock contention and
ambiguous close poisoning, descriptor reuse, missing/corrupt/incomplete/empty-run
audit findings and authorized cleanup, bounded pagination, and root/component/
file traversal and link attacks. Reopening the store preserves the committed
inode and verifies its bytes. The package-content check includes the new
zero-dependency module.

The CPU-LITE baseline used two deterministic 16 MiB synthetic payloads. A raw
SHA-256 pass took 29.7 ms (565 MB/s). The unique durable `put` took 141.5 ms
(118 MB/s), a verified read took 44.5 ms (376 MB/s), and a deduplicated `put`
took 186.4 ms (90 MB/s). Staging the second artifact took 74.0 ms (226 MB/s),
atomic promotion and directory sync took 32.1 ms, the two-artifact integrity
audit took 120.5 ms, and deletion plus directory sync took 31.8 ms. One retained
artifact occupied 16,777,216 logical and allocated bytes; dedupe did not change
that count. Peak process RSS was 90,730,496 bytes, below the 2 GiB harness bound.
These are single-run correctness baselines, not optimization targets or
cross-machine performance claims.

## Security and durability boundary

The root is trusted application-controlled local storage. `flock` serializes
cooperating application processes; it does not confine a hostile same-UID
process that ignores the lock, mutates the namespace, reads process memory, or
inherits a lock across `fork`. Applications must create adapters after worker
process creation and must not fork while a writer session is active. The root
must be provisioned on one local filesystem; the initialization probe establishes
the required behavior but is not a portable proof that a mount is physically
local.

The adapter reports completed filesystem sync operations. It does not claim
physical secure erase, survival through every storage-controller power-loss
mode, removal from snapshots/backups, or deletion of caller-held source media.
On macOS, deployment must additionally use a locally provisioned root without
granting ACLs on a volume that enforces ownership. No network service, remote
backend, new runtime dependency, private media, or external dataset is added.

## Limits

This issue implements only the filesystem evidence adapter and primitives the
future coordinator can compose. It does not add SQLite metadata, reference-edge
decisions, automatic orphan repair, end-to-end ingestion, a model, or queries.
Unknown files are reported but never imported. Destructive orphan cleanup and
reference-aware cascade deletion remain coordinator-owned work in later v0.1
issues.
