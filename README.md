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

VisualWorld has an experimental Python package (`0.1.0a0`) with help/version
commands, a reproducible developer toolchain, stable ingestion records/ports, and
a bounded Linux-only local-video source/probe adapter plus deterministic exact-PTS
frame sampling, original-pixel crop utilities, and a crash-recoverable local
evidence CAS. End-to-end ingestion, metadata storage, and queries are not
implemented yet.
See [current project state](docs/project/current.md) and the
[roadmap](docs/project/roadmap.md) for the proposed sequencing and explicit
non-goals.

## Try the scaffold

On Linux x86_64 (glibc) or macOS arm64, from a trusted checkout with host Python
3.9+ available:

```sh
python3 scripts/bootstrap.py
python3 scripts/dev.py sync
artifacts/toolchain/3.13.15/dev/bin/visualworld --help
artifacts/toolchain/3.13.15/dev/bin/visualworld --version
python3 scripts/dev.py check
```

Setup downloads checksum-pinned developer tools into ignored local directories;
it does not change system Python or download models/media. The application has
no third-party runtime dependencies. See [developer setup](docs/testing/development.md)
for compatibility, build/audit commands, network boundaries, and exact versions.
Nothing is published to a package registry by these commands.

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
