# VisualWorld

VisualWorld is intended to become an open-source, local-first system for converting video into a
persistent, queryable, evidence-backed representation of the observable world.

The project is intentionally being built in releases. The first release focuses
on deterministic ingestion, durable data contracts, provenance, and a
reproducible CPU-LITE benchmark baseline before adding real detector, tracker,
OCR, or vision-language model integrations.

```text
video -> observations -> tracklets -> entities -> world state -> queries
```

## Project status

VisualWorld `0.1.0` is the first experimental source/wheel release. It provides
help/version commands, a reproducible developer toolchain, stable ingestion
records/ports, a bounded Linux-only local-video source/probe adapter,
deterministic exact-PTS frame sampling, original-pixel crop utilities, a
crash-recoverable local evidence CAS, and transactional local SQLite metadata.
The library and CLI expose a deterministic end-to-end ingestion path for
fake/manual regions, including recovery and source deletion. Real-video CLI
input, perception, and queries are not implemented yet.
See [current project state](docs/project/current.md) and the
[roadmap](docs/project/roadmap.md) for the proposed sequencing and explicit
non-goals.

## Try the deterministic CLI slice

On Linux x86_64 (glibc) or macOS arm64, from a trusted checkout with host Python
3.9+ available:

```sh
python3 scripts/bootstrap.py
python3 scripts/dev.py sync
python=artifacts/toolchain/3.13.15/dev/bin/python
cli=artifacts/toolchain/3.13.15/dev/bin/visualworld
work_root="$(mktemp -d)"
store="$work_root/store"

"$cli" probe
ingest_result="$("$cli" ingest --store "$store")"
run_id="$(printf '%s\n' "$ingest_result" | "$python" -c \
  'import json,sys; print(json.load(sys.stdin)["result"]["run_id"])')"
evidence_id="$(printf '%s\n' "$ingest_result" | "$python" -c \
  'import json,sys; print(json.load(sys.stdin)["result"]["evidence_ids"][0])')"

"$cli" inspect-run --store "$store" --run-id "$run_id"
"$cli" list-samples --store "$store" --run-id "$run_id"
"$cli" show-evidence --store "$store" --evidence-id "$evidence_id" \
  --output "$work_root/crop.rgb24"
"$python" -c \
  'import hashlib,sys; data=open(sys.argv[1], "rb").read(); print(len(data), hashlib.sha256(data).hexdigest())' \
  "$work_root/crop.rgb24"

python3 scripts/dev.py check
```

The final verification line prints `6` and
`ee0c0ca710ef28a12edd6f7d1454fc6516e434d4e33c733f886bee5f3e62233c`.
The fixture is a built-in 2×2 packed-RGB24 test frame, not a real video or model
result. The five data commands emit one canonical JSON document; rejected
values and local paths are not echoed. `show-evidence` creates a read-only file,
refuses overwrite, and must target an existing private directory outside the
store.

Setup downloads checksum-pinned developer tools into ignored local directories;
it does not change system Python or download models/media. The application has
no third-party runtime dependencies. The reviewed wheel, source archive, SBOM,
and checksums are published in the
[v0.1.0 GitHub Release](https://github.com/mayank-gupta16/vision-query-system/releases/tag/v0.1.0).
See [developer setup](docs/testing/development.md) for compatibility, build/audit
commands, network boundaries, and exact versions. Nothing is published to a
package registry by these commands or this release.

## Principles

- preserve original pixels and exact evidence references;
- keep deterministic and probabilistic stages separate;
- represent uncertainty instead of inventing precision;
- push cheap filters ahead of expensive semantic inference;
- make models replaceable through stable interfaces;
- benchmark the 4 vCPU / 16 GB RAM CPU-LITE profile in every release;
- keep data local by default and treat external/model-provided content as
  untrusted data.

## Start here

- [Documentation index](docs/index.md)
- [Architecture overview](docs/architecture/overview.md)
- [Product vision](docs/product/vision.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License

Original VisualWorld software, tests, configuration, examples, and
project-authored documentation are licensed under the
[Apache License 2.0](LICENSE), SPDX identifier `Apache-2.0`, unless a file or
artifact states otherwise. Copyright 2026 Mayank Gupta and VisualWorld
contributors; contributors retain copyright to their work.

Models, weights, datasets, videos, annotations, generated media, codecs,
fonts/assets, hosted services, optional third-party backends, and incorporated
third-party material keep their own terms; inclusion in or use with VisualWorld
does not relicense them. See the [license decision](docs/legal/license-decision.md)
and [third-party notices](THIRD_PARTY_NOTICES.md).
