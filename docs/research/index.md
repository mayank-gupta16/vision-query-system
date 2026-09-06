# Research documentation

[Critical questions](critical-questions.md) lists unresolved questions that must
become bounded GitHub research issues. Results require question, hypothesis,
experiment, dataset/license, metrics, hardware, findings, limitations, and an
engineering recommendation. Research branches do not silently become product
architecture.

## Completed research

- [Application toolchain clean-install experiment](application-toolchain.md) —
  supports accepted ADR-0002 and issue #3; includes a
  [machine-readable CPU-LITE receipt](toolchain-cpu-lite-receipt.json).
- [Media runtime and isolation experiment](media-runtime.md) — supports accepted
  ADR-0003 and issue #4; includes a
  [machine-readable CPU-LITE receipt](media-runtime-cpu-lite-receipt.json).

## Implementation validation

- [Package scaffold](package-scaffold.md) — issue #8 reproducibility,
  clean-runtime packaging, dependency inventory, and
  [raw CPU-LITE setup evidence](scaffold-cpu-lite-receipt.json).
- [Local video source](local-video-source.md) — issue #11 exact metadata,
  structured failure, isolation, and CPU-LITE validation with a
  [machine-readable receipt](local-video-source-cpu-lite-receipt.json).
