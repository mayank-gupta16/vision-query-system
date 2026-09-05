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

VisualWorld is in repository bootstrap. There is no usable application yet.
See [current project state](docs/project/current.md) and the
[roadmap](docs/project/roadmap.md) for the proposed sequencing and explicit
non-goals.

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
