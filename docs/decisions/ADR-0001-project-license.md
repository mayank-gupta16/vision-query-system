# ADR-0001: Project and inbound contribution license

- Status: Accepted
- Date: 2026-09-06
- Decider: repository maintainer through delegated independent agent review
- Issue: https://github.com/mayank-gupta16/vision-query-system/issues/2

## Context

VisualWorld is intended to be a public, commercially usable, long-lived project
with separately licensed optional model, runtime, codec, dataset, and service
integrations. When this decision began, the repository had no license, so
copyright law reserved reuse and redistribution rights. The project needs a permissive core license,
clear inbound contribution terms, patent protection appropriate for a technical
platform, and low governance overhead for an initial solo maintainer.

This is an engineering governance recommendation, not legal advice. Counsel may
be appropriate before commercialization, accepting material corporate
contributions, or combining restrictive dependencies.

## Decision

1. License original VisualWorld software, tests, configuration, examples, and
   project-authored documentation under Apache License 2.0 (`Apache-2.0`). Add
   the exact unmodified official text as root `LICENSE`.
2. Treat contributions intentionally submitted for inclusion as Apache-2.0 under
   section 5, unless conspicuously designated otherwise before acceptance. State
   this inbound-equals-outbound rule in `CONTRIBUTING.md`.
3. Do not require a Contributor License Agreement initially. Contributors retain
   copyright to their contributions; the project receives the license grants in
   Apache-2.0.
4. When substantive external contribution intake opens, require Developer
   Certificate of Origin 1.1 sign-off for code and documentation commits as a
   lightweight rights/provenance certification. State explicitly that sign-off
   identity is public and retained in Git history. Data, media, model, and weight
   contributions require separate rights and privacy review; DCO sign-off alone
   is insufficient for them.
5. Use `SPDX-License-Identifier: Apache-2.0` in new source files once a source
   tree exists. Do not mechanically add headers to generated or third-party files.
6. Keep model weights/code, datasets/media/annotations, fonts/assets, hosted APIs,
   codecs/native runtimes, and optional adapters in separate verified inventories.
   The project license does not override their terms.
7. Add a `NOTICE` file only when required attribution notices exist; maintain
   `THIRD_PARTY_NOTICES.md` for applicable third-party acknowledgements.
8. Treat the VisualWorld name and branding separately; Apache-2.0 does not grant
   trademark rights except customary description of origin.

## Evidence

- The official Apache-2.0 text is OSI-approved, grants copyright rights in
  section 2, includes an explicit patent grant and patent-litigation termination
  in section 3, defines redistribution/notice duties in section 4, applies the
  same terms to intentionally submitted contributions by default in section 5,
  and excludes trademark permission in section 6:
  https://www.apache.org/licenses/LICENSE-2.0.html
- The official MIT text is OSI-approved and highly permissive, requiring
  preservation of its copyright/permission notice, but contains no comparable
  express patent grant or contribution-submission section:
  https://opensource.org/license/mit
- DCO 1.1 certifies the contributor has rights to submit under the indicated
  open-source license and makes clear the public contribution/sign-off record is
  retained and redistributable:
  https://developercertificate.org/

## Alternatives

### MIT

Benefits: extremely short, familiar, permissive, minimal notice burden.
Rejected in the proposal because VisualWorld may accumulate patent-relevant
tracking, query, storage, and inference work, while MIT does not state an express
patent grant or patent-litigation termination. MIT remains acceptable if the
maintainer prioritizes simplicity over that explicit patent framework.

### BSD-3-Clause

Benefits: permissive and adds a non-endorsement condition. It does not materially
improve the patent/contribution framework for this project compared with MIT.

### MPL-2.0

Benefits: file-level copyleft can preserve modifications to covered files while
allowing combination with proprietary work. It adds compliance complexity and
does not match the stated preference for a permissive core.

### GPL/AGPL family

Benefits: strong reciprocal source-sharing goals. Rejected for the proposed core
because it would materially constrain proprietary/commercial integration and
conflict with the requested permissive modular strategy.

### CLA from the first contribution

Benefits: can centralize broader relicensing or patent assurances. Rejected for
the initial stage because it adds contributor friction and legal/identity
administration before the project has a contributor base or defined relicensing
need. Reconsider for major corporate contributions or a dual-license strategy.
Pause external contribution intake before adopting proprietary dual licensing or
unilateral relicensing: Apache-2.0 inbound terms and DCO sign-off do not grant the
broader relicensing rights that such a change could require.

## Consequences

- Commercial use, modification, distribution, and sublicensing remain permitted
  subject to Apache-2.0 conditions.
- Contributors and users receive an explicit patent license limited to claims
  necessarily infringed by their contributions/work combination; bringing the
  specified patent litigation terminates the granted patent license for that work.
- Distributors must provide the license, mark modified files, preserve relevant
  notices, and carry NOTICE attributions when the project includes a NOTICE file.
- The license does not make incompatible model/data/service terms safe; the legal
  inventories and adapter isolation gates remain mandatory.
- This ADR authorizes the root `LICENSE`, aligned README/CONTRIBUTING and future
  package metadata, removal of the temporary contribution gate, and closure of
  issue #2 after the resulting PR and repository metadata are verified.
- Proprietary dual relicensing is not a current requirement. The public,
  permanent DCO sign-off record is an accepted consequence.

## Approval record

On 2026-09-06, the maintainer directed that PR decisions use independent Codex
subagent review because manual maintainer approval would not add codebase-specific
assurance. The licensing specialist independently recommended Apache-2.0,
inbound-equals-outbound terms, DCO 1.1 for substantive external code and
documentation contributions, no initial CLA, and separate rights inventories.
The complete acceptance diff remains subject to two independent final-head
reviews, repository validation, CI, and remote license-detection verification
before merge.
