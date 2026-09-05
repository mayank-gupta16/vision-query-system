# Project license decision

- Status: proposed Apache-2.0 decision in ADR-0001; maintainer approval required
- Current effect: no LICENSE file exists, so no reuse/redistribution grant is implied

The intended project is public, commercially usable, and modular around separately
licensed models/datasets. [ADR-0001](../decisions/ADR-0001-project-license.md)
compares:

- Apache-2.0: permissive, explicit patent grant and notice obligations;
- MIT: simpler permissive text, without Apache's explicit patent terms;
- any credible alternative required by selected core dependencies or governance.

The engineering recommendation is Apache-2.0 for its explicit patent and
contribution framework, with inbound-equals-outbound terms, no initial CLA, and
DCO 1.1 sign-off for code and documentation commits once external substantive
contribution intake opens. MIT is the simpler fallback if the maintainer
intentionally prefers minimal text over an express patent grant.

Acceptance requires maintainer approval, then an exact root LICENSE plus aligned
README, CONTRIBUTING, package metadata, and third-party notices. Actual
dependency/runtime/codec/model/data terms remain separate release gates.

Model weights, model code, datasets, media, fonts/assets, hosted APIs, and optional
backends remain separately licensed even after a project license is chosen.
