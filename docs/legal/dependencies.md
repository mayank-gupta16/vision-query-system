# Dependency inventory

No application/runtime dependencies are approved. The repository-policy workflow
currently uses only the pinned official action below; Python is supplied by the
`ubuntu-24.04` hosted runner and the validator uses only its standard library.

| Component | Immutable version | Purpose | License | Obligations/isolation | Status | Verified |
| --- | --- | --- | --- | --- | --- | --- |
| actions/checkout | `de0fac2e4500dabe0009e67214ff5f5447ce83dd` (v6.0.2) | CI checkout | MIT | Pin exact SHA; no persisted credentials | Approved for bootstrap | 2026-09-05 |

For every future Python/native/container dependency record source URL, exact
version/revision and digest/lock, SPDX identifier or `LicenseRef-*`, use,
distribution, transitive/native components, commercial implications, approval,
and verification date. Review the actual FFmpeg build and codecs, not only its
umbrella name.

Core defaults should use reviewed OSI-approved permissive licenses. Weak
copyleft requires compatibility review. Strong/network copyleft,
non-commercial, research-only, no-derivatives, unknown, or custom terms are
deny-by-default for core and require an ADR plus isolation if accepted.
