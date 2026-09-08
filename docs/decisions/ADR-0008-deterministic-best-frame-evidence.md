# ADR-0008: Deterministic best-frame evidence and on-demand crops

- Status: Accepted
- Date: 2026-09-08
- Decision owners: repository maintainers
- Related: issues #23, #30, #70, #72, and #74; ADR-0004 and ADR-0005

## Context

A completed clip-local tracklet can contain up to 64 detector-supported
observations. Retaining an original-resolution crop for every observation would
violate the bounded v0.2 path: issue #23 measured a median source crop at 56.25
times the detector crop. At the same time, source-resolvable tiny detail benefits
from an exact original crop when an inspection or declared downstream task needs
one.

The selector must use only facts already present in the pixel-free Observation
and Tracklet contracts. It must be reproducible across supported Python versions
and replaceable adapters, preserve enough detail to explain the order, and avoid
turning a best-frame heuristic into an OCR, identity, make/model, face, or plate
claim.

## Decision

`BestFrameEvidenceSelector` implements the stable `EvidenceSelector.select`
operation and a detailed metadata-only `plan` extension. The default selects at
most three observations from one completed tracklet. A caller may configure a
maximum from one through eight; no call can exceed the existing 64-observation
port input bound. Explicit materialization advertises and enforces a 128 MiB
packed-RGB24 source-frame bound.

For a point at exact rational source time `t`, with tracklet start/end times
`t_start` and `t_end`, each candidate records these exact integer components:

- `boundary_touch_count`: the number of half-open box sides equal to the source
  boundary; this is a truncation-risk proxy, not a claim that content was cut;
- detector `confidence_millionths`;
- `visible_area_pixels = (x_max - x_min) * (y_max - y_min)`;
- `source_area_pixels = source_width * source_height`;
- `visible_area_millionths = floor(visible_area_pixels * 1,000,000 /
  source_area_pixels)`; and
- the normalized integer numerator and positive denominator of
  `midpoint_distance_seconds_x2 = abs(2*t - t_start - t_end)`.

Candidates sort lexicographically by this frozen key, where lower is better:

```text
(
  boundary_touch_count,
  -confidence_millionths,
  -visible_area_millionths,
  -visible_area_pixels,
  midpoint_distance_seconds_x2,
  point_index,
  observation_id,
)
```

The boundary-risk preference prevents a high-confidence edge-clipped view from
winning by confidence alone. Confidence remains ahead of apparent size because
area is not an object-quality or detail-resolution claim. Normalized and raw
areas make large/small ordering exact even when millionth rounding collides. The
exact rational PTS distance supplies the temporal-center preference, including
for VFR and gapped samples. The verified chronological point index prefers the
earlier of two equally distant points. The observation identifier is the final
total-order tie-break. No floating point, pixels, learned quality score, or
adapter name participates.

Each selected `EvidenceIntent` retains its rank, all score components and
tie-break facts, exact source PTS, original-source Geometry, observation/frame/
tracklet/source links, and selector producer/configuration digest. It explicitly
declares `kind=original_frame`, `retention=derived_private`, and
`deletion_owner=coordinator_source_cascade`. An intent is not an artifact and
contains no pixels.

Original RGB24 pixels enter only the explicit `materialize` extension after a
caller supplies both an `inspection` or `downstream_detail` need and a detail
resolution state. Missing source pixels return `unknown/source_pixels_unavailable`.
Declared unresolvable detail returns `unknown/detail_unresolvable`; an unassessed
downstream-detail request returns `unknown/detail_resolution_unknown`. Inspection
may materialize an unassessed crop because inspection can be the act that resolves
that uncertainty.

Materialization delegates byte copying to the accepted `extract_rgb24_crop`
geometry boundary and returns its `Rgb24Crop` plus a matching version-1
`EvidenceRef`. The crop representation and every metadata mapping omit bytes.
The materialized metadata retains the caller-declared need and detail-resolution
state without treating either as model evidence.
The caller passes the returned artifact descriptor and bytes to `EvidenceStore`
only when persistence is justified. The coordinator remains the sole owner of
CAS staging, publication, retention, and source-cascade deletion.

The default producer configuration SHA-256 is
`98cca604cf1880af26173fb2210eedb5b9a5734dd7970e9024e734d0531a1de5`.
Changing the configured maximum produces a different digest.

## Consequences

- Repeated runs are explainable and byte-for-byte deterministic without reading
  source media during selection.
- At most eight evidence candidates can be planned for a tracklet, and zero crops
  are created by the default port operation.
- Boundary contact and size are documented heuristics, not semantic inference or
  proof that fine detail is available.
- Source decoding and durable evidence storage remain orchestration work for later
  v0.2 issues; this decision adds no filesystem, network, database, model, or
  dependency capability.
