# Evidence-safe ingestion coordinator validation

- Issue: V01-16
- Result: pass on the required CPU-LITE profile
- Evidence: [machine-readable receipt](evidence-safe-coordinator-cpu-lite-receipt.json)

## Implemented vertical slice

`IngestionCoordinator` composes deterministic offline source and sampler ports,
caller-supplied RGB24 frames and source-pixel regions, `LocalEvidenceStore`, and
`LocalWorldStore`. The version-1 path validates bounded inputs, binds selected
evidence digests into the producer configuration, deduplicates identical CAS
payloads, and publishes a deterministic committed manifest only after staged
artifacts, durable intents, metadata, references, and the commit marker agree.

Recovery handles every ingestion and deletion commit boundary idempotently.
Preparing data stays hidden, attributable staging and unreferenced artifacts are
cleaned without touching shared references, and explicit repair removes only
valid catalog-free artifacts. Source deletion freezes its source/run/frame/
evidence closure, rejects later writes, hides ordinary and recovery reads,
retains cross-source deduplicated evidence, removes unique artifacts, securely
purges metadata, truncates the WAL, and leaves an identifier-minimized receipt.

Structured events expose only stage, status, duration, aggregate item count,
and optional opaque run/deletion identifiers. Backend exception objects are
rebuilt at the coordinator boundary, so pixels, paths, prompts, credentials,
and private adapter details do not enter messages or traceback chains. No
network-capable adapter or third-party runtime dependency is used.

## Acceptance result

The fake/manual vertical run produced byte-exact retrievable original-pixel
evidence, an identical manifest on retry, and an already-committed disposition.
The source deletion completed, removed the unique artifact, and left 167,936
logical store bytes. All declared offline, determinism, evidence, retry,
deletion, timing, RSS, and disk checks passed.

The acceptance process peaked at 26,710,016 bytes RSS, below the 2 GiB bound.
The receipt records individual coordinator stage wall times plus exact hashes
for the coordinator, stores, and harness. These generated-fixture values are a
correctness and provenance baseline, not a cross-machine optimization claim.

## Limits

Version 1 accepts fake/manual regions and caller-supplied packed RGB24 pixels.
It does not decode pixels through the coordinator, call a detector, tracker,
OCR model, VLM, ReID model, or semantic index, expose the ingestion CLI, or
provide the combined 60-second application benchmark. Those remain assigned to
later milestone issues.
