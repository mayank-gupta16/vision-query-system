# SPDX-License-Identifier: Apache-2.0
"""Strict persistence-codec coverage for v0.2 evidence intents."""

from __future__ import annotations

import hashlib
import json
import traceback
from io import BytesIO
from typing import BinaryIO, cast

import pytest

from visualworld.evidence import (
    EvidenceIntent,
    EvidenceScore,
    dumps_evidence_intent,
    load_evidence_intent,
    loads_evidence_intent,
)
from visualworld.ingestion import (
    MAX_RECORD_BYTES,
    Geometry,
    MediaTime,
    Producer,
    RecordValidationError,
    TimeBase,
)

OBSERVATION_ID = "obs_" + "11" * 32
TRACKLET_ID = "trk_" + "22" * 32
SOURCE_ID = "src_" + "33" * 32
FRAME_ID = "frm_" + "44" * 32


def _encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _intent() -> EvidenceIntent:
    pts = MediaTime("10", TimeBase("1", "1000"))
    geometry = Geometry(10, 8, (1, 2, 5, 6), "inferred")
    score = EvidenceScore(
        0,
        900_000,
        16,
        80,
        200_000,
        1,
        2,
        0,
        1,
        pts,
        OBSERVATION_ID,
    )
    return EvidenceIntent(
        1,
        TRACKLET_ID,
        OBSERVATION_ID,
        SOURCE_ID,
        FRAME_ID,
        0,
        pts,
        geometry,
        score,
        Producer("visualworld.test-selector", "1", "55" * 32),
    )


def _set_path(mapping: dict[str, object], path: tuple[str, ...], value: object) -> None:
    current = mapping
    for part in path[:-1]:
        current = cast(dict[str, object], current[part])
    current[path[-1]] = value


def _delete_path(mapping: dict[str, object], path: tuple[str, ...]) -> None:
    current = mapping
    for part in path[:-1]:
        current = cast(dict[str, object], current[part])
    del current[path[-1]]


def test_evidence_intent_codec_has_canonical_byte_identity_and_round_trips() -> None:
    intent = _intent()
    encoded = dumps_evidence_intent(intent)
    digest = hashlib.sha256(encoded).hexdigest()

    assert encoded == _encode(intent.to_mapping())
    assert dumps_evidence_intent(intent) == encoded
    assert loads_evidence_intent(encoded) == intent
    assert load_evidence_intent(BytesIO(encoded)) == intent
    assert dumps_evidence_intent(loads_evidence_intent(encoded)) == encoded
    assert (
        hashlib.sha256(dumps_evidence_intent(loads_evidence_intent(encoded))).hexdigest() == digest
    )

    persisted = cast(dict[str, object], json.loads(encoded))
    assert {"crop", "rgb24", "content", "artifact"}.isdisjoint(persisted)


def test_evidence_intent_deeply_owns_nested_values_before_encoding() -> None:
    supplied_time_base = TimeBase("1", "1000")
    supplied_pts = MediaTime("10", supplied_time_base)
    supplied_geometry = Geometry(10, 8, (1, 2, 5, 6), "inferred")
    supplied_score = EvidenceScore(
        0,
        900_000,
        16,
        80,
        200_000,
        1,
        2,
        0,
        1,
        supplied_pts,
        OBSERVATION_ID,
    )
    supplied_selector = Producer("visualworld.test-selector", "1", "55" * 32)
    intent = EvidenceIntent(
        1,
        TRACKLET_ID,
        OBSERVATION_ID,
        SOURCE_ID,
        FRAME_ID,
        0,
        supplied_pts,
        supplied_geometry,
        supplied_score,
        supplied_selector,
    )
    encoded = dumps_evidence_intent(intent)

    assert intent.pts is not supplied_pts
    assert intent.pts.time_base is not supplied_time_base
    assert intent.geometry is not supplied_geometry
    assert intent.score is not supplied_score
    assert intent.selector is not supplied_selector

    object.__setattr__(supplied_time_base, "denominator", "1")
    object.__setattr__(supplied_pts, "value", "11")
    object.__setattr__(supplied_geometry, "box_xyxy", (0, 0, 10, 8))
    object.__setattr__(supplied_score, "confidence_millionths", 0)
    object.__setattr__(supplied_selector, "name", "private.mutated")

    assert dumps_evidence_intent(intent) == encoded
    assert loads_evidence_intent(encoded) == intent


@pytest.mark.parametrize(
    "path",
    [
        ("tracklet_id",),
        ("score", "components", "visible_area_pixels"),
        ("score", "tie_break", "pts"),
        ("score", "tie_break", "midpoint_distance_seconds_x2", "numerator"),
    ],
)
def test_codec_rejects_missing_required_fields(path: tuple[str, ...]) -> None:
    mapping = _intent().to_mapping()
    _delete_path(mapping, path)

    with pytest.raises(ValueError):
        EvidenceIntent.from_mapping(mapping)


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("score",),
        ("score", "components"),
        ("score", "tie_break"),
        ("score", "tie_break", "midpoint_distance_seconds_x2"),
    ],
)
def test_codec_rejects_unknown_fields_at_every_level(path: tuple[str, ...]) -> None:
    mapping = _intent().to_mapping()
    if path:
        current = mapping
        for part in path:
            current = cast(dict[str, object], current[part])
        current["private_extra"] = "must-not-survive"
    else:
        mapping["state"] = "complete"

    with pytest.raises(ValueError):
        EvidenceIntent.from_mapping(mapping)


@pytest.mark.parametrize(
    ("path", "invalid"),
    [
        (("schema",), "visualworld.other"),
        (("schema_version",), 2),
        (("schema_version",), True),
        (("rank",), 0),
        (("rank",), True),
        (("stream_index",), -1),
        (("stream_index",), True),
        (("tracklet_id",), "trk_private"),
        (("observation_id",), "obs_private"),
        (("source_id",), "src_private"),
        (("frame_id",), "frm_private"),
        (("kind",), "derived_crop"),
        (("retention",), "public"),
        (("deletion_owner",), "caller"),
        (("pts",), []),
        (("pts", "value"), "01"),
        (("pts", "basis"), "estimated"),
        (("pts", "time_base", "denominator"), "0"),
        (("geometry",), []),
        (("geometry", "source_width"), 0),
        (("geometry", "box_xyxy"), [1, 2, 1, 6]),
        (("geometry", "measurement"), "detected"),
        (("geometry", "space"), "detector_pixels"),
        (("geometry", "transform_to_source", "kind"), "projective"),
        (("selector",), []),
        (("selector", "name"), "private selector"),
        (("selector", "version"), 1),
        (("selector", "configuration_sha256"), "0" * 63),
        (("score",), []),
        (("score", "components", "boundary_touch_count"), 5),
        (("score", "components", "confidence_millionths"), 1_000_001),
        (("score", "components", "visible_area_pixels"), 0),
        (("score", "components", "source_area_pixels"), 15),
        (("score", "components", "visible_area_millionths"), -1),
        (("score", "components", "visible_area_millionths"), 200_001),
        (("score", "components", "confidence_millionths"), True),
        (("score", "tie_break", "observation_id"), 1),
        (("score", "tie_break", "observation_id"), "obs_private"),
        (("score", "tie_break", "point_count"), 0),
        (("score", "tie_break", "point_index"), 1),
        (("score", "tie_break", "midpoint_distance_seconds_x2", "numerator"), -1),
        (("score", "tie_break", "midpoint_distance_seconds_x2", "denominator"), 0),
        (("score", "tie_break", "midpoint_distance_seconds_x2", "numerator"), 2),
        (("score", "tie_break", "pts", "value"), "11"),
    ],
)
def test_codec_rejects_invalid_schema_link_and_nested_values(
    path: tuple[str, ...], invalid: object
) -> None:
    mapping = _intent().to_mapping()
    _set_path(mapping, path, invalid)

    with pytest.raises(ValueError):
        loads_evidence_intent(_encode(mapping))


def test_public_mapping_decoders_reject_hostile_scalar_subclasses() -> None:
    class EqualToEverything(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

        __hash__ = str.__hash__

    class IntSubclass(int):
        pass

    mutations: tuple[tuple[tuple[str, ...], object], ...] = (
        (("schema",), EqualToEverything("private.schema")),
        (("tracklet_id",), EqualToEverything(TRACKLET_ID)),
        (("rank",), IntSubclass(1)),
        (("score", "components", "boundary_touch_count"), IntSubclass(0)),
        (("score", "tie_break", "observation_id"), EqualToEverything(OBSERVATION_ID)),
        (("selector", "name"), EqualToEverything("visualworld.test-selector")),
        (("pts", "value"), EqualToEverything("10")),
        (("geometry", "measurement"), EqualToEverything("inferred")),
    )
    for path, invalid in mutations:
        mapping = _intent().to_mapping()
        _set_path(mapping, path, invalid)
        with pytest.raises(ValueError):
            EvidenceIntent.from_mapping(mapping)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"{}",
        b"[]",
        b"{",
        b"\xef\xbb\xbf{}",
        b"\xff",
        b'{"rank":1.0}',
        b'{"rank":NaN}',
    ],
)
def test_codec_rejects_truncated_or_corrupt_json(payload: bytes) -> None:
    with pytest.raises(RecordValidationError):
        loads_evidence_intent(payload)


def test_codec_rejects_duplicate_keys_and_non_byte_inputs() -> None:
    encoded = dumps_evidence_intent(_intent())
    duplicate = encoded[:-1] + b',"rank":1}'

    with pytest.raises(RecordValidationError, match="duplicate_key"):
        loads_evidence_intent(duplicate)
    with pytest.raises(ValueError, match="must be bytes"):
        loads_evidence_intent(cast(bytes, bytearray(encoded)))
    with pytest.raises(ValueError, match="unsupported evidence intent"):
        dumps_evidence_intent(cast(EvidenceIntent, object()))


def test_streaming_decoder_is_bounded_binary_and_redacted() -> None:
    class ChunkedReader:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload
            self.offset = 0
            self.requests: list[int] = []

        def read(self, maximum: int) -> bytes:
            self.requests.append(maximum)
            end = min(self.offset + 7, self.offset + maximum, len(self.payload))
            chunk = self.payload[self.offset : end]
            self.offset = end
            return chunk

    class TextReader:
        def read(self, _: int) -> str:
            return "private reader value"

    class BrokenReader:
        def read(self, _: int) -> bytes:
            raise OSError("private/path must not escape")

    encoded = dumps_evidence_intent(_intent())
    chunked = ChunkedReader(encoded)
    assert load_evidence_intent(cast(BinaryIO, chunked)) == _intent()
    assert chunked.requests[0] == MAX_RECORD_BYTES + 1
    assert all(
        0 < later <= earlier
        for earlier, later in zip(chunked.requests, chunked.requests[1:], strict=False)
    )

    with pytest.raises(RecordValidationError, match="encoded_record_too_large"):
        load_evidence_intent(BytesIO(b" " * (MAX_RECORD_BYTES + 1)))
    with pytest.raises(ValueError, match="reader must be binary") as text_error:
        load_evidence_intent(cast(BinaryIO, TextReader()))
    with pytest.raises(ValueError, match="read failed") as read_error:
        load_evidence_intent(cast(BinaryIO, BrokenReader()))

    rendered = "".join(traceback.format_exception(read_error.value))
    assert "private/path" not in rendered
    assert "private reader value" not in str(text_error.value)
    assert read_error.value.__cause__ is None
    assert read_error.value.__context__ is None


def test_validation_errors_never_echo_private_fields_or_emit_pixels() -> None:
    private_value = "private/path/DO_NOT_ECHO"
    mapping = _intent().to_mapping()
    mapping["source_id"] = private_value

    with pytest.raises(ValueError) as raised:
        loads_evidence_intent(_encode(mapping))

    rendered = "".join(traceback.format_exception(raised.value))
    assert private_value not in rendered
    assert "DO_NOT_ECHO" not in str(raised.value)
    assert b"DO_NOT_ECHO" not in dumps_evidence_intent(_intent())
