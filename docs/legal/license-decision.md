# Project license decision

- Status: needs maintainer decision before substantive contributions or release
- Current effect: no LICENSE file exists, so no reuse/redistribution grant is implied

The intended project is public, commercially usable, and modular around separately
licensed models/datasets. The decision issue must compare at least:

- Apache-2.0: permissive, explicit patent grant and notice obligations;
- MIT: simpler permissive text, without Apache's explicit patent terms;
- any credible alternative required by selected core dependencies or governance.

Acceptance requires checking the actual core dependency/runtime/codec licenses,
commercialization and patent goals, contribution policy, copyright holder, and
compatibility with distributed artifacts. An accepted ADR, root LICENSE, README,
package metadata, and third-party notices must agree.

Model weights, model code, datasets, media, fonts/assets, hosted APIs, and optional
backends remain separately licensed even after a project license is chosen.
