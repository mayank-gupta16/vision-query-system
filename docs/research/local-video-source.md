# Local video source implementation validation

- Issue: V01-11
- Result: pass on the required CPU-LITE profile
- Runtime: source-built PyAV 18.1.0 with libavformat/libavcodec 63.1.101
- Evidence: [machine-readable receipt](local-video-source-cpu-lite-receipt.json)

## Implemented boundary

The trusted parent accepts one bounded relative path, opens it beneath an approved
source root with `openat2`, rejects symlinks and non-regular files, and copies the
bytes into a sealed anonymous file. A fixed argument array launches the reviewed
worker from the root-owned runtime through systemd and bubblewrap. The worker is
non-root, has no network namespace, uses no-new-privileges plus seccomp, and applies
Landlock read/execute access only to the immutable runtime/library mounts. CPU,
memory, tasks, wall time, output, geometry, frame count, and decoded bytes are
bounded; cancellation and breaches kill the whole cgroup.

The worker decodes once and returns a strictly validated versioned record. It
preserves exact rational PTS and durations, encoded dimensions, rotation, codec,
stream time base, source fingerprint, key-frame state, and canonical RGB24 hashes.
Automatic timestamp generation is disabled and asserted.

## Acceptance result

The generated CFR, VFR, and 90-degree-rotation MOV fixtures matched every reviewed
fingerprint, dimension, duration, PTS, time-base, rotation, codec, frame-count, and
pixel-hash expectation. Observed per-fixture wall time was 99–103 ms and peak
cgroup memory was 13.7 MB. Corrupt, oversized, traversal, symlink, timeout, and
cancellation cases all returned their expected structured failures. A successful
worker result also requires its non-root, no-new-privileges, no-network, and
Landlock-denial probes to be true.

## Limits

This validates the Linux x86_64 first slice and project-generated tiny fixtures,
not broad container/codec compatibility or cross-machine performance. Native
hostile decode on macOS, remote/RTSP sources, redistribution of the media runtime,
and optimization remain out of scope.
