# Developer setup and validation

The `0.1.0` package is the first experimental ingestion-foundation release with
a deterministic fake/manual CLI slice, not yet a real-video ingestion or query
application. It has no third-party runtime packages. Release artifacts are
published on GitHub; no package-registry publishing configuration exists.

## Supported environments

CI tests ordinary-GIL CPython **3.13.15** and **3.14.7** on GitHub-hosted
`ubuntu-24.04` x86_64 and `macos-15` arm64. Bootstrap rejects other OS/architecture
pairs. Runner labels are not immutable OS images: each run logs host details;
interpreter/uv inputs are pinned independently. CPU-LITE validation uses the
disposable Ubuntu VM. Windows, Intel macOS, musl Linux, other Python builds, and
release installers are not claimed. A trusted host Python 3.9+ is needed only
to start the standard-library bootstrap.

From a trusted checkout:

```sh
python3 scripts/bootstrap.py
python3 scripts/dev.py sync
python3 scripts/dev.py check
python3 scripts/dev.py build
python3 scripts/dev.py audit
```

Repeat with `--python 3.14.7` on **each** command for that lane. Each version gets
a separate ignored `artifacts/toolchain/<version>` directory. Use
`--state /absolute/private/path` consistently to put it elsewhere. Keep it
private to the developer/job. No system Python, shell startup file, global tool,
or convenient global Python link is changed.

Bootstrap verifies the exact official uv 0.12.6 archive and executable SHA-256
before execution, extracting only the named regular executable. It supplies uv
a local one-record Python manifest with the approved python-build-standalone
20260825 URL/SHA-256. uv verifies the archive; bootstrap checks version/GIL mode
and records the executable digest. Reuse checks that saved digest. Host Python,
TLS roots, OS, and private tool directory remain trusted; this does not attest
every library mutation or the entire OS. Managed Python is for dev/CI only,
not redistribution.

## Command boundaries

| Command | Work | Network |
| --- | --- | --- |
| `bootstrap.py` | Reviewed uv/Python acquisition and receipt | Official GitHub releases |
| `dev.py sync` | Lock check; exact editable all-groups sync, no new third-party source builds | Approved public PyPI artifacts as needed |
| `dev.py check` | Lock, Ruff, strict mypy/pytest, application coverage, licenses, repository policy | uv offline/no-sync; checks do not intentionally use network |
| `dev.py build` | Hash-constrained sdist then wheel, fresh runtime, contents/SBOM, bad-hash control | Exact approved backend wheel if uncached |
| `dev.py audit` | Hashed lock export and pip-audit without pip resolution | Public dependency names/versions and advisory service |

The wrapper clears inherited `UV_*`, `PIP_*`, Python-path/home overrides and
virtual-environment selectors before setting private paths. Do not use untrusted
shared caches: uv can reuse a previously built cached wheel despite `no-build`.
Fresh CI restores no dependency cache. Host proxy/certificate settings are not
an application egress policy.

Unit/contract tests use only project-generated synthetic-v1 media; they use no
external media, models, downloads, inference, or services. The pure-Python
fixture generator runs in every supported CI lane, compares all output bytes to
the reviewed manifest, and fails on unexpected files, symlinks, or checksum/size
drift. Tooling tests use tiny temporary archives and mocked network/process
operations.
An offline uv flag is not an OS network sandbox; hostile-media boundaries belong
to the media ADR. The 90% coverage floor measures application source with branch
tracking. Tooling tests provide separate regression evidence, not inflated
application coverage.

Build output goes to fresh ignored `artifacts/build-*` directories with exact
sizes/hashes printed. Runtime-only CycloneDX 1.6 inventory comes from the newly
installed wheel, excluding dev tools and the external interpreter. The runtime
must contain only `visualworld-engine`, import outside the checkout, and pass
module/console help/version/probe plus the exact-crop CLI quickstart. No upload
step exists.

Forced PEP 517 and `--require-hashes` are mandatory: uv's bundled fast path does
not exercise the external backend hash. The negative control must report a hash
mismatch even with the correct backend cached. uv 0.12.6 also splits spaces in an
absolute constraint path; the wrapper uses the reviewed repository-relative
filename with a fixed working directory. Source/output paths may contain spaces.

To generate the three ephemeral CFR, VFR, and rotation fixtures manually, pass a
fresh empty output directory:

```sh
python3 scripts/generate_synthetic_fixtures.py \
  --output artifacts/fixtures/synthetic-v1
```

The command needs only the Python standard library and reviewed repository
source. It does not call FFmpeg, fetch media, or overwrite a non-empty output
directory. Its JSON receipt reports wall time and process peak RSS as a generous
PR-CI boundedness check, not as cross-machine optimization evidence. The
[canonical manifest](../../fixtures/synthetic-v1/manifest.json) contains only
deterministic facts.

The Linux-only local-video acceptance command requires the accepted root-owned
media runtime and its reviewed worker installed inside that runtime:

```sh
PYTHONPATH=src /opt/visualworld-runtime-probe/python/bin/python3.13 \
  scripts/run_media_acceptance.py \
  --runtime /opt/visualworld-runtime-probe \
  --worker /opt/visualworld-runtime-probe/worker/media_worker.py \
  --work-root /private/visualworld-validation \
  --output /private/visualworld-validation/media-acceptance.json
```

It regenerates only the approved fixtures and checks exact probe/frame facts,
structured corrupt/oversize/timeout/cancellation/path failures, and CPU-LITE
wall/RSS evidence. It is not a cross-machine optimization benchmark. The latest
reviewed result is the [issue #11 validation record](../research/local-video-source.md).

The exact-PTS sampler acceptance reuses that runtime and the same generated
fixtures:

```sh
PYTHONPATH=src /opt/visualworld-runtime-probe/python/bin/python3.13 \
  scripts/run_sampling_acceptance.py \
  --runtime /opt/visualworld-runtime-probe \
  --worker /opt/visualworld-runtime-probe/worker/media_worker.py \
  --work-root /private/visualworld-validation \
  --output /private/visualworld-validation/sampling-acceptance.json
```

It checks original exact PTS/identities, CFR/VFR goldens, repeated and paged
resume equivalence, cancellation, frame/duration caps, and a CPU-LITE
throughput/RSS baseline. It does not tune the policy for this machine.

The dependency-free original-pixel geometry/crop acceptance runs on the
reference managed interpreter and a private existing work directory:

```sh
PYTHONPATH=src artifacts/toolchain/3.13.15/python/\
cpython-3.13.15-linux-x86_64-gnu/bin/python3 \
  scripts/run_crop_acceptance.py \
  --work-root /private/visualworld-validation \
  --output /private/visualworld-validation/crop-acceptance.json
```

It verifies exact and half-scale detector mappings, the rotation fixture's
source-pixel crop hashes, private-root and traversal/symlink confinement, and the
60-second, 1080p, 5-FPS crop-copy CPU/RSS baseline. Missing or non-positive RSS
and timing observations fail the receipt. It emits only dimensions, counts,
hashes, policy names, and resource metrics—not crop bytes.

The dependency-free local EvidenceStore acceptance runs on the same managed
interpreter and a private existing work directory:

```sh
PYTHONPATH=src artifacts/toolchain/3.13.15/dev/bin/python \
  scripts/run_storage_acceptance.py \
  --work-root /private/visualworld-validation \
  --output /private/visualworld-validation/storage-acceptance.json
```

It uses deterministic 16 MiB synthetic payloads to check durable unique writes,
verified reads, dedupe, separate staging/promotion, bounded audit, deletion,
logical/allocated disk cost, hash/write throughput, wall/process CPU time, and
peak RSS. The JSON receipt omits store paths, artifact/stage/run identifiers,
artifact digests, and content.

The dependency-free local WorldStore acceptance uses the same profile and a
private existing work directory:

```sh
PYTHONPATH=src artifacts/toolchain/3.13.15/dev/bin/python \
  scripts/run_world_store_acceptance.py \
  --work-root /private/visualworld-validation \
  --output /private/visualworld-validation/world-store-acceptance.json
```

It commits one 60-second 1080p/5-FPS metadata workload (300 frames and 300
evidence records) through preparing, intent, hidden record-batch, finalization,
retry, paginated frame lookup, per-frame evidence lookup, reopen, and bounded
verification. The receipt reports transaction/index timings, SQLite/WAL disk
cost, CPU time, peak RSS, implementation hashes, and aggregate counts; it omits
store paths and all source/run/frame/evidence/artifact identifiers.

The dependency-free coordinator acceptance composes the real local stores with
the deterministic fake source/sampler and a manual source-pixel region:

```sh
PYTHONPATH=src artifacts/toolchain/3.13.15/dev/bin/python \
  scripts/run_coordinator_acceptance.py \
  --work-root /private/visualworld-validation \
  --output /private/visualworld-validation/coordinator-acceptance.json
```

It verifies a deterministic manifest, idempotent retry, byte-exact retrievable
RGB24 evidence, offline adapter capabilities, checkpointed source deletion, and
per-stage wall costs. The receipt contains aggregate dimensions, counts,
resource measurements, and implementation hashes; it omits store paths,
record/deletion identifiers, and pixel bytes. The combined 60-second application
baseline remains assigned to the v0.1 benchmark issue. The reviewed CPU-LITE
result is retained in the
[machine-readable receipt](../research/evidence-safe-coordinator-cpu-lite-receipt.json).

The deterministic CLI acceptance runs each user-visible command in a fresh
process and checks canonical success/error JSON, stable exit behavior, the
byte-exact default RGB24 crop, path/control-sequence redaction, process wall
time, child peak RSS, and store disk use:

```sh
PYTHONPATH=src artifacts/toolchain/3.13.15/dev/bin/python \
  scripts/run_cli_acceptance.py \
  --work-root /private/visualworld-validation \
  --output /private/visualworld-validation/cli-acceptance.json
```

It uses only the built-in 2×2 deterministic fixture and omits store paths,
record identifiers, and pixel bytes from its receipt. The reviewed CPU-LITE
result is retained in the
[machine-readable receipt](../research/cli-vertical-slice-cpu-lite-receipt.json).

The v0.1 release-gate regression suite composes the public CLI with reopened
stores and test-generated synthetic bytes. It checks exact ingest/inspect/list/
show/crop goldens, idempotent retry, crash recovery, deletion recovery, corrupt
and oversized inputs, path and symlink confinement, inert hostile metadata,
redaction, and no application network or process egress:

```sh
PYTHONPATH=src artifacts/toolchain/3.13.15/dev/bin/python \
  scripts/run_v01_regressions.py \
  --work-root /private/visualworld-validation \
  --output /private/visualworld-validation/v01-regressions.json
```

Its receipt contains only aggregate results, hashes, platform facts, and
resource measurements. It omits temporary paths, record/deletion identifiers,
hostile strings, and pixel bytes. The reviewed CPU-LITE result is retained in
the [machine-readable receipt](../research/v01-regressions-cpu-lite-receipt.json).

The combined v0.1 CPU-LITE benchmark exercises the full released fake/manual
boundary over a virtual 60-second 1920×1080 source: bounded exact-PTS sampling,
300 full-resolution crop/hash operations, transactional frame/evidence storage,
reopen/verification, retrieval, and paginated index queries. It generates only
Apache-2.0 synthetic bytes, retains no temporary store, and makes no decode,
inference, model-accuracy, or external-dataset claim:

```sh
revision=$(git rev-parse HEAD)
PYTHONPATH=src artifacts/toolchain/3.13.15/python/\
cpython-3.13.15-linux-x86_64-gnu/bin/python3 \
  scripts/run_v01_benchmark.py \
  --work-root /private/visualworld-validation \
  --output /private/visualworld-validation/v0.1-candidate.json \
  --revision "$revision"

PYTHONPATH=src artifacts/toolchain/3.13.15/python/\
cpython-3.13.15-linux-x86_64-gnu/bin/python3 \
  scripts/compare_v01_benchmarks.py \
  --baseline docs/benchmarks/v0.1-cpu-lite-baseline.json \
  --candidate /private/visualworld-validation/v0.1-candidate.json \
  --output /private/visualworld-validation/v0.1-comparison.json
```

Both tools emit only a canonical status to the terminal and put detailed
aggregate output in the requested JSON file. The comparator requires compatible
fixture, configuration, CPU-LITE/runtime provenance, and positive metrics. It
enforces the fixed 2 GiB RSS ceiling and the fixture manifest's initial 20%
single-run regression budget. The reviewed result, boundary, stage table, and
exact receipt are in the
[v0.1 CPU-LITE baseline report](../benchmarks/v0.1-cpu-lite-baseline.md).

Before v0.2 model research, validate the frozen evaluation policy and its
generated contract fixture with a new private output path:

```sh
validation_root=$(mktemp -d /tmp/visualworld-v02-gates.XXXXXX)
python3 scripts/evaluate_v02_gates.py \
  --manifest fixtures/v02-evaluation/detection-manifest.json \
  --receipt fixtures/v02-evaluation/detection-receipt.json \
  --as-of 2026-09-07 \
  --output "$validation_root/result.json"
```

The evaluator uses only the standard library and inert JSON. It rejects
duplicate/unknown fields, non-finite or boolean numbers, links/non-regular or
mutated inputs, incompatible hashes/profiles, fabricated aggregates, missing
gate values, stale/overbroad waivers, and output overwrite. See the
[v0.2 gate operator record](../benchmarks/v0.2-evaluation-gates.md) and
[ADR-0006](../decisions/ADR-0006-v02-evaluation-gates.md) before producing a
research receipt.

## Updates

Follow [ADR-0002](../decisions/ADR-0002-application-toolchain.md): one direct
package per focused reviewed PR, exact uv, seven-day cutoff, artifact/license/lock
review, clean four-lane installs, packaging, and audit. Only a documented urgent
security fix may waive the cooling period.

Root `uv.lock` is the application/developer lock. The dated
`experiments/toolchain-20260906` tree is frozen and must not be rewritten by
updates. Python updates remain manual until automation honors these constraints.
Actions stay SHA-pinned, separately inventoried, and never auto-merged.

See the [dependency ledger](../legal/dependencies.md),
[supply-chain policy](../security/supply-chain.md), and
[scaffold validation record](../research/package-scaffold.md).
