# Dataset and fixture policy

Ordinary CI initially generates tiny deterministic fixtures at test time from
reviewed source code and records their expected facts/checksums in a manifest.
Binary media is not committed during bootstrap. Prefer synthetic media with no
real plates/faces. Each generator/manifest records:

- source/owner and acquisition method;
- license or explicit permission and redistribution/derivative rights;
- consent/privacy classification and allowed uses;
- checksum, size, duration, dimensions, frame/time-base facts;
- annotation provenance and review date.

Do not commit video/audio/image/archive/database binaries, full YouTube/public
videos, private recordings, model weights, or large datasets during bootstrap.
An allowlisted committed-fixture policy may be introduced later only with
schema-validated rights/privacy/checksum enforcement. External datasets are
referenced by reproducible manifests with
source URL, exact revision, expected timestamps/annotations, checksum where
permitted, license/terms, and acquisition instructions. Public accessibility is
not permission to redistribute media or derived face/plate crops.

Benchmark splits and goldens must be locked before measuring a release. Changes
to annotations or metric code invalidate direct comparisons unless reported.

## Approved synthetic-v1 set

The [synthetic-v1 manifest](../../fixtures/synthetic-v1/manifest.json) approves
three project-authored 16x12 RGB fixtures: constant-rate, varying-rate, and a
90-degree display-rotation case. The standard-library generator uses no input
media, codec library, network, person, face, plate, text, font, or imported
asset. Exact container bytes, rational timestamps, moving source-pixel regions,
and per-frame RGB hashes are locked in the manifest.

Generate and checksum-scan the ephemeral media in a fresh ignored directory:

```sh
python3 scripts/generate_synthetic_fixtures.py \
  --output artifacts/fixtures/synthetic-v1
```

The command reports wall time, process peak RSS, and total bytes for the PR-CI
boundedness check. Those machine-dependent observations are not part of the
canonical fixture manifest. Generated `.mov` files remain untracked.
