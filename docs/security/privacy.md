# Privacy and data lifecycle

Raw video and every derivative—including crops, plates, faces, OCR, timestamps,
locations, trajectories, embeddings, prompts, caches, indexes, debug bundles,
and exports—inherit the source's sensitivity unless explicitly classified higher.

Defaults:

- local/offline processing with no listening service and no egress;
- remote/fetch/model adapters disabled until explicitly configured per project;
- disclose the destination and exact data categories before remote transmission;
- face crops, plate OCR, and cross-recording human ReID disabled until their
  capability has approved safeguards; real-world face identity remains out of scope;
- logs contain only stable operation names, aggregate counts/metrics, status,
  and opaque record/deletion IDs or content hashes explicitly needed for
  correlation—not paths, pixels, OCR/plates, raw prompts, tokens, credentials,
  or backend exception text;
- configurable retention and cascade deletion across artifacts, caches, indexes,
  prompts, world state, and exports.

"Preserve original pixels" means retain full available quality while authorized,
not retain personal data indefinitely. A deletion test must prove no derived
sensitive artifacts remain and audit any independently retained source reference.

[ADR-0005](../decisions/ADR-0005-local-storage-and-deletion.md) defines the
accepted local staged-write, recovery, reference-aware cascade deletion, and
reduced deletion-receipt protocol.

The local WorldStore keeps only canonical metadata, lookup projections, artifact
descriptors/references, and reduced coordination state in a private mode-0600
SQLite/WAL set. Schema version 2 adds pixel-free observations, completed
tracklets and exact ordinal membership, and complete selected-evidence intent
metadata; it adds no crops, model tensors, decoder output, source paths, video,
or artifact bytes. Fixed SQL remains internal, and backend exception text is
never surfaced. Preparing runs and every version-1 or version-2 record under a
pending deletion closure are not returned by ordinary list operations.

Best-frame selection is metadata-only. Its intent records exact source geometry,
opaque record identifiers, integer scores, producer provenance,
`derived_private` retention, and `coordinator_source_cascade` deletion ownership,
but no pixel bytes or source locator. Original RGB24 data may enter only the
explicit materialization call for a declared inspection or downstream-detail
need after the selector validates a freshly recomputed full-intent match against
the resupplied Tracklet and Observations. Selection snapshots those records by
capturing every field once and directly reconstructing exact recursive owned
values rather than using caller-owned serialization. Concurrent mutation and
unexpected failures produce static context-free port errors. Crop
representations, diagnostics, structured failures, and metadata mappings omit
those bytes. The caller remains responsible for supplying bytes from the intended
frame; no decoder attestation is claimed. Durable custody still requires the
accepted EvidenceStore/coordinator path.
