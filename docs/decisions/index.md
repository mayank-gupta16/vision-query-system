# Architecture decisions

Architecture Decision Records capture choices with long-lived consequences.
Use [the template](template.md) and add entries as `ADR-NNNN-short-title.md`.

The project license, application toolchain, Linux media-worker direction,
ingestion record contracts, local storage/deletion protocol, v0.2 evaluation
gates, separately provisioned Linux perception boundary, and deterministic
best-frame evidence policy are accepted. The
selected vehicle model/runtime may be used only through that private boundary;
redistribution and production datasets remain unapproved. Proposed choices must
not be treated as decisions until the ADR status is `Accepted`.

## Accepted

- [ADR-0001: Project and inbound contribution license](ADR-0001-project-license.md)
- [ADR-0002: Application toolchain and lock strategy](ADR-0002-application-toolchain.md)
- [ADR-0003: Media runtime and hostile-input isolation](ADR-0003-media-runtime-and-isolation.md)
- [ADR-0004: Ingestion records, identifiers, and rational time](ADR-0004-ingestion-records-identifiers-and-time.md)
- [ADR-0005: Local metadata, artifacts, recovery, and deletion](ADR-0005-local-storage-and-deletion.md)
- [ADR-0006: Frozen v0.2 evaluation gates and waivers](ADR-0006-v02-evaluation-gates.md)
- [ADR-0007: Isolated perception runtime and provisioning boundary](ADR-0007-isolated-perception-runtime.md)
- [ADR-0008: Deterministic best-frame evidence and on-demand crops](ADR-0008-deterministic-best-frame-evidence.md)
