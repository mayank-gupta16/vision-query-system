# ADR-0003: Media runtime and hostile-input isolation

- Status: Accepted
- Date: 2026-09-06
- Deciders: repository maintainers

## Context

Video containers, codecs, metadata, and parser behavior are hostile inputs.
VisualWorld also requires a decoded frame's bytes and provenance to stay paired,
with source timestamps preserved as exact rationals and missing timestamps left
missing. The initial media choice must unblock a prototype without moving native
parsers into the deterministic application process or approving an oversized
codec bundle.

## Decision

Use PyAV 18.1.0, built from its verified source distribution against
project-built FFmpeg 9.0.1 shared libraries, only inside an external media worker.
The FFmpeg source build is signature-verified, disables autodetection and network,
uses no external codecs, and omits GPL, version-3, and nonfree configure flags.
The exact measured component surface is frozen with the issue #4 experiment.

The trusted parent opens one bounded regular source and passes its descriptor.
The worker uses a fixed API path, not media-derived commands or paths. It decodes
once and emits bounded, versioned records that pair canonical RGB24 bytes with
their exact rational PTS/DTS/duration, source geometry/color metadata,
transformation record, hashes, and bounded side-data evidence. Automatic PTS
generation and metadata filling are disabled and asserted. Missing timestamps
remain null; inferred time, if later added, is a separate provenance-bearing
field.

PyAV 18.1.0 lacks per-frame sample-aspect-ratio and best-effort-timestamp
bindings. The first adapter must add a small reviewed binding or upstream patch
and prove it with generated fixtures. Until then, stream SAR is explicitly
labelled a guess and never stored as observed frame evidence.

For hostile media, Linux is the only initially supported decode platform. The
worker runs non-root in a bubblewrap filesystem/PID/network/user namespace inside
a cgroup-v2 supervisor with byte, frame, geometry, output, memory, task, CPU, and
wall limits. Capability probing is mandatory and failure returns
`IsolationUnavailable`; there is no ordinary-subprocess fallback. Seccomp,
Landlock, safe descriptor opening, whole-cgroup cancellation, and bounded output
draining are requirements for issue #11 rather than additional issue #4 tuning.

Native hostile decode on macOS is blocked. `sandbox-exec` is deprecated, while a
native App Sandbox helper does not provide the demonstrated job-wide resource
and descendant-kill controls. A no-network constrained Linux VM is the preferred
future path. Trusted generated-fixture experiments may run natively, but must not
be presented as the hostile-media guarantee.

No FFmpeg, PyAV, Python, or sandbox binary is added to the application package by
this decision. Redistribution remains unapproved until the release artifact has
complete notices, corresponding-source/relinking handling, platform manifests,
and codec-patent review for its intended jurisdictions.

## Alternatives

- **Minimal FFmpeg CLI worker:** smallest dependency surface and retained as a
  comparator, but its decode path replaces frame PTS with best-effort or
  synthesized values before filters. It cannot meet timestamp provenance.
- **Official PyAV wheel:** fast to install, but the measured 18.1.0 wheel bundles
  FFmpeg 8.1.2 plus many unrelated codec, crypto, display, and network libraries.
  Its broad composite licensing/provenance is not approved for the runtime.
- **PyAV in the main process:** rejected because native parsers would share the
  application's trust boundary.
- **GStreamer:** deferred; its plugin/registry surface does not resolve the
  one-pass provenance requirement for the first prototype.
- **Native libav helper:** remains a later option if the small PyAV provenance
  bindings become harder to maintain than a dedicated helper.

## Consequences

The first end-to-end prototype can target generated MP4/H.264 on the Linux
CPU-LITE path while the core remains portable and dependency-free. Media packages
stay adapter-local. The cost is a source-built native runtime and a Linux-only
hostile-media capability in the first slice. Additional containers/codecs,
macOS isolation, packaging, security-update cadence, and performance tuning are
incremental follow-up work, not blockers to the first working milestone.
