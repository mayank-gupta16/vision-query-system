# Threat model

## Protected assets

Source video, frames/crops, faces, plates/OCR, location/time, trajectories,
embeddings, prompts/responses, world state, credentials, host files, GitHub/release
integrity, and model/dependency artifacts are protected assets.

## Adversarial inputs

Media parsers may receive malformed files or decompression bombs. OCR/captions,
metadata, model output, datasets, websites, issues, comments, and fixtures may
contain injected instructions, SQL, commands, paths, or URLs. Remote sources may
attempt SSRF, redirects, DNS rebinding, or resource exhaustion. Dependencies and
weights may contain executable or compromised artifacts.

## Trust boundaries

Keep network fetching, media decoding, probabilistic inference, deterministic
world/query logic, artifact storage, and GitHub/release automation separate.
Adapters receive only the capabilities they require. Model inference receives no
shell, GitHub, database, arbitrary filesystem, or unrestricted network tools.

## P0 controls before implementation

- offline/local-only behavior and no listening socket by default;
- explicit authorization and audited destinations/data for remote adapters;
- bounded non-root media processing with cancellation and constrained access;
- typed/versioned model output with strict unknown-field/size/range validation;
- validated internal query AST and parameterized, read-only store operations;
- content integrity, locked dependencies, immutable model revisions, license records;
- sensitive-data minimization, redacted logs, retention, and cascade deletion;
- read-only fork PR CI with no secrets or elevated publishing capabilities.

Security issues must convert these controls into tests before their affected
capability is considered ready. Residual risks and waivers belong in the issue/ADR.

For the v0.2 perception path, hostile video, model XML/BIN, native runtime code,
and worker stdout/stderr remain inside the composite Linux worker boundary from
ADR-0007. Only a sealed source descriptor enters and validated pixel-free records
leave. Root-owned immutable closures, no network/telemetry/shell, bounded streams
and cgroup resources, whole-job cancellation, cleanup, and redacted stable errors
are mandatory. Missing closure or isolation capabilities are unsupported, never
permission to fall back.

The production detector enforces that boundary before each launch and again at
the worker protocol: the source snapshot must match the declared digest/size;
runtime, model, manifest, and first-party worker hashes must match; decoded frame
identity/time and stream geometry must match the requested records; and every
output field is exact-type, bounded, canonical, vehicle-only, and pixel/path-free.
Independent stdout/stderr limits, wall timeout, cancellation, and whole-cgroup
cleanup cover hostile or wedged descendants. OpenVINO thread creation is allowed
only inside the fixed task/process/cgroup limits; network, execution, namespace,
mount, tracing, kernel-module, and BPF syscalls remain denied.
