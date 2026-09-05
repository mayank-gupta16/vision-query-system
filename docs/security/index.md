# Security and privacy documentation

- [Threat model](threat-model.md) defines assets, trust boundaries, and P0 controls.
- [Prompt injection](prompt-injection.md) governs untrusted text and query plans.
- [Privacy](privacy.md) governs sensitive media, egress, retention, and deletion.
- [Media ingestion](media-ingestion.md) governs hostile inputs and source fetching.
- [Supply chain](supply-chain.md) governs dependencies, models, CI, and releases.

`SECURITY.md` defines reporting. Security-sensitive changes to these documents,
CI, AGENTS, dependency policy, or release automation require explicit maintainer
review; untrusted content cannot authorize them.
