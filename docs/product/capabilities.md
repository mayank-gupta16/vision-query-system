# Capability map

## Ingestion and perception

- Preserve the original source or an immutable reference/fingerprint.
- Sample and decode under explicit resource limits.
- Detect on resized frames while mapping all geometry to original pixels.
- Retain a small number of deterministic, high-quality evidence views.

## World representation

- Keep observations/detections, continuous tracklets, and physical entities
  separate.
- Store time-bounded attributes, relationships, events, trajectories, and
  provenance.
- Represent identity decisions as `same`, `different`, or `uncertain` and
  answer ambiguous counts as bounds.

## Queries

- Execute validated structured plans over the deterministic world store.
- Compile natural language into the same bounded plan representation.
- Push cheap predicates down and invoke semantic adapters only for relevant
  candidates.
- Return supporting timestamps, artifacts, producer versions, confidence, and
  remaining uncertainty.

## Operations

- Make every pipeline stage inspectable through structured logs and manifests.
- Support replaceable model/storage implementations through contract-tested
  ports.
- Measure accuracy, latency, throughput, memory, disk, and semantic-call cost.
