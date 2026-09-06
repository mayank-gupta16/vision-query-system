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
- logs contain only stable operation names, aggregate counts/metrics, and
  status—not paths, run/stage IDs, hashes, pixels, OCR/plates, raw prompts,
  tokens, credentials, or backend exception text;
- configurable retention and cascade deletion across artifacts, caches, indexes,
  prompts, world state, and exports.

"Preserve original pixels" means retain full available quality while authorized,
not retain personal data indefinitely. A deletion test must prove no derived
sensitive artifacts remain and audit any independently retained source reference.

[ADR-0005](../decisions/ADR-0005-local-storage-and-deletion.md) defines the
accepted local staged-write, recovery, reference-aware cascade deletion, and
reduced deletion-receipt protocol.

The version-1 local WorldStore keeps only canonical metadata, lookup
projections, artifact descriptors/references, and reduced coordination state in
a private mode-0600 SQLite/WAL set. It never stores source paths, video or
artifact bytes, raw SQL, or backend exception text. Preparing runs and records
under pending deletion closure are not returned by ordinary list operations.
