# SPDX-License-Identifier: Apache-2.0
"""Combined public experimental record dispatch tests."""

from __future__ import annotations

from io import BytesIO
from typing import BinaryIO, cast

import pytest

from visualworld.experimental import (
    ExperimentalRecord,
    dumps_experimental_record,
    identity_experimental_bytes,
    load_experimental_record,
    loads_experimental_record,
)
from visualworld.ingestion import (
    Artifact,
    EvidenceRef,
    Fingerprint,
    FrameRef,
    Geometry,
    MediaTime,
    Producer,
    Rational,
    RecordValidationError,
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
    identity_bytes,
)
from visualworld.perception import Observation, identity_perception_bytes


def experimental_records() -> tuple[ExperimentalRecord, ...]:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint("aa" * 32, "10"),
        (SourceStream(0, 64, 48, 0, time_base),),
    )
    frame = FrameRef.create(source.source_id, 0, "0", MediaTime("0", time_base))
    evidence = EvidenceRef.create(
        frame.frame_id,
        Artifact("dd" * 32, "1"),
        Geometry(64, 48, (4, 5, 20, 30), "measured"),
    )
    run = RunManifest.create(
        source.source_id,
        (Producer("visualworld.fake-sampler", "1", "ee" * 32),),
        Sampling(Rational("5", "1")),
        "committed",
        RunOutputs("1", "ff" * 32),
    )
    observation = Observation.create(
        source.source_id,
        "frm_" + "bb" * 32,
        0,
        MediaTime("0", time_base),
        Geometry(64, 48, (4, 5, 20, 30), "inferred"),
        "vehicle",
        900_000,
        Producer("visualworld.fake-detector", "1", "cc" * 32),
    )
    return source, frame, evidence, run, observation


def test_combined_experimental_union_dispatches_ingestion_and_perception_records() -> None:
    first, *records = experimental_records()
    source = cast(Source, first)
    observation = cast(Observation, records[-1])

    for record in (source, *records):
        encoded = dumps_experimental_record(record)
        assert loads_experimental_record(encoded) == record
        assert load_experimental_record(BytesIO(encoded)) == record
    assert identity_experimental_bytes(source) == identity_bytes(source)
    assert identity_experimental_bytes(observation) == identity_perception_bytes(observation)


def test_combined_experimental_dispatch_fails_closed() -> None:
    source = cast(Source, experimental_records()[0])

    class VendorFingerprint(Fingerprint):
        def to_mapping(self) -> dict[str, object]:
            return {**super().to_mapping(), "pixels": "private/path/secret"}

    nested = Source.create(VendorFingerprint("11" * 32, "10"), source.streams)

    with pytest.raises(RecordValidationError, match="non_plain_ingestion_value"):
        dumps_experimental_record(nested)
    with pytest.raises(RecordValidationError, match="non_plain_ingestion_value"):
        identity_experimental_bytes(nested)
    with pytest.raises(RecordValidationError, match="unsupported_experimental_record"):
        dumps_experimental_record(cast(ExperimentalRecord, object()))
    with pytest.raises(RecordValidationError, match="unsupported_experimental_record"):
        identity_experimental_bytes(cast(ExperimentalRecord, object()))
    with pytest.raises(RecordValidationError, match="record_must_be_bytes"):
        loads_experimental_record(cast(bytes, "not bytes"))
    with pytest.raises(RecordValidationError, match="unknown_schema_version"):
        loads_experimental_record(b'{"schema":"visualworld.source","schema_version":2}')
    with pytest.raises(RecordValidationError, match="unknown_schema") as raised:
        loads_experimental_record(b'{"schema":"private/path","schema_version":1}')
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_combined_experimental_reader_is_binary_bounded_and_redacted() -> None:
    class TextReader:
        def read(self, _: int) -> str:
            return "not bytes"

    class BrokenReader:
        def read(self, _: int) -> bytes:
            raise OSError("private detail")

    with pytest.raises(RecordValidationError, match="record_reader_must_be_binary"):
        load_experimental_record(cast(BinaryIO, TextReader()))
    with pytest.raises(RecordValidationError, match="record_read_failed") as raised:
        load_experimental_record(cast(BinaryIO, BrokenReader()))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
