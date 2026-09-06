# Testing strategy

- **Unit tests:** fast deterministic domain, geometry, validation, and planning logic.
- **Contract tests:** every implementation of Detector, Tracker, OCRProvider,
  WorldStore, SemanticReasoner, and other ports satisfies one shared behavior suite.
- **Integration tests:** tiny licensed videos exercise bounded stage composition.
- **Golden tests:** known source produces versioned world state and query evidence.
- **Security tests:** injection, invalid schema, traversal/symlink, SSRF, malformed
  media, resource limits, redaction, unsafe artifact formats, and default egress.
- **Benchmark tests:** larger versioned sets report accuracy and resource metrics.
- **Regression tests:** meaningful bugs receive the smallest practical reproducer.

Ordinary PR CI must be fast and CPU-only. Application tests must not initiate
model/data network calls and use deterministic fakes; runner checkout/setup may
still require GitHub network access and the hosted image can receive patches. It
never downloads large weights. Real-model/GPU benchmarks are
manual, scheduled, or release-triggered and publish complete provenance.

The version-1 `VideoSource`, `FrameSampler`, `EvidenceStore`, and `WorldStore`
fakes are instrumented in memory. Their shared contract tests monkeypatch shell,
network, filesystem, and SQL entry points to prove ordinary fake orchestration
causes no external side effect, and capability requests fail closed.

Tests must distinguish exact deterministic expectations from tolerance-based
probabilistic metrics. UNKNOWN and ambiguous count ranges are first-class
expected outputs, not test failures.
