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
- [v0.2 detector/runtime results](v0.2-detection-results.md) publishes the raw
  CPU-LITE evidence and selects vehicle-detection-0201 at confidence 0.95 as the
  evaluation baseline.
- [v0.2 frame-sampling methodology](v0.2-sampling-methodology.md) locks issue
  #22's source-separated moving-event clips, fixed/adaptive policies, PTS rules,
  accuracy/resource metrics, privacy boundary, and decision rule.
- [v0.2 frame-sampling results](v0.2-sampling-results.md) selects fixed 5 FPS as
  the lowest-sample passing default, rejects the measured adaptive trigger, and
  records explicit 8-FPS override boundaries.
- [v0.2 original-resolution crop methodology](v0.2-crop-methodology.md) locks
  issue #23's exact source/detector comparison, paired resolvable/absent detail
  charts, source-disjoint splits, privacy boundary, and decision rule.
- [v0.2 original-resolution crop results](v0.2-crop-results.md) finds material
  tiny-detail value and recommends transient on-demand source retrieval with
  selective best-frame retention instead of eager crop storage.
- [v0.2 short-term tracking methodology](v0.2-tracking-methodology.md) locks
  source-separated complete-sequence evaluation for issue #24 before test
  inference.
- [v0.2 short-term tracking results](v0.2-tracking-results.md) selects the
  global last-box IoU policy, records both passing finalists and their frozen
  test evidence, and keeps persistent ReID outside the measured boundary.

Version-specific measured implementation baselines are recorded with their
validation reports: [local video](../research/local-video-source.md),
[exact-PTS sampling](../research/pts-frame-sampling.md),
[original-pixel crops](../research/original-pixel-crops.md), and the
[local EvidenceStore](../research/local-evidence-store.md), and the
[local WorldStore](../research/local-world-store.md). They are reproducible
single-run correctness baselines, not cross-machine optimization claims.
