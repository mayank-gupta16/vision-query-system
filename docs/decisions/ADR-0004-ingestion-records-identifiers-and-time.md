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
decimal strings, booleans, arrays, and objects. Hashes are lowercase SHA-256
hexadecimal.

Identifiers are typed, 68-character strings:

- `src_<sha256>` is the SHA-256 of the complete immutable source bytes;
- `frm_<sha256>` hashes the canonical frame identity projection;
- `evi_<sha256>` hashes the canonical evidence identity projection; and
- `run_<sha256>` hashes the canonical run identity projection.

Each projection includes `identity_version: 1`. Mutable state, wall-clock time,
display labels, storage locations, and optional descriptive metadata never enter
an identity projection. When an existing identifier is encountered, its stored
identity projection must match byte-for-byte; otherwise ingestion stops with an
integrity error. Database sequence numbers may be private indexes but never
domain identifiers.

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

`basis` is `measured` when the value came from decoded source PTS and `estimated`
only when a documented deterministic policy derived it. Estimated time also
requires an ASCII `estimate_method` and producer version. Missing source PTS
remains missing at the decoder boundary; it must not silently become zero. A
sample becomes a durable `FrameRef` only after it has measured or explicitly
estimated rational time. Ties are ordered by `decode_index`.

### Record contracts

`Source` records immutable content identity, source byte count, privacy/access
classification, and bounded stream facts. A raw local path or URL is not stored
in the durable record; the caller supplies an authorized locator when opening
the source.

`FrameRef` identifies one decoded frame by source, stream, non-negative decimal
`decode_index`, and exact PTS. Its identity projection is exactly those fields
plus `identity_version`. Optional duration and key-frame facts do not change the
identifier.

`Geometry` is embedded by value. It always includes original-source dimensions
and a half-open integer `box_xyxy = [x_min, y_min, x_max, y_max]` in source
pixels. Bounds satisfy `0 <= min < max <= dimension`. `measurement` is one of
`measured`, `calibrated`, `estimated`, `inferred`, or `unknown`. Geometry
originating in another pixel space additionally stores the producer-space box
and an exact six-coefficient rational affine transform to source pixels;
source-space geometry uses `{"kind":"identity"}`.

`EvidenceRef` binds an immutable artifact by SHA-256 and byte count, and binds it
to a frame, evidence kind, optional geometry, media type, and retention class.
Artifact bytes are never embedded in metadata. Its identifier projection uses
the artifact digest, frame identifier, kind, and geometry; storage paths and
retention changes are excluded.

`RunManifest` binds one source to exact contract versions, ordered producer
names/versions/configuration digests, and sampling configuration. These fields
form its identity projection, so an equivalent retry receives the same `run_id`.
Operational state is `preparing`, `committed`, `failed`, or `cancelled` and is
excluded from identity. Only `committed` exposes outputs as complete. The
manifest stores sample count plus a digest of the separately stored ordered
sample index rather than embedding every sample.

Representative version-1 records follow. The source digest is the generated
issue #4 fixture; the other example artifact/configuration digests are synthetic.
The identifiers match the canonical identity rules above.

```json
{
  "schema": "visualworld.source",
  "schema_version": 1,
  "source_id": "src_af9eee534a9f18e8b2ac2e2c5c87dcc010fdeabf13b6335957749bc579426b9e",
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
  "frame_id": "frm_e0e8b4104283d7919e2e26f532d05d0e003111b2779a4fa303b737be1ef67325",
  "source_id": "src_af9eee534a9f18e8b2ac2e2c5c87dcc010fdeabf13b6335957749bc579426b9e",
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
  "evidence_id": "evi_65fd6bf89e62c7f9a6bed14f29849565dc43cf1da0d421cf39257e7c111ba373",
  "frame_id": "frm_e0e8b4104283d7919e2e26f532d05d0e003111b2779a4fa303b737be1ef67325",
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
  "run_id": "run_40d088e5f35f55c286ab8a76e6815ff45c20064c255f9dcd27ac7fad522782f2",
  "source_id": "src_af9eee534a9f18e8b2ac2e2c5c87dcc010fdeabf13b6335957749bc579426b9e",
  "contracts": {"source": 1, "frame_ref": 1, "evidence_ref": 1, "run_manifest": 1},
  "producers": [{"name": "visualworld.sampler", "version": "0.1.0a0", "configuration_sha256": "2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a"}],
  "sampling": {"policy": "nearest_eligible_pts", "target_fps": {"numerator": "5", "denominator": "1"}},
  "state": "committed",
  "outputs": {"sample_count": "1", "sample_index_sha256": "3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b"}
}
```

### Validation, limits, and migrations

Validation happens before hashing and again before persistence:

- metadata records are at most 256 KiB of canonical JSON, with nesting at most
  16 levels, at most 128 object members, and at most 64 elements in an embedded
  array; sample indexes are separate bounded streams;
- general strings are at most 4,096 UTF-8 bytes; schema names, enums, producer
  names, versions, and estimate methods are ASCII and at most 128 bytes;
- a source has at most 32 streams and a run at most 64 producers;
- digest and identifier syntax is exact; byte counts and indexes are unsigned
  64-bit decimal strings; dimensions and coordinates are bounded 31-bit JSON
  integers; and time values obey the ranges above;
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

The examples above are 537 bytes per `Source`, 448 bytes per `FrameRef`, 600
bytes per `EvidenceRef`, and 681 bytes per `RunManifest` after canonical
minification. At 5 FPS, 60 seconds (300 samples) is therefore about 307 KiB of
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
