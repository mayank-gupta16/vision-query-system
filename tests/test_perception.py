# SPDX-License-Identifier: Apache-2.0
"""Contract tests for v0.2 pixel-free perception records."""

from __future__ import annotations

import json
from collections.abc import Callable
from io import BytesIO
from typing import BinaryIO, cast

import pytest

from visualworld.ingestion import Geometry, MediaTime, Producer, RecordValidationError, TimeBase
from visualworld.perception import (
    MAX_TRACK_POINTS,
    Observation,
    PerceptionRecord,
    Tracklet,
    TrackPoint,
    dumps_perception_record,
    identity_perception_bytes,
    load_perception_record,
    loads_perception_record,
)

SOURCE_ID = "src_" + "11" * 32
FRAME_IDS = ("frm_" + "22" * 32, "frm_" + "33" * 32)
OBSERVATION_ID = "obs_70d5360054807dfa82ad25f62da132151a9298d85c19a5962de452ef085c28a9"
TRACKLET_ID = "trk_43995bf600c5b11d536c243a8093ad5aef3ce78105183a611939fa9d69ab8871"


def encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def records() -> tuple[Observation, Tracklet]:
    time_base = TimeBase("1", "1000")
    producer = Producer("visualworld.fake-detector", "1", "44" * 32)
    observations = tuple(
        Observation.create(
            SOURCE_ID,
            FRAME_IDS[index],
            0,
            MediaTime(str(index * 200), time_base),
            Geometry(64, 48, (4 + index, 5, 20 + index, 30), "inferred"),
            "vehicle",
            950_000 - index,
            producer,
        )
        for index in range(2)
    )
    tracker = Producer("visualworld.fake-tracker", "1", "55" * 32)
    points = tuple(TrackPoint.from_observation(observation) for observation in observations)
    return observations[0], Tracklet.create(SOURCE_ID, 0, "vehicle", points, "source_end", tracker)


def test_representative_perception_records_are_canonical_and_round_trip() -> None:
    observation, tracklet = records()
    all_records: tuple[PerceptionRecord, ...] = (observation, tracklet)

    for record in all_records:
        encoded = dumps_perception_record(record)
        assert encoded == encode(record.to_mapping())
        assert loads_perception_record(encoded) == record
        assert load_perception_record(BytesIO(encoded)) == record
        assert identity_perception_bytes(record) == encode(record.identity_projection())

    assert observation.observation_id == OBSERVATION_ID
    assert tracklet.tracklet_id == TRACKLET_ID
    assert tracklet.identity_scope == "source_clip"
    assert tracklet.continuity == "inferred"
    assert "entity_id" not in tracklet.to_mapping()


def test_observation_and_tracklet_identity_bind_inference_provenance() -> None:
    observation, tracklet = records()

    changed_observation = Observation.create(
        observation.source_id,
        observation.frame_id,
        observation.stream_index,
        observation.pts,
        observation.geometry,
        observation.category,
        observation.confidence_millionths - 1,
        observation.producer,
    )
    changed_tracklet = Tracklet.create(
        tracklet.source_id,
        tracklet.stream_index,
        tracklet.category,
        tracklet.points,
        "miss_timeout",
        tracklet.producer,
    )

    assert changed_observation.observation_id != observation.observation_id
    assert changed_tracklet.tracklet_id != tracklet.tracklet_id


@pytest.mark.parametrize(
    "invalid",
    [
        lambda: Observation.create(
            SOURCE_ID,
            FRAME_IDS[0],
            0,
            MediaTime("0", TimeBase("1", "1")),
            Geometry(2, 2, (0, 0, 1, 1), "inferred"),
            "person",
            1,
            Producer("p", "1", "0" * 64),
        ),
        lambda: Observation.create(
            SOURCE_ID,
            FRAME_IDS[0],
            0,
            MediaTime("0", TimeBase("1", "1")),
            Geometry(2, 2, (0, 0, 1, 1), "inferred"),
            "vehicle",
            cast(int, True),
            Producer("p", "1", "0" * 64),
        ),
        lambda: Observation.create(
            SOURCE_ID,
            FRAME_IDS[0],
            0,
            MediaTime("0", TimeBase("1", "1")),
            Geometry(2, 2, (0, 0, 1, 1), "inferred"),
            "vehicle",
            1_000_001,
            Producer("p", "1", "0" * 64),
        ),
        lambda: Tracklet.create(
            SOURCE_ID,
            0,
            "vehicle",
            (),
            "source_end",
            Producer("p", "1", "0" * 64),
        ),
        lambda: Tracklet.create(
            SOURCE_ID,
            0,
            "vehicle",
            records()[1].points,
            "reid_merge",
            Producer("p", "1", "0" * 64),
        ),
        lambda: Tracklet.create(
            SOURCE_ID,
            0,
            "vehicle",
            tuple(reversed(records()[1].points)),
            "source_end",
            Producer("p", "1", "0" * 64),
        ),
    ],
)
def test_perception_cross_field_invariants_fail_closed(invalid: Callable[[], object]) -> None:
    with pytest.raises(RecordValidationError):
        invalid()


def test_perception_nested_type_guards_raise_domain_errors() -> None:
    observation, tracklet = records()
    invalid = (
        lambda: Observation(
            observation.observation_id,
            observation.source_id,
            observation.frame_id,
            observation.stream_index,
            cast(MediaTime, object()),
            observation.geometry,
            observation.category,
            observation.confidence_millionths,
            observation.producer,
        ),
        lambda: Observation(
            observation.observation_id,
            observation.source_id,
            observation.frame_id,
            observation.stream_index,
            observation.pts,
            cast(Geometry, object()),
            observation.category,
            observation.confidence_millionths,
            observation.producer,
        ),
        lambda: Observation(
            observation.observation_id,
            observation.source_id,
            observation.frame_id,
            observation.stream_index,
            observation.pts,
            observation.geometry,
            observation.category,
            observation.confidence_millionths,
            cast(Producer, object()),
        ),
        lambda: Observation.create(
            observation.source_id,
            observation.frame_id,
            observation.stream_index,
            cast(MediaTime, object()),
            observation.geometry,
            observation.category,
            observation.confidence_millionths,
            observation.producer,
        ),
        lambda: Observation.create(
            observation.source_id,
            observation.frame_id,
            observation.stream_index,
            observation.pts,
            cast(Geometry, object()),
            observation.category,
            observation.confidence_millionths,
            observation.producer,
        ),
        lambda: Observation.create(
            observation.source_id,
            observation.frame_id,
            observation.stream_index,
            observation.pts,
            observation.geometry,
            observation.category,
            observation.confidence_millionths,
            cast(Producer, object()),
        ),
        lambda: TrackPoint(
            tracklet.points[0].observation_id,
            tracklet.points[0].frame_id,
            cast(MediaTime, object()),
            tracklet.points[0].geometry,
        ),
        lambda: TrackPoint(
            tracklet.points[0].observation_id,
            tracklet.points[0].frame_id,
            tracklet.points[0].pts,
            cast(Geometry, object()),
        ),
        lambda: TrackPoint.from_observation(cast(Observation, object())),
        lambda: Tracklet.create(
            tracklet.source_id,
            tracklet.stream_index,
            tracklet.category,
            cast(tuple[TrackPoint, ...], list(tracklet.points)),
            tracklet.termination_reason,
            tracklet.producer,
        ),
        lambda: Tracklet.create(
            tracklet.source_id,
            tracklet.stream_index,
            tracklet.category,
            (cast(TrackPoint, object()),),
            tracklet.termination_reason,
            tracklet.producer,
        ),
        lambda: Tracklet.create(
            tracklet.source_id,
            tracklet.stream_index,
            tracklet.category,
            tracklet.points,
            tracklet.termination_reason,
            cast(Producer, object()),
        ),
    )
    for construct in invalid:
        with pytest.raises(RecordValidationError):
            construct()


def test_tracklet_rejects_duplicate_observations_frames_and_mixed_geometry() -> None:
    _, tracklet = records()
    first, second = tracklet.points
    producer = tracklet.producer

    duplicate_observation = TrackPoint(
        first.observation_id,
        second.frame_id,
        second.pts,
        second.geometry,
    )
    duplicate_frame = TrackPoint(
        second.observation_id,
        first.frame_id,
        second.pts,
        second.geometry,
    )
    mixed_geometry = TrackPoint(
        second.observation_id,
        second.frame_id,
        second.pts,
        Geometry(65, 48, (5, 5, 21, 30), "inferred"),
    )

    point_sets = (
        (first, duplicate_observation),
        (first, duplicate_frame),
        (first, mixed_geometry),
    )
    for points in point_sets:
        with pytest.raises(RecordValidationError):
            Tracklet.create(SOURCE_ID, 0, "vehicle", points, "source_end", producer)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: value.update({"pixels": "private"}), "unknown_field"),
        (lambda value: value.update({"confidence_millionths": 0.5}), "floating_point_forbidden"),
        (lambda value: value.update({"confidence_millionths": True}), "expected_integer"),
        (lambda value: value.update({"schema_version": 2}), "unknown_schema_version"),
        (lambda value: value.update({"observation_id": "obs_" + "0" * 64}), "identifier_mismatch"),
    ],
)
def test_strict_observation_ingress_rejects_hostile_values(
    mutate: Callable[[dict[str, object]], object], code: str
) -> None:
    value = records()[0].to_mapping()
    mutate(value)
    with pytest.raises(RecordValidationError) as raised:
        loads_perception_record(encode(value))
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: value.update({"entity_id": "ent_" + "0" * 64}), "unknown_field"),
        (
            lambda value: cast(dict[str, object], value["start_pts"]).update({"value": "1"}),
            "start_pts_mismatch",
        ),
        (
            lambda value: cast(dict[str, object], value["end_pts"]).update({"value": "1"}),
            "end_pts_mismatch",
        ),
        (lambda value: value.update({"tracklet_id": "trk_" + "0" * 64}), "identifier_mismatch"),
    ],
)
def test_strict_tracklet_ingress_rejects_persistent_identity_and_mismatches(
    mutate: Callable[[dict[str, object]], object], code: str
) -> None:
    value = records()[1].to_mapping()
    mutate(value)
    with pytest.raises(RecordValidationError) as raised:
        loads_perception_record(encode(value))
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b'{"schema":1,"schema":2}', "duplicate_key"),
        (b'{"schema":"visualworld.unknown","schema_version":1}', "unknown_schema"),
        (b"[]", "expected_object"),
    ],
)
def test_perception_json_dispatch_is_strict(payload: bytes, code: str) -> None:
    with pytest.raises(RecordValidationError) as raised:
        loads_perception_record(payload)
    assert raised.value.code == code


def test_perception_reader_and_trajectory_are_bounded() -> None:
    observation, tracklet = records()
    one = tracklet.points[0]

    with pytest.raises(RecordValidationError, match="too_many_track_points"):
        Tracklet.create(
            SOURCE_ID,
            0,
            "vehicle",
            tuple(
                TrackPoint(
                    "obs_" + f"{index:064x}",
                    "frm_" + f"{index:064x}",
                    MediaTime(str(index), TimeBase("1", "1000")),
                    one.geometry,
                )
                for index in range(MAX_TRACK_POINTS + 1)
            ),
            "source_end",
            tracklet.producer,
        )

    class TextReader:
        def read(self, _: int) -> str:
            return "not bytes"

    class BrokenReader:
        def read(self, _: int) -> bytes:
            raise OSError("private detail")

    class ChunkedReader:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload
            self.offset = 0

        def read(self, maximum: int) -> bytes:
            end = min(self.offset + 7, self.offset + maximum, len(self.payload))
            chunk = self.payload[self.offset : end]
            self.offset = end
            return chunk

    with pytest.raises(RecordValidationError, match="record_reader_must_be_binary"):
        load_perception_record(cast(BinaryIO, TextReader()))
    with pytest.raises(RecordValidationError, match="record_read_failed"):
        load_perception_record(cast(BinaryIO, BrokenReader()))
    with pytest.raises(RecordValidationError, match="unsupported_perception_record"):
        dumps_perception_record(cast(PerceptionRecord, object()))
    with pytest.raises(RecordValidationError, match="unsupported_perception_record"):
        identity_perception_bytes(cast(PerceptionRecord, object()))
    with pytest.raises(RecordValidationError, match="record_must_be_bytes"):
        loads_perception_record(cast(bytes, "not bytes"))

    encoded = dumps_perception_record(observation)
    assert load_perception_record(cast(BinaryIO, ChunkedReader(encoded))) == observation
