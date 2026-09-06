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
