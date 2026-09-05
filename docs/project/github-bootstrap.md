# GitHub bootstrap status

## Prepared in the repository

- controlled issue forms for bugs, features, performance, and research;
- a PR template requiring issue, test, benchmark, risk, and license evidence;
- CODEOWNERS based on the remote owner `mayank-gupta16`;
- an immutable pin for the only GitHub Actions dependency;
- read-only, lightweight repository-policy CI;
- GitHub Actions Dependabot configuration;
- the controlled label taxonomy below.

## Verified live state

On 2026-09-06, `gh auth status`, `gh repo view`, and repository API checks verified
`mayank-gupta16` has admin access, the repository is public, Issues and Projects
are enabled, and Discussions are disabled. Secret scanning, push protection,
Dependabot alerts/security updates, and private vulnerability reporting are enabled.

No milestones, issues, rulesets, or repository-specific Project existed at the
start of bootstrap. The controlled labels, ten milestone shells, v0.1 issues
#1–#20, research issues #21–#29, and roadmap epics #30–#38 were created on
2026-09-06. Issue #1 is the assigned `agent:ready` bootstrap work; no
implementation issue is ready while licensing is unresolved.

Project creation is blocked because the token lacks `read:project`/`project`
scopes. GitHub-hosted CI has not run because the workflow is not yet on a pushed
branch. No ruleset exists; configure it only after the CI check name is verified.
Non-provider secret scanning patterns and validity checks remain disabled after
an enable attempt; standard secret scanning and push protection are enabled.

## Controlled labels

- Type: `type:feature`, `type:bug`, `type:research`, `type:refactor`,
  `type:performance`, `type:test`, `type:docs`, `type:security`, `type:chore`
- Priority: `priority:P0`, `priority:P1`, `priority:P2`, `priority:P3`
- Area: `area:video`, `area:detection`, `area:tracking`, `area:reid`,
  `area:world-model`, `area:query`, `area:vlm`, `area:ocr`, `area:evidence`,
  `area:geometry`, `area:benchmark`, `area:storage`, `area:cli`, `area:infra`,
  `area:docs`
- Workflow: `agent:ready`, `agent:needs-human`, `blocked`, `needs-decision`

## Proposed Project and rules

Create `VisualWorld Roadmap` only after milestones/issues exist. Fields: Status,
Priority, Milestone/Version, Area, Effort, Risk. Views: Current Milestone,
Roadmap, Bugs, Research, Blocked, Performance, Security.

After CI is proven on a PR, protect `main`: require PRs and the lightweight CI
check, resolved conversations, and block force pushes/deletion. Keep a
maintainer recovery path; do not require a code-owner approval that deadlocks a
solo maintainer.
