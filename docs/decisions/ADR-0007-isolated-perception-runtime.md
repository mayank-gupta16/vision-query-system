# ADR-0007: Isolated perception runtime and provisioning boundary

- Status: Accepted
- Date: 2026-09-08
- Deciders: repository maintainers

## Context

Issue #21 selected Open Model Zoo `vehicle-detection-0201` FP32 with OpenVINO
2026.3.1 and a confidence threshold of 0.95 as an evaluation baseline. That
selection did not approve a product runtime, artifact download during ingest, or
redistribution. Native decode and inference process hostile media, model files,
native libraries, and untrusted worker output. They therefore cannot share the
deterministic application process or acquire ambient host capabilities.

The selected interpreter archive also has two relevant representations. The
issue #21 benchmark used a uv-managed tree with SHA-256
`7bf650c9ac94c945b0ff154bdc96830bc1ad876d24b0cbe8deb5860fd3270a0c`.
uv added `BUILD` and `EXTERNALLY-MANAGED` files and rewrote build metadata. A
direct safe extraction of the same verified upstream archive has the stable tree
SHA-256 `7a39aea7cdb142c0c39fdbde871aded02c6c35549a76ca30d30a07eda0125b46`.
Unlike the historical evaluation digest, this product digest includes the three
shipped importable bytecode files; added, removed, or modified bytecode is drift.
Product provisioning must not silently depend on developer-tool mutation.

## Decision

Use one composite Linux x86_64 worker for both decoding and inference. The
trusted parent opens an authorized bounded local video beneath its configured
source root, seals the snapshot, and passes only the inherited read-only file
descriptor. Decode, transient RGB24 frames, 384x384 preprocessing, and OpenVINO
inference remain inside the worker. Only bounded canonical JSON containing
pixel-free observation fields may return. Pixels, vendor objects, paths, model
bytes, and arbitrary worker text do not cross the boundary.

The worker composes the accepted root-owned PyAV 18.1.0/minimal FFmpeg 9.0.1
media runtime `visualworld-pyav-18.1.0-ffmpeg-9.0.1-v2` from ADR-0003 with the
separately installed perception runtime. Both runtime manifests and both content
trees are verified before every launch. A missing, partial, mutable, unsafely
linked, non-regular, writable, non-root-owned, or digest-drifted closure fails
closed. Linux x86_64 with GNU
libc 2.28 or newer and CPU inference is the only supported platform. Every other
OS, architecture, libc/ABI, device, or absent isolation capability returns
`unsupported` or `isolation_unavailable`; there is no main-process or ordinary-
subprocess fallback.

The exact product lock is
[`workers/perception-runtime-v1.json`](../../workers/perception-runtime-v1.json),
raw SHA-256
`6a3af4c3f82d9fc08a45971ad27562c583c02daa1e26df0811d8fa7588e5a3f5`.
It binds:

- the CPython 3.13.15 python-build-standalone 20260825 archive, raw extracted
  interpreter tree, executable, version, source revision, size, URL, license,
  and known incomplete redistribution notices;
- the exact OpenVINO build
  `2026.3.1-22476-759c5a6ab8c-releases/2026/3`, NumPy 2.5.3, and
  openvino-telemetry 2025.2.0 wheels, including their RECORD-verified notice
  files and the notice-derived bundled-component/license-expression inventory;
- the selected model XML/BIN, pinned Open Model Zoo revision, byte sizes,
  hashes, URLs, format, and external license evidence;
- the issue #21 uv-managed Python and runtime closure hashes as evaluation
  provenance, plus the direct-extraction product runtime closure SHA-256
  `5859db2175bcf154586052d891f7e4f06cfb6b38cea5c62ac625c7d7aa961530`;
- the accepted media-runtime identity and manifest/tree hashes, inference
  configuration, platform, policy, distribution status, and worker limits.
- the first-party Apache-2.0 composite worker installed at
  `worker/perception_worker.py`, SHA-256
  `6188e2960985bf983f829222bf0f4c8d174275077b46605e7b451c44780c66aa`.

Provisioning is an administrator-invoked operation separate from application
execution. The standard-library-only
[`scripts/provision_perception_runtime.py`](../../scripts/provision_perception_runtime.py)
has distinct `verify-manifest`, `fetch`, `verify-cache`, `install`, and
`verify-runtime` commands. `fetch` is the only network-using command. It accepts
only the frozen HTTPS URLs and reviewed redirect hosts, ignores ambient proxy
configuration, verifies exact length and SHA-256 before parsing, writes private
temporary files, atomically links complete artifacts into the cache, and reuses
already verified artifacts on retry.
`install` re-verifies the complete cache, safely extracts only regular files and
internal file-resolving symlinks, verifies wheel RECORDs and all declared notices,
builds a private staging tree, checks every component hash, freezes it root-owned
and read-only, writes and fsyncs every complete file, fsyncs the frozen directory
graph bottom-up, and publishes through one atomic rename followed by destination-
parent fsync. Existing incomplete destinations are never overwritten. A failure
before publication removes staging; a failure after a visible publication leaves
only the already verified fail-closed root for a safe retry. Normal VisualWorld
commands neither import this tool nor download runtime artifacts.

Worker execution inherits ADR-0003's sealed-source and launcher controls. It must
use a clear environment, an unshared network namespace, non-root identity,
no-new-privileges, read-only runtime/input mounts, seccomp and Landlock policy,
and user/mount/PID/IPC/UTS isolation inside a cgroup-v2 supervisor. The frozen
limits are 4 CPU cores, 2 GiB RSS, 1,024 tasks, 300 seconds wall time, 16 MiB
canonical output/stdout, and 1 MiB stderr. The launcher concurrently drains both
streams, treats any malformed, oversized, crash, timeout, or unexpected stderr
result as a structured redacted failure, kills the whole cgroup on failure or
cancellation, waits for descendant cleanup, and does not expose worker text in
logs or exception chains.

The implemented worker permits the thread-creation syscalls OpenVINO/oneTBB
requires, under the 1,024-task cgroup and process limits, while seccomp denies
socket, process-execution, namespace, mount, tracing, module, BPF, and related
ambient-capability syscalls. Bubblewrap exposes only the two immutable runtimes,
fixed host loader libraries/cache, read-only CPU/NUMA/kernel-memory topology,
an isolated `/proc`, an isolated `/dev`, and a 32 MiB temporary filesystem.
Landlock independently restricts file reads/execution to the required runtime,
loader, topology, and `/proc` paths; an internal canary confirms other paths are
denied before inference.

`OPENVINO_TELEMETRY_CONSENT=NO` is the only accepted telemetry setting, and the
network namespace remains unshared even if a dependency ignores it. Remote code,
pickle or other executable model serialization, mutable revisions, runtime
discovery, arbitrary worker-supplied paths, ambient shell/network access, and
unsafe fallback are prohibited.

No component in this closure is redistributed by VisualWorld v0.2:

| Surface | Status |
| --- | --- |
| VisualWorld wheel/sdist | Excludes model, interpreter, wheels, media runtime, and native libraries |
| Model and perception runtime | User-provisioned private use only |
| Container, installer, VM image, or hosted mirror | Denied |
| Release SBOM/notices | External closure identified but excluded from the application artifact inventory |

Apache-2.0 availability does not itself grant a shipping decision. The
python-build-standalone composite notice set is incomplete for redistribution;
NumPy and native runtime obligations, corresponding-source/relinking questions,
codec/model patent review, Intel's human-rights use policy, vulnerability state,
and intended jurisdictions still require a separate shipping review. Any future
redistribution must update the ADR, manifest, dependency/model ledgers, notices,
and actual release SBOM together.

## Alternatives

- **Decode and inference in separate workers with pixels relayed by the parent:**
  rejected because it expands the pixel-bearing IPC surface and duplicates
  resource/cancellation coordination.
- **OpenVINO in the application process:** rejected because native parsing and
  untrusted model output would enter the deterministic trust boundary.
- **Provision through uv or pip resolution:** rejected because it introduces
  developer-tool mutation, mutable dependency discovery, and a larger network
  and package-install surface than the six reviewed artifacts.
- **Bundle a wheel, installer, or container:** rejected because redistribution
  obligations and jurisdictional review are incomplete.
- **Support macOS, GPU, or remote media now:** deferred because no equally strong
  reviewed hostile-input boundary exists and the v0.2 CPU-LITE path does not
  require them.

## Consequences

Issue #73 can implement one bounded detector adapter against a precise,
reproducible, offline closure without adding root runtime dependencies or model
artifacts to Git. The application remains portable and dependency-free, while
real perception is explicitly Linux-only and operationally more complex. Users
must provision and retain a private root-owned runtime separately. Security and
license updates require a new manifest/runtime identifier rather than mutating
this closure. Distribution remains denied until a later issue explicitly records
approval and every obligation.
