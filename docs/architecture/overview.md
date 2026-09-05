# Architecture overview

## North star

```text
Sources -> Perception -> Tracklets -> Entity resolution -> World model
                                                       |
Structured query --------------------------------------+
Natural language -> validated query plan --------------+
                                                       v
                                      evidence-backed answer
```

Core dependency direction is inward:

```text
framework-free domain records
        <- stable ports/contracts
        <- adapters and application services
        <- CLI/API entry points
```

Model SDKs, codecs, databases, and cloud services must not leak into domain
types. Concrete integrations live behind adapters and optional dependency
groups when practical.

## Low-compute data path

1. Fingerprint and probe a source without discarding original quality.
2. Sample timestamps (initially near 5 FPS) and decode bounded frames.
3. Detect on a downscaled representation.
4. Map boxes to original coordinates and form short-lived tracklets.
5. Select a few best original-resolution evidence views.
6. Store deterministic geometry/state and cheap attributes.
7. Filter query candidates using indexed facts.
8. Invoke expensive semantic adapters only for unresolved predicates.
9. Validate, version, cache, and attach semantic claims to evidence.

## Storage boundary

The v0.x default is expected to use a transactional local metadata store and a
content-addressed filesystem artifact store, both behind ports. Final storage
selection, migrations, identifiers, and deletion semantics require an ADR.
Full video blobs do not belong in a relational metadata database.

## Query boundary

Natural-language output must compile to a bounded, schema-validated internal
query plan. Only deterministic code may issue parameterized store operations.
Model-produced SQL, commands, paths, or URLs are data and are never executed.

## Debuggability

Every run records validated configuration, source fingerprint, component and
model versions, stage status, timing/counters, structured errors, and evidence
references. Optional debug artifacts follow the same privacy and retention rules
as their source.
