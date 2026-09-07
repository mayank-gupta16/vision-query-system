# Current project state

- Stable version: none; experimental package version 0.1.0a0 is unreleased
- Active GitHub milestone: v0.1 — Evidence-safe ingestion foundation
- Implementation: help/version package, deterministic developer and
  synthetic-fixture commands, version-1 ingestion domain records, the four v0.1
  ports with deterministic fakes, a bounded Linux local-video source adapter,
  an exact-PTS deterministic frame sampler, and exact detector/source geometry
  with byte-preserving original-pixel RGB24 crops, plus a private staged local
  evidence CAS with integrity, dedupe, bounded audit, and deletion primitives,
  and a private transactional SQLite WorldStore with migration and run/intents
  coordination primitives, plus a deterministic coordinator for bounded
  fake/manual original-pixel ingestion, crash recovery, orphan repair, and
  reference-aware source deletion, exposed through a canonical-JSON CLI for
  probing the built-in fixture, ingesting a manual box, inspecting run-owned
  samples, and exporting exact RGB24 evidence, plus a release-gate end-to-end
  regression suite for locked goldens, interrupted recovery/deletion, and
  hostile-input/no-egress boundaries
- Benchmark baseline: local source/probe, exact-PTS sampling, and original-pixel
  crop mapping/copy plus local evidence disk/hash/write and WorldStore
  transaction/index/disk passes, and end-to-end coordinator stage costs on
  CPU-LITE generated fixtures, plus fresh-process CLI wall/RSS/disk overhead for
  the deterministic vertical slice and bounded v0.1 golden/recovery/security
  regression-suite cost

## Established facts

- The product name is VisualWorld; the repository remains
  `mayank-gupta16/vision-query-system` pending a naming decision.
- The first installable package is `visualworld-engine`, import/CLI `visualworld`.
  Its v0.1 CLI exposes only the deterministic built-in fake/manual ingestion
  slice; no real-video CLI input, model inference, or query behavior is claimed.
  ADR-0002 governs its toolchain, exact lock, and four Linux/macOS Python lanes.
- The architecture separates observations, tracklets, persistent entities,
  temporal claims, evidence, uncertainty, and queries behind stable ports.
- CPU-LITE (4 vCPU, 16 GB RAM, no required GPU) is a mandatory benchmark profile.
- Apache-2.0 is the accepted project license with inbound-equals-outbound terms,
  DCO 1.1 for substantive external code/documentation contributions, and no
  initial CLA.
- ADR-0002 accepts a Python-first application: CPython 3.13.15 reference,
  CPython 3.14.7 compatibility, exact uv/uv_build 0.12.6, PEP 621 `src` layout,
  a universal lock with wheel-only installs for Linux x86_64 and macOS arm64,
  zero initial runtime dependencies, and a forced hash-verified PEP 517
  packaging lane.
  Model and external dataset choices remain unresolved.
- ADR-0003 accepts a source-built PyAV 18.1.0 worker linked to a minimal
  signature-verified FFmpeg 9.0.1 build for the first Linux ingestion slice.
  Hostile decode is isolated and fail-closed; native hostile decode on macOS is
  intentionally unsupported until a validated boundary exists. No media runtime
  has yet been added to the root package.
- ADR-0004 accepts strict versioned JSON ingestion records, typed SHA-256-based
  identifiers, exact rational PTS with measured/estimated provenance,
  original-pixel geometry, and explicit migration and untrusted-field limits.
  Its Source, FrameRef, Geometry, EvidenceRef, and RunManifest contracts now
  have a framework-free implementation with strict canonical serialization.
- ADR-0005 accepts local SQLite metadata plus a SHA-256 content-addressed
  filesystem artifact store with staged commits, recovery/audit, migrations,
  and reference-aware cascade deletion. Video blobs remain outside SQLite.
- VideoSource, FrameSampler, EvidenceStore, and WorldStore now have framework-free
  version-1 protocols, capability descriptors, structured errors, bounded call
  records, and deterministic in-memory fakes. Later ports remain conceptual.
- PtsFrameSampler implements the accepted first-PTS-anchored 5-FPS policy with
  exact rational comparison, deterministic CFR/VFR gap behavior, and atomic
  cursor-based bounded-page resume.
- DetectorTransform maps resized/letterboxed, quarter-turn display coordinates
  to encoded source pixels with exact rational affine coefficients, outward
  rounding, and source clamping. Packed RGB24 crop extraction preserves source
  bytes. Its validation sink rejects public or foreign-owned roots, writable or
  linked destination parents, traversal, overwrite, and incomplete writes;
  durable artifact custody remains assigned to the accepted EvidenceStore.
- LocalEvidenceStore implements the stable EvidenceStore port through the
  ADR-0005 `artifacts/v1/sha256` layout, staged atomic promotion, full
  integrity/dedupe checks, shared/exclusive writer locking, and bounded dry-run
  audit primitives. Reference classification, SQLite state, and cascade-deletion
  authorization remain coordinator-owned.
- LocalWorldStore implements the stable WorldStore port with private SQLite WAL,
  checksummed schema version 1, canonical JSON plus verified typed projections,
  fixed parameterized reads/writes, bounded verification, hidden preparing-run
  batches, durable artifact intents, and atomic run publication. It composes
  metadata mutations with an active EvidenceStore writer session.
- IngestionCoordinator composes deterministic offline source/sampler adapters
  with manual source-pixel regions and the two local stores. It publishes only
  complete runs, retries every durable crash boundary, repairs proven orphans,
  and performs reference-aware source deletion through a reduced checkpointed
  receipt. Structured stage events contain only identifiers, status, counts,
  and timings.

## Known blockers and limitations

- GitHub authentication/admin access and public visibility are verified. The
  controlled labels, ten milestone shells, v0.1 issues #1–#20, research issues
  #21–#29, and later roadmap epics #30–#38 exist. Secret scanning, push
  protection, Dependabot alerts/security updates, and private vulnerability
  reporting are enabled.
- Non-provider secret patterns and secret validity checks remain disabled after
  an enable attempt; availability/requirements need follow-up.
- The `VisualWorld Roadmap` Project is blocked because the current token lacks
  `read:project`/`project` scopes.
- Bootstrap PR #39 merged into `main` and closed issue #1 after its
  `repository-policy` check passed. Classic branch protection on `main` requires
  PRs, that status check, and resolved conversations; force pushes and deletion
  are blocked. Required approvals are zero and admins are not enforced so a solo
  maintainer retains recovery access.
- The first end-to-end library path requires caller-supplied RGB24 pixels and
  manual/fake regions; the CLI deliberately supplies only a built-in 2×2 fixture.
  No query engine, real perception model adapter, real-video CLI input, or
  combined 60-second application benchmark exists. The local-video adapter is
  Linux x86_64 only and tests generate only the approved tiny synthetic-v1 media
  plus temporary metadata/archive fixtures.
- The project license does not relicense models, weights, datasets, media,
  runtimes, codecs, services, or other third-party material.

## Next priorities

1. Establish issue #19's combined 60-second CPU-LITE application harness and
   baseline.
2. Cut and verify the v0.1 release through issue #20 after every prerequisite
   closes.
3. Keep model, dataset, media, runtime, codec, service, and third-party licenses
   in their separate release-gate inventories.
