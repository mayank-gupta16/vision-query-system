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
