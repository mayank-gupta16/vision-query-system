# Hostile media and source ingestion

Treat all media and source metadata as maliciously constructible.

- Separate network fetch, media decode, and indexing boundaries.
- Decode as non-root with no network and only the required source/output access.
- Use argument arrays, never shell-built FFmpeg/probe commands.
- Enforce source byte, duration, resolution, frame-count, decoded-output,
  CPU/RAM, wall-time, and artifact limits with cancellation.
- Canonicalize paths; reject traversal, unsafe filenames, and symlink escapes.
- Commit metadata/artifacts atomically so failures leave recoverable state.
- Allowlist schemes. Remote/RTSP sources require explicit permission plus
  redirect, DNS-rebinding, loopback, link-local, private-address, and cloud
  metadata protections.

Before release, fixtures must prove malformed/oversized media terminates within
limits, traversal cannot escape the workspace/artifact root, and remote fetches
cannot reach unintended local services.

## Accepted first-slice boundary

[ADR-0003](../decisions/ADR-0003-media-runtime-and-isolation.md) selects a
source-built PyAV/FFmpeg worker for the first Linux prototype. Hostile media must
run in the non-root namespace/cgroup boundary and fail with
`IsolationUnavailable` when its required capabilities are absent. Native hostile
decode on macOS is unsupported until a separately validated boundary exists.

Issue #11 owns the production launcher. It must safely open the source as a
bounded regular-file descriptor, mount only an immutable runtime closure, disable
network, add the reviewed seccomp/Landlock policy, cap and concurrently drain all
output, and kill the whole cgroup on cancellation or limit breach. There is no
fallback to a main-process or ordinary-subprocess decoder.

The implemented Linux launcher opens beneath the approved source root with
`openat2`, snapshots the source into a sealed anonymous file, and launches a
root-owned worker from inside the immutable runtime. Landlock permits read/execute
only on that runtime and its fixed library mounts; a mounted `/proc` path remains
denied. The worker also verifies non-root execution, no-new-privileges, and network
denial before its output is accepted. Missing capabilities fail closed.

## Perception composition

[ADR-0007](../decisions/ADR-0007-isolated-perception-runtime.md) extends this
boundary with a separately provisioned, root-owned perception closure. Decode
and inference run in one composite worker so RGB24 frames remain transient and
never traverse the application IPC boundary. The trusted parent still passes
only the sealed source descriptor. The worker returns bounded canonical JSON
containing pixel-free observations; worker stdout/stderr are untrusted and never
become log or exception text.

Both media and perception manifests and trees must validate before launch. A
missing notice, partial install, symlink/special file, permission or ownership
drift, malformed/oversized output, crash, timeout, or cancellation fails closed
and triggers whole-cgroup termination and descendant cleanup. The worker has a
clear environment, no network, no ambient shell, forced telemetry opt-out,
read-only mounts, non-root/no-new-privileges execution, seccomp, Landlock, and
bounded CPU/RSS/task/output/time limits. Linux x86_64 with GNU libc 2.28 or newer
and CPU inference is the only supported path; there is no native or ordinary-
subprocess fallback elsewhere.
