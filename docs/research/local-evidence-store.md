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
discard, integrity inspection, paginated inventory, and low-level deletion
operations. A writer session holds the one kernel advisory lock across staging,
future SQLite intent work, CAS promotion, and metadata completion. The adapter
does not infer references or delete orphans: the WorldStore/coordinator remains
the reference authority and must classify inventory results before any repair.

Initialization pins an absolute effective-UID-owned private root, creates only
the fixed versioned layout, and probes exclusive creation, file/directory sync,
and same-filesystem descriptor-relative rename. Existing objects with unexpected
type, owner, mode, link count, device, or symlink behavior fail closed. Mutations
stage through an exclusively created random file, hash and count the bytes,
change it to read-only, sync it, and either verify/deduplicate an existing CAS
file or atomically rename and sync both directories. A commit retry after a
post-rename interruption verifies the final artifact and succeeds idempotently.

All artifact and path-derived scalars must be exact built-in values. Traversal,
string/bytes subclass tricks, symlinks, FIFOs, hard links, hash/size conflicts,
corrupt destinations, and invalid stage handles are rejected with structured
redacted port errors. Reads take a shared lock; mutations and repair primitives
take the exclusive lock. The writer session can inspect and inventory under that
same exclusive lock before an authorized repair. Inventory is dry-run only and
has explicit scan-entry, payload, and total-byte limits.

## Acceptance result

Contract and adversarial tests cover layout and permissions, stable port
compatibility, staged and direct writes, repeated commit/discard/delete,
deduplication, synthetic same-hash/different-byte collision handling,
interrupted writes and rename recovery, lock contention, descriptor reuse,
missing/corrupt/incomplete/unknown audit findings, bounded pagination, and
root/component/file traversal and link attacks. Reopening the store preserves
the committed inode and verifies its bytes. The package-content check includes
the new zero-dependency module.

The CPU-LITE baseline used two deterministic 16 MiB synthetic payloads. A raw
SHA-256 pass took 28.6 ms (587 MB/s). The unique durable `put` took 127.7 ms
(131 MB/s), a verified read took 46.4 ms (361 MB/s), and a deduplicated `put`
took 171.2 ms (98 MB/s). Staging the second artifact took 61.7 ms (272 MB/s),
atomic promotion and directory sync took 31.3 ms, the two-artifact integrity
audit took 122.6 ms, and deletion plus directory sync took 31.7 ms. One retained
artifact occupied 16,777,216 logical and allocated bytes; dedupe did not change
that count. Peak process RSS was 90,509,312 bytes, below the 2 GiB harness bound.
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
