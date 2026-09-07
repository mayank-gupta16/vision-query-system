# Benchmark documentation

- [Methodology](methodology.md) defines profiles, metrics, and reporting rules.
- [Datasets](datasets.md) defines fixture and external-manifest requirements.
- [v0.1 CPU-LITE application baseline](v0.1-cpu-lite-baseline.md) records the
  pinned 60-second vertical-slice workload, result, boundary, and comparator;
  its [JSON receipt](v0.1-cpu-lite-baseline.json) retains exact provenance and
  measurements.
- [v0.2 frozen evaluation gates](v0.2-evaluation-gates.md) routes the detector,
  sampling, crop-value, and tracking experiments through the versioned policy,
  dataset/receipt contracts, fail-closed evaluator, and waiver rules.
- [v0.2 detector/runtime methodology](v0.2-detection-methodology.md) locks the
  issue #21 candidates, CC0 derived dataset, privacy boundary, calibration,
  metrics, CPU-only isolation, and reproduction command.

Version-specific measured implementation baselines are recorded with their
validation reports: [local video](../research/local-video-source.md),
[exact-PTS sampling](../research/pts-frame-sampling.md),
[original-pixel crops](../research/original-pixel-crops.md), and the
[local EvidenceStore](../research/local-evidence-store.md), and the
[local WorldStore](../research/local-world-store.md). They are reproducible
single-run correctness baselines, not cross-machine optimization claims.
