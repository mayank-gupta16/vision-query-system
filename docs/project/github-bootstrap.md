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
scopes. Bootstrap PR #39 ran the `repository-policy` GitHub-hosted check
successfully. Classic `main` branch protection now requires PRs, that check, and
resolved conversations; blocks force pushes/deletion; requires zero approvals;
and excludes admins from enforcement as a solo-maintainer recovery path.
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

## Proposed Project

Create `VisualWorld Roadmap` only after milestones/issues exist. Fields: Status,
Priority, Milestone/Version, Area, Effort, Risk. Views: Current Milestone,
Roadmap, Bugs, Research, Blocked, Performance, Security.

The Project remains the only listed bootstrap item blocked on additional OAuth
scope. Do not substitute repository labels for Project status fields.
