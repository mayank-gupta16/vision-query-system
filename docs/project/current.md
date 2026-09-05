# Current project state

- Stable version: none; unreleased repository bootstrap
- Active GitHub milestone: v0.1 — Evidence-safe ingestion foundation
- Active issue: #1 — Bootstrap product, architecture, and repository governance
- Implementation: none
- Benchmark baseline: none

## Established facts

- The product name is VisualWorld; the repository remains
  `mayank-gupta16/vision-query-system` pending a naming decision.
- The repository is documentation/governance only. Python is not yet an approved
  implementation decision merely because the initial `.gitignore` is Python-oriented.
- The architecture separates observations, tracklets, persistent entities,
  temporal claims, evidence, uncertainty, and queries behind stable ports.
- CPU-LITE (4 vCPU, 16 GB RAM, no required GPU) is a mandatory benchmark profile.
- Project license, implementation toolchain, storage, model, and dataset choices
  remain unresolved and require review/ADRs.

## Known blockers and limitations

- GitHub authentication/admin access and public visibility are verified. The
  controlled labels, ten milestone shells, v0.1 issues #1–#20, research issues
  #21–#29, and later roadmap epics #30–#38 exist. Secret scanning, push
  protection, Dependabot alerts/security updates, and private vulnerability
  reporting are enabled.
- Non-provider secret patterns and secret validity checks remain disabled after
  an enable attempt; availability/requirements need follow-up.
- The `VisualWorld Roadmap` Project is blocked because the current token lacks
  `read:project`/`project` scopes. No branch ruleset exists yet, and GitHub-hosted
  CI must first run on the bootstrap PR.
- No video ingestion, database, query engine, model adapter, test fixture, or
  benchmark implementation exists.
- No license grants reuse rights yet.

## Next priorities

1. Commit/push the bootstrap, open its PR, and verify policy CI.
2. Obtain GitHub Project scopes, create `VisualWorld Roadmap`, and add views/fields.
3. Configure a practical `main` ruleset after its required CI check exists.
4. Merge/close #1, then resolve project license issue #2.
5. Proceed through toolchain/media/schema/storage decisions #3–#6; do not mark
   implementation issues ready while the contribution-license gate is active.
