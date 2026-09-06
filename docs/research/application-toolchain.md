# Application toolchain clean-install experiment

- Issue: https://github.com/mayank-gupta16/vision-query-system/issues/3
- Experiment date: 2026-09-06
- Result: supports [ADR-0002](../decisions/ADR-0002-application-toolchain.md)

## Question and hypothesis

Can a Python-first, zero-runtime-dependency VisualWorld scaffold provide an exact
cross-platform lock, strict developer checks, an independently hash-verified
build, and a small CPU-LITE footprint without introducing a container, compiler,
or JavaScript toolchain?

The hypothesis was that CPython 3.13 plus one pinned uv executable and a PEP 621
`src` package would satisfy those constraints. CPython 3.14 was tested as a
compatibility lane rather than assumed safe.

## Candidate assessment

| Candidate | Strength | Cost or risk | License conclusion | Result |
| --- | --- | --- | --- | --- |
| CPython + uv + uv_build | One executable covers Python acquisition, universal lock, sync, build, and export; wheel availability can be required per platform | uv is separately bootstrapped; its bundled backend fast path can bypass external backend hash verification unless PEP 517 is forced | CPython uses the PSF license stack; uv and uv_build are `Apache-2.0 OR MIT` | Selected |
| CPython + pip/venv + pip-tools | Familiar PyPA components and requirements hash mode | More independently versioned pieces; no single project environment/Python workflow or native universal lock | Permissive; pip-tools is BSD-3-Clause | Not selected |
| Poetry | Integrated project, lock, environment, and publishing workflow | Additional Python frontend and workflow; not benchmark-tested here | MIT | Not selected |
| PDM | PEP-oriented integrated project manager with lock and environment features | Another Python frontend; overlaps the chosen uv capabilities | MIT | Not selected |
| Rust-first core | Strong performance ceiling and static native artifact | Compiler, unsafe/native review, platform wheels, and FFI before a measured hotspot | Tool and crate-specific inventory required | Deferred |
| TypeScript-first core | Natural future web-client language | Does not fit the local Python media/ML adapter ecosystem; adds Node supply chain | Tool and package-specific inventory required | Deferred |

Primary candidate-license evidence: [uv 0.12.6 Apache license](https://github.com/astral-sh/uv/blob/0.12.6/LICENSE-APACHE),
[uv 0.12.6 MIT license](https://github.com/astral-sh/uv/blob/0.12.6/LICENSE-MIT),
[pip-tools BSD-3-Clause license](https://github.com/jazzband/pip-tools/blob/main/LICENSE),
[Poetry MIT license](https://github.com/python-poetry/poetry/blob/main/LICENSE),
[PDM MIT license](https://github.com/pdm-project/pdm/blob/main/LICENSE), and
[Hatch MIT license](https://github.com/pypa/hatch/blob/master/LICENSE.txt).

## Probe configuration

The [frozen experiment inputs](../../experiments/toolchain-20260906/README.md)
include the complete manifest, lock, build hashes, source, tests, and replay
commands. They are independent of the application scaffold in issue #8.

The disposable probe used:

- PEP 621 metadata and a `src/visualworld_probe` package with import, module, and
  console-entry smoke tests;
- `requires-python = ">=3.13,<3.15"` and `.python-version` set to 3.13.15;
- uv and uv_build exactly 0.12.6;
- `first-index`, prereleases disabled, `no-build = true`, and required wheel
  environments for Linux x86_64 and macOS arm64;
- fixed upload cutoff `2026-08-30T00:00:00Z`;
- Ruff `>=0.16.5,<0.17`, mypy `>=2.3.1,<2.4`, pytest `>=9.1.1,<10`,
  coverage `>=7.16,<8`, and pip-audit `>=2.10.1,<2.11`;
- no application runtime dependencies.

The resulting universal lock contains 41 packages, installs 40 packages including
the probe, and has SHA-256
`3527d93219a14b0e3eef4aa1cade762a02fdff8fed82da9d2bf93d5176e04c72`.
The same lock bytes were produced on both measured platforms. The build backend
constraint is recorded in the lock header, but the backend distribution is not a
normal locked package entry, so the packaging lane verifies it separately.

## Immutable inputs

| Input | Source/revision | SHA-256 or identity |
| --- | --- | --- |
| uv 0.12.6 source | [tag 0.12.6](https://github.com/astral-sh/uv/releases/tag/0.12.6), commit `7938ca5d53dbb9c614a4a030df406e41ff101ab9` | Tag published 2026-08-25T19:41:07Z |
| uv Linux x86_64 archive | [official release artifact](https://github.com/astral-sh/uv/releases/download/0.12.6/uv-x86_64-unknown-linux-gnu.tar.gz) | `8681d8921e7d520fb368991dcf5f9c1905b80f5bf2a265a0ed085c8d8e342477` |
| uv macOS arm64 archive | [official release artifact](https://github.com/astral-sh/uv/releases/download/0.12.6/uv-aarch64-apple-darwin.tar.gz) | `14b459d51ea2e71eeba28c45a268c922bdf8607fc6455e3f40b4e082895d160d` |
| Extracted uv Linux x86_64 executable | From the verified archive above | `d381f11517c66523211b0876552ff7dea5c1b4b0f13800571b35225761302fba` |
| Extracted uv macOS arm64 executable | From the verified archive above | `e8929237934c8679686428f5a7736c7ae7a5fe7a33b0504d1b03446cdbc43c94` |
| uv_build 0.12.6 Linux x86_64 wheel | [PyPI release](https://pypi.org/project/uv-build/0.12.6/) | `1597b3f7de552f7ddd23bc1c2f4adddde7cdb5a48d644cd38bdb94aca98e64ab` |
| uv_build 0.12.6 macOS arm64 wheel | [PyPI release](https://pypi.org/project/uv-build/0.12.6/) | `d483396368b57ba21bf89b06cd14d1d248074d90e1677e9e8c9fb10b407e4d8e` |
| Managed CPython 3.13.15 Linux x86_64 | [python-build-standalone 20260825 artifact](https://github.com/astral-sh/python-build-standalone/releases/download/20260825/cpython-3.13.15%2B20260825-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz) | `8af9a8214c71b2dd698005e39fab87aad02a994330508857da4e6d1ba7e6ddb6` |
| Managed CPython 3.13.15 macOS arm64 | [python-build-standalone 20260825 artifact](https://github.com/astral-sh/python-build-standalone/releases/download/20260825/cpython-3.13.15%2B20260825-aarch64-apple-darwin-install_only_stripped.tar.gz) | `149038dd0c194c25d4616d7e42a35f67f2edee96412788f74115819b6a4c8548` |
| Managed CPython 3.14.7 Linux x86_64 | [python-build-standalone 20260825 artifact](https://github.com/astral-sh/python-build-standalone/releases/download/20260825/cpython-3.14.7%2B20260825-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz) | `a0f39e822fd3b96a2605f30f954acf9a2cdf91faa1385a13e24bc407a41e05ea` |

The controlled experiment used uv-managed `python-build-standalone` artifacts
to remove host-package-manager drift. They are developer/CI interpreter inputs,
not VisualWorld release contents. Their composite license inventory is addressed
in [the dependency ledger](../legal/dependencies.md); calling the complete binary
only a CPython license identifier would omit bundled-component notices.

uv 0.12.10 was already available, but its official release timestamp was
2026-09-04T23:15:57Z. It had not cleared the selected seven-day cooling period,
so the experiment retained 0.12.6 rather than treating newest as automatically
safer.

## Environments and method

The CPU-LITE node was a disposable Ubuntu 26.04.1 LTS x86_64 VM with 4 vCPU,
15 GiB visible RAM, 8 GiB swap, and no required GPU. The second host was macOS
26.6.2 arm64. Network-backed Python setup, cold sync, and warm offline replay were
timed separately; checks and package smoke tests were pass/fail checks.
`/usr/bin/time` peak RSS and
filesystem allocation were recorded; each timing is one run and is diagnostic,
not a performance gate.

Checks were Ruff format/lint, strict mypy, strict pytest, branch coverage, and a
hash-bearing pip-audit input. Both Linux interpreter lanes and the macOS 3.13 lane
ran the same source/tests. Packaging built the sdist and then the wheel from that
sdist. The wheel was installed into a clean runtime-only environment before
import, `python -m`, and console-script smoke tests.

## Results

| Host/runtime | Operation | Wall time | Peak RSS | Allocated footprint/result |
| --- | --- | ---: | ---: | --- |
| CPU-LITE / CPython 3.13.15 | Managed Python cold install | 0.78 s | 55,316 KiB | 104 MiB interpreter tree |
| CPU-LITE / CPython 3.13.15 | Initial universal lock | 0.22 s | 67,804 KiB | 41 packages; initially 133,188 bytes |
| CPU-LITE / CPython 3.13.15 | Cold locked dev sync | 0.83 s | 71,416 KiB | 110 MiB environment; 9.7 MiB task cache |
| CPU-LITE / CPython 3.13.15 | Warm offline replay | 0.03 s | 39,336 KiB | Identical installed package list |
| CPU-LITE / CPython 3.14.7 | Managed Python cold install | 0.79 s | 56,288 KiB | Compatibility runtime installed |
| CPU-LITE / CPython 3.14.7 | Cold locked dev sync | 0.64 s | 70,540 KiB | 114 MiB environment; 2.4 MiB task cache |
| macOS arm64 / CPython 3.13.15 | Managed Python cold install | 3.79 s | 36,651,008 bytes | 69 MiB interpreter tree |
| macOS arm64 / CPython 3.13.15 | Cold locked dev sync | 6.11 s | 63,897,600 bytes | 119 MiB environment; 123 MiB task cache |
| CPU-LITE clean runtime venv | Install built wheel | not separately timed | not separately recorded | 172 KiB project allocation; zero third-party runtime packages |

Format, lint, type, test, and coverage checks passed on Linux 3.13, Linux 3.14,
and macOS 3.13. With branch measurement enabled, the tiny probe covered all six
statements and contains no branches. The Linux 3.13 audit
reported no known vulnerabilities in the resolved development environment on the
experiment date; that is a point-in-time observation, not a future guarantee.
The final explicit build-backend constraint increased the lock to 133,268 bytes
without changing the selected package versions. The uv executable itself occupies
51,102,256 bytes on Linux and 37,639,312 bytes on macOS, separate from the Python,
environment, and cache footprints above.

The frozen source adds SPDX comment headers to the original timed probe. All
checks were rerun on the frozen source. The forced PEP 517 build of those
checked-in inputs was byte-identical across Linux and macOS:

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `visualworld_probe-0.0.0-py3-none-any.whl` | 2,363 | `4951f2579a2d401814e1c8ca2332f73f17352d9880d22da3d71191ac02159841` |
| `visualworld_probe-0.0.0.tar.gz` | 1,367 | `254b4cb2d3171a681e459aa26dee639b058dcb985d90b83e8e3b79e8d1066d38` |

## Failure probes and design corrections

1. **Stale non-editable local wheel:** after source changed without a version
   change, a non-editable sync reused the cached first-party wheel and initially
   missed the new `__main__.py`. A forced reinstall rebuilt it. Normal developer
   and test syncs therefore remain editable; packaging tests build once and install
   that fresh artifact into a clean environment.
2. **Bundled backend shortcut:** `uv build` without `--force-pep517` reported that
   it used the bundled `uv_build` backend. It successfully built even with an
   intentionally invalid external backend hash because no external backend wheel
   was resolved. With `--force-pep517`, the correct two-hash constraint built on
   both platforms and an all-zero negative-control hash failed with an explicit
   platform-specific hash mismatch on both platforms. Release-package validation
   therefore forces PEP 517.
3. **Tool scope:** Ruff scanned a preserved nonstandard virtual-environment
   directory placed under the probe tree. Canonical checks will target the source,
   test, script, and benchmark trees; disposable environments stay outside them.
4. **Legacy metadata:** macOS resolution warned while normalizing invalid legacy
   `>=3.6.*` metadata from transitive dev packages. Resolution and checks passed,
   but issue #8 must retain the exact lock and re-evaluate on every update.

## License observations

The application wheel has no third-party runtime dependency. The development lock
is not shipped with the wheel. The resolved dev graph includes MPL-2.0 `pathspec`
1.1.1 and `certifi` 2026.7.22; internal development/CI use does not distribute
them, while any future bundled environment must preserve their licenses and meet
MPL file-level source obligations. `pip-audit` 2.10.1's installed wheel declares
Apache-2.0 and its project documents ISC-derived resolvelib examples; source-tree
redistribution needs an explicit ISC notice review. Issue #8 must generate the
complete locked inventory rather than relying only on package metadata fields.

## Limitations

- The probe intentionally excludes media, storage, model, native-code, and
  application dependencies; later ADRs must measure their own wheel/platform
  compatibility and footprint.
- Timings are single runs on two hosts and vary with network and filesystem cache.
- macOS 3.14 and Windows were not measured. Windows support is not claimed.
- No registry name was reserved and no package was published.
- A clean-install experiment reduces toolchain uncertainty; it does not prove the
  security of future packages or their installation code.

## Recommendation

Adopt the measured Python/uv design with the forced, hash-constrained PEP 517
packaging lane, editable development sync, exact universal lock, zero root runtime
dependencies, and weekly reviewed updates. Keep every media/model/storage choice
outside this decision.

Primary behavior references: [Python 3.13.15 release](https://www.python.org/downloads/release/python-31315/),
[Python 3.14.7 release](https://www.python.org/downloads/release/python-3147/),
[uv locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/),
[uv project settings](https://docs.astral.sh/uv/reference/settings/),
[uv build constraints and hashes](https://docs.astral.sh/uv/concepts/projects/build/),
and [uv packaging guidance](https://docs.astral.sh/uv/guides/package/).
