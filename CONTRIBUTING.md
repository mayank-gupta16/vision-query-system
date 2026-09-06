# Contributing to VisualWorld

VisualWorld is being developed issue by issue. Before implementation, find or
request an approved issue in the active milestone. Issues marked `agent:ready`
have enough scope and acceptance criteria for autonomous work.

Only issues whose dependencies and durable decisions are resolved may receive
`agent:ready`. The accepted project license does not bypass an issue's technical,
security, privacy, provenance, or third-party-rights gates.

## Contribution license and sign-off

Unless conspicuously designated otherwise before submission and explicitly
accepted by maintainers, an intentional contribution submitted for inclusion is
provided under Apache License 2.0, consistent with section 5 of
[the project license](LICENSE). Contributors retain copyright; no copyright
assignment or Contributor License Agreement is required. Nonstandard terms must
be agreed in writing before submission or the contribution will not be merged.

Substantive external code and documentation commits must certify
[Developer Certificate of Origin 1.1](https://developercertificate.org/) terms
with a real-name sign-off:

```text
Signed-off-by: Full Name <email@example.com>
```

Create it with `git commit --signoff`. The name, email, commit, and sign-off are
public and retained in Git history. Maintainers verify the sign-off before merge.
A DCO sign-off does not establish dataset consent, media privacy rights, model or
weight redistribution rights, or clean generated-code provenance.

Do not submit data, video, annotations, models, weights, fonts/assets, or other
third-party material through the ordinary code path. Start with an issue and a
rights/privacy manifest; explicit review is required before inclusion.

## Workflow

1. Read `AGENTS.md`, `docs/project/current.md`, the issue, and only the relevant
   indexed documentation.
2. Create a focused branch such as `feat/123-description`,
   `fix/456-description`, or `research/789-description`.
3. Keep the change within the issue scope and add tests appropriate to the risk.
4. Run `python3 scripts/validate_repository.py` plus the checks named by the
   relevant module and issue.
5. Open a PR that links the issue and includes actual test/benchmark evidence.
6. If you are an external contributor, sign off each substantive code or
   documentation commit as described above.

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
