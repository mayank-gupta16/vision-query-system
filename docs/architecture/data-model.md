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
limits, and migration rules. The framework-free implementation and strict JSON
reader live in `visualworld.ingestion`; later world-model records remain out of
scope until their milestones exercise them.

`visualworld.geometry` implements the version-1 deterministic bridge from a
resized, optionally letterboxed and display-rotated detector input back to
encoded source pixels. It uses exact rational affine coefficients, outward
rounding, source-bound clamping, and byte-preserving packed-RGB24 crops. Crop
pixels are never included in object representations or errors.

## Version-1 perception records

Issue [#70](https://github.com/mayank-gupta16/vision-query-system/issues/70)
adds the first framework-free perception records in `visualworld.perception`:

- `Observation` is one detector-supported `vehicle` claim. Its content-derived
  `obs_` identifier binds the source and frame identifiers, stream, exact source
  PTS, original-pixel geometry and transform, integer-millionth confidence, and
  exact producer version/configuration digest.
- `Tracklet` is completed continuity within one source clip. Its content-derived
  `trk_` identifier binds an ordered trajectory of observation/frame links,
  exact PTS and geometry, tracker provenance, and the explicit `cut`,
  `miss_timeout`, or `source_end` reason.

Both schemas use the same bounded canonical-JSON profile and structured
validation errors as the ingestion records, but remain a separate
`PerceptionRecord` union until the v0.2 WorldStore migration is implemented.
They contain no pixels, artifact bytes, vendor SDK values, source locators, or
persistent entity identifier. `identity_scope=source_clip` and
`continuity=inferred` are mandatory; a tracklet cannot be treated as proof that
two observations belong to one persistent entity.

The first contract bounds a completed tracklet to 64 strictly time-ordered
points. Longer sources must be rejected or handled by a later reviewed paging
extension; silently splitting continuity or manufacturing a page-boundary
termination reason is not permitted.
