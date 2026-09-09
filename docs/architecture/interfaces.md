# Stable interfaces

The architecture must provide replaceable ports for:

- `VideoSource`
- `FrameSampler`
- `Detector`
- `Tracker`
- `ReIdentifier`
- `EmbeddingProvider`
- `Segmenter`
- `OCRProvider`
- `SemanticReasoner`
- `DepthProvider`
- `EvidenceSelector`
- `OriginalFrameReader`
- `EntityResolver`
- `WorldStore`
- `EvidenceStore`
- `QueryPlanner`
- `QueryExecutor`

Ports exchange versioned domain records and structured errors, not vendor SDK
objects. Each implementation must pass the same contract suite. Deterministic
fake implementations are mandatory for orchestration, query, recovery, and
security tests in ordinary CI.

Capabilities and resource needs must be discoverable rather than inferred from
implementation names. Optional adapters declare dependency/model licenses,
hardware needs, offline/remote behavior, and the exact data they may transmit.

## Version-1 ingestion ports

The v0.1 executable contract intentionally implements only four ports:

| Port | Version-1 operations |
| --- | --- |
| `VideoSource` | Probe one already-authorized source and read bounded, per-stream decode-index-paged `FrameRef` batches. |
| `FrameSampler` | Select a deterministic bounded subset from supplied `FrameRef` candidates. |
| `EvidenceStore` | Put digest- and size-verified artifact bytes and retrieve them by SHA-256. |
| `WorldStore` | Atomically commit bounded ingestion-record batches, retrieve by typed ID, and list bounded per-stream frames/evidence. |

`CapabilityDescriptor` identifies the port and contract version, implementation
version, deterministic/offline behavior, and batch/payload bounds. V1 application
orchestration never receives or grants shell, network, arbitrary-filesystem, or
raw-SQL handles; an attempted ambient capability request returns the structured
`capability_denied` error. Adapter internals remain responsible for their
separately approved confinement, and process sandboxing is outside this contract.

Port failures expose a stable error code, port, operation, and retryability flag;
their message does not include untrusted paths, identifiers, content, or backend
exceptions. Instrumentation records only the port, operation, and bounded item
count. The standard-library `visualworld.ports` module includes deterministic
in-memory fakes for all four contracts. Fake batches contain at most 64 records;
the evidence fake advertises its configurable payload bound and defaults to 1
MiB. The other ports listed above remain conceptual until a milestone exercises
them.

`visualworld.media.LocalVideoSource` is the first production `VideoSource`
implementation. It accepts only one already-authorized local relative path and
uses the separately provisioned Linux media runtime; it has no remote/RTSP or
native macOS fallback. Its extra probe details and resource metrics remain
adapter diagnostics rather than additions to the stable port contract.

`visualworld.sampling.PtsFrameSampler` is the first production `FrameSampler`.
Its ordinary `sample` method implements the stable bounded port. Its
`sample_page` extension implements the same policy across bounded pages using an
opaque immutable cursor and one exact prior-frame overlap. The cursor binds the
active limits as well as source and policy state; the sampler has no hidden
sampling state, ambient effects, adaptive policy, or frame synthesis.

Detector-to-source mapping and RGB24 crop extraction are deterministic domain
utilities rather than a new model or storage port. The crop value can produce
the `Artifact` descriptor consumed by `EvidenceStore`; the utility's optional
validation sink accepts only a private, effective-UID-owned artifact root and
descriptor-relative destinations through trusted, non-writable-by-others
parents. Its confinement boundary covers untrusted destination strings and
pre-existing links. The trusted caller must serialize same-principal namespace
mutation during the call; durable custody and the accepted writer-lock/CAS
commit protocol remain the responsibility of `EvidenceStore`.

`visualworld.storage.LocalEvidenceStore` is the first production
`EvidenceStore`. The stable port remains only digest/size-verified `put` and
SHA-256 `get`. Adapter extensions provide serializable staged handles,
idempotent commit/discard, bounded integrity inspection/inventory, identity-bound
cleanup handles for incomplete stages, and coordinator-authorized deletion.
`writer_session()` lets the future coordinator hold the shared store lock across
filesystem and SQLite intent/reference steps; the filesystem adapter itself
never decides whether an artifact is referenced, orphaned, or safe to clean.

`visualworld.world_store.LocalWorldStore` is the first production `WorldStore`.
It stores canonical version-1 records in typed SQLite `STRICT` tables with
deterministic projection columns and fixed parameterized access. Adapter
extensions keep preparing-run records hidden, expose page-recoverable pending
runs and artifact stage intents,
and atomically publish a committed run only after every intent has a matching
owned evidence reference. Mutations normally acquire the shared `writer.lock`;
the same APIs can instead consume an active `EvidenceWriterSession` so the
future coordinator can keep filesystem and metadata steps under one exclusive
kernel lock without receiving a raw SQLite connection.

The additive private schema version 2 keeps that stable port and its version-1
record union unchanged. `LocalWorldStore` adapter methods accept exact
`Observation` and completed `Tracklet` records in bounded hidden batches, retain
ordered track points as the trajectory projection, and persist the complete
metadata-only `EvidenceIntent` before optionally linking it to a run-owned
`EvidenceRef`. Finalization validates source, frame, run, membership, intent,
artifact-reference, and CAS agreement before publishing the existing run marker
in the same SQLite transaction. Reads use complete integer-PTS cursors and short
SQLite snapshots; source deletion and failed-run cleanup traverse the additive
graph without treating a model, category, or hardware runtime as a storage
concept.

## Version-1 perception ports

Issue [#70](https://github.com/mayank-gupta16/vision-query-system/issues/70)
defines three additive, experimental ports without selecting a native runtime:

| Port | Version-1 operation |
| --- | --- |
| `Detector` | Detect over one `Source` and a bounded tuple of its `FrameRef` values, returning pixel-free `Observation` records. |
| `Tracker` | Form completed clip-local `Tracklet` records from a bounded source/frame/observation set. |
| `EvidenceSelector` | Select a bounded ordered tuple of observation identifiers from one completed tracklet. |

Each result is explicitly `complete`, `unknown`, or `unsupported`. A complete
result may be empty; an incomplete result contains no partial records and must
carry a bounded, path-inert stable reason code. This keeps unavailable source
pixels, an absent runtime, an unsupported platform, or a class outside the
measured boundary distinct from a measured frame with no vehicle.

The stable `Detector` operation passes source and frame references, not pixels,
paths, model tensors, or vendor objects. A later production adapter owns its
authorized decode/inference boundary internally. `Tracker` and
`EvidenceSelector` receive only versioned pixel-free domain values. `Tracker`
may also receive one `FrameDiscontinuity` score per frame; the RGB24 scoring
utility runs transiently outside the port, and neither pixels nor source paths
cross the tracking boundary. All three ports use the existing
capability/error/instrumentation contract and have offline deterministic fakes
for ordinary orchestration, recovery, security, and adapter-substitution tests.
Fake detector and tracker boundaries verify unique frame positions, frame
PTS/duration time bases against the selected source stream, and observation
geometry dimensions against that stream.

`visualworld.tracking.GlobalLastBoxTracker` is the first concrete `Tracker`.
For the measured vehicle/5-FPS boundary it performs exact global
maximum-total-IoU assignment against each active track's last detector-supported
box, applies the frozen 0.10 IoU threshold, tolerates five misses, and terminates
on the sixth. A discontinuity score of at least 1,500 basis points terminates
active tracklets before post-cut association. Missing discontinuity scores
return `unknown`; other categories and sampling rates return `unsupported`.
Deterministic IDs are source-clip-local and never imply persistent ReID.

Its `track_page` extension carries bounded active state in an opaque immutable
cursor, preserving association across page boundaries without hidden progress
state or a fabricated page termination. The cursor owns deep snapshots of and
binds the source, stream, time base, limits, exact last position, active
detector-supported trajectories, miss counts, and bounded diagnostics. A keyed
integrity seal binds it to the tracker instance that issued it, so altered or
cross-instance cursors fail closed. End-of-stream finalization is explicit;
cancelled or invalid calls publish neither a call record nor continuation state.

The stable operation remains bounded to 64 frames and 64 points in one completed
tracklet. A detector/tracker frame batch may carry at most 4,096 observations so
the bound still represents the frozen six-object tracking fixture. The concrete
tracker extension can resume longer clips across bounded pages, but it still
fails closed if one uninterrupted trajectory exceeds the record's 64-point
limit. Perception persistence and coordination remain assigned to issues
#75–#76; an adapter cannot hide those behaviors behind an implementation name.

## Version-2 perception coordination

`PerceptionCoordinator` is an additive local-store composition contract. It
replays opaque source pages from zero after a recovered hidden run; sampler and
tracker cursors never cross a process boundary. It reserves one sampler slot
for exact page overlap, keeps tracker continuation in-process, and rejects an
uninterrupted 65th point with a bounded `limit_exceeded` error before any run
is published. Its configuration bounds source candidates, pages, sampled
frames, observations, completed tracklets, and metadata intents. The metadata
byte budget covers the canonical source, the larger preparing/committed run
manifest, and every graph record; storage-engine overhead is outside this
logical-record budget.

The coordinator accepts only deterministic offline adapters and records their
capability configuration as run provenance. A discontinuity provider receives
only `Source`, the prior selected `FrameRef` (or `None` at replay start), and
the exact selected page; it returns complete, unknown, or unsupported
pixel-free `FrameDiscontinuity` facts. Unknown scores are propagated rather
than synthesized. The vehicle-only path validates source/frame/stream/PTS
relationships and keeps all pixels, paths, crops, and artifact bytes outside
the public config, result, events, errors, and stored v0.2 graph.

`PerceptionCoordinator` invokes trusted in-process application adapters, as the
version-1 port boundary requires. `max_duration_ns` is a cooperative response
budget checked after every adapter return and before persistence; it cannot
preempt blocked Python code. An adapter that needs an enforced deadline or
cancellation must provide its own reviewed isolation. The production OpenVINO
detector provides that isolation. Adapter `timeout` and `cancelled` failures,
and otherwise-valid values returned after the elapsed budget, are redacted and
fail closed.

The coordinator validates the authorized `Source` record and every pixel-free
`FrameRef` relationship, then requires the final source record to be identical.
The metadata-only `run` path remains unchanged. The additive
`run_with_original_frames` path records distinct reader and materialization
producers, uses the exact reader for page-spanning hard-cut scores, and reads
only frames selected by an explicit evidence intent for crop materialization.
Unknown or unsupported pixel access propagates without publishing a partial
graph.

One writer session persists hidden schema-v2 frames, observations, completed
tracklets, and metadata-only `EvidenceIntent`s. For the materializing path, the
same session stages exact crops, records their artifact intents, promotes the
CAS bytes, commits the matching `EvidenceRef` records and selection links, then
atomically publishes the run marker. A retry verifies both the visible graph and
CAS before reporting an already-committed disposition. The producer/config-
distinct materializing run never upgrades or mutates a committed metadata-only
run.

`OriginalFrameReader` is a vendor-neutral, bounded port over one `Source` and
exact `FrameRef` values. Its complete result pairs each requested reference with
owned packed RGB24 bytes in request order; `unknown` and `unsupported` results
contain no frames. The first production implementation is Linux-only and reads
an already-authorized local file through the frozen media runtime plus the
separate original-frame worker overlay. Its private sealed-memory transport,
runtime verification, source/frame reconciliation, and whole-cgroup cleanup are
adapter internals. Public results, instrumentation, errors, and persistence stay
path- and pixel-free. The deterministic fake implements the same record and
limit contract in ordinary CI.

`visualworld.evidence.BestFrameEvidenceSelector` is the first concrete
`EvidenceSelector`. Its stable `select` operation remains pixel-free and returns
at most three observation identifiers by default, with a caller-configurable
hard ceiling of eight. Its metadata-only `plan` extension preserves the rank,
integer confidence/visible-area/boundary-contact components, deterministic
source-time tie-breaks, exact source Geometry, selector provenance, private
retention classification, and coordinator-owned source-cascade deletion policy.
The ranking and default configuration digest are frozen by
[ADR-0008](../decisions/ADR-0008-deterministic-best-frame-evidence.md).

The selector's separate `materialize` extension accepts RGB24 bytes only after
an explicit inspection or downstream-detail request. The caller resupplies the
completed Tracklet and its exact Observation set; the selector validates that
context, replans, and requires an exact match for the full issued intent before
touching pixels. It then uses the existing exact crop utility and produces a
matching `EvidenceRef`; missing pixels or declared unresolvable detail remain
`unknown`. Planning does not decode, crop, write, or retain anything. Generic
callers may still supply already-authorized RGB24 to this extension, whose crop
hash alone does not attest the source frame. The production materializing
coordinator instead obtains that RGB24 from the exact `OriginalFrameReader`,
preserving the Source/FrameRef/runtime binding through crop publication and
source-cascade deletion.

`visualworld.detection.OpenVinoVehicleDetector` is the first concrete
`Detector`. Its fixture-worker seam implements the same record contract in
ordinary Linux/macOS CI. Its production `IsolatedPerceptionWorker` supports only
the exact ADR-0007 Linux x86_64 closure: the parent opens and seals one authorized
local source, the composite worker decodes requested indices and runs the frozen
384×384 RGB-to-BGR/NHWC-to-NCHW OpenVINO CPU pipeline, and only strict canonical
pixel-free detections return. The adapter maps those display-oriented detector
boxes as normalized millionths through `DetectorTransform`, retaining model
coordinate precision and rounding outward only once at encoded-source pixels.
It emits category `vehicle` only at confidence 950,000 millionths or higher. Its
producer configuration and separate provenance bind the threshold, preprocessing,
model XML/BIN, runtime closure, worker, perception manifest, media runtime, and
source digest/size. Unsupported platforms and missing or drifted isolation return
stable fail-closed outcomes; no native fallback or download path exists.
