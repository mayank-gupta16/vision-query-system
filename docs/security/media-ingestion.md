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

Issue #9 owns the production launcher. It must safely open the source as a
bounded regular-file descriptor, mount only an immutable runtime closure, disable
network, add the reviewed seccomp/Landlock policy, cap and concurrently drain all
output, and kill the whole cgroup on cancellation or limit breach. There is no
fallback to a main-process or ordinary-subprocess decoder.
