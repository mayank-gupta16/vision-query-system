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
and per-frame RGB hashes are locked in the manifest. The digest-linked
[crop-golden companion](../../fixtures/synthetic-v1/crops.json) locks exact
moving-region source boxes and packed-RGB24 crop hashes without changing the
fixture manifest used by earlier validation receipts.

Generate and checksum-scan the ephemeral media in a fresh ignored directory:

```sh
python3 scripts/generate_synthetic_fixtures.py \
  --output artifacts/fixtures/synthetic-v1
```

The command reports wall time, process peak RSS, and total bytes for the PR-CI
boundedness check. Those machine-dependent observations are not part of the
canonical fixture manifest. Generated `.mov` files remain untracked.

## Approved v0.2 evaluation sets

The detector, sampling, and original-resolution crop research sets are approved
for evaluation only. The
[detector methodology](v0.2-detection-methodology.md) records ten CC0 vehicle
sources and their 20 deterministic, plate-sanitized still derivatives. The
[sampling methodology](v0.2-sampling-methodology.md) records ten source-separated
six-second clips generated from those sanitized derivatives, with 40 locked
moving-object events covering small/fast motion, camera motion plus occlusion,
hard cuts, and a VFR gap. The
[crop methodology](v0.2-crop-methodology.md) records ten source-separated
three-frame 4K clips with paired generated source-resolvable and source-absent
detail charts at tiny, small, and medium scales. The charts contain no real
face, plate, identifier, biometric, or personal data. All binary sources and
derivatives remain ignored; none of these datasets is a product asset or release
payload.
