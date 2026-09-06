# World data model

The world model must preserve the distinction between:

- **Observation/detection:** an object-like claim at one source time.
- **Tracklet:** continuity across adjacent observations.
- **Entity:** one hypothesized physical object across one or more tracklets.

These identifiers are different types and must not be interchangeable.

## Required concepts

- `Source`: immutable identity/fingerprint, dimensions, time base, rotation,
  provenance, and access policy.
- `FrameRef`: source time/PTS and frame index when available.
- `Geometry`: original-source pixel coordinates plus source dimensions and the
  recorded detector-to-source transform.
- `EvidenceRef`: content hash or immutable source reference, time, geometry,
  quality scores, and retention classification.
- `Claim`: value or `UNKNOWN`, confidence/calibration, producer and version,
  configuration/schema version, evidence references, and optional validity
  interval.
- `Tracklet`: ordered observations plus start/end, trajectory, and termination
  reason.
- `Entity`: stable identity with resolution decisions and linked tracklets.
- `Relationship` and `Event`: typed, time-bounded claims that can be extended
  without destructive schema changes.

Identity resolution returns `same`, `different`, or `uncertain`. Counts over
uncertain groups must expose a confirmed minimum and possible maximum rather
than fabricating one exact value.

Metric geometry must be tagged `measured`, `calibrated`, `estimated`,
`inferred`, or `unknown`. Pixel-space geometry is deterministic; physical-world
geometry is not exact unless calibration supports it.

[ADR-0004](../decisions/ADR-0004-ingestion-records-identifiers-and-time.md)
defines the accepted version-1 ingestion records, typed content identifiers,
exact rational PTS representation, original-pixel geometry, serialization
limits, and migration rules.
