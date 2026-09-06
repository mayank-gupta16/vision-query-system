# ADR-0002: Application toolchain and lock strategy

- Status: Accepted
- Date: 2026-09-06
- Decider: repository maintainer through delegated independent agent review
- Issue: https://github.com/mayank-gupta16/vision-query-system/issues/3

## Context

VisualWorld needs a small, reproducible application foundation before media,
storage, or model code is introduced. The foundation must work on the mandatory
CPU-LITE profile, keep the deterministic domain independent from vendor SDKs,
support exact transitive resolution, and avoid silently executing package source
during installation. The initial Python-oriented `.gitignore` was not an
implementation decision.

The clean-install experiment and measurements supporting this decision are in
[the toolchain research report](../research/application-toolchain.md).

## Decision

### Language and supported runtimes

1. Implement the v0.1 application and deterministic core in Python. Use standard
   GIL-enabled CPython 3.13.15 as the reference developer and release runtime.
   Declare package compatibility as `>=3.13,<3.15` and continuously test exact
   CPython 3.13.15 and 3.14.7.
2. Do not enable free-threaded CPython or the experimental JIT by default. A
   future change requires measurements against the ordinary build and a separate
   compatibility decision.
3. Keep the domain, evidence, provenance, uncertainty, and query contracts pure
   Python and free of adapter SDK types. Rust or another native extension may be
   proposed only for a measured hotspot that materially prevents a ratified
   CPU-LITE gate; it requires an ADR, portable-wheel plan, license inventory, and
   pure-Python or structured unsupported-path behavior. TypeScript is deferred
   until a browser or desktop client exists.
4. Tier-1 application platforms are Linux x86_64 and macOS arm64. Windows and
   other architectures are unclaimed until their clean-install and test lanes
   exist. Platform-independent core design remains required.

### Package and repository layout

1. Use PEP 621 metadata in one root `pyproject.toml`, a `src/visualworld`
   package, a `tests` tree, and a `visualworld` console entry point.
2. Use `visualworld-engine` as the distribution name and `visualworld` as the
   import package and CLI name. HTTP 404 responses from PyPI on 2026-09-06 are
   not a name reservation; issue #8 must recheck availability before any publish
   configuration is enabled. Publishing remains disabled until a release issue
   explicitly authorizes it.
3. Start with one package and no uv workspace. Optional media, storage, and model
   adapters remain separate dependency groups or separately locked projects once
   their own ADRs approve them. The root application has zero third-party runtime
   dependencies at scaffold time.

### Environment, resolution, and build

1. Use uv 0.12.6, exact, as the environment and lock manager. It was released on
   2026-08-25, clears the initial seven-day adoption window, and is licensed
   `Apache-2.0 OR MIT`. Pin the uv executable outside `uv.lock`; bootstrap and CI
   instructions must identify the official release URL and SHA-256 for each
   supported platform. Do not commit the executable.
2. Commit uv's universal `uv.lock`. CI must run `uv lock --check` and
   `uv sync --locked --all-groups`; ordinary developer and test syncs install the
   first-party project editable. Commands that only consume the environment use
   `--locked` or `--frozen` as appropriate and must not mutate the lock.
3. Configure resolution with `first-index`, prereleases disabled, a fixed
   initial `exclude-newer = "2026-08-30T00:00:00Z"` timestamp, and required
   environments for Linux x86_64 and macOS arm64. Set `no-build = true` to reject
   new third-party source builds. Use clean or trusted wheel caches: uv can reuse
   a previously built cached wheel despite `no-build`. Because required-environment
   resolution alone does not prove that every package has a usable wheel, every
   lock update must complete a clean no-build sync on both supported platforms.
   Direct URLs, VCS requirements, path dependencies outside the repository, and
   alternate indexes are deny-by-default and require explicit supply-chain review.
4. Use `uv_build==0.12.6` as the exact PEP 517 backend. Keep the exact backend
   constraint in project configuration and a checked-in build-constraint file
   containing only the approved Linux x86_64 and macOS arm64 wheel hashes.
   Release-package validation must use `uv build --force-pep517 --no-sources`
   with that constraint file and `--require-hashes`, then install the newly built
   wheel into a clean environment and smoke-test import and CLI behavior.
   `--force-pep517` is mandatory because uv otherwise uses its bundled fast path,
   which does not consume or validate the external backend wheel hash.
5. Treat the exact lock as the dependency-integrity record. It must contain
   registry artifact hashes for every selected distribution. Export hash-bearing
   audit input from that lock; do not hand-maintain a second resolver input.
   A lock-derived CycloneDX inventory is useful review evidence. The release
   SBOM must describe the actual shipped environment, including native components
   and excluding unshipped development tools, as the release gates require.

### Development checks

Issue #8 will lock the resolved versions demonstrated by the experiment:

| Function | Selected tool | Initial resolved version | License |
| --- | --- | --- | --- |
| Format and lint | Ruff | 0.16.5 | MIT |
| Static typing | mypy | 2.3.1 | MIT |
| Unit and contract tests | pytest | 9.1.1 | MIT |
| Branch coverage | coverage.py | 7.16.0 | Apache-2.0 |
| Dependency audit | pip-audit | 2.10.1 | Apache-2.0 |

Ruff formatting and linting, strict mypy, strict pytest configuration, and branch
coverage are required. The initial repository-wide coverage floor is 90%; new
deterministic safety-critical modules should target 100% branch coverage and may
define a higher local gate. Coverage must not be lowered to merge a change.

Do not add pre-commit, tox, nox, Poetry, PDM, a task-runner package, Node, or a
native compiler to the initial scaffold. A tool may be added later only when it
removes a demonstrated gap without duplicating an existing check.

### Update policy

1. Review toolchain and dependency updates weekly, but update one direct package
   at a time. Advance the fixed upload cutoff only after a seven-day cooling
   period. A documented urgent security fix may waive the delay.
2. Regenerate the universal lock with the exact approved uv executable. Review
   the dependency, artifact, platform, license, vulnerability, and footprint
   diff; run the full Linux and macOS matrix and a clean-wheel smoke test.
3. Never auto-merge dependency or action updates. Pin GitHub Actions to immutable
   commit SHAs and keep their inventory separate from Python packages.

## Alternatives

### CPython 3.14 as the sole baseline

It passed the Linux compatibility experiment, but 3.13 remains the safer initial
baseline for native media and ML ecosystem availability. Keeping 3.14 in CI
detects forward-compatibility problems without making every later adapter depend
on the newest interpreter immediately.

### Older CPython or multiple implementation languages immediately

Python 3.12 would widen compatibility but shorten the useful support horizon and
offers no required feature for this zero-runtime-dependency scaffold. It was not
benchmark-tested in this experiment. A Rust-first core would
add compiler, wheel, unsafe-code, and cross-platform supply-chain work before a
hotspot exists. A TypeScript-first core would not match the expected local media
and ML adapter ecosystem.

### pip/venv with pip-tools

This uses familiar standards and permissively licensed tools, but requires more
pieces to manage Python acquisition, environment sync, build constraints,
cross-platform artifacts, and a universal lock. The alternative was evaluated
from its documented workflow, not benchmark-tested against uv.

### Poetry or PDM

Both provide integrated, permissively licensed project management. Each adds a
larger Python-managed frontend while uv already supplied exact environment,
locking, Python, build, and export operations with a single native executable.

### Hatchling, setuptools, or another initial build backend

These are credible established backends. `uv_build` produced identical tiny
artifacts on both measured platforms and keeps the chosen toolchain narrow. The
forced PEP 517, hash-constrained packaging lane preserves an independent build
boundary instead of relying on uv's bundled backend shortcut.

### A uv workspace from the beginning

Workspaces help coordinated multi-package repositories, but they share one lock.
VisualWorld has only one approved package today, while future heavy adapters may
need intentionally independent platform and license boundaries. Add a workspace
only after a concrete package topology is accepted.

## Consequences

- Issue #8 may create the package scaffold, exact lock, developer commands, and
  CI matrix within these boundaries.
- Media runtimes, native codecs, storage clients, model SDKs, and model weights
  remain unapproved and cannot enter the root dependency set through this ADR.
- Python is a stable implementation boundary, not a promise that every future
  adapter is pure Python.
- The 3.13/3.14 and Linux/macOS matrix adds CI cost but makes declared
  compatibility testable.
- The wheel-only policy may exclude useful packages that publish only source;
  accepting one requires a focused ADR and reviewed build process.
- Dev-only transitive MPL-2.0 packages found in the experiment are not shipped
  with VisualWorld. If a future artifact redistributes its development
  environment, the relevant license files, covered-source obligations, and
  notices must be handled before release.
- The package name remains provisional until issue #8 verifies registry state;
  changing a pre-release distribution name has no API compatibility cost.
