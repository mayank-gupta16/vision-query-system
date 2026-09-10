# SPDX-License-Identifier: Apache-2.0
"""Contract, geometry, boundary, and hostile-output tests for v0.2 detection."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import platform
import runpy
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from visualworld import detection
from visualworld.detection import (
    DETECTOR_CONFIDENCE_FLOOR_MILLIONTHS,
    DETECTOR_CONFIGURATION_SHA256,
    DETECTOR_COORDINATE_SCALE,
    DETECTOR_INPUT_HEIGHT,
    DETECTOR_INPUT_WIDTH,
    DETECTOR_MANIFEST_SHA256,
    DETECTOR_MODEL_BIN_SHA256,
    DETECTOR_MODEL_XML_SHA256,
    DETECTOR_PRODUCER,
    DETECTOR_RUNTIME_CLOSURE_SHA256,
    DETECTOR_RUNTIME_ID,
    DETECTOR_WORKER_SHA256,
    DetectionProvenance,
    FixturePerceptionWorker,
    IsolatedPerceptionWorker,
    OpenVinoVehicleDetector,
    PerceptionLimits,
    PerceptionRuntime,
    PerceptionWorker,
    PerceptionWorkerResult,
    WorkerDetection,
    WorkerFrame,
)
from visualworld.ingestion import Fingerprint, FrameRef, MediaTime, Source, SourceStream, TimeBase
from visualworld.media import MediaRuntime
from visualworld.ports import (
    DetectionResult,
    Detector,
    FakeDetector,
    PerceptionResultState,
    PortCall,
    PortError,
    PortErrorCode,
    PortKind,
)


def _source(
    *,
    digest: str = "aa" * 32,
    source_bytes: int = 1000,
    width: int = 640,
    height: int = 360,
    rotation: int = 0,
) -> Source:
    return Source.create(
        Fingerprint(digest, str(source_bytes)),
        (SourceStream(0, width, height, rotation, TimeBase("1", "1000")),),
    )


def _frames(source: Source, count: int = 2) -> tuple[FrameRef, ...]:
    time_base = source.streams[0].time_base
    return tuple(
        FrameRef.create(
            source.source_id,
            0,
            str(index),
            MediaTime(str(index * 200), time_base),
            MediaTime("40", time_base),
            index == 0,
        )
        for index in range(count)
    )


def _normalized_box(
    box: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    left, top, right, bottom = box
    return (
        left * DETECTOR_COORDINATE_SCALE // DETECTOR_INPUT_WIDTH,
        top * DETECTOR_COORDINATE_SCALE // DETECTOR_INPUT_HEIGHT,
        (right * DETECTOR_COORDINATE_SCALE + DETECTOR_INPUT_WIDTH - 1) // DETECTOR_INPUT_WIDTH,
        (bottom * DETECTOR_COORDINATE_SCALE + DETECTOR_INPUT_HEIGHT - 1) // DETECTOR_INPUT_HEIGHT,
    )


def _worker_result(
    source: Source,
    frames: tuple[FrameRef, ...],
    detections: tuple[tuple[WorkerDetection, ...], ...] | None = None,
) -> PerceptionWorkerResult:
    stream = source.streams[0]
    selected = ((),) * len(frames) if detections is None else detections
    return PerceptionWorkerResult(
        DetectionProvenance(source.fingerprint.digest, int(source.fingerprint.bytes)),
        stream.stream_index,
        stream.width,
        stream.height,
        stream.rotation_degrees % 360,
        stream.time_base,
        tuple(
            WorkerFrame(
                frame.decode_index,
                frame.pts,
                frame.duration,
                bool(frame.key_frame),
                frame_detections,
            )
            for frame, frame_detections in zip(frames, selected, strict=True)
        ),
    )


def _worker_payload(source: Source, frames: tuple[FrameRef, ...]) -> dict[str, object]:
    stream = source.streams[0]

    def worker_time(value: MediaTime | None) -> dict[str, object] | None:
        if value is None:
            return None
        return {"time_base": value.time_base.to_mapping(), "value": value.value}

    return {
        "frames": [
            {
                "decode_index": frame.decode_index,
                "detections": [
                    {
                        "box_normalized_millionths": [250_000, 250_000, 500_000, 500_000],
                        "confidence_millionths": 975_000,
                    }
                ],
                "duration": worker_time(frame.duration),
                "key_frame": bool(frame.key_frame),
                "pts": worker_time(frame.pts),
            }
            for frame in sorted(frames, key=lambda item: int(item.decode_index))
        ],
        "isolation": {
            "landlock_denied": True,
            "network_denied": True,
            "no_new_privileges": True,
            "non_root": True,
        },
        "runtime": {
            "model_bin_sha256": DETECTOR_MODEL_BIN_SHA256,
            "model_xml_sha256": DETECTOR_MODEL_XML_SHA256,
            "numpy": "2.5.3",
            "openvino": "2026.3.1-22476-759c5a6ab8c-releases/2026/3",
            "pyav": "18.1.0",
            "runtime_id": DETECTOR_RUNTIME_ID,
            "telemetry": "2025.2.0",
            "worker_sha256": DETECTOR_WORKER_SHA256,
        },
        "schema_version": 1,
        "source": {
            "bytes": int(source.fingerprint.bytes),
            "sha256": source.fingerprint.digest,
        },
        "status": "ok",
        "stream": {
            "height": stream.height,
            "rotation_degrees": stream.rotation_degrees % 360,
            "stream_index": stream.stream_index,
            "time_base": stream.time_base.to_mapping(),
            "width": stream.width,
        },
    }


def _run(
    payload: object,
    *,
    returncode: int = 0,
    stderr: bytes = b'{"schema_version":1,"status":"ok"}\n',
) -> detection._WorkerRun:
    stdout = (
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if not isinstance(payload, bytes)
        else payload
    )
    return detection._WorkerRun(stdout, stderr, returncode, 12, 4, 1024)


def test_detector_contract_fake_substitution_and_exact_provenance() -> None:
    source = _source()
    frames = _frames(source)
    worker = FixturePerceptionWorker(
        _worker_result(
            source,
            frames,
            (
                (WorkerDetection((250_000, 250_000, 500_000, 500_000), 975_000),),
                (),
            ),
        )
    )
    adapter = OpenVinoVehicleDetector(worker)

    result = adapter.detect(source, frames)

    assert isinstance(adapter, Detector)
    assert isinstance(worker, PerceptionWorker)
    assert result.state is PerceptionResultState.COMPLETE
    assert len(result.observations) == 1
    observation = result.observations[0]
    assert observation.frame_id == frames[0].frame_id
    assert observation.category == "vehicle"
    assert observation.confidence_millionths == 975_000
    assert observation.geometry.box_xyxy == (160, 90, 320, 180)
    assert observation.producer == DETECTOR_PRODUCER
    assert adapter.descriptor.implementation == "visualworld.openvino-vehicle-detector"
    assert adapter.descriptor.deterministic is True
    assert adapter.descriptor.offline is True
    assert adapter.descriptor.allowed_effects == ()
    assert adapter.calls == (PortCall(PortKind.DETECTOR, "detect", 2),)
    assert worker.calls == 1
    assert FakeDetector(result).detect(source, frames) == result

    provenance = adapter.provenance(source)
    assert provenance.source_sha256 == source.fingerprint.digest
    assert provenance.source_bytes == 1000
    assert provenance.configuration_sha256 == DETECTOR_CONFIGURATION_SHA256
    assert provenance.confidence_floor_millionths == 950_000
    assert provenance.model_xml_sha256 == DETECTOR_MODEL_XML_SHA256
    assert provenance.model_bin_sha256 == DETECTOR_MODEL_BIN_SHA256
    assert provenance.runtime_id == DETECTOR_RUNTIME_ID
    assert provenance.runtime_closure_sha256 == DETECTOR_RUNTIME_CLOSURE_SHA256
    assert provenance.worker_sha256 == DETECTOR_WORKER_SHA256
    assert provenance.perception_manifest_sha256 == DETECTOR_MANIFEST_SHA256


def test_detector_outputs_own_nested_time_and_producer_values() -> None:
    source = _source()
    frames = _frames(source, 1)
    worker = FixturePerceptionWorker(
        _worker_result(
            source,
            frames,
            ((WorkerDetection((250_000, 250_000, 500_000, 500_000), 975_000),),),
        )
    )
    adapter = OpenVinoVehicleDetector(worker)

    first = adapter.detect(source, frames).observations[0]
    assert first.pts is not frames[0].pts
    assert first.pts.time_base is not frames[0].pts.time_base
    assert first.producer is not DETECTOR_PRODUCER
    object.__setattr__(first.pts, "value", "/private/changed-time")
    object.__setattr__(first.pts.time_base, "denominator", "/private/changed-base")
    object.__setattr__(first.producer, "name", "/private/changed-producer")
    published_producer = adapter.producer
    object.__setattr__(published_producer, "name", "/private/changed-property")

    second = adapter.detect(source, frames).observations[0]

    assert frames[0].pts.value == "0"
    assert frames[0].pts.time_base.denominator == "1000"
    assert DETECTOR_PRODUCER.name == "visualworld.openvino-vehicle-detector"
    assert second.pts == frames[0].pts
    assert second.pts is not frames[0].pts
    assert second.producer == DETECTOR_PRODUCER
    assert second.producer is not first.producer
    assert adapter.producer == DETECTOR_PRODUCER
    assert adapter.producer is not published_producer
    assert adapter.descriptor.implementation == "visualworld.openvino-vehicle-detector"


def test_frozen_detector_configuration_and_worker_digest_are_exact() -> None:
    root = Path(__file__).resolve().parents[1]
    assert DETECTOR_INPUT_WIDTH == DETECTOR_INPUT_HEIGHT == 384
    assert DETECTOR_COORDINATE_SCALE == 1_000_000
    assert DETECTOR_CONFIDENCE_FLOOR_MILLIONTHS == 950_000
    assert (
        DETECTOR_CONFIGURATION_SHA256
        == "cff8a2f9ec2dfad146ba56d0e7f88c2f8e8df18b4195ba4b1cc0a170703f9b99"
    )
    assert hashlib.sha256((root / "workers/perception_worker.py").read_bytes()).hexdigest() == (
        DETECTOR_WORKER_SHA256
    )
    assert (
        hashlib.sha256((root / "workers/perception-runtime-v1.json").read_bytes()).hexdigest()
        == DETECTOR_MANIFEST_SHA256
    )


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, (0, 0, 320, 180)),
        (90, (0, 180, 320, 360)),
        (180, (320, 180, 640, 360)),
        (270, (320, 0, 640, 180)),
    ],
)
def test_generated_rotation_goldens_map_to_encoded_source(
    rotation: int,
    expected: tuple[int, int, int, int],
) -> None:
    source = _source(rotation=rotation)
    frames = _frames(source, 1)
    adapter = OpenVinoVehicleDetector(
        FixturePerceptionWorker(
            _worker_result(
                source,
                frames,
                ((WorkerDetection((0, 0, 500_000, 500_000), 950_000),),),
            )
        )
    )

    geometry = adapter.detect(source, frames).observations[0].geometry

    assert geometry.box_xyxy == expected
    assert geometry.source_width == 640
    assert geometry.source_height == 360
    assert geometry.producer_space is not None
    assert geometry.producer_space.width == DETECTOR_COORDINATE_SCALE
    assert geometry.producer_space.height == DETECTOR_COORDINATE_SCALE
    assert geometry.producer_space.box_xyxy == (0, 0, 500_000, 500_000)
    assert geometry.transform_kind == "affine_rational"
    assert geometry.measurement == "inferred"


@pytest.mark.parametrize(
    ("width", "height", "box", "expected"),
    [
        (641, 359, (1, 1, 383, 383), (1, 0, 640, 359)),
        (1279, 721, (48, 64, 337, 289), (159, 120, 1123, 543)),
        (17, 13, (17, 23, 301, 355), (0, 0, 14, 13)),
    ],
)
def test_generated_resize_goldens_have_no_letterbox_or_silent_clipping(
    width: int,
    height: int,
    box: tuple[int, int, int, int],
    expected: tuple[int, int, int, int],
) -> None:
    source = _source(width=width, height=height)
    frames = _frames(source, 1)
    adapter = OpenVinoVehicleDetector(
        FixturePerceptionWorker(
            _worker_result(source, frames, ((WorkerDetection(_normalized_box(box), 999_999),),))
        )
    )
    geometry = adapter.detect(source, frames).observations[0].geometry

    assert geometry.box_xyxy == expected
    assert geometry.coefficients is not None
    assert geometry.coefficients.b.numerator == "0"
    assert geometry.coefficients.c.numerator == "0"
    assert geometry.coefficients.d.numerator == "0"
    assert geometry.coefficients.f.numerator == "0"


def _outward_scaled(value: int, extent: int, *, upper: bool) -> int:
    numerator = value * extent
    return (
        (numerator + DETECTOR_COORDINATE_SCALE - 1) // DETECTOR_COORDINATE_SCALE
        if upper
        else (numerator // DETECTOR_COORDINATE_SCALE)
    )


def _independent_source_box(
    box: tuple[int, int, int, int],
    width: int,
    height: int,
    rotation: int,
) -> tuple[int, int, int, int]:
    left, top, right, bottom = box
    if rotation == 0:
        return (
            _outward_scaled(left, width, upper=False),
            _outward_scaled(top, height, upper=False),
            _outward_scaled(right, width, upper=True),
            _outward_scaled(bottom, height, upper=True),
        )
    if rotation == 90:
        return (
            _outward_scaled(top, width, upper=False),
            height - _outward_scaled(right, height, upper=True),
            _outward_scaled(bottom, width, upper=True),
            height - _outward_scaled(left, height, upper=False),
        )
    if rotation == 180:
        return (
            width - _outward_scaled(right, width, upper=True),
            height - _outward_scaled(bottom, height, upper=True),
            width - _outward_scaled(left, width, upper=False),
            height - _outward_scaled(top, height, upper=False),
        )
    return (
        width - _outward_scaled(bottom, width, upper=True),
        _outward_scaled(left, height, upper=False),
        width - _outward_scaled(top, width, upper=False),
        _outward_scaled(right, height, upper=True),
    )


def test_generated_pixel_goldens_match_independent_quarter_turn_oracle() -> None:
    dimensions = ((17, 13), (641, 359), (1920, 1080), (4095, 2161))
    boxes = (
        (0, 0, 1, 1),
        (1, 2, 383, 382),
        (17, 29, 203, 311),
        (191, 191, 384, 384),
    )
    for width, height in dimensions:
        for rotation in (0, 90, 180, 270):
            source = _source(width=width, height=height, rotation=rotation)
            frames = _frames(source, 1)
            for box in boxes:
                normalized_box = _normalized_box(box)
                adapter = OpenVinoVehicleDetector(
                    FixturePerceptionWorker(
                        _worker_result(
                            source,
                            frames,
                            ((WorkerDetection(normalized_box, 950_000),),),
                        )
                    )
                )

                actual = adapter.detect(source, frames).observations[0].geometry.box_xyxy
                expected = _independent_source_box(normalized_box, width, height, rotation)

                assert all(
                    abs(observed - golden) <= 1
                    for observed, golden in zip(actual, expected, strict=True)
                )
                assert actual == expected


@pytest.mark.parametrize("width", [1920, 4095])
def test_normalized_model_coordinates_round_only_once_at_source_scale(width: int) -> None:
    source = _source(width=width, height=2161)
    frames = _frames(source, 1)
    raw_coordinates = (
        0.019055500626564026,
        0.203_456_789,
        0.701_234_567,
        0.812_345_678,
    )
    worker = _worker_namespace()
    parse_detections = cast(Any, worker["_detections"])
    worker_values = parse_detections(
        _SyntheticModelOutput([[0.0, 0.0, 0.95, *raw_coordinates]]),
        1,
    )
    normalized = tuple(worker_values[0]["box_normalized_millionths"])
    output = _worker_result(
        source,
        frames,
        (
            (
                WorkerDetection(
                    cast(tuple[int, int, int, int], normalized),
                    950_000,
                ),
            ),
        ),
    )

    actual = (
        OpenVinoVehicleDetector(FixturePerceptionWorker(output))
        .detect(source, frames)
        .observations[0]
        .geometry.box_xyxy
    )
    ideal = (
        math.floor(raw_coordinates[0] * width),
        math.floor(raw_coordinates[1] * 2161),
        math.ceil(raw_coordinates[2] * width),
        math.ceil(raw_coordinates[3] * 2161),
    )

    assert all(
        abs(observed - expected) <= 1 for observed, expected in zip(actual, ideal, strict=True)
    )
    assert actual[0] >= ideal[0] - 1


class _SyntheticModelOutput:
    def __init__(self, rows: list[list[float]]) -> None:
        self.shape = (1, 1, len(rows), 7)
        self.size = len(rows) * 7
        self._rows = rows

    def reshape(self, _shape: tuple[int, int]) -> list[list[float]]:
        return self._rows


def _worker_namespace() -> dict[str, object]:
    worker = Path(__file__).resolve().parents[1] / "workers/perception_worker.py"
    return runpy.run_path(os.fspath(worker))


def test_worker_detection_filter_matches_exact_reviewed_threshold_and_class() -> None:
    worker = _worker_namespace()
    detect = cast(Any, worker["_detections"])
    output = _SyntheticModelOutput(
        [
            [0.0, 0.0, 0.95, 0.25, 0.25, 0.5, 0.5],
            [0.0, 1.0, 0.999, 0.0, 0.0, 1.0, 1.0],
            [0.0, 0.0, 0.949_999, 0.0, 0.0, 1.0, 1.0],
            [0.0, 0.0, 1.1, -0.5, 0.5, 1.5, 2.0],
            [-1.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0],
            [0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0],
        ]
    )

    assert detect(output, 2) == [
        {
            "box_normalized_millionths": [0, 500_000, 1_000_000, 1_000_000],
            "confidence_millionths": 1_000_000,
        },
        {
            "box_normalized_millionths": [250_000, 250_000, 500_000, 500_000],
            "confidence_millionths": 950_000,
        },
    ]

    with pytest.raises(cast(type[BaseException], worker["_LimitExceeded"])):
        detect(output, 1)


@pytest.mark.skipif(
    sys.platform != "linux" or platform.machine() != "x86_64",
    reason="the approved perception seccomp profile is Linux x86_64 only",
)
def test_worker_seccomp_allows_inference_threads_but_denies_network_and_unshare() -> None:
    worker = Path(__file__).resolve().parents[1] / "workers/perception_worker.py"
    code = """
import ctypes
import errno
import json
import runpy
import socket
import sys
import threading

namespace = runpy.run_path(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)
namespace["_no_new_privileges"](libc)
namespace["_apply_seccomp"](libc)
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM)
except OSError as error:
    network_denied = error.errno == errno.EPERM
else:
    network_denied = False
completed = []
thread = threading.Thread(target=lambda: completed.append(True))
thread.start()
thread.join()
ctypes.set_errno(0)
unshare_result = libc.syscall(272, 0x10000000)
unshare_denied = unshare_result == -1 and ctypes.get_errno() == errno.EPERM
result = {
    "network_denied": network_denied,
    "thread_completed": completed == [True],
    "unshare_denied": unshare_denied,
}
print(json.dumps(result, sort_keys=True))
"""

    completed = subprocess.run(
        [sys.executable, "-c", code, os.fspath(worker)],
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {
        "network_denied": True,
        "thread_completed": True,
        "unshare_denied": True,
    }
    assert completed.stderr == b""


def test_empty_and_unsupported_batches_do_not_invoke_native_worker() -> None:
    source = _source()
    frames = _frames(source, 1)

    class UnsupportedWorker:
        supported = False

        def infer(self, _source: Source, _frames: tuple[FrameRef, ...]) -> PerceptionWorkerResult:
            raise AssertionError("must not run")

    adapter = OpenVinoVehicleDetector(cast(PerceptionWorker, UnsupportedWorker()))
    assert adapter.detect(source, ()).state is PerceptionResultState.COMPLETE
    unsupported = adapter.detect(source, frames)
    assert unsupported == DetectionResult(
        PerceptionResultState.UNSUPPORTED,
        reason="platform_unsupported",
    )
    assert adapter.calls == (
        PortCall(PortKind.DETECTOR, "detect", 0),
        PortCall(PortKind.DETECTOR, "detect", 1),
    )


def test_valid_multi_stream_batch_is_explicitly_unsupported() -> None:
    first = SourceStream(0, 640, 360, 0, TimeBase("1", "1000"))
    second = SourceStream(1, 320, 240, 0, TimeBase("1", "1000"))
    source = Source.create(Fingerprint("bb" * 32, "1000"), (first, second))
    frames = (
        FrameRef.create(source.source_id, 0, "0", MediaTime("0", first.time_base)),
        FrameRef.create(source.source_id, 1, "0", MediaTime("0", second.time_base)),
    )
    placeholder = _worker_result(_source(), _frames(_source(), 1))
    worker = FixturePerceptionWorker(placeholder)
    adapter = OpenVinoVehicleDetector(worker)

    result = adapter.detect(source, frames)

    assert result.state is PerceptionResultState.UNSUPPORTED
    assert result.reason == "multi_stream_batch_unsupported"
    assert worker.calls == 0


def test_worker_output_parser_builds_only_bounded_pixel_free_values() -> None:
    source = _source()
    frames = _frames(source)

    result = detection._decode_worker_output(
        _run(_worker_payload(source, frames)),
        source,
        frames,
        source.fingerprint.digest,
        int(source.fingerprint.bytes),
        PerceptionLimits(),
    )

    assert result.provenance == DetectionProvenance(source.fingerprint.digest, 1000)
    assert tuple(item.decode_index for item in result.frames) == ("0", "1")
    assert result.frames[0].detections == (
        WorkerDetection((250_000, 250_000, 500_000, 500_000), 975_000),
    )
    rendered = repr(result)
    assert "pixel" not in rendered.lower()
    assert "source.mov" not in rendered


def _invalid_payloads() -> list[object]:
    source = _source()
    frames = _frames(source)
    payloads: list[object] = []

    def changed(mutator: Any) -> None:
        value = copy.deepcopy(_worker_payload(source, frames))
        mutator(value)
        payloads.append(value)

    changed(lambda item: cast(dict[str, object], item).update(schema_version=True))
    changed(lambda item: cast(dict[str, object], item).update(secret="private"))
    changed(
        lambda item: cast(dict[str, object], cast(dict[str, object], item)["source"]).update(
            sha256="bb" * 32
        )
    )
    changed(
        lambda item: cast(dict[str, object], cast(dict[str, object], item)["runtime"]).update(
            openvino="2027.0.0"
        )
    )
    changed(
        lambda item: cast(dict[str, object], cast(dict[str, object], item)["isolation"]).update(
            network_denied=False
        )
    )
    changed(
        lambda item: cast(dict[str, object], cast(dict[str, object], item)["stream"]).update(
            width=641
        )
    )
    changed(
        lambda item: cast(
            dict[str, object], cast(list[object], cast(dict[str, object], item)["frames"])[0]
        ).update(decode_index="1")
    )
    changed(
        lambda item: cast(
            dict[str, object],
            cast(
                list[object],
                cast(
                    dict[str, object],
                    cast(list[object], cast(dict[str, object], item)["frames"])[0],
                )["detections"],
            )[0],
        ).update(box_normalized_millionths=[1_000_000, 0, 1_000_000, 10])
    )
    changed(
        lambda item: cast(
            dict[str, object],
            cast(
                list[object],
                cast(
                    dict[str, object],
                    cast(list[object], cast(dict[str, object], item)["frames"])[0],
                )["detections"],
            )[0],
        ).update(confidence_millionths=949_999)
    )
    payloads.extend(
        [
            b"not-json\n",
            b'{"frames":[]}\n',
            b"[" * 2000 + b"0" + b"]" * 2000,
        ]
    )
    return payloads


@pytest.mark.parametrize("payload", _invalid_payloads())
def test_hostile_worker_output_fails_closed_and_redacted(payload: object) -> None:
    source = _source()
    frames = _frames(source)

    with pytest.raises(PortError) as raised:
        detection._decode_worker_output(
            _run(payload),
            source,
            frames,
            source.fingerprint.digest,
            int(source.fingerprint.bytes),
            PerceptionLimits(),
        )

    assert raised.value.code in {
        PortErrorCode.DECODE_FAILED,
        PortErrorCode.ISOLATION_UNAVAILABLE,
    }
    assert raised.value.port is PortKind.DETECTOR
    assert "private" not in str(raised.value)
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (20, PortErrorCode.LIMIT_EXCEEDED),
        (21, PortErrorCode.DECODE_FAILED),
        (22, PortErrorCode.ISOLATION_UNAVAILABLE),
        (127, PortErrorCode.DECODE_FAILED),
    ],
)
def test_worker_exit_codes_are_structured(returncode: int, expected: PortErrorCode) -> None:
    source = _source()
    frames = _frames(source)
    with pytest.raises(PortError) as raised:
        detection._decode_worker_output(
            _run(b"private backend failure", returncode=returncode, stderr=b"private path"),
            source,
            frames,
            source.fingerprint.digest,
            1000,
            PerceptionLimits(),
        )
    assert raised.value.code is expected
    assert "private" not in str(raised.value)


def test_noncanonical_unexpected_stderr_and_oversized_output_are_rejected() -> None:
    source = _source()
    frames = _frames(source)
    payload = _worker_payload(source, frames)
    limits = PerceptionLimits(max_stdout_bytes=8192)
    cases = (
        (_run(json.dumps(payload).encode()), PortErrorCode.DECODE_FAILED),
        (_run(payload, stderr=b"private warning\n"), PortErrorCode.DECODE_FAILED),
        (_run(b"x" * 8193), PortErrorCode.LIMIT_EXCEEDED),
    )
    for run, expected in cases:
        with pytest.raises(PortError) as raised:
            detection._decode_worker_output(run, source, frames, "aa" * 32, 1000, limits)
        assert raised.value.code is expected
        assert "private" not in str(raised.value)


def test_backend_exceptions_and_forged_results_never_publish_calls_or_details() -> None:
    source = _source()
    frames = _frames(source, 1)

    class BrokenWorker:
        supported = True

        def infer(self, _source: Source, _frames: tuple[FrameRef, ...]) -> PerceptionWorkerResult:
            raise OSError("/private/source.mov contains secret pixels")

    broken = OpenVinoVehicleDetector(cast(PerceptionWorker, BrokenWorker()))
    with pytest.raises(PortError) as raised:
        broken.detect(source, frames)
    assert raised.value.code is PortErrorCode.DECODE_FAILED
    assert "private" not in str(raised.value)
    assert raised.value.__context__ is None
    assert broken.calls == ()

    forged = _worker_result(source, frames)
    object.__setattr__(forged, "width", 1)

    class ForgedWorker:
        supported = True

        def infer(self, _source: Source, _frames: tuple[FrameRef, ...]) -> PerceptionWorkerResult:
            return forged

    adapter = OpenVinoVehicleDetector(cast(PerceptionWorker, ForgedWorker()))
    with pytest.raises(PortError, match="decode_failed"):
        adapter.detect(source, frames)
    assert adapter.calls == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"max_source_bytes": 0},
        {"max_source_bytes": 2**30 + 1},
        {"max_detections_per_frame": 65},
        {"max_stdout_bytes": 16 * 1024 * 1024 + 1},
        {"max_stderr_bytes": 1024 * 1024 + 1},
        {"wall_timeout_ms": 300_001},
        {"memory_bytes": 2 * 1024 * 1024 * 1024 + 1},
        {"task_count": 1025},
        {"wall_timeout_ms": True},
    ],
)
def test_perception_limits_are_bounded(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="perception limit"):
        PerceptionLimits(**cast(dict[str, int], changes))


def test_namespace_and_systemd_arguments_are_fixed_source_opaque_and_offline(
    tmp_path: Path,
) -> None:
    runtime = PerceptionRuntime(
        Path("/opt/visualworld-perception-v1"),
        MediaRuntime(
            Path("/opt/visualworld-media-v2"),
            Path("/opt/visualworld-media-v2/worker/media_worker.py"),
        ),
    )
    limits = PerceptionLimits()
    namespace = detection._namespace_argv(runtime, limits, (0, 7))
    systemd = detection._systemd_argv(runtime, limits, 19, "visualworld-perception-test", (0, 7))

    assert namespace[0] == "/usr/bin/bwrap"
    assert "--unshare-net" in namespace
    assert "--disable-userns" in namespace
    assert "--clearenv" in namespace
    assert "OPENVINO_TELEMETRY_CONSENT" in namespace
    assert "NO" in namespace
    assert "/perception-runtime/worker/perception_worker.py" in namespace
    assert "/media-runtime/venv/lib/python3.13/site-packages" in ":".join(namespace)
    assert "0,7" in namespace
    assert not any("http://" in item or "https://" in item or "rtsp://" in item for item in systemd)
    assert not any(os.fspath(tmp_path) in item for item in systemd)
    assert any("OpenFile=/proc/" in item and "/fd/19:" in item for item in systemd)
    assert "--property=KillMode=control-group" in systemd
    assert "--property=CPUQuota=400%" in systemd
    assert "--property=MemoryMax=2147483648" in systemd
    assert "--property=TasksMax=1024" in systemd
    assert systemd[0] == "/usr/bin/systemd-run"


def _child(code: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _kill_process(_unit: str, _cgroup: Path, process: subprocess.Popen[bytes]) -> bool:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    return True


def test_supervisor_drains_stdout_and_stderr_with_independent_bounds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(detection, "_CGROUP_ROOT", tmp_path)
    process = _child("import sys;sys.stdout.buffer.write(b'ok');sys.stderr.buffer.write(b'fine')")
    result = detection._drain_worker(process, "test", PerceptionLimits(), None)
    assert result.stdout == b"ok"
    assert result.stderr == b"fine"
    assert result.returncode == 0


@pytest.mark.parametrize("failure", ["stdout", "stderr", "timeout", "cancel"])
def test_supervisor_kills_and_cleans_the_whole_process_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    monkeypatch.setattr(detection, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(detection, "_kill_unit", _kill_process)
    monkeypatch.setattr(detection, "_wait_unit_stopped", lambda *_args: True)
    cancelled = threading.Event()
    if failure in {"stdout", "stderr"}:
        stream = "stdout" if failure == "stdout" else "stderr"
        process = _child(
            f"import sys,time;sys.{stream}.buffer.write(b'x'*4096);"
            f"sys.{stream}.flush();time.sleep(1)"
        )
        limits = PerceptionLimits(max_stdout_bytes=16, max_stderr_bytes=16)
        expected = PortErrorCode.LIMIT_EXCEEDED
    else:
        process = _child("import time;time.sleep(1)")
        limits = PerceptionLimits(wall_timeout_ms=10)
        expected = PortErrorCode.TIMEOUT
        if failure == "cancel":
            cancelled.set()
            limits = PerceptionLimits()
            expected = PortErrorCode.CANCELLED
    with pytest.raises(PortError) as raised:
        detection._drain_worker(process, "test", limits, cancelled)
    assert raised.value.code is expected
    assert process.poll() is not None


def test_supervisor_reads_cancellation_without_overridable_method(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(detection, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(detection, "_kill_unit", _kill_process)
    monkeypatch.setattr(detection, "_wait_unit_stopped", lambda *_args: True)
    cancelled = threading.Event()
    cancelled.set()

    def poisoned_method() -> bool:
        raise KeyboardInterrupt("/private/cancel-token")

    object.__setattr__(cancelled, "is_set", poisoned_method)
    process = _child("import time;time.sleep(1)")

    with pytest.raises(PortError) as raised:
        detection._drain_worker(process, "test", PerceptionLimits(), cancelled)

    assert raised.value.code is PortErrorCode.CANCELLED
    assert raised.value.__context__ is None
    assert "/private" not in str(raised.value)
    assert process.poll() is not None


def test_supervisor_rejects_hostile_cancellation_flag_without_boolean_coercion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class HostileFlag:
        def __bool__(self) -> bool:
            raise KeyboardInterrupt("/private/cancel-flag")

    monkeypatch.setattr(detection, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(detection, "_kill_unit", _kill_process)
    monkeypatch.setattr(detection, "_wait_unit_stopped", lambda *_args: True)
    cancelled = threading.Event()
    object.__setattr__(cancelled, "_flag", HostileFlag())
    process = _child("import time;time.sleep(1)")

    with pytest.raises(PortError) as raised:
        detection._drain_worker(process, "test", PerceptionLimits(), cancelled)

    assert raised.value.code is PortErrorCode.ISOLATION_UNAVAILABLE
    assert raised.value.__context__ is None
    assert "/private" not in str(raised.value)
    assert process.poll() is not None


def test_production_and_fixture_workers_are_adapter_substitutable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.mov"
    source_path.write_bytes(b"sealed-source")
    digest = hashlib.sha256(b"sealed-source").hexdigest()
    source = _source(digest=digest, source_bytes=len(b"sealed-source"))
    frames = _frames(source)
    payload = _worker_payload(source, frames)
    runtime = PerceptionRuntime(
        Path("/opt/perception"),
        MediaRuntime(Path("/opt/media"), Path("/opt/media/worker/media_worker.py")),
    )
    monkeypatch.setattr(detection, "_supported_platform", lambda: True)
    monkeypatch.setattr(detection, "_verify_media_boundary", lambda _runtime, **_kwargs: None)
    monkeypatch.setattr(detection, "_verify_perception_boundary", lambda _runtime, **_kwargs: None)
    monkeypatch.setattr(
        detection,
        "_open_source",
        lambda _root, _relative, _maximum, **_kwargs: (
            os.open(source_path, os.O_RDONLY),
            source_path.stat(),
        ),
    )

    def snapshot(source_fd: int, _maximum: int, **_kwargs: object) -> tuple[int, str, int]:
        content = os.read(source_fd, 1024)
        return os.dup(source_fd), hashlib.sha256(content).hexdigest(), len(content)

    monkeypatch.setattr(detection, "_sealed_snapshot", snapshot)
    monkeypatch.setattr(detection, "_run_worker", lambda *_args: _run(payload))
    production_worker = IsolatedPerceptionWorker(tmp_path, "source.mov", runtime)
    production = OpenVinoVehicleDetector(production_worker).detect(source, frames)
    fixture = OpenVinoVehicleDetector(
        FixturePerceptionWorker(
            detection._decode_worker_output(
                _run(payload),
                source,
                frames,
                digest,
                len(b"sealed-source"),
                PerceptionLimits(),
            )
        )
    ).detect(source, frames)

    assert production == fixture
    assert production.state is PerceptionResultState.COMPLETE
    assert len(production.observations) == 2


def test_production_worker_snapshots_runtime_and_limits_before_verify_and_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.mov"
    content = b"sealed-source"
    source_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    source = _source(digest=digest, source_bytes=len(content))
    frames = _frames(source, 1)
    media = MediaRuntime(Path("/opt/media"), Path("/opt/media/worker/media_worker.py"))
    runtime = PerceptionRuntime(Path("/opt/perception"), media)
    limits = PerceptionLimits(max_source_bytes=4096, wall_timeout_ms=12_345)
    worker = IsolatedPerceptionWorker(
        tmp_path,
        "source.mov",
        runtime,
        limits=limits,
    )
    object.__setattr__(runtime, "root", Path("/private/borrowed-runtime"))
    object.__setattr__(media, "root", Path("/private/borrowed-media"))
    object.__setattr__(limits, "max_source_bytes", 2**30 + 1)
    object.__setattr__(limits, "wall_timeout_ms", 300_001)
    seen: dict[str, object] = {}
    clock = [0]
    deadlines: list[int] = []
    monkeypatch.setattr(cast(Any, detection).time, "monotonic_ns", lambda: clock[0])
    monkeypatch.setattr(detection, "_supported_platform", lambda: True)

    def verify_media(value: MediaRuntime, **kwargs: object) -> None:
        seen["media"] = value
        deadlines.append(cast(int, kwargs["deadline_ns"]))

    def verify_perception(value: PerceptionRuntime, **kwargs: object) -> None:
        seen["verified_runtime"] = value
        deadlines.append(cast(int, kwargs["deadline_ns"]))
        clock[0] = 2_345_000_000
        object.__setattr__(cast(Any, worker)._runtime, "root", Path("/private/race-runtime"))
        object.__setattr__(cast(Any, worker)._limits, "wall_timeout_ms", 300_001)

    def open_source(
        _root: Path, _relative: str, maximum: int, **_kwargs: object
    ) -> tuple[int, os.stat_result]:
        seen["open_maximum"] = maximum
        deadlines.append(cast(int, _kwargs["deadline_ns"]))
        return os.open(source_path, os.O_RDONLY), source_path.stat()

    def snapshot(source_fd: int, maximum: int, **_kwargs: object) -> tuple[int, str, int]:
        seen["snapshot_maximum"] = maximum
        deadlines.append(cast(int, _kwargs["deadline_ns"]))
        raw = os.read(source_fd, 1024)
        return os.dup(source_fd), hashlib.sha256(raw).hexdigest(), len(raw)

    def run_worker(
        used_runtime: PerceptionRuntime,
        used_limits: PerceptionLimits,
        *_args: object,
    ) -> detection._WorkerRun:
        seen["launched_runtime"] = used_runtime
        seen["launched_limits"] = used_limits
        return _run(_worker_payload(source, frames))

    monkeypatch.setattr(detection, "_verify_media_boundary", verify_media)
    monkeypatch.setattr(detection, "_verify_perception_boundary", verify_perception)
    monkeypatch.setattr(detection, "_open_source", open_source)
    monkeypatch.setattr(detection, "_sealed_snapshot", snapshot)
    monkeypatch.setattr(detection, "_run_worker", run_worker)

    result = OpenVinoVehicleDetector(worker).detect(source, frames)

    assert result.state is PerceptionResultState.COMPLETE
    assert cast(MediaRuntime, seen["media"]).root == Path("/opt/media")
    verified = cast(PerceptionRuntime, seen["verified_runtime"])
    launched = cast(PerceptionRuntime, seen["launched_runtime"])
    launched_limits = cast(PerceptionLimits, seen["launched_limits"])
    assert verified is launched
    assert launched.root == Path("/opt/perception")
    assert launched.media.root == Path("/opt/media")
    assert set(deadlines) == {12_345_000_000}
    assert launched_limits.wall_timeout_ms == 10_000
    assert seen["open_maximum"] == seen["snapshot_maximum"] == 4096


@pytest.mark.parametrize(("failed_close", "expected_runs"), [(1, 0), (2, 1)])
def test_production_worker_close_failures_are_nested_closed_once_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_close: int,
    expected_runs: int,
) -> None:
    source_path = tmp_path / "source.mov"
    content = b"sealed-source"
    source_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    source = _source(digest=digest, source_bytes=len(content))
    frames = _frames(source, 1)
    runtime = PerceptionRuntime(
        Path("/opt/perception"),
        MediaRuntime(Path("/opt/media"), Path("/opt/media/worker/media_worker.py")),
    )
    descriptors: list[int] = []
    close_calls: list[int] = []
    run_calls = 0
    monkeypatch.setattr(detection, "_supported_platform", lambda: True)
    monkeypatch.setattr(detection, "_verify_media_boundary", lambda _runtime, **_kwargs: None)
    monkeypatch.setattr(detection, "_verify_perception_boundary", lambda _runtime, **_kwargs: None)

    def open_source(*_args: object, **_kwargs: object) -> tuple[int, os.stat_result]:
        descriptor = os.open(source_path, os.O_RDONLY)
        descriptors.append(descriptor)
        return descriptor, source_path.stat()

    def snapshot(source_fd: int, _maximum: int, **_kwargs: object) -> tuple[int, str, int]:
        raw = os.read(source_fd, 1024)
        descriptor = os.dup(source_fd)
        descriptors.append(descriptor)
        return descriptor, hashlib.sha256(raw).hexdigest(), len(raw)

    def close_file(stream: Any) -> bool:
        descriptor = stream.fileno()
        stream.close()
        close_calls.append(descriptor)
        return len(close_calls) != failed_close

    def run_worker(*_args: object) -> detection._WorkerRun:
        nonlocal run_calls
        run_calls += 1
        return _run(_worker_payload(source, frames))

    monkeypatch.setattr(detection, "_open_source", open_source)
    monkeypatch.setattr(detection, "_sealed_snapshot", snapshot)
    monkeypatch.setattr(detection, "_close_file", close_file)
    monkeypatch.setattr(detection, "_run_worker", run_worker)
    adapter = OpenVinoVehicleDetector(IsolatedPerceptionWorker(tmp_path, "source.mov", runtime))

    with pytest.raises(PortError) as raised:
        adapter.detect(source, frames)

    assert raised.value.code is PortErrorCode.ISOLATION_UNAVAILABLE
    assert raised.value.__context__ is None
    assert "/private" not in str(raised.value)
    assert adapter.calls == ()
    assert run_calls == expected_runs
    assert len(close_calls) == len(descriptors) == 2
    assert len(set(close_calls)) == 2
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_production_worker_closes_source_and_snapshot_on_digest_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.mov"
    source_path.write_bytes(b"sealed-source")
    declared = _source(digest="aa" * 32, source_bytes=len(b"sealed-source"))
    frames = _frames(declared, 1)
    runtime = PerceptionRuntime(
        Path("/opt/perception"),
        MediaRuntime(Path("/opt/media"), Path("/opt/media/worker/media_worker.py")),
    )
    descriptors: list[int] = []
    monkeypatch.setattr(detection, "_supported_platform", lambda: True)
    monkeypatch.setattr(detection, "_verify_media_boundary", lambda _runtime, **_kwargs: None)
    monkeypatch.setattr(detection, "_verify_perception_boundary", lambda _runtime, **_kwargs: None)

    def open_source(*_args: object, **_kwargs: object) -> tuple[int, os.stat_result]:
        descriptor = os.open(source_path, os.O_RDONLY)
        descriptors.append(descriptor)
        return descriptor, source_path.stat()

    def snapshot(source_fd: int, _maximum: int, **_kwargs: object) -> tuple[int, str, int]:
        descriptor = os.dup(source_fd)
        descriptors.append(descriptor)
        return descriptor, "bb" * 32, len(b"sealed-source")

    monkeypatch.setattr(detection, "_open_source", open_source)
    monkeypatch.setattr(detection, "_sealed_snapshot", snapshot)
    monkeypatch.setattr(
        detection,
        "_run_worker",
        lambda *_args: pytest.fail("digest conflict must stop before native execution"),
    )
    adapter = OpenVinoVehicleDetector(IsolatedPerceptionWorker(tmp_path, "source.mov", runtime))

    with pytest.raises(PortError) as raised:
        adapter.detect(declared, frames)

    assert raised.value.code is PortErrorCode.CONFLICT
    assert raised.value.__context__ is None
    assert adapter.calls == ()
    assert len(descriptors) == 2
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_production_worker_reports_absent_boundary_without_opening_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = _source()
    frames = _frames(source, 1)
    runtime = PerceptionRuntime(
        Path("/opt/missing-perception"),
        MediaRuntime(Path("/opt/missing-media"), Path("/opt/missing-media/worker/media_worker.py")),
    )
    monkeypatch.setattr(detection, "_supported_platform", lambda: True)
    monkeypatch.setattr(detection, "_verify_media_boundary", lambda _runtime, **_kwargs: None)

    def unavailable(_runtime: PerceptionRuntime, **_kwargs: object) -> None:
        raise PortError(PortErrorCode.ISOLATION_UNAVAILABLE, PortKind.DETECTOR, "detect")

    monkeypatch.setattr(detection, "_verify_perception_boundary", unavailable)
    monkeypatch.setattr(
        detection,
        "_open_source",
        lambda *_args: pytest.fail("source must not open after runtime verification fails"),
    )
    adapter = OpenVinoVehicleDetector(
        IsolatedPerceptionWorker(tmp_path, "private-source.mov", runtime)
    )

    with pytest.raises(PortError) as raised:
        adapter.detect(source, frames)
    assert raised.value.code is PortErrorCode.ISOLATION_UNAVAILABLE
    assert "private-source" not in str(raised.value)
    assert adapter.calls == ()


def test_public_perception_preflight_shares_one_deadline_across_both_closures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = PerceptionRuntime(
        Path("/private/perception-runtime"),
        MediaRuntime(
            Path("/private/media-runtime"),
            Path("/private/media-runtime/worker/media_worker.py"),
        ),
    )
    clock = [0]
    deadlines: list[int] = []
    monkeypatch.setattr(cast(Any, detection).time, "monotonic_ns", lambda: clock[0])

    def verify_media(_runtime: MediaRuntime, **kwargs: object) -> None:
        deadlines.append(cast(int, kwargs["deadline_ns"]))
        clock[0] = 4_000_000

    def verify_perception(_runtime: PerceptionRuntime, **kwargs: object) -> None:
        deadlines.append(cast(int, kwargs["deadline_ns"]))

    monkeypatch.setattr(detection, "_verify_media_boundary", verify_media)
    monkeypatch.setattr(detection, "_verify_perception_boundary", verify_perception)

    detection.verify_perception_runtime(
        runtime,
        limits=PerceptionLimits(wall_timeout_ms=10),
    )

    assert deadlines == [10_000_000, 10_000_000]

    cancelled = threading.Event()
    cancelled.set()
    monkeypatch.setattr(
        cast(Any, detection).time,
        "monotonic_ns",
        lambda: pytest.fail("pre-cancelled preflight must not start a deadline"),
    )
    with pytest.raises(PortError) as raised:
        detection.verify_perception_runtime(runtime, cancelled=cancelled)
    assert raised.value.code is PortErrorCode.CANCELLED
    assert str(raised.value) == "cancelled at detector.detect"
    assert "private" not in str(raised.value)


def test_detector_preflight_timeout_stops_before_perception_and_source_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = PerceptionRuntime(
        Path("/private/perception-runtime"),
        MediaRuntime(
            Path("/private/media-runtime"),
            Path("/private/media-runtime/worker/media_worker.py"),
        ),
    )
    worker = IsolatedPerceptionWorker(
        tmp_path,
        "private-source.mov",
        runtime,
        limits=PerceptionLimits(wall_timeout_ms=10),
    )
    clock = [0]
    monkeypatch.setattr(cast(Any, detection).time, "monotonic_ns", lambda: clock[0])
    monkeypatch.setattr(detection, "_supported_platform", lambda: True)

    def consume_budget(_runtime: MediaRuntime, **_kwargs: object) -> None:
        clock[0] = 10_000_000

    monkeypatch.setattr(detection, "_verify_media_boundary", consume_budget)
    monkeypatch.setattr(
        detection,
        "_verify_perception_boundary",
        lambda *_args, **_kwargs: pytest.fail("expired preflight must stop verification"),
    )
    monkeypatch.setattr(
        detection,
        "_open_source",
        lambda *_args, **_kwargs: pytest.fail("expired preflight must not open the source"),
    )

    source = _source()
    with pytest.raises(PortError) as raised:
        worker.infer(source, _frames(source, 1))

    assert raised.value.code is PortErrorCode.TIMEOUT
    assert str(raised.value) == "timeout at detector.detect"
    assert "private" not in str(raised.value)


def test_production_worker_returns_unsupported_without_checking_native_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = _source()
    frames = _frames(source, 1)
    runtime = PerceptionRuntime(
        Path("/opt/perception"),
        MediaRuntime(Path("/opt/media"), Path("/opt/media/worker/media_worker.py")),
    )
    monkeypatch.setattr(detection, "_supported_platform", lambda: False)
    monkeypatch.setattr(
        detection,
        "_verify_media_boundary",
        lambda _runtime: pytest.fail("unsupported platform must not touch native state"),
    )
    adapter = OpenVinoVehicleDetector(IsolatedPerceptionWorker(tmp_path, "source.mov", runtime))

    result = adapter.detect(source, frames)

    assert result.state is PerceptionResultState.UNSUPPORTED
    assert result.reason == "platform_unsupported"


def _synthetic_perception_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> PerceptionRuntime:
    root = tmp_path / "runtime"
    files = {
        "python/bin/python3.13": b"python",
        "python/lib/sysconfig.py": b"sysconfig",
        "site-packages/openvino/__init__.py": b"openvino",
        "model/vehicle-detection-0201.xml": b"xml",
        "model/vehicle-detection-0201.bin": b"bin",
        "worker/perception_worker.py": b"worker",
    }
    for relative, raw in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o444)
    monkeypatch.setattr(detection, "_TRUSTED_RUNTIME_UID", tmp_path.stat().st_uid)
    monkeypatch.setattr(detection, "_trusted_traversable_directory", lambda _path: True)
    monkeypatch.setattr(
        detection,
        "_PYTHON_TREE_SHA256",
        detection._tree_sha256(root / "python", normalize_python_root=True),
    )
    monkeypatch.setattr(
        detection,
        "_SITE_PACKAGES_TREE_SHA256",
        detection._tree_sha256(root / "site-packages"),
    )
    monkeypatch.setattr(detection, "_SITE_PACKAGES_LOGICAL_BYTES", len(b"openvino"))
    monkeypatch.setattr(detection, "_MODEL_TREE_SHA256", detection._tree_sha256(root / "model"))
    monkeypatch.setattr(
        detection,
        "_PYTHON_EXECUTABLE_SHA256",
        hashlib.sha256(b"python").hexdigest(),
    )
    monkeypatch.setattr(detection, "DETECTOR_WORKER_SHA256", hashlib.sha256(b"worker").hexdigest())
    manifest_raw = b"synthetic manifest\n"
    monkeypatch.setattr(
        detection,
        "DETECTOR_MANIFEST_SHA256",
        hashlib.sha256(manifest_raw).hexdigest(),
    )
    manifest_path = root / detection._PERCEPTION_MANIFEST_NAME
    manifest_path.write_bytes(manifest_raw)
    manifest_path.chmod(0o444)
    receipt_path = root / detection._PERCEPTION_RECEIPT_NAME
    receipt_path.write_bytes(
        (json.dumps(detection._expected_receipt(), sort_keys=True, indent=2) + "\n").encode()
    )
    receipt_path.chmod(0o444)
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        path.chmod(0o555)
    root.chmod(0o555)
    return PerceptionRuntime(
        root,
        MediaRuntime(Path("/opt/media"), Path("/opt/media/worker/media_worker.py")),
    )


def test_perception_runtime_verifier_accepts_exact_tree_and_rejects_worker_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _synthetic_perception_runtime(monkeypatch, tmp_path)
    detection._verify_perception_boundary(runtime)

    worker = runtime.root / "worker/perception_worker.py"
    worker.parent.chmod(0o755)
    worker.chmod(0o644)
    worker.write_bytes(b"tampered")
    worker.chmod(0o444)
    worker.parent.chmod(0o555)
    with pytest.raises(PortError, match="isolation_unavailable"):
        detection._verify_perception_boundary(runtime)


def test_worker_records_reject_mutation_and_do_not_store_pixels_or_paths() -> None:
    source = _source()
    frames = _frames(source, 1)
    result = _worker_result(source, frames, ((WorkerDetection((1, 2, 3, 4), 950_000),),))
    assert "pixels" not in repr(result).lower()
    assert not any("path" in field for field in type(result).__dataclass_fields__)

    for changes in (
        {"box_normalized_millionths": (0, 0, 0, 1)},
        {"confidence_millionths": 949_999},
    ):
        with pytest.raises(ValueError, match="invalid worker detection"):
            replace(result.frames[0].detections[0], **changes)


def test_detector_value_records_reject_every_frozen_provenance_drift() -> None:
    provenance = DetectionProvenance("aa" * 32, 1)
    changes = (
        {"source_sha256": "bad"},
        {"source_bytes": True},
        {"configuration_sha256": "00" * 32},
        {"confidence_floor_millionths": 949_999},
        {"model_xml_sha256": "00" * 32},
        {"model_bin_sha256": "00" * 32},
        {"runtime_id": "other"},
        {"runtime_closure_sha256": "00" * 32},
        {"worker_sha256": "00" * 32},
        {"perception_manifest_sha256": "00" * 32},
        {"media_runtime_id": "other"},
        {"media_manifest_sha256": "00" * 32},
        {"media_tree_sha256": "00" * 32},
    )
    for change in changes:
        with pytest.raises(ValueError, match="invalid detector provenance"):
            replace(provenance, **change)


def test_hostile_scalar_subclasses_cannot_forge_detector_provenance() -> None:
    class Liar(str):
        def __eq__(self, _other: object) -> bool:
            return True

        def __ne__(self, _other: object) -> bool:
            return False

    with pytest.raises(ValueError, match="invalid detector provenance"):
        DetectionProvenance(
            "aa" * 32,
            1,
            configuration_sha256=cast(str, Liar("not-a-digest")),
            runtime_id=cast(str, Liar("/private/path")),
        )

    source = _source()
    frames = _frames(source, 1)
    result = _worker_result(source, frames)
    object.__setattr__(
        result.provenance,
        "configuration_sha256",
        Liar("not-a-digest"),
    )
    object.__setattr__(result.provenance, "runtime_id", Liar("/private/path"))

    class HostileWorker:
        supported = True

        def infer(
            self,
            _source: Source,
            _frames: tuple[FrameRef, ...],
        ) -> PerceptionWorkerResult:
            return result

    adapter = OpenVinoVehicleDetector(cast(PerceptionWorker, HostileWorker()))
    with pytest.raises(PortError) as raised:
        adapter.detect(source, frames)

    assert raised.value.code is PortErrorCode.DECODE_FAILED
    assert raised.value.__context__ is None
    assert "/private" not in str(raised.value)
    assert adapter.calls == ()


def test_worker_value_records_reject_hostile_shapes_times_and_duplicates() -> None:
    time_base = TimeBase("1", "1000")
    pts = MediaTime("0", time_base)
    detection_value = WorkerDetection((1, 2, 3, 4), 950_000)
    frame = WorkerFrame("0", pts, None, True, (detection_value,))
    result = PerceptionWorkerResult(
        DetectionProvenance("aa" * 32, 1),
        0,
        640,
        360,
        0,
        time_base,
        (frame,),
    )

    invalid_detections = (
        {"box_normalized_millionths": cast(Any, [1, 2, 3, 4])},
        {"box_normalized_millionths": cast(Any, (1, 2, 3))},
        {"box_normalized_millionths": cast(Any, (True, 2, 3, 4))},
        {"confidence_millionths": True},
    )
    for change in invalid_detections:
        with pytest.raises(ValueError, match="invalid worker detection"):
            replace(detection_value, **change)

    mismatched_duration = MediaTime("1", TimeBase("1", "999"))
    invalid_frames = (
        {"decode_index": "01"},
        {"key_frame": cast(Any, 1)},
        {"detections": cast(Any, [detection_value])},
        {"duration": mismatched_duration},
        {"detections": (detection_value, detection_value)},
        {"pts": cast(Any, object())},
    )
    for change in invalid_frames:
        with pytest.raises(ValueError):
            replace(frame, **change)

    mismatched_frame = WorkerFrame(
        "1",
        MediaTime("1", TimeBase("1", "999")),
        None,
        False,
    )
    invalid_results = (
        {"provenance": cast(Any, object())},
        {"stream_index": True},
        {"width": 0},
        {"height": 0},
        {"rotation_degrees": 1},
        {"time_base": cast(Any, object())},
        {"frames": cast(Any, [frame])},
        {"frames": (mismatched_frame,)},
        {"frames": (frame, frame)},
    )
    for change in invalid_results:
        with pytest.raises(ValueError, match="invalid worker result"):
            replace(result, **change)

    bad_time = MediaTime("0", time_base)
    object.__setattr__(bad_time, "basis", "estimated")
    with pytest.raises(ValueError, match="invalid worker time"):
        detection._validate_time_for_record(bad_time)


def test_protocol_scalar_parsers_and_constructors_fail_closed() -> None:
    invalid_calls = (
        lambda: detection._plain_digest("not-a-digest"),
        lambda: detection._plain_token("contains a space"),
        lambda: detection._bounded_integer(True, 0, 1),
        lambda: detection._decimal("01"),
        lambda: detection._signed_decimal("-0"),
        lambda: detection._mapping([], frozenset()),
        lambda: detection._time_base({"numerator": "0", "denominator": "1"}),
        lambda: detection._media_time(
            {"value": "-0", "time_base": {"numerator": "1", "denominator": "1"}}
        ),
        lambda: detection._no_duplicate_object([("field", 1), ("field", 2)]),
    )
    for invoke in invalid_calls:
        with pytest.raises(PortError) as raised:
            invoke()
        assert raised.value.code is PortErrorCode.DECODE_FAILED

    with pytest.raises(ValueError):
        FixturePerceptionWorker(cast(Any, object()))
    with pytest.raises(ValueError):
        PerceptionRuntime(cast(Any, "/runtime"), cast(Any, object()))
    with pytest.raises(ValueError):
        OpenVinoVehicleDetector(cast(Any, object()))


def test_runtime_file_tree_and_trust_helpers_bind_bytes_links_and_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(detection, "_TRUSTED_RUNTIME_UID", tmp_path.stat().st_uid)
    root = tmp_path / "runtime"
    package = root / "package"
    package.mkdir(parents=True)
    payload = package / "payload"
    payload.write_bytes(b"approved")
    payload.chmod(0o444)
    sysconfig = package / "_sysconfigdata_test.py"
    sysconfig.write_bytes(os.fsencode(root) + b"/lib")
    sysconfig.chmod(0o444)
    link = package / "link"
    link.symlink_to("payload")
    root.chmod(0o555)
    package.chmod(0o555)

    assert detection._read_runtime_file(payload) == b"approved"
    assert detection._runtime_link_target(root, link) == b"package/payload"
    assert detection._tree_sha256(root)
    assert detection._tree_sha256(root, normalize_python_root=True)
    assert detection._logical_size(root) == len(b"approved") + len(os.fsencode(root) + b"/lib")
    detection._validate_frozen_runtime(root)
    assert detection._trusted_traversable_directory(root) is True
    assert detection._trusted_traversable_directory(tmp_path / "missing") is False

    payload.chmod(0o644)
    with pytest.raises(PortError, match="isolation_unavailable"):
        detection._read_runtime_file(payload)
    with pytest.raises(PortError, match="isolation_unavailable"):
        detection._validate_frozen_runtime(root)
    payload.chmod(0o444)

    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    escaping = package / "escaping"
    package.chmod(0o755)
    escaping.symlink_to("../../outside")
    package.chmod(0o555)
    with pytest.raises(PortError, match="isolation_unavailable"):
        detection._runtime_link_target(root, escaping)
    package.chmod(0o755)
    escaping.unlink()
    package.chmod(0o555)

    fifo = package / "fifo"
    package.chmod(0o755)
    os.mkfifo(fifo)
    package.chmod(0o555)
    with pytest.raises(PortError, match="isolation_unavailable"):
        detection._tree_sha256(root)
    package.chmod(0o755)
    fifo.unlink()
    package.chmod(0o555)

    with pytest.raises(PortError, match="isolation_unavailable"):
        detection._read_runtime_file(tmp_path / "missing")
    root.chmod(0o775)
    assert detection._trusted_traversable_directory(root) is False


def test_runtime_file_read_checks_cancellation_inside_hash_input_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(detection, "_TRUSTED_RUNTIME_UID", tmp_path.stat().st_uid)
    payload = tmp_path / "private-runtime-file"
    payload.write_bytes(b"approved")
    payload.chmod(0o444)
    cancelled = threading.Event()
    real_read = os.read

    def cancel_after_read(descriptor: int, maximum: int) -> bytes:
        raw = real_read(descriptor, maximum)
        cancelled.set()
        return raw

    monkeypatch.setattr(cast(Any, detection).os, "read", cancel_after_read)

    with pytest.raises(PortError) as raised:
        detection._read_runtime_file(
            payload,
            cancelled=cancelled,
            deadline_ns=time.monotonic_ns() + 1_000_000_000,
        )

    assert raised.value.code is PortErrorCode.CANCELLED
    assert str(raised.value) == "cancelled at detector.detect"
    assert "private-runtime-file" not in str(raised.value)


def test_cgroup_and_systemd_status_helpers_cover_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = cast(Any, detection)
    cgroup = tmp_path / "unit"
    cgroup.mkdir()
    cpu = cgroup / "cpu.stat"
    cpu.write_text("usage_usec 123\ninvalid value\n", encoding="ascii")
    scalar = cgroup / "memory.peak"
    scalar.write_text("456\n", encoding="ascii")
    assert detection._read_number(cpu, "usage_usec") == 123
    assert detection._read_number(cpu, "missing") == 0
    assert detection._read_number(scalar) == 456
    assert detection._read_number(cgroup / "missing") == 0

    assert detection._cgroup_processes(cgroup) == ()
    (cgroup / "cgroup.procs").write_text("12 34\n", encoding="ascii")
    assert detection._cgroup_processes(cgroup) == (12, 34)
    (cgroup / "cgroup.procs").write_text("hostile\n", encoding="ascii")
    assert detection._cgroup_processes(cgroup) is None

    responses = iter(
        (
            SimpleNamespace(returncode=0, stdout=b"inactive\n"),
            SimpleNamespace(returncode=1, stdout=b"active\n"),
        )
    )
    monkeypatch.setattr(backend.subprocess, "run", lambda *_args, **_kwargs: next(responses))
    assert detection._unit_inactive("unit.service") is True
    assert detection._unit_inactive("unit.service") is False
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private")),
    )
    assert detection._unit_inactive("unit.service") is False

    monkeypatch.setattr(detection, "_cgroup_processes", lambda _path: ())
    monkeypatch.setattr(detection, "_unit_inactive", lambda _unit: True)
    assert detection._wait_unit_stopped("unit.service", cgroup, 0) is True
    monkeypatch.setattr(detection, "_cgroup_processes", lambda _path: (1,))
    monkeypatch.setattr(detection, "_unit_inactive", lambda _unit: False)
    ticks = iter((0.0, 1.0))
    monkeypatch.setattr(backend.time, "monotonic", lambda: next(ticks))
    assert detection._wait_unit_stopped("unit.service", cgroup, 0) is False


def test_kill_and_cleanup_helpers_use_cgroup_then_systemd_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = cast(Any, detection)
    process = cast(subprocess.Popen[bytes], SimpleNamespace(pid=999_999))
    monkeypatch.setattr(backend.os, "killpg", lambda *_args: None)
    cgroup = tmp_path / "unit"
    cgroup.mkdir()
    (cgroup / "cgroup.kill").write_bytes(b"")
    assert detection._kill_unit("unit.service", cgroup, process) is True

    calls: list[str] = []

    def systemctl(*_args: object, **_kwargs: object) -> SimpleNamespace:
        calls.append("systemctl")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(backend.subprocess, "run", systemctl)
    assert detection._kill_unit("unit.service", tmp_path / "missing", process) is True
    assert calls == ["systemctl"]

    class Process:
        pid = 999_999

        def __init__(self, waits: list[object]) -> None:
            self.waits = waits

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: int) -> int:
            value = self.waits.pop(0)
            if isinstance(value, BaseException):
                raise value
            return cast(int, value)

    monkeypatch.setattr(detection, "_kill_unit", lambda *_args: True)
    monkeypatch.setattr(detection, "_cgroup_processes", lambda _path: ())
    monkeypatch.setattr(detection, "_wait_unit_stopped", lambda *_args: True)
    successful = cast(subprocess.Popen[bytes], Process([0]))
    assert detection._ensure_unit_stopped("unit.service", cgroup, successful) is True

    timeout = subprocess.TimeoutExpired("worker", 2)
    wedged = cast(subprocess.Popen[bytes], Process([timeout, timeout]))
    assert detection._ensure_unit_stopped("unit.service", cgroup, wedged) is False


def test_run_worker_handles_launch_success_failure_and_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = cast(Any, detection)
    runtime = PerceptionRuntime(
        Path("/opt/perception"),
        MediaRuntime(Path("/opt/media"), Path("/opt/media/worker/media_worker.py")),
    )
    limits = PerceptionLimits()
    process = cast(subprocess.Popen[bytes], SimpleNamespace())
    expected = detection._WorkerRun(b"ok", b"", 0, 1, 2, 3)
    reset_calls: list[object] = []
    monkeypatch.setattr(backend.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(detection, "_drain_worker", lambda *_args: expected)
    monkeypatch.setattr(detection, "_ensure_unit_stopped", lambda *_args: True)

    def reset_failed(*args: object, **_kwargs: object) -> SimpleNamespace:
        reset_calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(backend.subprocess, "run", reset_failed)
    assert detection._run_worker(runtime, limits, 3, (0,), None) == expected
    assert reset_calls

    monkeypatch.setattr(
        backend.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private")),
    )
    with pytest.raises(PortError, match="isolation_unavailable"):
        detection._run_worker(runtime, limits, 3, (0,), None)

    monkeypatch.setattr(backend.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        detection,
        "_drain_worker",
        lambda *_args: (_ for _ in ()).throw(
            PortError(PortErrorCode.TIMEOUT, PortKind.DETECTOR, "detect")
        ),
    )
    monkeypatch.setattr(detection, "_ensure_unit_stopped", lambda *_args: False)
    with pytest.raises(PortError) as raised:
        detection._run_worker(runtime, limits, 3, (0,), None)
    assert raised.value.code is PortErrorCode.ISOLATION_UNAVAILABLE


@pytest.mark.parametrize(
    ("system", "machine", "libc", "version", "expected"),
    [
        ("linux", "x86_64", "glibc", "2.28", True),
        ("darwin", "arm64", "", "", False),
        ("linux", "x86_64", "musl", "1.2", False),
        ("linux", "x86_64", "glibc", "not-a-version", False),
    ],
)
def test_supported_platform_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    libc: str,
    version: str,
    expected: bool,
) -> None:
    backend = cast(Any, detection)
    monkeypatch.setattr(backend.sys, "platform", system)
    monkeypatch.setattr(backend.platform, "machine", lambda: machine)
    monkeypatch.setattr(backend.platform, "libc_ver", lambda: (libc, version))
    assert detection._supported_platform() is expected


def test_isolated_worker_constructor_and_prelaunch_failures_are_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = PerceptionRuntime(
        Path("/opt/perception"),
        MediaRuntime(Path("/opt/media"), Path("/opt/media/worker/media_worker.py")),
    )
    invalid_constructors: tuple[Callable[[], object], ...] = (
        lambda: IsolatedPerceptionWorker(cast(Any, "root"), "source.mov", runtime),
        lambda: IsolatedPerceptionWorker(tmp_path, cast(Any, 1), runtime),
        lambda: IsolatedPerceptionWorker(tmp_path, "source.mov", cast(Any, object())),
        lambda: IsolatedPerceptionWorker(
            tmp_path,
            "source.mov",
            runtime,
            limits=cast(Any, object()),
        ),
        lambda: IsolatedPerceptionWorker(
            tmp_path,
            "source.mov",
            runtime,
            cancelled=cast(Any, object()),
        ),
    )
    for create in invalid_constructors:
        with pytest.raises(ValueError):
            create()

    source = _source()
    frames = _frames(source)
    worker = IsolatedPerceptionWorker(tmp_path, "source.mov", runtime)
    monkeypatch.setattr(detection, "_supported_platform", lambda: False)
    with pytest.raises(PortError) as raised:
        worker.infer(source, frames)
    assert raised.value.code is PortErrorCode.UNSUPPORTED

    monkeypatch.setattr(detection, "_supported_platform", lambda: True)
    with pytest.raises(ValueError):
        worker.infer(source, ())

    multi_stream = Source.create(
        Fingerprint("bb" * 32, "1000"),
        (
            SourceStream(0, 640, 360, 0, TimeBase("1", "1000")),
            SourceStream(1, 640, 360, 0, TimeBase("1", "1000")),
        ),
    )
    multi_frames = (
        FrameRef.create(multi_stream.source_id, 0, "0", MediaTime("0", TimeBase("1", "1000"))),
        FrameRef.create(multi_stream.source_id, 1, "0", MediaTime("0", TimeBase("1", "1000"))),
    )
    with pytest.raises(PortError) as raised:
        worker.infer(multi_stream, multi_frames)
    assert raised.value.code is PortErrorCode.INVALID_REQUEST

    bounded = IsolatedPerceptionWorker(
        tmp_path,
        "source.mov",
        runtime,
        limits=PerceptionLimits(max_frames=1),
    )
    with pytest.raises(PortError) as raised:
        bounded.infer(source, (frames[1],))
    assert raised.value.code is PortErrorCode.LIMIT_EXCEEDED

    monkeypatch.setattr(
        detection,
        "_verify_media_boundary",
        lambda _runtime, **_kwargs: (_ for _ in ()).throw(
            PortError(PortErrorCode.ISOLATION_UNAVAILABLE, PortKind.VIDEO_SOURCE, "probe")
        ),
    )
    with pytest.raises(PortError) as raised:
        worker.infer(source, frames)
    assert raised.value.port is PortKind.DETECTOR
    assert raised.value.__context__ is None


def test_isolated_worker_redacts_source_open_and_snapshot_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = PerceptionRuntime(
        Path("/opt/perception"),
        MediaRuntime(Path("/opt/media"), Path("/opt/media/worker/media_worker.py")),
    )
    source = _source()
    frames = _frames(source, 1)
    worker = IsolatedPerceptionWorker(tmp_path, "private.mov", runtime)
    monkeypatch.setattr(detection, "_supported_platform", lambda: True)
    monkeypatch.setattr(detection, "_verify_media_boundary", lambda _runtime, **_kwargs: None)
    monkeypatch.setattr(detection, "_verify_perception_boundary", lambda _runtime, **_kwargs: None)
    monkeypatch.setattr(
        detection,
        "_open_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PortError(PortErrorCode.INVALID_REQUEST, PortKind.VIDEO_SOURCE, "open")
        ),
    )
    with pytest.raises(PortError) as raised:
        worker.infer(source, frames)
    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert raised.value.__context__ is None

    source_path = tmp_path / "source.mov"
    source_path.write_bytes(b"source")
    descriptors: list[int] = []

    def open_source(*_args: object, **_kwargs: object) -> tuple[int, os.stat_result]:
        descriptor = os.open(source_path, os.O_RDONLY)
        descriptors.append(descriptor)
        return descriptor, source_path.stat()

    monkeypatch.setattr(detection, "_open_source", open_source)
    monkeypatch.setattr(
        detection,
        "_sealed_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PortError(PortErrorCode.LIMIT_EXCEEDED, PortKind.VIDEO_SOURCE, "read")
        ),
    )
    with pytest.raises(PortError) as raised:
        worker.infer(source, frames)
    assert raised.value.code is PortErrorCode.LIMIT_EXCEEDED
    assert raised.value.__context__ is None
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_adapter_redacts_capability_protocol_and_frame_mapping_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = cast(Any, detection)
    source = _source()
    frames = _frames(source, 1)

    class CapabilityFailure:
        @property
        def supported(self) -> bool:
            raise OSError("private capability detail")

        def infer(self, _source: Source, _frames: tuple[FrameRef, ...]) -> PerceptionWorkerResult:
            raise AssertionError

    with pytest.raises(PortError) as raised:
        OpenVinoVehicleDetector(cast(PerceptionWorker, CapabilityFailure())).detect(source, frames)
    assert raised.value.code is PortErrorCode.ISOLATION_UNAVAILABLE
    assert raised.value.__context__ is None

    class NonBooleanCapability:
        supported = cast(Any, "yes")

        def infer(self, _source: Source, _frames: tuple[FrameRef, ...]) -> PerceptionWorkerResult:
            raise AssertionError

    with pytest.raises(PortError, match="isolation_unavailable"):
        OpenVinoVehicleDetector(cast(PerceptionWorker, NonBooleanCapability())).detect(
            source, frames
        )

    class WrongResult:
        supported = True

        def infer(self, _source: Source, _frames: tuple[FrameRef, ...]) -> Any:
            return object()

    adapter = OpenVinoVehicleDetector(cast(PerceptionWorker, WrongResult()))
    assert adapter.producer == DETECTOR_PRODUCER
    with pytest.raises(PortError, match="decode_failed"):
        adapter.detect(source, frames)

    oversized_source = _source(source_bytes=2**63)
    valid_worker = FixturePerceptionWorker(_worker_result(source, frames))
    with pytest.raises(PortError) as raised:
        OpenVinoVehicleDetector(valid_worker).provenance(oversized_source)
    assert raised.value.code is PortErrorCode.INVALID_REQUEST

    mismatched_frame = replace(
        _worker_result(source, frames).frames[0],
        key_frame=False,
    )
    mismatched = replace(_worker_result(source, frames), frames=(mismatched_frame,))
    with pytest.raises(PortError, match="decode_failed"):
        OpenVinoVehicleDetector(FixturePerceptionWorker(mismatched)).detect(source, frames)

    output = _worker_result(
        source,
        frames,
        ((WorkerDetection((1, 1, 2, 2), 950_000),),),
    )
    monkeypatch.setattr(
        backend.DetectorTransform,
        "map_box",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("private geometry")),
    )
    with pytest.raises(PortError) as raised:
        OpenVinoVehicleDetector(FixturePerceptionWorker(output)).detect(source, frames)
    assert raised.value.code is PortErrorCode.DECODE_FAILED
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "failure",
    ["trust", "iterdir", "entries", "manifest", "receipt", "tree", "executable"],
)
def test_perception_runtime_verifier_rejects_each_outer_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    runtime = _synthetic_perception_runtime(monkeypatch, tmp_path)
    root = runtime.root
    if failure == "trust":
        monkeypatch.setattr(detection, "_trusted_traversable_directory", lambda _path: False)
    elif failure == "iterdir":
        real_iterdir = Path.iterdir

        def failed_iterdir(path: Path) -> Any:
            if path == root:
                raise OSError("private runtime")
            return real_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", failed_iterdir)
    elif failure == "entries":
        root.chmod(0o755)
        unexpected = root / "unexpected"
        unexpected.write_bytes(b"unexpected")
        unexpected.chmod(0o444)
        root.chmod(0o555)
    elif failure == "manifest":
        monkeypatch.setattr(detection, "DETECTOR_MANIFEST_SHA256", "00" * 32)
    elif failure == "receipt":
        receipt = root / detection._PERCEPTION_RECEIPT_NAME
        receipt.chmod(0o644)
        receipt.write_bytes(b"invalid receipt\n")
        receipt.chmod(0o444)
    elif failure == "tree":
        monkeypatch.setattr(detection, "_tree_sha256", lambda *_args, **_kwargs: "00" * 32)
    else:
        monkeypatch.setattr(detection, "_PYTHON_EXECUTABLE_SHA256", "00" * 32)

    with pytest.raises(PortError) as raised:
        detection._verify_perception_boundary(runtime)
    assert raised.value.code is PortErrorCode.ISOLATION_UNAVAILABLE
    assert raised.value.__context__ is None


def test_worker_output_protocol_rejects_semantic_frame_mismatches() -> None:
    source = _source()
    frames = _frames(source)
    payloads: list[dict[str, object]] = []

    def fresh() -> dict[str, object]:
        return copy.deepcopy(_worker_payload(source, frames))

    status = fresh()
    status["status"] = "failed"
    payloads.append(status)

    rotation = fresh()
    cast(dict[str, object], rotation["stream"])["rotation_degrees"] = 1
    payloads.append(rotation)

    non_list = fresh()
    non_list["frames"] = None
    payloads.append(non_list)

    unknown_frame = fresh()
    cast(dict[str, object], cast(list[object], unknown_frame["frames"])[0])["decode_index"] = "99"
    payloads.append(unknown_frame)

    invalid_key_frame = fresh()
    cast(dict[str, object], cast(list[object], invalid_key_frame["frames"])[0])["key_frame"] = 1
    payloads.append(invalid_key_frame)

    invalid_detections = fresh()
    cast(dict[str, object], cast(list[object], invalid_detections["frames"])[0])["detections"] = (
        None
    )
    payloads.append(invalid_detections)

    invalid_box = fresh()
    invalid_box_frame = cast(dict[str, object], cast(list[object], invalid_box["frames"])[0])
    cast(dict[str, object], cast(list[object], invalid_box_frame["detections"])[0])[
        "box_normalized_millionths"
    ] = None
    payloads.append(invalid_box)

    duplicate_detection = fresh()
    duplicate_frame = cast(dict[str, object], cast(list[object], duplicate_detection["frames"])[0])
    values = cast(list[object], duplicate_frame["detections"])
    values.append(copy.deepcopy(values[0]))
    payloads.append(duplicate_detection)

    wrong_order = fresh()
    cast(list[object], wrong_order["frames"]).reverse()
    payloads.append(wrong_order)

    for payload in payloads:
        with pytest.raises(PortError) as raised:
            detection._decode_worker_output(
                _run(payload),
                source,
                frames,
                source.fingerprint.digest,
                int(source.fingerprint.bytes),
                PerceptionLimits(),
            )
        assert raised.value.code in {PortErrorCode.DECODE_FAILED, PortErrorCode.LIMIT_EXCEEDED}
        assert raised.value.__context__ is None
