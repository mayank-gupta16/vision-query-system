# SPDX-License-Identifier: Apache-2.0
"""Contract tests for the version-1 ingestion domain records."""

from __future__ import annotations

import json
from collections.abc import Callable
from io import BytesIO
from typing import BinaryIO, cast

import pytest

from visualworld.ingestion import (
    MAX_RECORD_BYTES,
    AffineCoefficients,
    Artifact,
    EvidenceRef,
    Fingerprint,
    FrameRef,
    Geometry,
    MediaTime,
    Producer,
    ProducerSpace,
    Rational,
    Record,
    RecordValidationError,
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
    compare_media_time,
    dumps_record,
    identity_bytes,
    load_record,
    loads_record,
)

SOURCE_DIGEST = "af9eee534a9f18e8b2ac2e2c5c87dcc010fdeabf13b6335957749bc579426b9e"
SOURCE_ID = "src_8557c30d43a6c7e7a6710008e0e14f0afa86ae409fc4b8ad44bb4b97509486c9"
FRAME_ID = "frm_53b715d4410841bda9c052b9583d11e9a8da41903af5643beaff376031f02efc"
EVIDENCE_ID = "evi_aef14ee06f30df79edc781c85b74fc9689199c621058752f907c7e7aeb129c8a"
RUN_ID = "run_158f21ca6b67a074f5ec83175fc68f0e7eee6440483a883df288d5fbfddaab38"


def records() -> tuple[Source, FrameRef, EvidenceRef, RunManifest]:
    time_base = TimeBase("1", "90000")
    source = Source.create(
        Fingerprint(SOURCE_DIGEST, "145031"),
        (SourceStream(0, 320, 240, 0, time_base),),
    )
    frame = FrameRef.create(
        source.source_id,
        0,
        "0",
        MediaTime("90000", time_base),
        duration=MediaTime("9000", time_base),
        key_frame=True,
    )
    evidence = EvidenceRef.create(
        frame.frame_id,
        Artifact("1f" * 32, "691200"),
        Geometry(320, 240, (0, 0, 320, 240), "measured"),
    )
    manifest = RunManifest.create(
        source.source_id,
        (Producer("visualworld.sampler", "0.1.0a0", "2a" * 32),),
        Sampling(Rational("5", "1")),
        "committed",
        RunOutputs("1", "3b" * 32),
    )
    return source, frame, evidence, manifest


def encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def test_representative_records_have_locked_ids_and_round_trip() -> None:
    expected_ids = (SOURCE_ID, FRAME_ID, EVIDENCE_ID, RUN_ID)
    all_records: tuple[Record, ...] = records()

    for record, expected_id in zip(all_records, expected_ids, strict=True):
        identifier = next(
            value
            for key, value in record.to_mapping().items()
            if key in {"source_id", "frame_id", "evidence_id", "run_id"}
        )
        assert identifier == expected_id
        encoded = dumps_record(record)
        assert encoded == encode(record.to_mapping())
        assert loads_record(encoded) == record
        assert load_record(BytesIO(encoded)) == record
        assert len(encoded) <= MAX_RECORD_BYTES

    assert identity_bytes(records()[0]) == (
        b'{"fingerprint":{"algorithm":"sha256","bytes":"145031","digest":"'
        + SOURCE_DIGEST.encode()
        + b'"},"identity_version":1}'
    )


def test_identity_projections_exclude_non_identity_facts() -> None:
    source, frame, evidence, manifest = records()

    changed_stream = Source.create(source.fingerprint)
    changed_frame_facts = FrameRef.create(
        frame.source_id,
        frame.stream_index,
        frame.decode_index,
        frame.pts,
        duration=None,
        key_frame=False,
    )
    changed_artifact_facts = EvidenceRef.create(
        evidence.frame_id,
        Artifact(evidence.artifact.sha256, "1"),
        evidence.geometry,
    )
    changed_run_state = RunManifest.create(
        manifest.source_id,
        manifest.producers,
        manifest.sampling,
        "preparing",
    )

    assert changed_stream.source_id == source.source_id
    assert changed_frame_facts.frame_id == frame.frame_id
    assert changed_artifact_facts.evidence_id == evidence.evidence_id
    assert changed_run_state.run_id == manifest.run_id


def test_records_reject_caller_owned_mutable_collections() -> None:
    source, _, _, manifest = records()
    mutable_streams = list(source.streams)
    mutable_producers = list(manifest.producers)

    with pytest.raises(RecordValidationError, match="streams_must_be_tuple"):
        Source.create(source.fingerprint, cast(tuple[SourceStream, ...], mutable_streams))
    with pytest.raises(RecordValidationError, match="producers_must_be_tuple"):
        RunManifest.create(
            source.source_id,
            cast(tuple[Producer, ...], mutable_producers),
            manifest.sampling,
            "preparing",
        )


@pytest.mark.parametrize(
    "invalid",
    [
        lambda: SourceStream(0, 1, 1, 0, cast(TimeBase, object())),
        lambda: MediaTime("0", cast(TimeBase, object())),
        lambda: Sampling(cast(Rational, object())),
        lambda: Source(SOURCE_ID, cast(Fingerprint, object())),
        lambda: FrameRef(FRAME_ID, SOURCE_ID, 0, "0", cast(MediaTime, object())),
        lambda: EvidenceRef(
            EVIDENCE_ID,
            FRAME_ID,
            cast(Artifact, object()),
            None,
        ),
        lambda: RunManifest(
            RUN_ID,
            SOURCE_ID,
            (),
            cast(Sampling, object()),
            "preparing",
        ),
        lambda: RunManifest(
            RUN_ID,
            SOURCE_ID,
            (),
            Sampling(Rational("1", "1")),
            "committed",
            cast(RunOutputs, object()),
        ),
    ],
)
def test_nested_constructor_values_fail_with_domain_error(invalid: Callable[[], object]) -> None:
    with pytest.raises(RecordValidationError):
        invalid()


@pytest.mark.parametrize(
    "invalid",
    [
        lambda: Source.create(cast(Fingerprint, object())),
        lambda: FrameRef.create(SOURCE_ID, 0, "0", cast(MediaTime, object())),
        lambda: EvidenceRef.create(
            FRAME_ID,
            cast(Artifact, object()),
            None,
        ),
        lambda: EvidenceRef.create(
            FRAME_ID,
            Artifact("0" * 64, "0"),
            cast(Geometry, object()),
        ),
        lambda: RunManifest.create(
            SOURCE_ID,
            (cast(Producer, object()),),
            Sampling(Rational("1", "1")),
            "preparing",
        ),
        lambda: RunManifest.create(
            SOURCE_ID,
            (),
            cast(Sampling, object()),
            "preparing",
        ),
    ],
)
def test_create_factories_fail_with_domain_error(invalid: Callable[[], object]) -> None:
    with pytest.raises(RecordValidationError):
        invalid()


def test_estimated_time_and_exact_comparison() -> None:
    producer = Producer("visualworld.sampler", "0.1.0a0", "2a" * 32)
    estimated = MediaTime(
        "99000",
        TimeBase("1", "90000"),
        basis="estimated",
        estimate_method="previous_pts_plus_duration",
        estimate_producer=producer,
    )
    frame = FrameRef.create(SOURCE_ID, 0, "1", estimated)

    assert loads_record(dumps_record(frame)) == frame
    assert (
        compare_media_time(MediaTime("1", TimeBase("1", "3")), MediaTime("2", TimeBase("1", "6")))
        == 0
    )
    assert (
        compare_media_time(MediaTime("-1", TimeBase("1", "2")), MediaTime("0", TimeBase("1", "1")))
        == -1
    )
    assert (
        compare_media_time(MediaTime("3", TimeBase("1", "2")), MediaTime("1", TimeBase("1", "1")))
        == 1
    )


def test_affine_geometry_round_trip_uses_exact_rationals() -> None:
    zero = Rational("0", "1")
    two = Rational("2", "1")
    geometry = Geometry(
        320,
        240,
        (20, 40, 100, 120),
        "calibrated",
        transform_kind="affine_rational",
        producer_space=ProducerSpace(160, 120, (10, 20, 50, 60)),
        coefficients=AffineCoefficients(two, zero, zero, zero, two, zero),
    )
    evidence = EvidenceRef.create(FRAME_ID, Artifact("1f" * 32, "1"), geometry)

    assert loads_record(dumps_record(evidence)) == evidence
    assert geometry.coefficients is not None
    assert geometry.coefficients.a.fraction().numerator == 2


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"[]", "expected_object"),
        (b"{", "invalid_json"),
        (b"\xff", "invalid_json"),
        (b"\xef\xbb\xbf{}", "bom_forbidden"),
        (b'{"schema":1,"schema":2}', "duplicate_key"),
        (b'{"schema":1.5}', "floating_point_forbidden"),
        (b'{"schema":NaN}', "floating_point_forbidden"),
        (b'{"schema":' + b"9" * 5000 + b"}", "invalid_json"),
        (b" " * (MAX_RECORD_BYTES + 1), "encoded_record_too_large"),
    ],
)
def test_strict_json_ingress_rejects_invalid_bytes(payload: bytes, code: str) -> None:
    with pytest.raises(RecordValidationError) as raised:
        loads_record(payload)
    assert raised.value.code == code


def test_reader_is_bounded_and_binary() -> None:
    class BrokenReader:
        def read(self, _: int) -> bytes:
            raise OSError("private detail")

    class TextReader:
        def read(self, _: int) -> str:
            return "not bytes"

    class ChunkedReader:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload
            self.offset = 0

        def read(self, maximum: int) -> bytes:
            end = min(self.offset + 7, self.offset + maximum, len(self.payload))
            chunk = self.payload[self.offset : end]
            self.offset = end
            return chunk

    encoded = dumps_record(records()[0])
    assert load_record(cast(BinaryIO, ChunkedReader(encoded))) == records()[0]
    with pytest.raises(RecordValidationError, match="encoded_record_too_large"):
        load_record(BytesIO(b" " * (MAX_RECORD_BYTES + 1)))
    oversized = encoded + b" " * (MAX_RECORD_BYTES + 1 - len(encoded))
    with pytest.raises(RecordValidationError, match="encoded_record_too_large"):
        load_record(cast(BinaryIO, ChunkedReader(oversized)))
    with pytest.raises(RecordValidationError, match="record_read_failed"):
        load_record(cast(BinaryIO, BrokenReader()))
    with pytest.raises(RecordValidationError, match="record_reader_must_be_binary"):
        load_record(cast(BinaryIO, TextReader()))


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda value: value.update({"unexpected": True}), "unknown_field"),
        (lambda value: value.pop("streams"), "missing_field"),
        (lambda value: value.update({"schema_version": 2}), "unknown_schema_version"),
        (lambda value: value.update({"identity_version": 2}), "unknown_identity_version"),
        (lambda value: value.update({"schema": "visualworld.unknown"}), "unknown_schema"),
        (lambda value: value.update({"source_id": "src_" + "0" * 64}), "identifier_mismatch"),
    ],
)
def test_envelope_and_identifier_fail_closed(
    change: Callable[[dict[str, object]], object], code: str
) -> None:
    value = records()[0].to_mapping()
    change(value)
    with pytest.raises(RecordValidationError) as raised:
        loads_record(encode(value))
    assert raised.value.code == code


@pytest.mark.parametrize("decimal", ["01", "+1", "-0", "-1", str(2**64)])
def test_unsigned_decimal_grammar_and_range(decimal: str) -> None:
    value = records()[0].to_mapping()
    fingerprint = cast(dict[str, object], value["fingerprint"])
    fingerprint["bytes"] = decimal
    with pytest.raises(RecordValidationError):
        loads_record(encode(value))


@pytest.mark.parametrize(
    "invalid",
    [
        lambda: MediaTime("0", TimeBase("1", "1"), estimate_method="bad"),
        lambda: MediaTime("0", TimeBase("1", "1"), basis="estimated"),
        lambda: MediaTime(
            "0",
            TimeBase("1", "1"),
            basis="estimated",
            estimate_method="bad",
            estimate_producer=Producer("p", "1", "0" * 64),
        ),
        lambda: Geometry(10, 10, (0, 0, 10, 10), "bad"),
        lambda: Geometry(
            10,
            10,
            (0, 0, 10, 10),
            "measured",
            producer_space=ProducerSpace(10, 10, (0, 0, 10, 10)),
        ),
        lambda: Geometry(10, 10, (0, 0, 10, 10), "measured", "affine_rational"),
        lambda: Geometry(
            10,
            10,
            (0, 0, 9, 10),
            "measured",
            "affine_rational",
            ProducerSpace(10, 10, (0, 0, 10, 10)),
            AffineCoefficients(
                Rational("1", "1"),
                Rational("0", "1"),
                Rational("0", "1"),
                Rational("0", "1"),
                Rational("1", "1"),
                Rational("0", "1"),
            ),
        ),
        lambda: Sampling(Rational("0", "1")),
        lambda: RunManifest.create(SOURCE_ID, (), Sampling(Rational("1", "1")), "committed"),
        lambda: RunManifest.create(
            SOURCE_ID,
            (),
            Sampling(Rational("1", "1")),
            "failed",
            RunOutputs("0", "0" * 64),
        ),
    ],
)
def test_cross_field_invariants(invalid: Callable[[], object]) -> None:
    with pytest.raises(RecordValidationError):
        invalid()


@pytest.mark.parametrize(
    "change",
    [
        lambda value: cast(dict[str, object], value["origin"]).update({"kind": "url"}),
        lambda value: cast(dict[str, object], value["origin"]).update({"locator_stored": True}),
        lambda value: cast(dict[str, object], value["access"]).update({"classification": "public"}),
        lambda value: cast(dict[str, object], value["access"]).update({"retention": "forever"}),
        lambda value: cast(list[object], value["streams"]).append(
            cast(list[object], value["streams"])[0]
        ),
    ],
)
def test_source_contract_rejects_unsupported_values(
    change: Callable[[dict[str, object]], object],
) -> None:
    value = records()[0].to_mapping()
    change(value)
    with pytest.raises(RecordValidationError):
        loads_record(encode(value))


def test_geometry_and_evidence_require_explicit_valid_shape() -> None:
    evidence = records()[2].to_mapping()
    evidence.pop("geometry")
    with pytest.raises(RecordValidationError, match="missing_field"):
        loads_record(encode(evidence))

    evidence = records()[2].to_mapping()
    evidence["geometry"] = None
    artifact = cast(dict[str, object], evidence["artifact"])
    artifact["sha256"] = "2f" * 32
    # A valid nullable geometry reaches identity verification, rather than being rejected as absent.
    with pytest.raises(RecordValidationError, match="identifier_mismatch"):
        loads_record(encode(evidence))

    manifest = records()[3].to_mapping()
    contracts = cast(dict[str, object], manifest["contracts"])
    contracts["source"] = 2
    with pytest.raises(RecordValidationError, match="unknown_contract_version"):
        loads_record(encode(manifest))


def test_size_depth_array_and_string_limits_are_enforced_before_dispatch() -> None:
    source = records()[0].to_mapping()

    source["extra"] = [None] * 65
    with pytest.raises(RecordValidationError, match="too_many_array_items"):
        loads_record(encode(source))

    source = records()[0].to_mapping()
    nested: object = None
    for _ in range(16):
        nested = {"x": nested}
    source["extra"] = nested
    with pytest.raises(RecordValidationError, match="maximum_depth_exceeded"):
        loads_record(encode(source))

    source = records()[0].to_mapping()
    source["extra"] = "x" * 4097
    with pytest.raises(RecordValidationError, match="string_too_long"):
        loads_record(encode(source))

    oversized_object = {f"k{index}": None for index in range(129)}
    with pytest.raises(RecordValidationError, match="too_many_object_members"):
        loads_record(encode(oversized_object))


def test_numeric_and_collection_boundaries() -> None:
    max_i31 = 2**31 - 1
    max_u32 = 2**32 - 1
    max_u64 = 2**64 - 1
    time_base = TimeBase(str(max_u32), str(max_u32))
    minimum_time = MediaTime(str(-(2**63)), time_base)
    maximum_time = MediaTime(str(2**63 - 1), time_base)
    streams = tuple(
        SourceStream(index, max_i31, max_i31, -max_i31 if index == 0 else max_i31, time_base)
        for index in range(32)
    )
    source = Source.create(Fingerprint("0" * 64, str(max_u64)), streams)
    producers = tuple(Producer(f"p{index}", "1", "0" * 64) for index in range(64))
    run = RunManifest.create(
        source.source_id,
        producers,
        Sampling(Rational(str(2**63 - 1), str(max_u32))),
        "preparing",
    )

    loaded_source = loads_record(dumps_record(source))
    loaded_run = loads_record(dumps_record(run))
    assert isinstance(loaded_source, Source)
    assert isinstance(loaded_run, RunManifest)
    assert len(loaded_source.streams) == 32
    assert len(loaded_run.producers) == 64
    assert compare_media_time(minimum_time, maximum_time) == -1

    with pytest.raises(RecordValidationError, match="too_many_streams"):
        Source.create(source.fingerprint, (*streams, streams[0]))
    with pytest.raises(RecordValidationError, match="too_many_array_items"):
        RunManifest.create(
            source.source_id,
            (*producers, producers[0]),
            run.sampling,
            "preparing",
        )
    with pytest.raises(RecordValidationError, match="decimal_out_of_range"):
        MediaTime(str(2**63), time_base)
    with pytest.raises(RecordValidationError, match="decimal_out_of_range"):
        TimeBase(str(max_u32 + 1), "1")
    with pytest.raises(RecordValidationError, match="integer_out_of_range"):
        SourceStream(max_i31 + 1, 1, 1, 0, TimeBase("1", "1"))


def test_unicode_and_ascii_contracts_fail_closed() -> None:
    with pytest.raises(RecordValidationError, match="token_not_ascii"):
        Producer("café", "1", "0" * 64)

    value = records()[0].to_mapping()
    value["e\u0301"] = None
    with pytest.raises(RecordValidationError, match="string_not_nfc"):
        loads_record(encode(value))


def test_public_serializers_reject_non_records_and_non_bytes() -> None:
    with pytest.raises(RecordValidationError, match="unsupported_record"):
        dumps_record(cast(Record, object()))
    with pytest.raises(RecordValidationError, match="unsupported_record"):
        identity_bytes(cast(Record, object()))
    with pytest.raises(RecordValidationError, match="record_must_be_bytes"):
        loads_record(cast(bytes, "not bytes"))
