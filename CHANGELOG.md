# Changelog

All notable project changes will be recorded here.

## Unreleased

- Add the deterministic v0.1 CLI vertical slice with `probe`, fake/manual
  `ingest`, `inspect-run`, run-owned `list-samples`, and exact RGB24
  `show-evidence`; data commands use canonical JSON, stable exit codes,
  path/input redaction, checked-in goldens, a fresh-wheel quickstart, and
  CPU-LITE evidence.
- Add deterministic evidence-safe ingestion coordination with fake/manual
  original-pixel regions, staged commit markers, crash-boundary retry, orphan
  repair, reference-aware cascade deletion, redacted stage timings, and a
  CPU-LITE acceptance receipt.
- Add the private versioned local WorldStore with checksummed transactional
  SQLite migrations, canonical-record/projection integrity, WAL concurrency,
  run/intents coordination primitives, adversarial tests, and CPU-LITE
  transaction/index/disk evidence.
- Add the private versioned local EvidenceStore with staged atomic CAS writes,
  integrity verification, deduplication, coordinator-held locking, crash-remnant
  cleanup handles, bounded streaming audit and deletion primitives, adversarial
  tests, and CPU-LITE disk/hash/write evidence.
- Add exact rational detector-to-source geometry for resize, letterbox, and
  quarter-turn display rotation plus deterministic, root-confined original-pixel
  RGB24 crop extraction and CPU-LITE evidence.
- Add deterministic exact-rational PTS frame sampling with documented CFR/VFR
  gap and tie policy, bounded cursor-based resume, cancellation, and CPU-LITE
  evidence.
- Add the bounded Linux local-video adapter: sealed local sources, exact rational
  timestamps and frame hashes, root-owned PyAV worker runtime, namespace/cgroup
  isolation, Landlock/seccomp policy, structured failures, and CPU-LITE evidence.
- Define the four version-1 ingestion ports with capability descriptors,
  structured errors, bounded instrumentation, and deterministic offline fakes.
- Add framework-free version-1 Source, FrameRef, Geometry, EvidenceRef, and
  RunManifest records with canonical JSON, typed identities, exact rational
  time, strict input limits, and round-trip contract tests.
- Add the deterministic, standard-library synthetic-v1 CFR, VFR, and rotation
  fixture generator with locked pixel/timestamp/checksum and rights/privacy
  manifest checks; generated media remains untracked.
- Add the experimental `visualworld-engine` 0.1.0a0 help/version scaffold,
  checksum-pinned developer bootstrap, exact lock, offline checks, packaging
  integrity tests, separate runtime/development inventories, and four CI lanes.

- Bootstrap durable product, architecture, project, benchmark, testing,
  security, and legal documentation.
- Add repository governance templates and lightweight policy CI.
- Establish the GitHub label taxonomy, ten milestone shells, initial v0.1 and
  research issues, and later roadmap epics.
- Enable private vulnerability reporting, secret scanning/push protection, and
  Dependabot alerts/security updates.

No software version has been released.
