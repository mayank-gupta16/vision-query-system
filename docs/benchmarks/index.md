# Benchmark documentation

- [Methodology](methodology.md) defines profiles, metrics, and reporting rules.
- [Datasets](datasets.md) defines fixture and external-manifest requirements.
- [v0.1 CPU-LITE application baseline](v0.1-cpu-lite-baseline.md) records the
  pinned 60-second vertical-slice workload, result, boundary, and comparator;
  its [JSON receipt](v0.1-cpu-lite-baseline.json) retains exact provenance and
  measurements.

Version-specific measured implementation baselines are recorded with their
validation reports: [local video](../research/local-video-source.md),
[exact-PTS sampling](../research/pts-frame-sampling.md),
[original-pixel crops](../research/original-pixel-crops.md), and the
[local EvidenceStore](../research/local-evidence-store.md), and the
[local WorldStore](../research/local-world-store.md). They are reproducible
single-run correctness baselines, not cross-machine optimization claims.
