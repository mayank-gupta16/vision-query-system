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
  `miss_timeout`, or `source_end` reason. Every trajectory point repeats its
  source, stream, and category binding so a strict reader can verify the point
  against its enclosing tracklet without resolving another record.
- `FrameDiscontinuity` is an ephemeral, pixel-free value binding one frame's
  exact source/stream/PTS identity to an integer mean-absolute-RGB change score
  from 0 to 10,000 basis points. It has no independent content identifier and is
  not a persisted world record. RGB bytes are consumed only by the transient
  scoring utility and never enter a tracker request, cursor, diagnostic, error,
  or serialized perception record.

The `Observation` and `Tracklet` schemas use the same bounded canonical-JSON
profile and structured validation errors as the ingestion records.
`visualworld.experimental` exposes the additive `ExperimentalRecord` union and
strict combined dispatch across v0.1 ingestion and perception schemas; the
narrower v0.1 `Record` union remains the WorldStore commit contract until the
v0.2 storage migration is implemented. They contain no pixels, artifact bytes,
vendor SDK values, source locators, or persistent entity identifier.
`identity_scope=source_clip` and `continuity=inferred` are mandatory; a tracklet
cannot be treated as proof that two observations belong to one persistent
entity.

The first contract bounds a completed tracklet to 64 strictly time-ordered,
detector-supported points. `GlobalLastBoxTracker.track_page` now provides the
reviewed paging extension: an opaque immutable cursor carries owned active
tracklet snapshots and miss counts across pages. The issuing tracker authenticates
that state before use. Only `cut`, `miss_timeout`, or an explicit `source_end`
can complete a tracklet. Paging does not manufacture a termination or reuse
clip-local track ordinals. A continuous trajectory that would exceed 64 points
fails with a structured limit error rather than being silently split.

`EvidenceIntent` is the pixel-free bridge from one completed tracklet to a
possible original-frame `EvidenceRef`. It is not a persisted artifact. It binds
the selected observation and source-coordinate geometry to an ordered rank,
the complete deterministic score/tie-break breakdown, selector producer and
configuration digest, `derived_private` retention, and coordinator-owned source
cascade deletion. Only an explicit materialization request can add an exact
RGB24 crop and content-addressed reference. Materialization first recomputes the
plan from the resupplied completed Tracklet and Observation set and requires the
full intent to match. Unavailable or unresolvable detail remains `UNKNOWN`. The
selector does not add OCR, identity, make/model, face, or plate fields.
