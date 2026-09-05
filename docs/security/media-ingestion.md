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
