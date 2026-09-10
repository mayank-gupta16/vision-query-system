# Current project state

- Stable version: 0.1.0, first released 2026-09-07 with experimental public APIs
- Next GitHub milestone: v0.2 — Detection and short-term tracklets
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
  hostile-input/no-egress boundaries, plus strict pixel-free v0.2 Observation
  and clip-local Tracklet records and bounded Detector, Tracker, and
  EvidenceSelector contracts with deterministic fakes, and a pinned combined
  CPU-LITE benchmark harness with machine-readable comparison, plus an accepted
  separately provisioned Linux x86_64/GNU-libc-2.28+ perception boundary with a
  frozen artifact/runtime manifest and standard-library fail-closed provisioner,
  plus a deterministic global-last-box IoU tracker with pixel-free hard-cut
  scores and opaque bounded-page continuation, plus the bounded offline
  OpenVINO `vehicle-detection-0201` adapter with a sealed composite
  decode/inference worker and deterministic CI fixture seam, plus a concrete
  integer-only best-frame selector with pixel-free source-coordinate intents
  and explicit on-demand exact RGB24 materialization, plus additive schema-v2
  persistence for generic observations, completed tracklets, ordered points,
  and selected-evidence metadata with atomic publication and cascade recovery,
  plus a generic bounded original-frame reader with a frozen Linux-only
  hostile-decode overlay and a producer-distinct coordinator path that computes
  exact RGB24 discontinuities, materializes only selected crops, and publishes
  their CAS bytes and provenance atomically, plus an additive canonical-JSON
  real-video perception CLI that validates the fixed closure before store
  access, runs the measured vehicle-only composition, pages committed records,
  exports run-owned selected crops, and invokes durable recovery/deletion
- Benchmark baseline: local source/probe, exact-PTS sampling, and original-pixel
  crop mapping/copy plus local evidence disk/hash/write and WorldStore
  transaction/index/disk passes, and end-to-end coordinator stage costs on
  CPU-LITE generated fixtures, plus fresh-process CLI wall/RSS/disk overhead for
  the deterministic vertical slice and bounded v0.1 golden/recovery/security
  regression-suite cost, plus the
  [combined 60-second application baseline](../benchmarks/v0.1-cpu-lite-baseline.md)
  and the [v0.2 detector/runtime selection](../benchmarks/v0.2-detection-results.md),
  which chooses `vehicle-detection-0201` at confidence 0.95 as the separately
  provisioned Linux baseline, plus the
  [v0.2 sampling result](../benchmarks/v0.2-sampling-results.md),
  which chooses fixed nearest-PTS 5 FPS and keeps fixed 8 FPS as a bounded
  expert override, plus the
  [v0.2 crop-value result](../benchmarks/v0.2-crop-results.md), which finds
  material tiny-detail value in exact original-resolution crops but rejects
  eager retention because median crop bytes are 56.25x the detector crop, plus
  the [v0.2 short-term tracking result](../benchmarks/v0.2-tracking-results.md),
  which selects global last-box IoU association with a 0.10 threshold, five
  missed 5 FPS samples, and a 0.15 hard-cut threshold.

## Established facts

- The product name is VisualWorld; the repository remains
  `mayank-gupta16/vision-query-system` pending a naming decision.
- The first installable package is `visualworld-engine`, import/CLI `visualworld`.
  Its v0.1 CLI exposes only the deterministic built-in fake/manual ingestion
  slice; no real-video CLI input, model inference, or query behavior is claimed.
  The additive `visualworld perception` namespace is the v0.2 experimental
  surface and does not change any v0.1 command or result byte.
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
  `vehicle-detection-0201` with OpenVINO 2026.3.1 is the selected v0.2 baseline.
  ADR-0007 approves it only as a user-provisioned private Linux x86_64 GNU libc
  2.28-or-newer closure;
  product redistribution and production data remain unapproved.
- Fixed nearest-PTS 5 FPS is the selected v0.2 sampling evaluation default. The
  measured 3-to-8 FPS tile-motion strategy is not selected because it consumed
  53.3% more samples without improving a gate metric. Fixed 8 FPS is reserved
  for explicit out-of-boundary requirements or a later measured tracking
  escalation; no production adapter has yet changed.
- Original-resolution retrieval is justified on demand for source-resolvable
  tiny detail: exact source crops improved the paired tiny readable-detail
  metric by 1,601 basis points with zero source-absent gain. Preserve source
  coordinates and retrieve transiently for a declared downstream need; do not
  eagerly retain every crop because median source evidence was 56.25x the
  detector-input crop. The evidence selector now plans at most three views by
  default and reaches the exact crop/EvidenceRef boundary only after an explicit
  inspection or resolvable downstream-detail request.
- Global last-box IoU is the selected v0.2 short-term tracking policy. On the
  held-out vehicle fixture it passes every frozen gate at 8,217 HOTA, 8,580
  IDF1, 8 switches and 24 fragmentations per 1,000 visible track frames, zero
  false cut continuations, 4.251x real time, and 562.32 MiB peak RSS. Keep
  identities clip-local, terminate before post-cut association, allow five
  missed 5 FPS samples, and do not infer persistent ReID. `GlobalLastBoxTracker`
  implements that exact boundary with deterministic global assignment, bounded
  resumable state, explicit unknown/unsupported outcomes, and no pixel or path
  values in the port.
- ADR-0003 accepts the source-built
  `visualworld-pyav-18.1.0-ffmpeg-9.0.1-v2` worker closure for the first Linux
  ingestion slice. The minimal signature-verified FFmpeg build, CPython, PyAV,
  and worker are fully tree-bound; development files, external links, and
  multiply linked files are excluded. Hostile decode is isolated and
  fail-closed; native hostile decode on macOS is intentionally unsupported
  until a validated boundary exists. No media runtime has been added to the
  root package.
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
  records, and deterministic in-memory fakes. Ports outside the v0.1 set and
  the three v0.2 perception contracts below remain conceptual except for the
  bounded detector/tracker/selector composition described below.
- Observation and completed clip-local Tracklet now have strict version-1
  canonical records. Detector, Tracker, and EvidenceSelector have additive
  experimental ports with bounded offline fakes and explicit complete,
  `UNKNOWN`, or unsupported results. The first contracts exchange only source,
  frame, geometry, time, confidence/provenance, tracklet, and observation-ID
  values. A combined public experimental record dispatcher is additive while
  the v0.1 WorldStore union remains unchanged. `PerceptionCoordinator` now
  composes bounded deterministic pages into one restart-safe, metadata-only
  schema-v2 publication with hidden-run recovery and opaque in-process cursors.
  It preserves the vehicle-only measured path and propagates unknown or
  unsupported outcomes without publishing partial output. Its additive
  materializing path obtains exact authorized RGB24 through the generic
  `OriginalFrameReader`, computes page-spanning discontinuities, and publishes
  selected crops plus their provenance in the same recoverable writer session.
  The first concrete
  detector now implements the selected vehicle-only 384×384 OpenVINO CPU policy
  behind the accepted isolated worker; ordinary CI substitutes its deterministic
  pixel-free fixture seam without provisioning native artifacts.
- ADR-0007 freezes the exact selected model, CPython/OpenVINO/NumPy/telemetry
  closure, notices, media-runtime linkage, worker limits, platform, and
  no-redistribution status. Its explicit provisioner is the only acquisition
  path; it hash-verifies and atomically publishes a root-owned read-only closure.
  Normal application execution remains offline. The bounded detector revalidates
  that closure and a manifest-bound first-party worker before every launch,
  passes only a sealed source descriptor, and validates canonical pixel-free
  output before creating original-coordinate observations.
- `OriginalFrameReader` adds bounded, vendor-neutral access to exact authorized
  source frames. Its deterministic fake covers ordinary CI. The production
  adapter is Linux x86_64/GNU-libc-2.28+ only and reuses the unchanged accepted
  media closure with a separately frozen first-party worker overlay, sealed
  source and output descriptors, exact Source/FrameRef reconciliation, static
  errors, and whole-cgroup cleanup. No full frames enter logs, metadata, or
  durable storage; only explicitly selected crops reach the evidence CAS.
- `BestFrameEvidenceSelector` implements the frozen metadata-only ordering from
  boundary contact, detector confidence, normalized/raw visible area, and
  source-time position. Its intents preserve every score component,
  original-source geometry, selector provenance, private retention, and
  coordinator deletion ownership. Missing pixels and unresolvable detail stay
  `UNKNOWN`; selection never performs OCR, identity, make/model, face, or plate
  inference and never stores pixels by default.
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
  an ordered checksummed schema ledger through version 2, canonical JSON plus
  verified typed projections, fixed parameterized reads/writes, and bounded
  verification. Its additive v0.2 adapter APIs persist generic observations,
  completed tracklets and their exact ordered points, and full selected-evidence
  intents while keeping preparing runs hidden. Publication validates the entire
  run-owned graph and durable CAS references before one atomic commit-marker
  transition; recovery and source deletion cover the same graph. The stable
  v0.1 `WorldStore` record contract and stored canonical bytes remain unchanged.
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
- The `VisualWorld Roadmap` Project remains blocked because the current token
  lacks `read:project`/`project` scopes. Issue #41 records the named v0.1 release
  waiver: owner `mayank-gupta16`, organizational-view-only risk, canonical
  issue/milestone/documentation controls, and the scoped follow-up.
- Bootstrap PR #39 merged into `main` and closed issue #1 after its
  `repository-policy` check passed. Classic branch protection on `main` requires
  PRs, that status check, and resolved conversations; force pushes and deletion
  are blocked. Required approvals are zero and admins are not enforced so a solo
  maintainer retains recovery access.
- The v0.1 CLI deliberately supplies only a built-in 2×2 fixture. The additive
  v0.2 namespace wires the real detector and original-frame adapters into an
  explicit local-video command, but native Linux execution of the complete CLI
  path and original-frame overlay remains to be recorded on the supported
  root-owned runtime; macOS correctly reports it as unsupported. No query engine
  exists.
  The combined benchmark therefore measures generated exact-PTS records,
  full-resolution crop/hash work, and the local stores without claiming decode
  or perception throughput. The local-video adapter is Linux x86_64 only and
  tests generate only the approved tiny synthetic-v1 media plus temporary
  metadata/archive fixtures.
- The project license does not relicense models, weights, datasets, media,
  runtimes, codecs, services, or other third-party material.

## Next priorities

1. Continue the #30 critical path with the end-to-end regressions, benchmark,
   and release issues #78–#80 after the perception CLI.
2. Preserve the accepted detector and tracker boundaries while completing the
   first real-video perception vertical slice; do not broaden model/platform or
   identity scope before its release gates pass.
3. Keep model, dataset, media, runtime, codec, service, and third-party licenses
   in their separate release-gate inventories.
