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
The exact issue #4 measured component surface remains frozen in its experiment
receipt. For the V1 worker, issue #7 additionally enables only FFmpeg's built-in
`rawvideo` decoder so the approved synthetic MOV/RGB fixtures exercise the same
worker path. A source rebuild from the same verified FFmpeg 9.0.1 archive kept
network and encoders disabled, retained the LGPL-2.1-or-later result, and decoded
all fixture PTS, rotation, and RGB hashes exactly through PyAV's inherited-file
boundary. The approved V1 surface is therefore the `mov,h264` demuxers,
`h264,rawvideo` decoders, and `h264` parser; the frozen issue #4 receipt itself is
not rewritten.

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
The issue #4 experiment demonstrated the selected namespace/cgroup direction
with denied host access and egress, bounded blocked output, prompt cancellation,
and no surviving descendant; it is not the production launcher.

Native hostile decode on macOS is blocked. `sandbox-exec` is deprecated, while a
native App Sandbox helper does not provide the demonstrated job-wide resource
and descendant-kill controls. A no-network constrained Linux VM is the preferred
future path. Trusted generated-fixture experiments may run natively, but must not
be presented as the hostile-media guarantee.

No FFmpeg, PyAV, Python, or sandbox binary is added to the application package by
this decision. Redistribution remains unapproved until the release artifact has
complete notices, corresponding-source/relinking handling, platform manifests,
and codec-patent review for its intended jurisdictions.

The accepted deployment closure is
`visualworld-pyav-18.1.0-ffmpeg-9.0.1-v2`. It contains only the frozen FFmpeg
and CPython trees, PyAV package and distribution metadata, and reviewed worker;
development probes, virtual-environment launchers, and ambient-tool links are
excluded. Its FFmpeg subtree is the issue #7 minimal rebuild; the compiled
demuxers, decoders, and parser are exactly the surface declared above. Its
manifest SHA-256 is
`58cf6f64280888ecc01c647044c38b9b56f197389b6bfc7ead7fbbe93a52ba32`
and its manifest-excluding tree SHA-256 is
`7015262cd5dfdfd976d6ee092f0f93541597ea35e0331c3abb9ce6d4c54daeaf`.
Every regular file must have one link, and every symlink must be relative,
non-dangling, confined to the closure, and resolve to a regular file. Version
`v1` is retired because its virtual-environment interpreter link resolved to an
unbound host path.

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

The first end-to-end prototype can use generated MOV/rawvideo fixtures and
MP4/H.264 on the Linux CPU-LITE path while the core remains portable and
dependency-free. Media packages stay adapter-local. The cost is a source-built
native runtime and a Linux-only hostile-media capability in the first slice.
Additional containers/codecs beyond that approved surface,
macOS isolation, packaging, security-update cadence, and performance tuning are
incremental follow-up work, not blockers to the first working milestone.
