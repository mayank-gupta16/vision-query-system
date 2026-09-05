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

No software license has been selected yet. Until a license is added, copyright
law reserves all rights; do not assume permission to redistribute or create
derivative works. The decision and its acceptance checks are tracked in
[the license decision record](docs/legal/license-decision.md).
