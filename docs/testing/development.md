# Developer setup and validation

The `0.1.0a0` package is an experimental help/version scaffold, not a video
ingestion or query application. It has no third-party runtime packages and no
publishing configuration.

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
tracking; this scaffold has no application branches. Tooling tests provide
separate regression evidence, not inflated application coverage.

Build output goes to fresh ignored `artifacts/build-*` directories with exact
sizes/hashes printed. Runtime-only CycloneDX 1.6 inventory comes from the newly
installed wheel, excluding dev tools and the external interpreter. The runtime
must contain only `visualworld-engine`, import outside the checkout, and pass
module/console help/version checks. No upload step exists.

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
