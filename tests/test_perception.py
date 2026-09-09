# SPDX-License-Identifier: Apache-2.0
"""Contract tests for v0.2 pixel-free perception records."""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
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
TRACKLET_ID = "trk_1dfe350c6e7f7fdd9304d6ce8be5f72a8343651787492e892856f1dff64f8703"
GOLDEN_ROOT = Path(__file__).parent / "goldens"
VEHICLE_GOLDENS = (
    (
        GOLDEN_ROOT / "perception-observation-v1.json",
        GOLDEN_ROOT / "perception-observation-identity-v1.json",
    ),
    (
        GOLDEN_ROOT / "perception-tracklet-v1.json",
        GOLDEN_ROOT / "perception-tracklet-identity-v1.json",
    ),
)


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

    for record, (record_golden, identity_golden) in zip(
        all_records,
        VEHICLE_GOLDENS,
        strict=True,
    ):
        encoded = dumps_perception_record(record)
        assert encoded == encode(record.to_mapping())
        assert loads_perception_record(encoded) == record
        assert load_perception_record(BytesIO(encoded)) == record
        assert identity_perception_bytes(record) == encode(record.identity_projection())
        assert encoded + b"\n" == record_golden.read_bytes()
        assert identity_perception_bytes(record) + b"\n" == identity_golden.read_bytes()

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


@pytest.mark.parametrize("category", ("animal", "traffic_light", "a" + "z" * 127))
def test_non_vehicle_categories_round_trip_through_all_perception_records(category: str) -> None:
    vehicle_observation, _ = records()
    observation = Observation.create(
        vehicle_observation.source_id,
        vehicle_observation.frame_id,
        vehicle_observation.stream_index,
        vehicle_observation.pts,
        vehicle_observation.geometry,
        category,
        vehicle_observation.confidence_millionths,
        vehicle_observation.producer,
    )
    point = TrackPoint.from_observation(observation)
    tracklet = Tracklet.create(
        observation.source_id,
        observation.stream_index,
        category,
        (point,),
        "source_end",
        Producer("visualworld.fake-tracker", "1", "55" * 32),
    )

    assert observation.category == point.category == tracklet.category == category
    assert observation.observation_id != vehicle_observation.observation_id
    assert loads_perception_record(dumps_perception_record(observation)) == observation
    assert loads_perception_record(dumps_perception_record(tracklet)) == tracklet


@pytest.mark.parametrize(
    "category",
    (
        "",
        "Animal",
        "animal-light",
        "animal/path",
        "animal space",
        "animal\n",
        "café",
        "a" * 129,
        "a\u0301",
    ),
)
def test_perception_categories_reject_invalid_tokens_in_all_record_paths(category: str) -> None:
    observation, tracklet = records()
    invalid_observation = {
        **observation.to_mapping(),
        "category": category,
    }
    invalid_tracklet = {
        **tracklet.to_mapping(),
        "category": category,
    }
    invalid_point_tracklet = {
        **tracklet.to_mapping(),
        "points": [
            {
                **tracklet.points[0].to_mapping(),
                "category": category,
            },
            *[point.to_mapping() for point in tracklet.points[1:]],
        ],
    }

    with pytest.raises(RecordValidationError):
        Observation.create(
            observation.source_id,
            observation.frame_id,
            observation.stream_index,
            observation.pts,
            observation.geometry,
            category,
            observation.confidence_millionths,
            observation.producer,
        )
    with pytest.raises(RecordValidationError):
        Observation(
            observation.observation_id,
            observation.source_id,
            observation.frame_id,
            observation.stream_index,
            observation.pts,
            observation.geometry,
            category,
            observation.confidence_millionths,
            observation.producer,
        )
    with pytest.raises(RecordValidationError):
        TrackPoint(
            observation.observation_id,
            observation.source_id,
            observation.frame_id,
            observation.stream_index,
            observation.pts,
            observation.geometry,
            category,
        )
    with pytest.raises(RecordValidationError):
        Tracklet(
            tracklet.tracklet_id,
            tracklet.source_id,
            tracklet.stream_index,
            category,
            tracklet.points,
            tracklet.termination_reason,
            tracklet.producer,
        )
    with pytest.raises(RecordValidationError):
        loads_perception_record(encode(invalid_observation))
    with pytest.raises(RecordValidationError):
        loads_perception_record(encode(invalid_tracklet))
    with pytest.raises(RecordValidationError):
        loads_perception_record(encode(invalid_point_tracklet))


@pytest.mark.parametrize(
    "invalid",
    [
        lambda: Observation.create(
            SOURCE_ID,
            FRAME_IDS[0],
            0,
            MediaTime("0", TimeBase("1", "1")),
            Geometry(2, 2, (0, 0, 1, 1), "inferred"),
            "Person",
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
            observation_id=tracklet.points[0].observation_id,
            source_id=tracklet.points[0].source_id,
            frame_id=tracklet.points[0].frame_id,
            stream_index=tracklet.points[0].stream_index,
            pts=cast(MediaTime, object()),
            geometry=tracklet.points[0].geometry,
            category=tracklet.points[0].category,
        ),
        lambda: TrackPoint(
            observation_id=tracklet.points[0].observation_id,
            source_id=tracklet.points[0].source_id,
            frame_id=tracklet.points[0].frame_id,
            stream_index=tracklet.points[0].stream_index,
            pts=tracklet.points[0].pts,
            geometry=cast(Geometry, object()),
            category=tracklet.points[0].category,
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
        observation_id=first.observation_id,
        source_id=second.source_id,
        frame_id=second.frame_id,
        stream_index=second.stream_index,
        pts=second.pts,
        geometry=second.geometry,
        category=second.category,
    )
    duplicate_frame = TrackPoint(
        observation_id=second.observation_id,
        source_id=second.source_id,
        frame_id=first.frame_id,
        stream_index=second.stream_index,
        pts=second.pts,
        geometry=second.geometry,
        category=second.category,
    )
    mixed_geometry = TrackPoint(
        observation_id=second.observation_id,
        source_id=second.source_id,
        frame_id=second.frame_id,
        stream_index=second.stream_index,
        pts=second.pts,
        geometry=Geometry(65, 48, (5, 5, 21, 30), "inferred"),
        category=second.category,
    )

    point_sets = (
        (first, duplicate_observation),
        (first, duplicate_frame),
        (first, mixed_geometry),
    )
    for points in point_sets:
        with pytest.raises(RecordValidationError):
            Tracklet.create(SOURCE_ID, 0, "vehicle", points, "source_end", producer)


def test_tracklet_points_are_self_verifying_for_source_stream_and_category() -> None:
    _, tracklet = records()
    first = tracklet.points[0]
    mismatches = (
        TrackPoint(
            first.observation_id,
            "src_" + "99" * 32,
            first.frame_id,
            first.stream_index,
            first.pts,
            first.geometry,
            first.category,
        ),
        TrackPoint(
            first.observation_id,
            first.source_id,
            first.frame_id,
            1,
            first.pts,
            first.geometry,
            first.category,
        ),
    )

    for point in mismatches:
        with pytest.raises(RecordValidationError, match="track_point_scope_mismatch"):
            Tracklet.create(
                tracklet.source_id,
                tracklet.stream_index,
                tracklet.category,
                (point,),
                tracklet.termination_reason,
                tracklet.producer,
            )


def test_perception_records_reject_hostile_subclasses_before_serialization() -> None:
    observation, _ = records()

    class PixelGeometry(Geometry):
        def to_mapping(self) -> dict[str, object]:
            return {**super().to_mapping(), "pixels": "private"}

    class DerivedTimeBase(TimeBase):
        pass

    with pytest.raises(RecordValidationError, match="invalid_geometry"):
        Observation.create(
            observation.source_id,
            observation.frame_id,
            observation.stream_index,
            observation.pts,
            PixelGeometry(64, 48, (4, 5, 20, 30), "inferred"),
            observation.category,
            observation.confidence_millionths,
            observation.producer,
        )
    with pytest.raises(RecordValidationError, match="invalid_time_base"):
        Observation.create(
            observation.source_id,
            observation.frame_id,
            observation.stream_index,
            MediaTime("0", DerivedTimeBase("1", "1000")),
            observation.geometry,
            observation.category,
            observation.confidence_millionths,
            observation.producer,
        )

    class DerivedObservation(Observation):
        pass

    hostile = DerivedObservation(
        observation.observation_id,
        observation.source_id,
        observation.frame_id,
        observation.stream_index,
        observation.pts,
        observation.geometry,
        observation.category,
        observation.confidence_millionths,
        observation.producer,
    )
    object.__setattr__(hostile, "vendor_payload", b"private pixels")
    with pytest.raises(RecordValidationError, match="unsupported_perception_record"):
        dumps_perception_record(cast(PerceptionRecord, hostile))


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


def test_perception_failures_do_not_echo_hostile_paths_or_exception_details() -> None:
    hostile_path = "private/path/secret"
    oversized = (f'{{"{hostile_path}":"' + "x" * 4_097 + '"}').encode()
    unknown = encode({"schema": "private/path", "schema_version": 1})
    with pytest.raises(RecordValidationError, match="string_too_long") as shape_error:
        loads_perception_record(oversized)
    with pytest.raises(RecordValidationError, match="unknown_schema") as schema_error:
        loads_perception_record(unknown)

    for raised in (shape_error.value, schema_error.value):
        rendered = "".join(traceback.format_exception(raised))
        assert hostile_path not in rendered
        assert "private/path" not in rendered
        assert raised.__cause__ is None
        assert raised.__context__ is None


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
                    observation_id="obs_" + f"{index:064x}",
                    source_id=one.source_id,
                    frame_id="frm_" + f"{index:064x}",
                    stream_index=one.stream_index,
                    pts=MediaTime(str(index), TimeBase("1", "1000")),
                    geometry=one.geometry,
                    category=one.category,
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
    with pytest.raises(RecordValidationError, match="record_read_failed") as read_error:
        load_perception_record(cast(BinaryIO, BrokenReader()))
    assert read_error.value.__cause__ is None
    assert read_error.value.__context__ is None
    assert "private detail" not in "".join(traceback.format_exception(read_error.value))
    with pytest.raises(RecordValidationError, match="unsupported_perception_record"):
        dumps_perception_record(cast(PerceptionRecord, object()))
    with pytest.raises(RecordValidationError, match="unsupported_perception_record"):
        identity_perception_bytes(cast(PerceptionRecord, object()))
    with pytest.raises(RecordValidationError, match="record_must_be_bytes"):
        loads_perception_record(cast(bytes, "not bytes"))

    encoded = dumps_perception_record(observation)
    assert load_perception_record(cast(BinaryIO, ChunkedReader(encoded))) == observation
