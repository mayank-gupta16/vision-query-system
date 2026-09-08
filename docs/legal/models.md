# Model inventory

No model code, weights, hosted model API, or model runtime is approved for
redistribution or inclusion in a VisualWorld release. ADR-0007 approves only the
selected `vehicle-detection-0201` artifacts for separately user-provisioned,
private use inside the supported offline Linux worker. The other candidates
remain issue #21 evaluation inputs. No artifact is committed or packaged.

Each candidate entry must record:

| Model/artifact | Code/weights/service source | Immutable revision + digest | Use | Code license | Weight/data/service terms | Distribution/commercial limits | Runtime/format | Isolation | Status/review date |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Open Model Zoo `vehicle-detection-0200` FP32 XML/BIN | Open Model Zoo metadata at commit `86ba23e80b27eb9149da911e5c023b108cb06e80`; Intel storage `2023.0/models_bin/1` | XML SHA-256 `6a238f0d781922b66ebfb0556a8386762f7a151635897cbc07abb57692d565cc`; BIN `5955a5b4769d335d779b9a5052417a4a12b6e08c0146b1dba45a1df039147cc8` | Vehicle-only detector candidate, 256 x 256 | Apache-2.0 OMZ metadata | Model YAML links Apache-2.0 repository license; storage SHA-384 and byte size also locked | Commercial use and redistribution permitted with Apache notice obligations; Intel human-rights use policy remains applicable | OpenVINO FP32 IR; OpenVINO 2026.3.1 CPU | Network-unshared read-only worker; no remote code or executable serialization | Evaluated, not selected: failed easy precision; evaluation approval only, 2026-09-07 |
| Open Model Zoo `vehicle-detection-0201` FP32 XML/BIN | Same pinned OMZ revision and Intel storage series | XML SHA-256 `ae39ec7c4cc5c1ab5ef3db71c8fa307500f07a87f17d95bdb0d1c84762751d1a`; BIN `612df843314c179460e754316d67e6eedd0e778f96ad1129fc0c34c8e935cb0b` | Same detector family, 384 x 384 | Apache-2.0 | Same | Same | Same | Same | Selected at confidence 0.95; approved only for user-provisioned private use through ADR-0007; redistribution denied, 2026-09-08 |
| Open Model Zoo `vehicle-detection-0202` FP32 XML/BIN | Same pinned OMZ revision and Intel storage series | XML SHA-256 `099b9bdbd7033221c058f997194e3e8eb178c089a1eb0928261533bcbc9d70b2`; BIN `6ed234fa498f8aea32c179198ce70975d3d9fedf8a1227e682fd8ad892fe4b17` | Same detector family, 512 x 512 | Apache-2.0 | Same | Same | Same | Same | Evaluated, passed but not selected due CPU cost; evaluation approval only, 2026-09-07 |

Do not label non-commercial, research-only, or custom-restricted artifacts as
open source. Pin hub revisions immutably and verify digests. Deny
`trust_remote_code` and pickle/executable weights by default; exceptions require
an ADR, source review, isolated worker, and explicit risk owner.

The exact artifact URLs, upstream SHA-384 values, input/output contract, and
runtime closure are in the
[`candidates.json`](../../fixtures/v02-detection-research/candidates.json)
research lock. The product lock and provisioning/distribution controls are in
[`perception-runtime-v1.json`](../../workers/perception-runtime-v1.json). The
provisioner downloads from the original upstream; VisualWorld does not mirror or
bundle the artifacts. Adding weights or a runtime to a wheel, installer,
container, service, or release still requires a separate shipping review and
explicitly updated notices/SBOM.
