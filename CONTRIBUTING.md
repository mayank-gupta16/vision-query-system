# Contributing to VisualWorld

VisualWorld is being developed issue by issue. Before implementation, find or
request an approved issue in the active milestone. Issues marked `agent:ready`
have enough scope and acceptance criteria for autonomous work.

**Temporary license gate:** no project license or inbound contribution terms
have been approved. Do not submit substantive external code or data contributions
until the license decision is accepted and this notice is replaced. Documentation
feedback may be proposed in an issue. No implementation issue may receive
`agent:ready` while this gate is active.

## Workflow

1. Read `AGENTS.md`, `docs/project/current.md`, the issue, and only the relevant
   indexed documentation.
2. Create a focused branch such as `feat/123-description`,
   `fix/456-description`, or `research/789-description`.
3. Keep the change within the issue scope and add tests appropriate to the risk.
4. Run `python3 scripts/validate_repository.py` plus the checks named by the
   relevant module and issue.
5. Open a PR that links the issue and includes actual test/benchmark evidence.

Do not commit secrets, private or copyrighted media, large model weights, or
private datasets. Use tiny redistributable fixtures and dataset manifests.

Open-ended ideas belong in Discussions when enabled. Security vulnerabilities
must follow [SECURITY.md](SECURITY.md), not public issues.

## Design changes

Use an ADR for long-lived decisions such as world schema, identity strategy,
storage, query language, plugin contracts, restrictive model licenses, or
breaking interfaces. Do not use ADRs for routine implementation details.

## Conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
