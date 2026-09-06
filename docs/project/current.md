# Current project state

- Stable version: none; experimental package version 0.1.0a0 is unreleased
- Active GitHub milestone: v0.1 — Evidence-safe ingestion foundation
- Implementation: help/version-only package and deterministic developer commands
- Benchmark baseline: none

## Established facts

- The product name is VisualWorld; the repository remains
  `mayank-gupta16/vision-query-system` pending a naming decision.
- The first installable package is `visualworld-engine`, import/CLI `visualworld`.
  Only help/version behavior exists; no video or query functionality is claimed.
  ADR-0002 governs its toolchain, exact lock, and four Linux/macOS Python lanes.
- The architecture separates observations, tracklets, persistent entities,
  temporal claims, evidence, uncertainty, and queries behind stable ports.
- CPU-LITE (4 vCPU, 16 GB RAM, no required GPU) is a mandatory benchmark profile.
- Apache-2.0 is the accepted project license with inbound-equals-outbound terms,
  DCO 1.1 for substantive external code/documentation contributions, and no
  initial CLA.
- ADR-0002 accepts a Python-first application: CPython 3.13.15 reference,
  CPython 3.14.7 compatibility, exact uv/uv_build 0.12.6, PEP 621 `src` layout,
  a universal lock with wheel-only installs for Linux x86_64 and macOS arm64,
  zero initial runtime dependencies, and a forced hash-verified PEP 517
  packaging lane.
  Model and dataset choices remain unresolved.
- ADR-0003 accepts a source-built PyAV 18.1.0 worker linked to a minimal
  signature-verified FFmpeg 9.0.1 build for the first Linux ingestion slice.
  Hostile decode is isolated and fail-closed; native hostile decode on macOS is
  intentionally unsupported until a validated boundary exists. No media runtime
  has yet been added to the root package.
- ADR-0004 accepts strict versioned JSON ingestion records, typed SHA-256-based
  identifiers, exact rational PTS with measured/estimated provenance,
  original-pixel geometry, and explicit migration and untrusted-field limits.
- ADR-0005 accepts local SQLite metadata plus a SHA-256 content-addressed
  filesystem artifact store with staged commits, recovery/audit, migrations,
  and reference-aware cascade deletion. Video blobs remain outside SQLite.

## Known blockers and limitations

- GitHub authentication/admin access and public visibility are verified. The
  controlled labels, ten milestone shells, v0.1 issues #1–#20, research issues
  #21–#29, and later roadmap epics #30–#38 exist. Secret scanning, push
  protection, Dependabot alerts/security updates, and private vulnerability
  reporting are enabled.
- Non-provider secret patterns and secret validity checks remain disabled after
  an enable attempt; availability/requirements need follow-up.
- The `VisualWorld Roadmap` Project is blocked because the current token lacks
  `read:project`/`project` scopes.
- Bootstrap PR #39 merged into `main` and closed issue #1 after its
  `repository-policy` check passed. Classic branch protection on `main` requires
  PRs, that status check, and resolved conversations; force pushes and deletion
  are blocked. Required approvals are zero and admins are not enforced so a solo
  maintainer retains recovery access.
- No video ingestion, database, query engine, model adapter, media fixture, or
  application benchmark implementation exists. Package/tooling tests use only
  generated temporary metadata/archive fixtures.
- The project license does not relicense models, weights, datasets, media,
  runtimes, codecs, services, or other third-party material.

## Next priorities

1. Resolve OAuth scope blocker #41, create `VisualWorld Roadmap`, and add views/fields.
2. Implement the smallest v0.1 end-to-end ingestion slice in issue dependency
   order; preserve the root package's zero runtime dependencies by keeping media
   dependencies adapter-local.
3. Keep model, dataset, media, runtime, codec, service, and third-party licenses
   in their separate release-gate inventories.
