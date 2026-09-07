# ADR-0006: Frozen v0.2 evaluation gates and waivers

- Status: Accepted
- Date: 2026-09-07
- Deciders: repository maintainers
- Issue: [#64](https://github.com/mayank-gupta16/vision-query-system/issues/64)

## Context

The v0.2 detector, sampling, crop-value, and tracker experiments must not choose
their datasets, metric definitions, repetition statistic, or passing thresholds
after candidate results are visible. The first release established deterministic
records, original-pixel evidence, and a CPU-LITE resource boundary, but did not
approve a model, model runtime, dataset, or tracker.

The research issues need one shared contract that makes weak or incomplete
evidence fail closed while still allowing an explicit no-selection result. The
contract must separate first-candidate selection from later regression checks,
preserve honest `UNKNOWN` results, and keep a release waiver narrower than an
informal approval.

## Decision

### Canonical artifacts

The canonical policy is
[`fixtures/v02-evaluation/policy.json`](../../fixtures/v02-evaluation/policy.json).
It has schema `visualworld.v02-evaluation-policy`, schema version `1`, and policy
version `v0.2-gates-1`. The policy is frozen before #21 begins. The accompanying
generated [dataset manifest](../../fixtures/v02-evaluation/detection-manifest.json)
and [passing receipt](../../fixtures/v02-evaluation/detection-receipt.json) are
contract fixtures, not measured detector evidence or an approved candidate.

The standard-library
[`evaluate_v02_gates.py`](../../scripts/evaluate_v02_gates.py) command validates
the policy, one experiment-specific dataset manifest, and one test-split receipt.
It recomputes aggregates and dispersion from the repetitions, applies the
absolute gates, and applies baseline regression gates only in `regression`
phase. It emits a versioned result bound to the exact candidate and baseline
receipt SHA-256 values, candidate/configuration, dataset, policy,
runtime/profile, and optional baseline. The result contains no raw media,
annotations, paths, or candidate output.

All policy, manifest, receipt, and result numbers are bounded JSON integers.
Rates and percentages use basis points; frame rate and ratios use milli-units.
Floating point, non-finite values, booleans in numeric fields, duplicate object
keys, unknown fields, links, non-regular inputs, oversized documents, mutated
input files, incompatible schemas/digests/profiles, and fabricated aggregates
are rejected.

### Dataset and candidate eligibility

Each experiment uses a separate immutable manifest with distinct `calibration`
and `test` item-ID and annotation digests. The manifest records acquisition
owner/method/source/revision/digest, required strata, commercial-use permission,
allowed uses, redistribution/derivative facts, license expression, consent,
privacy classification, sensitive-content facts, and derived-output retention.

The v0.2 gate set permits only Apache-2.0, CC-BY-4.0, or CC0-1.0 evaluation
data, with commercial evaluation allowed. Gate media must contain no personal
data, real people, faces, or plates, and no sensitive derived output is retained.
This intentionally prefers generated and privacy-safe licensed scenes. A future
need for sensitive evaluation data requires a new policy version and privacy
review; it is not a receipt-level waiver.

Every candidate artifact records its source and terms URLs, immutable revision
and SHA-256, purpose, code/weights/runtime/configuration kind, format, license,
commercial and redistribution facts, isolation, review date, and evaluation
approval. Candidate licenses are limited to Apache-2.0, BSD-2-Clause,
BSD-3-Clause, or MIT. Remote code and executable model serialization are denied.
Pickle-like formats are absent from the format allowlist. Validation records
URLs as inert provenance and never opens or executes them.

### Repetitions and statistics

CPU-LITE is exactly 4 Linux x86_64 vCPUs, at least 16,000,000,000 bytes of RAM,
and no GPU. Each receipt records the CPU model, kernel release, memory, and exact
CPython 3.13/3.14 patch version. One warm-up is excluded, followed by five
measured repetitions using the ordered seeds `1729`, `3253`, `5081`, `7919`,
and `104729`.

The aggregate is the integer median for ordinary metrics, the maximum for peak
RSS, and the adverse-rounded integer mean for run failure and cut-continuation
counts. Dispersion is median absolute deviation in the metric's unit. An adverse
lower-is-better regression is rounded up; an adverse higher-is-better regression
is also rounded away from passing. This prevents an outlier memory breach or
fractional deterioration from being hidden by aggregation/truncation.

Parameter and candidate exploration uses only the manifest's `calibration`
split. A gate receipt always binds the untouched `test` split. `selection` phase
applies absolute gates without a baseline. Once a candidate baseline is frozen,
`regression` phase requires a compatible passing, unwaived baseline with the
same policy, dataset, profile, evaluator, and metric implementation hashes.

### Pre-result thresholds

The initial floors are product-operability thresholds, not claims of
state-of-the-art accuracy:

| Experiment | Absolute gates | Rationale |
| --- | --- | --- |
| Detection #21 | Overall precision/recall at least 70%; easy precision 80% and recall 85%; small/distant precision/recall 50%; macro AP50 overall 50%, easy 65%, small/distant 30%; at least 5 FPS and 1.0× real time; no failed repetition; peak RSS at most 2 GiB; install plus model at most 1 GiB; cold/warm start at most 30/5 seconds | Reject a fast but unusable detector, retain an explicit hard-object floor, sustain the v0.1 near-5-FPS path, and fit the existing CPU-LITE envelope. |
| Sampling #22 | Object-event recall at least 95% overall/cut/VFR and 90% for small-fast/camera-motion; missed-track opportunity rate at most 5% overall/cut/VFR and 10% for hard motion strata; at least 1.0× real time; no failed repetition; peak RSS at most 2 GiB | Favor recall before compute savings and prevent a cheap policy from erasing the events tracking needs. Sample and storage rates remain diagnostics used to choose the least-cost passing policy. |
| Crop value #23 | Source-box error at most one pixel and exact bytes on generated goldens; crop latency at most one second; no failed repetition; peak RSS at most 2 GiB | Geometry and byte identity are correctness invariants. Readable-detail and specialist-accuracy gains, evidence bytes, and storage tradeoffs remain diagnostic because #23 exists to measure whether a value threshold is justified. `UNKNOWN` is valid only for those diagnostics. |
| Tracking #24 | Overall HOTA at least 45% and IDF1 60%, with 40%/50% hard-stratum floors; at most 50 ID switches and 100 fragmentations per 1,000 track frames overall, relaxed to 75/125 in hard strata; zero false continuation across cuts; at least 1.0× real time; no failed repetition; peak RSS at most 2 GiB | Establish a non-trivial short-term identity floor while making cut termination a hard safety invariant and preserving CPU-LITE operability. Persistent identity remains out of scope. |

Accuracy and identity metrics use a 5% relative regression budget. Throughput,
latency, footprint, and RSS use 10%. Exact-byte, one-pixel, zero-failure, and
zero-cut-continuation invariants use an absolute zero tolerance instead of a
relative budget because a zero baseline has no meaningful relative denominator.
Diagnostic metrics have no pass threshold or regression budget and cannot make
a receipt pass by replacing a required gate with `UNKNOWN`.

The calculation identifiers in the policy name the metric contracts. Each
experiment must freeze the actual evaluator source/revision/digest before the
test split is opened. Precision and recall use one-to-one class-matched IoU 0.50
assignments; AP50 is the macro class average at IoU 0.50. Object-event recall and
missed-track opportunities use manifest event intervals. Mapping error is the
maximum source-box coordinate error in milli-pixels. HOTA and IDF1 use the
receipt's pinned TrackEval-compatible implementation; switch, fragmentation,
and false-cut counts use the locked manifest identities and cut boundaries.
Changing calculation code or annotations invalidates compatibility even when a
metric keeps the same display name.

The reference semantics are the official
[COCO evaluator](https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocotools/cocoeval.py),
the primary [HOTA paper](https://arxiv.org/abs/2009.07736), and its official
[TrackEval reference implementation](https://github.com/JonathonLuiten/TrackEval).
Those links are design sources, not mutable runtime dependencies or approval to
download them. Each experiment records an immutable revision, digest, license,
and source URL in its receipt before the metric result is accepted.

### Waivers

A failed gate normally produces `fail`. A `waived` result is possible only when
the receipt embeds an exact waiver whose owner is `mayank-gupta16`, scope equals
every and only the failed absolute/regression gates, risk and review trigger are
non-empty, at least one compensating control exists, and a repository follow-up
issue is named. The waiver can last at most 30 days and must be unexpired both on
the evaluation date and the caller's explicit/current `as-of` date.

A waiver cannot repair malformed evidence, invalid rights/privacy, an
incompatible baseline/profile, future-dated evaluation, unapproved artifacts,
or missing metrics. A passing result carrying an unnecessary waiver is invalid.

### Amendments

Prior policy, manifests, receipts, and results are immutable evidence. A change
creates a new policy version and records why the old evidence is no longer
directly comparable. Threshold recommendations from #21–#24 may produce such an
amendment before epic #30 is decomposed, but must receive the same code,
security, privacy, licensing, and benchmark review. They do not rewrite
`v0.2-gates-1` or retroactively convert diagnostics into passing evidence.

#### Accepted non-numeric amendment: `v0.2-gates-2`

Issue #21's security/licensing review found that a wheel-level artifact cannot
truthfully use only its project's primary license when the exact wheel metadata
and bundled notices describe a wider closure. The accepted
[`v0.2-gates-2` amendment](../../fixtures/v02-evaluation/policy-v0.2-gates-2.json)
binds the original policy SHA-256 and inherits all of its datasets, metrics,
numeric gates, repetitions, statistics, profile, and waiver rules unchanged.

For amended receipts, every artifact distinguishes upstream redistribution
permission from project distribution approval. Project distribution remains
`not-approved`. Runtime wheels carry a gate-enforced license-evidence object:
the package metadata expression, the complete embedded license/notice path and
digest inventory, and explicitly named bundled native components. The receipt
also binds a verified runtime-closure digest. The pinned CPython build is exempt
from wheel evidence because its composite license and no-redistribution boundary
are maintained in the interpreter ledger; its archive, executable, and
location-independent runtime-tree digests remain part of the benchmark closure.

The original policy and receipts remain intact historical evidence. Issue #21's
superseding measurements use `v0.2-gates-2`; they do not claim compatibility
with the earlier receipt hashes.

## Consequences

- #21 can begin against stable protocol and eligibility boundaries.
- #22–#24 still wait for the selected detector baseline required by their issue
  dependencies; the gate policy alone does not unblock them.
- A candidate can legitimately produce no selection instead of weakening a gate.
- The initial numeric floors are deliberately reversible before v1.0, but every
  revision is explicit, reviewable, and incompatible with prior evidence.
- Model/runtime/data acquisition remains separate work with its own immutable
  inventory entries and legal review.

## Alternatives rejected

- **Choose thresholds after benchmarking:** exposes the test set and rewards
  candidate-specific goalpost movement.
- **Store floating-point metrics:** permits non-finite values and
  platform-dependent boundary behavior.
- **Use one average without strata:** lets easy scenes hide small/distant,
  motion, cut, occlusion, or dense-crossing failures.
- **Treat missing values as zero or ignore them:** confuses unsupported evidence
  with a measured result. Required gates fail on `UNKNOWN`; explicitly named
  diagnostics report it.
- **Allow informal milestone waivers:** cannot prove owner, scope, expiry, risk,
  controls, or follow-up at release time.
