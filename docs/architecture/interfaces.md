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
