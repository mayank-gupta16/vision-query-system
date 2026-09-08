# SPDX-License-Identifier: Apache-2.0
"""Combined experimental record surface for additive post-v0.1 domains."""

from __future__ import annotations

from dataclasses import fields
from typing import Any, BinaryIO, cast

from visualworld.ingestion import (
    MAX_I31,
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
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
    _bounded_int,
    _fail,
    _parse_json,
    _token,
    dumps_record,
    identity_bytes,
    loads_record,
)
from visualworld.ingestion import (
    Record as IngestionRecord,
)
from visualworld.perception import (
    Observation,
    PerceptionRecord,
    Tracklet,
    dumps_perception_record,
    identity_perception_bytes,
    loads_perception_record,
)

ExperimentalRecord = IngestionRecord | PerceptionRecord

_INGESTION_TYPES = {Source, FrameRef, EvidenceRef, RunManifest}
_PERCEPTION_TYPES = {Observation, Tracklet}
_INGESTION_SCHEMAS = frozenset(
    {Source.schema, FrameRef.schema, EvidenceRef.schema, RunManifest.schema}
)
_PERCEPTION_SCHEMAS = frozenset({Observation.schema, Tracklet.schema})
_INGESTION_VALUE_TYPES = {
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
    RunManifest,
    RunOutputs,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
}


def _require_plain_ingestion_value(value: object) -> None:
    value_type = type(value)
    if value is None or value_type in {bool, int, str}:
        return
    if value_type is tuple:
        for item in cast(tuple[object, ...], value):
            _require_plain_ingestion_value(item)
        return
    if value_type not in _INGESTION_VALUE_TYPES:
        _fail("non_plain_ingestion_value")
    for field in fields(cast(Any, value)):
        _require_plain_ingestion_value(getattr(value, field.name))
    cast(Any, value_type).__post_init__(value)


def dumps_experimental_record(record: ExperimentalRecord) -> bytes:
    """Serialize one exact-type v0.1 or additive perception record."""
    if type(record) in _INGESTION_TYPES:
        _require_plain_ingestion_value(record)
        return dumps_record(cast(IngestionRecord, record))
    if type(record) in _PERCEPTION_TYPES:
        return dumps_perception_record(cast(PerceptionRecord, record))
    _fail("unsupported_experimental_record")


def identity_experimental_bytes(record: ExperimentalRecord) -> bytes:
    """Return the canonical identity projection for any experimental record."""
    if type(record) in _INGESTION_TYPES:
        _require_plain_ingestion_value(record)
        return identity_bytes(cast(IngestionRecord, record))
    if type(record) in _PERCEPTION_TYPES:
        return identity_perception_bytes(cast(PerceptionRecord, record))
    _fail("unsupported_experimental_record")


def loads_experimental_record(data: bytes) -> ExperimentalRecord:
    """Strictly dispatch one bounded record across all public experimental schemas."""
    if type(data) is not bytes:
        _fail("record_must_be_bytes")
    value = _parse_json(data)
    schema = _token(value.get("schema"), "schema")
    version = _bounded_int(value.get("schema_version"), 1, MAX_I31, "schema_version")
    if version != 1:
        _fail("unknown_schema_version", "schema_version")
    if schema in _INGESTION_SCHEMAS:
        return loads_record(data)
    if schema in _PERCEPTION_SCHEMAS:
        return loads_perception_record(data)
    _fail("unknown_schema", "schema")


def load_experimental_record(reader: BinaryIO) -> ExperimentalRecord:
    """Read at most 256 KiB plus one byte before combined record dispatch."""
    chunks: list[bytes] = []
    remaining = MAX_RECORD_BYTES + 1
    read_failed = False
    try:
        while remaining > 0:
            chunk = reader.read(remaining)
            if type(chunk) is not bytes:
                _fail("record_reader_must_be_binary")
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        read_failed = True
    if read_failed:
        _fail("record_read_failed")
    return loads_experimental_record(b"".join(chunks))


__all__ = [
    "ExperimentalRecord",
    "dumps_experimental_record",
    "identity_experimental_bytes",
    "load_experimental_record",
    "loads_experimental_record",
]
