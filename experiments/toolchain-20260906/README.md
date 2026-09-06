# Frozen toolchain experiment

Reproducible inputs for [issue #3](https://github.com/mayank-gupta16/vision-query-system/issues/3)
and the [research report](../../docs/research/application-toolchain.md). This is
an inert import/CLI probe, not the VisualWorld application scaffold. It has no
runtime dependencies, media, models, private inputs, or network behavior.

The exact uv executable and managed Python inputs are listed in the report.
Verify their hashes before running them. From this directory, with that uv on
PATH and CPython 3.13.15 installed:

```sh
uv lock --check
uv sync --locked --all-groups
uv run --locked ruff format --check src tests
uv run --locked ruff check src tests
uv run --locked mypy src tests
uv run --locked coverage run -m pytest
uv run --locked coverage report
uv build --force-pep517 --no-sources --build-constraint build-constraints.txt --require-hashes
```

Repeat the sync/check commands with `UV_PYTHON=3.14.7` in a distinct empty
environment to verify the compatibility lane. Cold measurements require distinct
empty `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, and `UV_PROJECT_ENVIRONMENT`
locations. Keep disposable environments outside this tree. Record OS, architecture,
Python/uv versions, timing, peak RSS, allocated disk, and cache state.

For a packaging smoke test, create a fresh external venv with `uv venv`, install
the newly built wheel with `uv pip install --python <venv-python> --no-deps
<wheel>`, and run import, `python -m visualworld_probe`, and
`<venv>/bin/visualworld-probe`. Inspect the installed package list and wheel
contents. The probe must be its only distribution.

The vulnerability check is a separate, explicitly network-enabled operation:

```sh
uv export --locked --all-groups --no-emit-project --format requirements-txt --output-file /tmp/visualworld-probe-audit.txt
uv run --locked pip-audit --require-hashes --disable-pip --no-deps -r /tmp/visualworld-probe-audit.txt
```

The package names and versions in that input are public tool dependencies. Audit
results depend on the advisory database at execution time. Do not include private
package names or credentials in an audit request.

To reproduce the hash-rejection control, use a separate copy of the build
constraint file with a 64-zero SHA-256. The forced PEP 517 build must exit nonzero
with a hash mismatch, including when the correct backend wheel is already cached.

The lock and inputs are intentionally frozen for this research record. Dependency
updates belong in the application scaffold; future experiments get a separate
dated directory. Keep generated environments, caches, distributions, and audit
outputs untracked. Original probe code is Apache-2.0 under the root license.
