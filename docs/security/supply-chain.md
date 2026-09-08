# Dependency, model, CI, and release supply chain

- Install releases only from exact transitive lockfiles.
- Record source, immutable version/revision, SHA-256 digest, license, approval,
  runtime, and obligations for dependencies, models, datasets, native codecs,
  containers, and GitHub Actions.
- Pin model-hub artifacts to immutable revisions, never mutable tags.
- Deny `trust_remote_code` and pickle/executable model formats by default. Prefer
  non-executable formats such as safetensors or ONNX when technically suitable.
- Exceptions require an ADR, reviewed source, digest verification, isolation,
  and explicit risk owner.
- Produce an SBOM from the actual release environment and reject unwaived high or
  critical vulnerabilities at release.

CI uses least privilege, hosted runners, timeouts, immutable action pins, and no
secrets for fork PRs. Do not use `pull_request_target` to execute or check out PR
code. Publishing runs only from protected trusted refs/environments and should
use OIDC/trusted publishing rather than long-lived credentials.

Protected configuration includes workflows, AGENTS files, dependency manifests
and locks, SECURITY, and security/legal policy. These require explicit review.

## Scaffold controls

[Developer commands](../testing/development.md) acquire SHA-256-verified uv/Python,
clear resolver/Python environment overrides, and use an exact universal PyPI
lock. PEP 517 packaging separately verifies the build backend's approved wheel
hashes, including a mandatory wrong-hash rejection test. These protect acquisition
integrity; they do not make third-party code harmless or replace host isolation.

CI uses fresh tool directories without restored shared caches, no fork secrets,
and a four-lane aggregate application gate. The offline check command does not
install dependencies. Vulnerability auditing is separately network-enabled and
submits only public tool package names/versions. No media or model data is sent.
Runtime package inventory excludes unshipped developer tools and interpreters.

The v0.2 perception closure is governed by
[ADR-0007](../decisions/ADR-0007-isolated-perception-runtime.md) and the frozen
[perception manifest](../../workers/perception-runtime-v1.json). It is not an
application dependency or release payload. Acquisition occurs only through the
separately invoked standard-library provisioner, from exact allowlisted HTTPS
URLs, into a private cache with byte-length and SHA-256 verification before any
archive parsing. Installation re-verifies wheel RECORDs and declared notices,
rejects paths/links/special files and partial roots, and atomically publishes a
root-owned read-only closure after file and directory fsync, then fsyncs the
destination parent. Full-tree verification includes shipped Python bytecode and
rejects external or dangling symlinks and external hardlinks in both composed
runtimes. Ordinary application execution stays offline and never resolves or
downloads dependencies. Only Linux x86_64 with GNU libc 2.28 or newer is
accepted. Model/runtime redistribution remains denied pending a separate
complete shipping review.
