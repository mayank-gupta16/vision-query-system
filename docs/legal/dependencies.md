# Dependency inventory

The application scaffold has no third-party runtime package. ADR-0002
approves the following developer/build tools and interpreter range; the root
`uv.lock` and [development-tool artifact inventory](development-tool-inventory.json)
record the exact reviewed transitive graph. Media,
storage, model, codec, and service dependencies remain unapproved.

| Component | Immutable version | Purpose | License | Obligations/isolation | Status | Verified |
| --- | --- | --- | --- | --- | --- | --- |
| actions/checkout | `de0fac2e4500dabe0009e67214ff5f5447ce83dd` (v6.0.2) | CI checkout | MIT | Pin exact SHA; no persisted credentials | Approved for bootstrap | 2026-09-05 |
| CPython | 3.13.15 reference; 3.14.7 compatibility; python-build-standalone 20260825 | Developer/CI interpreter source | `LicenseRef-python-build-standalone-composite-20260825` (defined below) | No interpreter redistribution under this approval | Approved runtime range and developer/CI inputs | 2026-09-06 |
| uv | 0.12.6; source commit `7938ca5d53dbb9c614a4a030df406e41ff101ab9` | Python acquisition, lock, sync, build, export | Apache-2.0 OR MIT | Verify official platform archive hash; do not commit or redistribute executable | Approved developer/CI tool | 2026-09-06 |
| uv_build | 0.12.6 | PEP 517 build backend | Apache-2.0 OR MIT | Exact two-platform wheel hashes; force PEP 517 for hash verification | Approved build-only tool | 2026-09-06 |
| Ruff | 0.16.5 initial lock | Format and lint | MIT | Dev-only; preserve license if redistributed | Approved developer/CI tool | 2026-09-06 |
| mypy | 2.3.1 initial lock | Static typing | MIT | Dev-only; preserve license if redistributed | Approved developer/CI tool | 2026-09-06 |
| pytest | 9.1.1 initial lock | Unit and contract tests | MIT | Dev-only; preserve license if redistributed | Approved developer/CI tool | 2026-09-06 |
| coverage.py | 7.16.0 initial lock | Branch coverage | Apache-2.0 | Dev-only; preserve license/notice if redistributed | Approved developer/CI tool | 2026-09-06 |
| pip-audit | 2.10.1 initial lock | Vulnerability audit | Apache-2.0; ISC-derived example provenance documented upstream | Dev-only; review ISC notice if source/examples are redistributed | Approved developer/CI tool | 2026-09-06 |

For every future Python/native/container dependency record source URL, exact
version/revision and digest/lock, SPDX identifier or `LicenseRef-*`, use,
distribution, transitive/native components, commercial implications, approval,
and verification date. Review the actual FFmpeg build and codecs, not only its
umbrella name.

Core defaults should use reviewed OSI-approved permissive licenses. Weak
copyleft requires compatibility review. Strong/network copyleft,
non-commercial, research-only, no-derivatives, unknown, or custom terms are
deny-by-default for core and require an ADR plus isolation if accepted.

The measured development graph also includes MPL-2.0 `pathspec` 1.1.1 and
`certifi` 2026.7.22. They are approved only as transitive dev/CI tools and are
not included in the VisualWorld wheel. If a development environment or
certificate bundle is distributed, preserve the applicable notices and satisfy
MPL-2.0 file-level covered-source obligations. See the
[clean-install report](../research/application-toolchain.md) for exact artifact
hashes, footprint, and the license caveat.

The reviewed dev-only license metadata gaps are resolved by upstream license
files and the installed wheels, not inferred from missing metadata:

| Locked package | Normalized license | Primary evidence |
| --- | --- | --- |
| markdown-it-py 4.2.0 | MIT | [Package license](https://github.com/executablebooks/markdown-it-py/blob/v4.2.0/LICENSE) and [upstream markdown-it notice](https://github.com/executablebooks/markdown-it-py/blob/v4.2.0/LICENSE.markdown-it) |
| mdurl 0.1.2 | MIT | [Whole license file with all grants](https://github.com/executablebooks/mdurl/blob/0.1.2/LICENSE) |
| pathspec 1.1.1 | MPL-2.0 | [Versioned license](https://github.com/cpburnz/python-pathspec/blob/v1.1.1/LICENSE) |
| pip-api 0.0.34 | Apache-2.0 | [Release metadata](https://pypi.org/project/pip-api/0.0.34/) and bundled wheel license |
| pip-audit 2.10.1 | Apache-2.0; ISC examples caveat | [Versioned license](https://github.com/pypa/pip-audit/blob/v2.10.1/LICENSE) and [release attribution](https://pypi.org/project/pip-audit/2.10.1/) |
| tomli-w 1.2.0 | MIT | [Release metadata](https://pypi.org/project/tomli-w/1.2.0/) and bundled wheel license |
| certifi 2026.7.22 | MPL-2.0 | [Versioned license](https://github.com/certifi/python-certifi/blob/2026.07.22/LICENSE) |

See [Mozilla's MPL FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/) for private-use
and file-level distribution guidance. This is a scoped dependency review, not a
complete redistribution approval for the development environment.

## Exact development-tool artifact inventory

The machine-readable [inventory](development-tool-inventory.json), verified on
2026-09-06, contains all 40 third-party packages in the universal lock. Each
Linux x86_64 and macOS arm64 CPython 3.13/3.14 lane installs 39 of these;
`colorama` 0.4.6 is a Windows-only conditional dependency and is not installed
on any approved lane. Its wheel is inspected for completeness, not a Windows
support claim. The first-party `visualworld-engine` package is excluded.

The inventory records 60 actual wheel artifacts: official PyPI download URLs,
SHA-256 values verified against the lock before ZIP inspection, wheel metadata
hashes, actual license metadata, and 129 license/notice/attribution-file
fingerprints. Ordinary-GIL wheel tags were selected for the two Python versions,
Linux glibc platforms through the Ubuntu 24.04 lane, and macOS 15 arm64. Shared
pure-Python and stable-ABI wheels are listed once with their applicable lanes.
Each CI lane additionally verifies its actual installed evidence; tag selection
by itself is not claimed as a successful installation.

The inventory's `locked_packages_sha256` hashes UTF-8 JSON of the ordered
third-party `uv.lock` package records, with sorted keys and compact separators.
It covers all artifact records, including unselected architectures and source
archives, not only the 60 inspected wheels. A dependency, source, or artifact
change therefore requires a new license review. Unselected source archives
are not approved for building merely because their hashes are in the lock.

Run `python scripts/inspect_dependency_licenses.py --check` inside the locked
all-groups environment. This offline, standard-library-only check compares the
entire graph and the current lane's actual distribution metadata and notice
bytes with the reviewed record. It rejects missing/extra distributions,
version drift, unapproved non-registry packages, missing evidence, and paths
outside the environment. It does not import the inspected packages. The clean,
hash-verified uv sync checks package-code integrity; metadata/license comparison
does not replace that install check, a vulnerability audit, or legal review.

For an update, download the exact candidate wheels from the reviewed lock and
inspect each with `python scripts/inspect_dependency_licenses.py --inspect-wheel
PATH --sha256 APPROVED_SHA256`. Review the returned metadata, bundled licenses,
embedded components, platform mappings, and any gaps before updating the JSON;
do not normalize an unknown license automatically. Re-run clean sync and the
inventory check on all four lanes. Keep the prior research fixture unchanged.

Additional actual-artifact findings are scoped to developer/CI use:

| Component | Reviewed finding and disposition |
| --- | --- |
| colorama 0.4.6 | License metadata fields are absent; bundled `LICENSE.txt` is BSD-3-Clause. Unselected on supported platforms. |
| defusedxml 0.7.1 | Legacy `PSFL` spelling resolves to the bundled PSF version 2 license, `PSF-2.0`. |
| sortedcontainers 2.4.0 | Legacy `Apache 2.0` spelling resolves to the bundled Apache-2.0 grant. |
| packageurl-python 0.17.6 | Wheel metadata declares MIT, but the wheel omits license text. The locked sdist includes `mit.LICENSE`, matching the [release-commit MIT file](https://github.com/package-url/packageurl-python/blob/04b755ccc388d6a53bb5277d1f95de4baa727deb/mit.LICENSE): SHA-256 `8e442c79545ac0c0a1a2cf0cf213312a45826acd6cdc6af223447dd5f708ee5d`. Complete notices are a prerequisite to any environment redistribution. |
| mypy 2.3.1 | MIT application code bundles an Apache-2.0 typeshed snapshot. The exact wheel and both typeshed license copies are fingerprinted; do not describe the entire wheel as MIT-only. |
| license-expression 30.4.4 | Apache-2.0 code bundles a CC-BY-4.0 ScanCode license index (upstream revision `1dfa89ae348338b23a359c4c6b23e39c128a41e5`, SPDX list 3.27) and a modified public-domain pyahocorasick Python subset. The latter's explicit source-header identifier is `LicenseRef-scancode-public-domain`, with original revision `ec2fb9cb393f571fd4316ea98ed7b65992f16127`. Bundled `.ABOUT` records and license text are fingerprinted. Keep attribution and modification records if distributed; this is not approval to add these data/code to the application core. |
| typing-extensions 4.16.0 | Metadata declares PSF-2.0, while the bundled file preserves earlier Python history and grants. Retain the whole notice if redistributed. |
| pip 26.2.1 | MIT tool with 18 separately versioned vendored components recorded from its actual `vendor.txt`; MIT, Apache-2.0, BSD, ISC, Python historical, and MPL-2.0 notices are retained. Vendored versions can differ from top-level locked packages, notably certifi 2026.6.17 versus 2026.7.22. |

Top-level package labels do not relicense embedded code, data, type stubs,
certificates, or notices. The JSON explicitly records these discovered embedded
components. None is shipped in the VisualWorld wheel. A future bundled developer
environment, installer, container, or binary redistribution needs a fresh
complete component/notice/source-obligation review, including missing notices
and the public-domain declaration's jurisdictional implications. CC-BY data and
this public-domain subset are accepted only as unchanged development-tool
inputs, not as new default-core dependency choices.

The development inventory is **not** the runtime SBOM. The initial wheel has
zero third-party runtime dependencies; release artifact validation must establish
that independently. uv, its build backend, managed Python, CI Actions, future
native libraries, model weights, and datasets belong in their separate ledgers.

## Managed Python composite license record

`LicenseRef-python-build-standalone-composite-20260825` names a composite
interpreter distribution, not a new license grant. CPython itself uses
`Python-2.0` and retained historical grants including `CNRI-Python`. The archive
also includes separately licensed libraries and pip 26.2.1 with vendored
components. The builder's source license is MPL-2.0; that does not describe the
produced interpreter's aggregate license.

The approved inputs are the ordinary-GIL `install_only_stripped` artifacts in
the [research report](../research/application-toolchain.md), from
python-build-standalone source commit
`c0aa3bbdc2fff56a77ad1ecec68b1e47794d8779`. Review evidence comes from the
corresponding `pgo+lto` full archives' `PYTHON.json` and license directory:

| Full-archive manifest | `PYTHON.json` SHA-256 |
| --- | --- |
| CPython 3.13.15 Linux x86_64 | `3955043789f62b36e261c97d319460efd039d6a1afb4dc248d8c09749f07ccc0` |
| CPython 3.13.15 macOS arm64 | `d06143507242aab63b63250d8e582018e8f0b28e71f942edd7786250a35ad7c8` |
| CPython 3.14.7 Linux x86_64 | `e282e21c8a2e5e288c23c346b9d2d4b30b63a2879cc4a8e0a6b72c990503485d` |
| CPython 3.14.7 macOS arm64 | `882a0510d6fda84f32c1b861ec3f44a9cc9edb51127a3340077fbe3396c899d9` |

Relevant bundled components include OpenSSL 3.5.8 (Apache-2.0), permissive
compression/ffi/Tcl/Tk/X11 libraries, SQLite's public-domain dedication, and, on
Linux, Berkeley DB 6.0.19 (Sleepycat). The Linux 3.14 build also includes zstd
(BSD-3-Clause). The raw manifest conservatively lists the older OpenSSL license
alongside Apache-2.0; exact target configuration identifies OpenSSL 3.5.8.

The fourth CI lane's macOS 3.14.7
[`pgo+lto` full archive](https://github.com/astral-sh/python-build-standalone/releases/download/20260825/cpython-3.14.7%2B20260825-aarch64-apple-darwin-pgo%2Blto-full.tar.zst)
has SHA-256
`9cc1dd70274721d2643b6bb8b59bf7544de6d0417b9645cae49fbfae7ce13bdc`.
Its manifest confirms ordinary GIL, `aarch64-apple-darwin`, and statically linked
zstd 1.5.7 (BSD-3-Clause), additional to the Mac 3.13 component set. Its `zlib`
entry links the system library. The manifest references `LICENSE.zstd.txt` and
`LICENSE.zlib-ng.txt`, but those files are absent from this full archive; a
manifest path is not proof that a notice was delivered. The pinned builder
source identifies zstd 1.5.7 and its source archive hash
`f24b52470d12f466e9fa4fcc94e6c530625ada51d7b36de7fdc6ed7e6f499c8e`.
The matching [upstream BSD license](https://github.com/python/cpython-source-deps/blob/eef946ae8cf1591c0e5cc5f43486210768647c2e/LICENSE)
was reviewed, SHA-256
`7055266497633c9025b777c78eb7235af13922117480ed5c674677adc381c9d8`.
These notice gaps reinforce the no-redistribution boundary; they do not grant
permission to mirror the interpreter or claim that its notice set is complete.

This approval is limited to using these interpreter downloads for development,
CI, and evaluation. VisualWorld wheels, source archives, containers, installers,
and release artifacts must not embed or mirror them under this approval. Future
redistribution needs a separate review of all license/notice files and source
obligations, including Sleepycat. New interpreter builds require fresh artifact
hash, provenance, component-license, and vulnerability review. Runtime performance
reports still record the interpreter version and build independently of the
application SBOM.

Primary evidence: [pinned uv download metadata](https://github.com/astral-sh/uv/blob/7938ca5d53dbb9c614a4a030df406e41ff101ab9/crates/uv-python/download-metadata.json),
[python-build-standalone release](https://github.com/astral-sh/python-build-standalone/releases/tag/20260825),
[upstream licensing guidance](https://github.com/astral-sh/python-build-standalone/blob/c0aa3bbdc2fff56a77ad1ecec68b1e47794d8779/docs/running.rst),
and [archive contents](https://github.com/astral-sh/python-build-standalone/blob/c0aa3bbdc2fff56a77ad1ecec68b1e47794d8779/docs/distributions.rst).
