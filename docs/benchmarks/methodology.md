# Benchmark methodology

## Hardware profiles

- **CPU-LITE:** 4 vCPU, 16 GB system RAM, no required GPU. Mandatory for every release.
- **GPU-DEV:** 16 GB VRAM-class development GPU.
- **EDGE:** Jetson, NPU, or accelerator-class device with exact model recorded.
- **HIGH-PERFORMANCE:** larger GPU where a workload justifies it.

## Reproducibility record

Every result records commit, operating system, CPU/GPU/RAM, runtime and dependency
versions, model/revision/digest, source manifest/checksums, configuration, warm-up,
repetitions, wall time, and raw machine-readable output. Compare accuracy and
resources together; never label a theoretical estimate as measured performance.

## Metrics

- Detection: precision, recall, mAP.
- Tracking: HOTA, IDF1, ID switches, fragmentation.
- Entity resolution: false merge/split, unique-count error, ReID accuracy, range coverage.
- Attributes: precision, recall, F1.
- OCR: character error rate, exact and partial plate accuracy.
- Query: answer/evidence-localization accuracy, timestamp error, unsupported handling.
- Performance: FPS, real-time factor, CPU, RSS/VRAM, disk/index size, semantic calls
  per video minute, and wall time.

Report uncertainty and failure counts. A result is comparable only when profile,
inputs, configuration, and metric implementation are compatible.
