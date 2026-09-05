# Model inventory

No model code, weights, hosted model API, or model runtime is approved.

Each candidate entry must record:

| Model/artifact | Code/weights/service source | Immutable revision + digest | Use | Code license | Weight/data/service terms | Distribution/commercial limits | Runtime/format | Isolation | Status/review date |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

Do not label non-commercial, research-only, or custom-restricted artifacts as
open source. Pin hub revisions immutably and verify digests. Deny
`trust_remote_code` and pickle/executable weights by default; exceptions require
an ADR, source review, isolated worker, and explicit risk owner.
