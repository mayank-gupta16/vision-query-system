# Project license decision

- Status: Apache-2.0 decision accepted in ADR-0001 on 2026-09-06
- Current effect: the root LICENSE grants Apache-2.0 rights for original project work

The intended project is public, commercially usable, and modular around separately
licensed models/datasets. [ADR-0001](../decisions/ADR-0001-project-license.md)
compares:

- Apache-2.0: permissive, explicit patent grant and notice obligations;
- MIT: simpler permissive text, without Apache's explicit patent terms;
- any credible alternative required by selected core dependencies or governance.

The accepted policy is Apache-2.0 for its explicit patent and contribution
framework, with inbound-equals-outbound terms, no initial CLA, and DCO 1.1
sign-off for substantive external code and documentation commits. MIT was the
simpler fallback but lacks an express patent grant in its text.

The exact root LICENSE, README, CONTRIBUTING, and third-party notices agree.
No application package metadata exists yet; future metadata must use the SPDX
identifier `Apache-2.0`. Actual dependency/runtime/codec/model/data terms remain
separate release gates.

Model weights, model code, datasets, media, fonts/assets, hosted APIs, and optional
backends remain separately licensed even after a project license is chosen.
