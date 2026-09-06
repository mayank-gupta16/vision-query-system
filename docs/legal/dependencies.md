# Dependency inventory

The application scaffold has no approved third-party runtime package. ADR-0002
approves the following developer/build tools and interpreter range; issue #8 will
commit their exact universal lock and complete transitive inventory. Media,
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

Relevant bundled components include OpenSSL 3.5.8 (Apache-2.0), permissive
compression/ffi/Tcl/Tk/X11 libraries, SQLite's public-domain dedication, and, on
Linux, Berkeley DB 6.0.19 (Sleepycat). The Linux 3.14 build also includes zstd
(BSD-3-Clause). The raw manifest conservatively lists the older OpenSSL license
alongside Apache-2.0; exact target configuration identifies OpenSSL 3.5.8.

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
