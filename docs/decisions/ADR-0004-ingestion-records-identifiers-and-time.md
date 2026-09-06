# ADR-0004: Ingestion records, identifiers, and rational time

- Status: Accepted
- Date: 2026-09-06
- Deciders: repository maintainers
- Issue: [#5](https://github.com/mayank-gupta16/vision-query-system/issues/5)

## Context

The first ingestion slice needs durable identities and exact source time before
storage or domain code is implemented. Repeated ingestion must converge on the
same records, timestamps must not pass through floating point, and evidence must
remain traceable to original-source pixels. These contracts must stay independent
of a database, media adapter, or model SDK.

This decision covers only `Source`, `FrameRef`, `Geometry`, `EvidenceRef`, and
`RunManifest`. Detection, tracklet, entity, claim, relationship, and event
records remain deferred until their milestone exercises them.

## Decision

### Common envelope and serialization

Every record is a strict UTF-8 JSON object with:

- `schema`, an ASCII name such as `visualworld.frame_ref`;
- `schema_version`, a positive integer beginning at `1`; and
- the record-specific identifier and fields below.

Writers emit JSON Canonicalization Scheme (RFC 8785) bytes: sorted object keys,
no insignificant whitespace, no duplicate keys, no byte-order mark, and no
floating-point values. Identity inputs use only normalized NFC strings, exact
decimal strings, bounded JSON integers, booleans, arrays, and objects. Hashes
are lowercase SHA-256 hexadecimal.

Identifiers are typed strings:

- `src_<sha256>` hashes the canonical source identity projection;
- `frm_<sha256>` hashes the canonical frame identity projection;
- `evi_<sha256>` hashes the canonical evidence identity projection; and
- `run_<sha256>` hashes the canonical run identity projection.

Every record persists `identity_version: 1`; version-1 schemas require that
value, and it is part of every identity projection. The four normative identity
projections are the following exact object shapes (shown as canonical bytes):

```json
{"fingerprint":{"algorithm":"sha256","bytes":"145031","digest":"af9eee534a9f18e8b2ac2e2c5c87dcc010fdeabf13b6335957749bc579426b9e"},"identity_version":1}
```

```json
{"decode_index":"0","identity_version":1,"pts":{"basis":"measured","time_base":{"denominator":"90000","numerator":"1"},"value":"90000"},"source_id":"src_8557c30d43a6c7e7a6710008e0e14f0afa86ae409fc4b8ad44bb4b97509486c9","stream_index":0}
```

```json
{"artifact_sha256":"1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f","frame_id":"frm_53b715d4410841bda9c052b9583d11e9a8da41903af5643beaff376031f02efc","geometry":{"box_xyxy":[0,0,320,240],"measurement":"measured","source_height":240,"source_width":320,"space":"source_pixels","transform_to_source":{"kind":"identity"}},"identity_version":1,"kind":"original_frame"}
```

```json
{"contracts":{"evidence_ref":1,"frame_ref":1,"run_manifest":1,"source":1},"identity_version":1,"producers":[{"configuration_sha256":"2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a","name":"visualworld.sampler","version":"0.1.0a0"}],"sampling":{"policy":"nearest_eligible_pts","target_fps":{"denominator":"1","numerator":"5"}},"source_id":"src_8557c30d43a6c7e7a6710008e0e14f0afa86ae409fc4b8ad44bb4b97509486c9"}
```

The source projection is `{identity_version, fingerprint}`; the frame projection
is `{identity_version, source_id, stream_index, decode_index, pts}`; the evidence
projection is `{identity_version, frame_id, kind, artifact_sha256, geometry}`
(with the digest deliberately flattened from `artifact.sha256`); and the run
projection is `{identity_version, source_id, contracts, producers, sampling}`.
No other field enters a version-1 hash.

Mutable state, wall-clock time, display labels, storage locations, and optional
descriptive metadata never enter an identity projection. When an existing
identifier is encountered, its stored identity projection must match
byte-for-byte; otherwise ingestion stops with an integrity error. Database
sequence numbers may be private indexes but never domain identifiers.

Signed decimal strings match `0|-?[1-9][0-9]*`; unsigned decimal strings match
`0|[1-9][0-9]*`. Leading zeroes, a plus sign, and negative zero are invalid.
`schema_version`, `identity_version`, `stream_index`, dimensions, coordinates,
and rotation degrees are bounded JSON integers. PTS, duration, rational
components, byte counts, decode indexes, and sample counts use decimal strings.

### Exact media time

A media timestamp is:

```json
{"basis":"measured","time_base":{"denominator":"90000","numerator":"1"},"value":"90000"}
```

`value` is a signed 64-bit decimal string. Time-base numerator and denominator
are positive unsigned 32-bit decimal strings, copied from the selected stream;
the exact time in seconds is `value * numerator / denominator`. Negative PTS is
valid. Code compares times by checked cross multiplication, never by float or
rounded milliseconds.

`basis` is `measured` when the value came from decoded source PTS. Its object has
exactly `basis`, `time_base`, and `value`. Estimated time has this exact shape:

```json
{"basis":"estimated","estimate":{"method":"previous_pts_plus_duration","producer":{"configuration_sha256":"2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a","name":"visualworld.sampler","version":"0.1.0a0"}},"time_base":{"denominator":"90000","numerator":"1"},"value":"99000"}
```

`estimate` is required only for `estimated`; `method` and all producer fields
are ASCII and enter the frame identity through `pts`. Missing source PTS remains
missing at the decoder boundary; it must not silently become zero. A sample
becomes a durable `FrameRef` only after it has measured or explicitly estimated
rational time. Ties are ordered by `decode_index`.

### Record contracts

`Source` records immutable content identity, source byte count, privacy/access
classification, and bounded stream facts. A raw local path or URL is not stored
in the durable record; the caller supplies an authorized locator when opening
the source.

`FrameRef` identifies one decoded frame by source, stream, non-negative decimal
`decode_index`, and exact PTS. Its identity projection is exactly those fields
plus `identity_version`. Optional duration and key-frame facts do not change the
identifier. The optional JSON members are named `duration` and `key_frame` and
are omitted, rather than written as `null`, when unknown.

`Geometry` is embedded by value. It always includes original-source dimensions
and a half-open integer `box_xyxy = [x_min, y_min, x_max, y_max]` in source
pixels. Bounds satisfy `0 <= min < max <= dimension`. `measurement` is one of
`measured`, `calibrated`, `estimated`, `inferred`, or `unknown`. Source-space
geometry uses `{"kind":"identity"}`.

Geometry originating in producer pixels also requires `producer_space` with
positive integer `width`/`height`, its half-open `box_xyxy`, and
`transform_to_source` with `kind: "affine_rational"` plus coefficients `a`
through `f`. Each coefficient is
`{"numerator": <signed-decimal>, "denominator": <positive-unsigned-decimal>}`.
The exact mapping is `source_x = a*x + b*y + c` and
`source_y = d*x + e*y + f`. The stored source box must equal the source-bounded
axis-aligned box obtained by transforming all four producer-box corners and
rounding minima down and maxima up. This consistency check uses rational
arithmetic. Identity geometry forbids `producer_space`; affine geometry requires
it.

This is the exact affine variant shape; coefficients are named, not positional:

```json
{"box_xyxy":[20,40,100,120],"measurement":"calibrated","producer_space":{"box_xyxy":[10,20,50,60],"height":120,"width":160},"source_height":240,"source_width":320,"space":"source_pixels","transform_to_source":{"coefficients":{"a":{"denominator":"1","numerator":"2"},"b":{"denominator":"1","numerator":"0"},"c":{"denominator":"1","numerator":"0"},"d":{"denominator":"1","numerator":"0"},"e":{"denominator":"1","numerator":"2"},"f":{"denominator":"1","numerator":"0"}},"kind":"affine_rational"}}
```

`EvidenceRef` binds an immutable artifact by SHA-256 and byte count, and binds it
to a frame, evidence kind, nullable geometry, media type, and retention class.
The `geometry` field is always present: it contains a valid Geometry object or
JSON `null`. The evidence identity projection includes that exact value, so an
absent key is invalid and `null` is unambiguous. Artifact bytes are never
embedded in metadata. Storage paths and retention changes are excluded from
identity.

`RunManifest` binds one source to exact contract versions, ordered producer
names/versions/configuration digests, and sampling configuration. These fields
form its identity projection, so an equivalent retry receives the same `run_id`.
Operational state is `preparing`, `committed`, `failed`, or `cancelled` and is
excluded from identity. Only `committed` exposes outputs as complete. The
manifest stores sample count plus a digest of the separately stored ordered
sample index rather than embedding every sample.

The initial version-1 vocabulary is deliberately narrow: local sources use
`local_file`, `private`, `source_controlled`, and `video`; original RGB evidence
uses `original_frame`, `application/vnd.visualworld.rgb24`, and
`derived_private`; sampling uses `nearest_eligible_pts`; and estimated time uses
`previous_pts_plus_duration`. Unknown values fail closed until a later contract
version accepts them. Empty source-stream and run-producer lists are valid, and
source stream indexes must be unique. A committed run requires `outputs`; every
other state forbids it.

Representative version-1 records follow. The source digest is the generated
issue #4 fixture; the other example artifact/configuration digests are synthetic.
The identifiers match the canonical identity rules above.

```json
{
  "schema": "visualworld.source",
  "schema_version": 1,
  "identity_version": 1,
  "source_id": "src_8557c30d43a6c7e7a6710008e0e14f0afa86ae409fc4b8ad44bb4b97509486c9",
  "fingerprint": {"algorithm": "sha256", "digest": "af9eee534a9f18e8b2ac2e2c5c87dcc010fdeabf13b6335957749bc579426b9e", "bytes": "145031"},
  "origin": {"kind": "local_file", "locator_stored": false},
  "access": {"classification": "private", "retention": "source_controlled"},
  "streams": [{"stream_index": 0, "media_type": "video", "width": 320, "height": 240, "rotation_degrees": 0, "time_base": {"numerator": "1", "denominator": "90000"}}]
}
```

```json
{
  "schema": "visualworld.frame_ref",
  "schema_version": 1,
  "identity_version": 1,
  "frame_id": "frm_53b715d4410841bda9c052b9583d11e9a8da41903af5643beaff376031f02efc",
  "source_id": "src_8557c30d43a6c7e7a6710008e0e14f0afa86ae409fc4b8ad44bb4b97509486c9",
  "stream_index": 0,
  "decode_index": "0",
  "pts": {"value": "90000", "time_base": {"numerator": "1", "denominator": "90000"}, "basis": "measured"},
  "duration": {"value": "9000", "time_base": {"numerator": "1", "denominator": "90000"}, "basis": "measured"}
}
```

```json
{
  "schema": "visualworld.evidence_ref",
  "schema_version": 1,
  "identity_version": 1,
  "evidence_id": "evi_aef14ee06f30df79edc781c85b74fc9689199c621058752f907c7e7aeb129c8a",
  "frame_id": "frm_53b715d4410841bda9c052b9583d11e9a8da41903af5643beaff376031f02efc",
  "kind": "original_frame",
  "artifact": {"sha256": "1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f", "bytes": "691200", "media_type": "application/vnd.visualworld.rgb24"},
  "geometry": {"space": "source_pixels", "source_width": 320, "source_height": 240, "box_xyxy": [0, 0, 320, 240], "measurement": "measured", "transform_to_source": {"kind": "identity"}},
  "retention": "derived_private"
}
```

```json
{
  "schema": "visualworld.run_manifest",
  "schema_version": 1,
  "identity_version": 1,
  "run_id": "run_158f21ca6b67a074f5ec83175fc68f0e7eee6440483a883df288d5fbfddaab38",
  "source_id": "src_8557c30d43a6c7e7a6710008e0e14f0afa86ae409fc4b8ad44bb4b97509486c9",
  "contracts": {"source": 1, "frame_ref": 1, "evidence_ref": 1, "run_manifest": 1},
  "producers": [{"name": "visualworld.sampler", "version": "0.1.0a0", "configuration_sha256": "2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a"}],
  "sampling": {"policy": "nearest_eligible_pts", "target_fps": {"numerator": "5", "denominator": "1"}},
  "state": "committed",
  "outputs": {"sample_count": "1", "sample_index_sha256": "3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b"}
}
```

### Validation, limits, and migrations

Validation happens before hashing and again before persistence:

- an ingress reader accepts at most 256 KiB of encoded bytes before JSON parsing;
  it reads at most 256 KiB plus one byte and rejects overflow, including input
  inflated by whitespace, before decoding or canonicalization;
- canonical metadata is also at most 256 KiB, with nesting at most 16 levels, at
  most 128 object members, and at most 64 elements in an embedded array; sample
  indexes are separate bounded streams; the root object is depth level 1 and
  each object or array value advances one level;
- general strings are at most 4,096 UTF-8 bytes; schema names, enums, producer
  names, versions, and estimate methods are ASCII and at most 128 bytes;
- a source has at most 32 streams and a run at most 64 producers;
- digest and identifier syntax is exact; byte counts, decode indexes, and sample
  counts are unsigned 64-bit decimal strings; stream indexes, dimensions,
  and coordinates are JSON integers from zero through `2^31 - 1` (dimensions
  are positive), rotation is between `-(2^31 - 1)` and `2^31 - 1`, affine
  numerators are signed 64-bit decimal strings, affine denominators are positive
  unsigned 32-bit decimal strings, and time values obey the ranges above;
- unknown fields, unknown enum values, invalid UTF-8/NFC, duplicate keys,
  non-finite values, and unknown schema versions fail closed; and
- paths, URLs, SQL, commands, or model output in descriptive fields remain data
  and are never executed by a record reader.

Readers dispatch on `(schema, schema_version)`. Each migration is a pure,
deterministic `vN -> vN+1` transformation with fixtures for both directions when
lossless. It validates input and output, retains original fingerprint/time/
producer/evidence fields, and records the migration producer/version. Existing
IDs stay fixed when their identity projection is unchanged. If an identity rule
must change, increment `identity_version`, mint a new typed ID, and retain an
explicit `supersedes` reference; never silently rewrite an ID.

The complete record examples above are 558 bytes per `Source`, 469 bytes per
`FrameRef`, 621 bytes per `EvidenceRef`, and 702 bytes per `RunManifest` after
canonical minification. At 5 FPS, 60 seconds (300 samples) is about 319 KiB of
frame/evidence metadata plus one small source and manifest record. Binary
evidence dominates disk use and is measured separately by the evidence store;
no optimization decision is made here.

## Alternatives

- UUIDv7 identifiers were rejected for durable content records because retries
  would create new identities and require a database uniqueness layer.
- Database integers were rejected because they couple domain identity to the
  storage backend and are not portable across exports.
- Hashing each complete record was rejected because adding retention or display
  metadata would change identity. Versioned identity projections keep the
  immutable semantic core explicit.
- Floating-point seconds and integer milliseconds were rejected because both
  lose source PTS precision. Normalizing every timestamp to a new common time
  base was also rejected because it obscures the original tick representation.
- Protobuf, MessagePack, and CBOR were deferred. They may become indexed or wire
  encodings later, but canonical JSON is sufficient for the inspectable MVP and
  remains the authoritative interchange form.

## Consequences

Repeated ingestion can be idempotent without database-generated IDs, and every
sample/evidence reference retains exact source time and original-pixel geometry.
Records are inspectable and backend-independent. Strict limits and schemas make
untrusted metadata fail predictably.

The application must implement canonicalization, typed validation, collision
checks, and explicit migrations before persistence. SHA-256 source identity
requires reading the complete local source once. JSON metadata is larger than a
binary encoding, which is acceptable for the v0.1 working slice; storage layout
and transaction behavior remain issue #6 decisions.
