# Media runtime and isolation experiment

- Issue: https://github.com/mayank-gupta16/vision-query-system/issues/4
- Experiment date: 2026-09-06
- Result: supports [ADR-0003](../decisions/ADR-0003-media-runtime-and-isolation.md)

## Question and hypothesis

Can the first video prototype preserve frame-level evidence while keeping native
media parsing outside the application trust boundary on the required CPU-LITE
host?

The hypothesis was that a small FFmpeg CLI worker would be enough. Measurement
rejected that hypothesis: FFmpeg's CLI decode path can synthesize frame PTS before
the metadata filter observes it. A source-built PyAV worker was then tested as the
smallest Python-aligned one-pass alternative.

## Candidate result

| Candidate | Evidence fidelity | Runtime/legal surface | Result |
| --- | --- | --- | --- |
| Minimal FFmpeg 9.0.1 CLI | Pixels and metadata can be emitted from one process, but missing PTS may become inferred PTS and full display matrices are not structured | Small, project-built, LGPL-2.1-or-later configuration | Comparator only |
| PyAV 18.1.0 source + minimal FFmpeg 9.0.1 | One decode loop pairs nullable rational timestamps, exact pixel bytes, color, and raw side-data hashes | PyAV BSD-3-Clause plus project-built LGPL-2.1-or-later FFmpeg; source build required | Selected worker API |
| Official PyAV 18.1.0 wheel | Convenient and functional | Bundles FFmpeg 8.1.2 and a broad external codec/network/display closure; Linux wheel 35,786,210 bytes | Not approved |
| GStreamer | Credible plugin framework | Additional registry/plugin surface without fixing the first provenance blocker | Deferred |

The official FFmpeg 9.0.1 archive was verified with the published release key
fingerprint `FCF986EA15E6E293A5644F10B4322F04D67658D8`; its SHA-256 is
`cf38e0e28c7e5605942c4a77755349b0145804a397af37eb1fb4c77cb237f635`.
The PyAV 18.1.0 sdist SHA-256 is
`47bfc286e1bc9de7ab4681fc2b575cd2460a66919d31ffe1bd5aa54fae531a28`.
The measured source build linked PyAV to FFmpeg 9.0.1 and reported
`LGPL version 2.1 or later`; it enabled no external libraries.

## Correctness and failure probes

Generated H.264 fixtures covered MP4 rational timestamps with a nonzero start,
an elementary stream with no timestamps, and a 90-degree display matrix. The
selected worker returned all 30 MP4 frames from PTS 90,000 through 351,000 at
time base 1/90,000. All five elementary-stream frames retained null PTS. The
display matrix was preserved as nine signed 32-bit values, and every framed
RGB24 payload passed its embedded SHA-256 and exact byte-length check.

Malformed bytes and a truncated MP4 returned bounded decode errors. A 4096×2160
frame, a source one byte over 64 MiB, a two-frame limit, and a decoded-output
limit each returned the expected deterministic limit classification. All eight
cases ended in 0.11–0.21 seconds in the bubblewrap probe; none reached the
five-second outer timeout.

One integration failure improved the frozen probe: creating a separate PyAV
reformatter for every frame accumulated conversion workers and hit a 32-task
cgroup cap. Reusing one reformatter completed the same decode at that cap. This
is a correctness/resource-lifetime requirement for issue #11, not a performance
optimization project.

## CPU-LITE observation

The disposable Ubuntu 26.04.1 x86_64 host had 4 vCPU, 15,841,396 KiB RAM, and no
required GPU. After two warm-ups, ten unsandboxed worker runs decoded the
30-frame 320×240 fixture in a median 0.115 seconds (about 261 frames/s) with
38,332 KiB median process peak RSS. Ten bubblewrap runs had a median 0.110-second
wall time; the launcher-only RSS reported by GNU time is intentionally discarded.
One full systemd-cgroup plus bubblewrap run completed in 122 ms, used 118 ms CPU,
and reported a 26.1 MiB cgroup memory peak. Ten cold PyAV imports had a median
55 ms wall time and 26,068 KiB peak RSS.

These small generated-input measurements are prototype diagnostics, not release
performance promises. Raw samples, hashes, commands, exit codes, and limitations
are in the [machine-readable receipt](media-runtime-cpu-lite-receipt.json).

## Isolation and platform conclusion

Ubuntu's packaged bubblewrap 0.11.1 successfully launched as non-root despite the
host's restricted generic user namespaces. The measured empty-root namespace hid
host-private paths, disabled the host network namespace, retained seek on the
single inherited regular-file descriptor, and required explicit
`--remount-ro /` to prevent writes outside the capped temporary filesystem.
Cgroup v2 supplied aggregate memory/task/CPU-bandwidth/deadline controls.

Issue #11 must turn the measured command into a deterministic supervisor with
safe descriptor opening, exact runtime staging, inner seccomp and Landlock,
whole-cgroup cancellation, output caps, capability probes, and fail-closed error
mapping. The research did not implement a production sandbox.

On macOS 26.6.2 arm64, the installed `sandbox-exec` documentation marks it
deprecated. App Sandbox is useful defense in depth but does not establish the
same hard job-wide resource and descendant-kill guarantee. Hostile media remains
unsupported there until a constrained no-network Linux VM or equivalent boundary
passes the same tests.

## Licensing and limitations

PyAV is BSD-3-Clause; the selected FFmpeg configuration is
LGPL-2.1-or-later. This is not approval to redistribute their binaries. The
official PyAV wheel's bundled external components are explicitly excluded.
H.264, HEVC, and MPEG-4 patent exposure is separate from copyright licensing and
requires jurisdiction-specific review before commercial distribution claims.

PyAV 18.1.0 does not expose per-frame SAR or best-effort timestamps. Stream SAR
is therefore labelled as a guess in the probe. Issue #11 must add the small
binding before storing SAR as observed evidence. The experiment uses synthetic
media only, exercises Linux x86_64 only, and does not establish broad real-video
codec coverage.

Primary references: [FFmpeg downloads](https://ffmpeg.org/download.html),
[FFmpeg legal checklist](https://ffmpeg.org/legal.html),
[FFmpeg fd protocol](https://ffmpeg.org/ffmpeg-protocols.html#fd),
[PyAV 18.1.0 release](https://github.com/PyAV-Org/PyAV/releases/tag/v18.1.0),
[bubblewrap security model](https://github.com/containers/bubblewrap), and
[Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html).
