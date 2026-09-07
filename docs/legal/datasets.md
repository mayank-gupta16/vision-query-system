# Dataset and media rights inventory

No external dataset, recorded video, annotation set, or crop collection is
approved for the product or release. The only approved bootstrap media is the
project-authored
[synthetic-v1 set](../../fixtures/synthetic-v1/manifest.json): algorithmic RGB
patterns generated from reviewed source with no external assets or personal
data. Its Apache-2.0 permission, redistribution/derivative rights, privacy
classification, allowed uses, retention, provenance, and review date are locked
in the machine-readable manifest; generated binary media remains untracked.

Issue #21 additionally approves the
[`v02-detection-cc0-derived-1`](../../fixtures/v02-detection-research/dataset-manifest.json)
set for detector evaluation only. It derives 20 locked frames from ten
source-separated Wikimedia Commons CC0 photographs. The detailed source ledger
pins page/file revisions, artists, exact 1280-pixel thumbnail URLs and hashes,
object crops, plate redactions, privacy review, and deterministic transform.
The output contains one vehicle per frame and no person, face, plate, or retained
personal data. Source JPEGs and derived PPMs remain ignored; neither set is a
product asset or release payload.

For every artifact record source/owner, acquisition method, exact revision/hash,
license or permission, redistribution and derivative rights, consent/privacy
status, allowed uses, annotation provenance, retention requirements, and review
date. A public URL does not grant redistribution rights.

Bootstrap CI fixtures are generated at test time from reviewed source and their
machine-readable rights/privacy/checksum manifest; binary media is not committed.
Any future committed fixture requires an enforced allowlist and affirmative
redistribution permission. Public YouTube/video material, private recordings,
faces, plates, and derived crops stay outside the repository unless the recorded
rights and privacy basis explicitly permit their use and redistribution.
