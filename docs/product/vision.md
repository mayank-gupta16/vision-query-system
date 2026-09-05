# Product vision

VisualWorld converts video into persistent, queryable, evidence-backed world
state. A user should be able to ask structured or natural-language questions
about entities, attributes, relationships, events, trajectories, and time, and
receive answers linked to exact source evidence and calibrated uncertainty.

The fundamental abstraction is:

```text
video
  -> observations
  -> tracklets
  -> persistent entities
  -> temporal attributes, relationships, and events
  -> world database
  -> structured or natural-language query
  -> evidence-backed answer
```

Video is queryable data; a vision-language model is only one replaceable
component. Cheap deterministic work and specialist models should narrow
candidates before expensive semantic inference. Equivalent semantic results
are cached with enough versioning to prevent stale reuse.

The system is local-first and must remain useful on the CPU-LITE reference
machine: 4 vCPU, 16 GB system RAM, no required GPU. Processing recorded video
slower than real time is acceptable when accuracy, evidence, and reproducibility
are preserved.

The mature product should support recorded files, legally permitted public
sources, dashcams, CCTV, RTSP, webcams, multiple cameras, and large collections.
These arrive in stages; the current repository does not yet implement them.
