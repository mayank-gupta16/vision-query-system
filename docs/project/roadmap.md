# Version roadmap

This initial dependency ladder was materialized as GitHub milestone shells on
2026-09-06 under the bootstrap mission. Each boundary must be reviewed before
the milestone is activated. Research may run one milestone ahead, but production
choices require measured results and an accepted ADR where the consequence is
long-lived.

Before activating v0.2 or any later milestone, a gate-calibration issue must
freeze dataset/metric versions, repetitions/statistics, absolute minimums,
per-metric regression budgets, expected UNKNOWN/uncertainty behavior, and the
maintainer responsible for waivers. For v0.2 this is issue #64 and
[ADR-0006](../decisions/ADR-0006-v02-evaluation-gates.md). Metrics described
below as "reported" are diagnostics, not passing gates, until those thresholds
are ratified.

```text
v0.1 -> v0.2 -> v0.3 -> v0.4 -> v0.5 -> v0.6
                 |                        |
                 +-----> v0.7 <-----------+
                    v0.3 + v0.7 -> v0.8
                 v0.6 + v0.7 + v0.8 -> v0.9 -> v1.0
```

Every version also satisfies [the shared release gates](release-gates.md).

## v0.1 — Evidence-safe ingestion foundation

- **Goal:** local video to a reproducible run manifest, timestamped sampled-frame
  index, original-pixel evidence, inspectable local store, and CLI using fakes.
- **Includes:** repo/toolchain decisions, domain records and ports, local source
  probe, PTS-aware sampling near 5 FPS, coordinate/crop utilities, local metadata
  and artifact stores, fake adapters, tiny fixtures/goldens, CPU-LITE harness.
- **Excludes:** real detector, tracker, OCR, VLM, ReID, natural language, streams.
- **Dependencies:** accepted license/toolchain/media and storage/schema/ID decisions.
- **Acceptance:** clean install/checks; repeated ingest is deterministic and
  idempotent; every sample/crop maps to source time/original pixels; invalid or
  interrupted sources fail structurally and recover safely.
- **Benchmark gate:** locked generated-fixture checksums; stored timestamp equals
  the decoded source PTS with zero rational conversion loss; sample-selection
  error is bounded to the nearest eligible frame under the documented CFR/VFR
  gap policy; crop mapping within one pixel; record wall time, throughput,
  peak RSS, CPU, and disk for 60 seconds of 1080p at 5 FPS on CPU-LITE, with
  peak RSS no higher than 2 GiB. First throughput is a baseline, not a claim.
- **Release criteria:** every required v0.1 issue except the release ticket is
  closed, then the release ticket verifies baseline, documentation, changelog,
  tag, GitHub Release, and milestone closure.

## v0.2 — Detection and short-term tracklets

- **Goal:** one supported CPU-conscious detector/tracker path produces traceable
  detections, trajectories, tracklets, and best-frame evidence.
- **Includes:** adapter selection research, contract tests, original-coordinate
  remapping, deterministic evidence scoring, cut/gap termination.
- **Excludes:** cross-gap entity merge, OCR, fine-grained attributes, NL queries.
- **Dependencies:** v0.1 and numeric adapter thresholds set by research.
- **Acceptance:** a local video yields inspectable tracklets and evidence; cuts
  and long gaps terminate tracklets; adapter substitution preserves contracts.
- **Benchmark gate:** report precision/recall/mAP, HOTA, IDF1, ID switches,
  fragmentation, FPS/RTF, CPU/RAM, and disk on locked sets and CPU-LITE, then
  apply the ratified per-metric minimums and regression budgets.
- **Release criteria:** selected adapter/license decision, contract suite,
  benchmark report, and shared gates pass.

## v0.3 — Persistent entities and world state

- **Goal:** durable, versioned entities, claims, intervals, relationships, and
  conservative within-video identity resolution.
- **Includes:** migrations, `same/different/uncertain`, count ranges, temporal
  facts, evidence/provenance queries.
- **Excludes:** advanced long-gap/cross-camera ReID and general event inference.
- **Dependencies:** v0.2 and schema/storage decisions.
- **Acceptance:** restart-safe world state; ambiguous fixtures return bounds or
  `UNKNOWN`; migrations preserve evidence and producer versions.
- **Benchmark gate:** 100% easy identity goldens, zero false merges on safety
  fixtures, and reported false merge/split, ReID accuracy, and count error.
- **Release criteria:** migration/recovery tests and shared gates pass.

## v0.4 — Query engine and lazy semantics

- **Goal:** structured and natural-language requests share a safe planner and
  executor that performs cheap filters before optional semantic calls.
- **Includes:** validated QueryPlan AST, planner/executor, SemanticReasoner port,
  cache versioning, predicate pushdown, unsupported-query results.
- **Excludes:** specialist OCR and advanced temporal event semantics.
- **Dependencies:** v0.3.
- **Acceptance:** answers resolve to evidence/provenance; equivalent repeat queries
  cause no extra semantic calls; model output cannot execute SQL, commands,
  paths, or URLs.
- **Benchmark gate:** deterministic query goldens and injection/invalid-plan
  corpus pass; answer/evidence/timestamp accuracy and semantic calls per video
  minute meet the ratified gate-calibration thresholds.
- **Release criteria:** query security review, cache invalidation tests, and
  shared gates pass.

## v0.5 — OCR, attributes, relationships, and evidence quality

- **Goal:** multi-frame OCR and time-bounded attributes/relationships provide
  calibrated, query-time enrichment and best-view evidence.
- **Includes:** hierarchical plate/OCR processing, conflicting alternatives,
  make/model/color/helmet where supported, anonymous face crops when permitted.
- **Excludes:** real-person identification and advanced long-gap event/ReID.
- **Dependencies:** v0.4 and specialist-versus-VLM/licensing research.
- **Acceptance:** unreadable/partial/probable/confirmed states survive end to end;
  conflicts are retained; sensitive features remain opt-in and locally handled.
- **Benchmark gate:** CER, exact/partial plate accuracy, attribute F1, evidence
  quality, semantic-call budget, and CPU-LITE cost meet research-set thresholds.
- **Release criteria:** privacy/retention tests and shared gates pass.

## v0.6 — Advanced ReID and temporal events

- **Goal:** conservative long-gap reappearance, moving-camera identity,
  associations, and extensible temporal events.
- **Includes:** entity fingerprints, uncertainty propagation, event adapters,
  time-bounded relationships, difficult-case fixtures.
- **Excludes:** live multi-camera operations and real-world biometric identity.
- **Dependencies:** v0.5.
- **Acceptance:** hard cases preserve uncertainty and evidence; entity merges are
  explainable/reversible; events remain versioned claims.
- **Benchmark gate:** ratified false-merge ceiling is prioritized over recall;
  false splits, count-bound coverage, event F1, and timestamp error meet their
  calibrated thresholds.
- **Release criteria:** difficult-case suite and shared gates pass.

## v0.7 — Benchmark and performance maturity

- **Goal:** make reproducible quality/resource evaluation a product capability.
- **Includes:** CPU-LITE, GPU-DEV, EDGE, and HIGH-PERFORMANCE profiles; dataset
  manifests; scheduled/manual workflows; regression comparison and reports.
- **Excludes:** unmeasured optimization claims and mandatory GPU PR jobs.
- **Dependencies:** benchmarkable components from v0.2 through v0.6.
- **Acceptance:** one command produces machine-readable and human-readable
  results with hardware/config/model/data provenance.
- **Benchmark gate:** every supported profile is reported and CPU-LITE always
  runs; no unexplained material quality or greater-than-10% resource regression.
- **Release criteria:** published benchmark report and shared gates pass.

## v0.8 — Live, edge, and multi-source operation

- **Goal:** bounded RTSP/webcam ingestion and resumable incremental world updates.
- **Includes:** queues/backpressure, cancellation/reconnect, rolling retention,
  device profiles, explicit multi-camera uncertainty/calibration boundaries.
- **Excludes:** distributed high availability and biometric identity.
- **Dependencies:** v0.3 and v0.7.
- **Acceptance:** reconnects do not duplicate committed segments; resource use is
  bounded; remote/network sources require explicit authorization.
- **Benchmark gate:** two-hour fault-injected soak plus measured edge profile;
  RSS/disk ceilings, continuity loss, duplicate tolerance, and recovery SLA meet
  thresholds ratified before milestone activation.
- **Release criteria:** network/privacy threat review and shared gates pass.

## v0.9 — Hardening and release candidate

- **Goal:** stabilize migrations, recovery, packaging, retention, security,
  compatibility, and operational documentation.
- **Includes:** install/upgrade paths, corruption recovery, SBOM, threat-model
  verification, public API candidate, dependency/license audit.
- **Excludes:** unresolved experimental APIs presented as stable.
- **Dependencies:** v0.6, v0.7, and v0.8.
- **Acceptance:** supported upgrade/recovery paths are reproducible; source and
  derived-data deletion is complete; operational failures are diagnosable.
- **Benchmark gate:** full regression matrix and release-candidate benchmark
  meet the frozen v0.9 quality/resource budgets; waivers name an owner and risk.
- **Release criteria:** no P0/P1, security/license review, and shared gates pass.

## v1.0 — Stable observable-world engine

- **Goal:** a supported local product with stable contracts for recorded and
  live video and evidence-backed structured/NL queries.
- **Includes:** compatibility/deprecation policy, supported profiles/adapters,
  complete evidence/uncertainty behavior, reproducible install and upgrade.
- **Excludes:** real-world face identification and uncalibrated exact geometry.
- **Dependencies:** v0.9.
- **Acceptance:** the difficult hour-long acceptance corpus supports the documented
  query classes and honest `unknown/ambiguous/range` responses.
- **Benchmark gate:** all supported profiles and interface compatibility suite
  meet the v1.0 ratified thresholds with published results.
- **Release criteria:** stable docs/examples, migration/support policy, final
  license inventories, tag, GitHub Release, and shared gates pass.
