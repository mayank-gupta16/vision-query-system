# SPDX-License-Identifier: Apache-2.0
"""Mac-safe contract and helper tests for the Linux original-frame worker."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import stat
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from visualworld.ingestion import (
    Fingerprint,
    FrameRef,
    MediaTime,
    Source,
    SourceStream,
    TimeBase,
)

_ROOT = Path(__file__).resolve().parents[1]
_WORKER = _ROOT / "workers/original_frame_worker.py"
_MANIFEST = _ROOT / "workers/original-frame-runtime-v1.json"
_MEDIA_MANIFEST = _ROOT / "workers/visualworld-runtime.json"


def _worker_namespace() -> dict[str, object]:
    return runpy.run_path(os.fspath(_WORKER))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _request(*, indexes: tuple[int, ...] = (0, 2)) -> dict[str, object]:
    fingerprint = Fingerprint(digest="ab" * 32, bytes="12")
    time_base = TimeBase(numerator="1", denominator="1000")
    stream = SourceStream(
        stream_index=0,
        width=2,
        height=2,
        rotation_degrees=0,
        time_base=time_base,
    )
    source = Source.create(fingerprint, (stream,))
    frames = [
        FrameRef.create(
            source_id=source.source_id,
            stream_index=0,
            decode_index=str(index),
            pts=MediaTime(str(index), time_base),
            duration=MediaTime("1", time_base),
            key_frame=index == 0,
        ).to_mapping()
        for index in indexes
    ]
    return {
        "frames": frames,
        "output_bytes": str(2 * 2 * 3 * len(frames)),
        "schema": "visualworld.original_frame_request",
        "schema_version": 1,
        "source": source.to_mapping(),
    }


def test_manifest_pins_worker_and_unchanged_media_runtime() -> None:
    manifest = json.loads(_MANIFEST.read_bytes())
    media = json.loads(_MEDIA_MANIFEST.read_bytes())

    assert (
        manifest["application_worker"]["sha256"] == hashlib.sha256(_WORKER.read_bytes()).hexdigest()
    )
    assert manifest["media_runtime"] == {
        "ffmpeg_version": media["ffmpeg_version"],
        "libavcodec_version": media["libavcodec_version"],
        "libavformat_version": media["libavformat_version"],
        "manifest_path": "workers/visualworld-runtime.json",
        "manifest_sha256": hashlib.sha256(_MEDIA_MANIFEST.read_bytes()).hexdigest(),
        "pyav_version": media["pyav_version"],
        "runtime_id": media["runtime_id"],
        "tree_sha256": media["tree_sha256"],
        "worker_sha256": media["worker_sha256"],
    }
    assert manifest["support"] == {
        "architecture": "x86_64",
        "libc": "glibc",
        "minimum_libc_version": "2.28",
        "operating_system": "Linux",
        "python_abi": "cp313",
        "unsupported_elsewhere": True,
    }
    assert manifest["ipc"]["source"]["descriptor"] == 3
    assert manifest["ipc"]["uncompressed_output"]["descriptor"] == 4
    assert manifest["ipc"]["request"]["pixel_free"] is True
    assert manifest["ipc"]["response"]["pixel_free"] is True
    assert manifest["worker"]["isolation"]["read_only_overlay"] is True

    worker = _worker_namespace()
    assert worker["_LANDLOCK_READ_PATHS"] == ("/runtime", "/overlay", "/lib", "/lib64")


def test_strict_canonical_request_accepts_real_source_and_frame_ref_mappings() -> None:
    worker = _worker_namespace()
    parse_request = cast(Any, worker["_parse_request"])
    request = _request()

    parsed = parse_request(_canonical(request))

    assert parsed["source"] == request["source"]
    assert parsed["frames"] == request["frames"]
    assert parsed["source_bytes"] == 12
    assert parsed["output_bytes"] == 24
    source = cast(dict[str, object], request["source"])
    streams = cast(list[object], source["streams"])
    assert parsed["stream"] == streams[0]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.update(output_bytes="23"),
        lambda item: item.update(extra="forbidden"),
        lambda item: item["frames"].reverse(),
        lambda item: item["frames"].append(deepcopy(item["frames"][0])),
        lambda item: item["frames"][0].update(frame_id="frm_" + "0" * 64),
        lambda item: item["source"].update(source_id="src_" + "0" * 64),
        lambda item: item["source"]["streams"][0].update(width=5000),
        lambda item: item["frames"][0]["pts"].update(value="1"),
        lambda item: item["frames"][0].update(stream_index=1),
    ],
)
def test_request_rejects_mismatch_duplicate_order_geometry_pts_and_index(
    mutation: Any,
) -> None:
    worker = _worker_namespace()
    parse_request = cast(Any, worker["_parse_request"])
    invalid_request = cast(type[Exception], worker["_InvalidRequest"])
    request = _request()
    mutation(request)

    with pytest.raises(invalid_request, match="invalid_request"):
        parse_request(_canonical(request))


@pytest.mark.parametrize(
    "payload",
    [
        _canonical(_request()) + b"\n",
        b'{"frames":[],"frames":[],"output_bytes":"1","schema":'
        b'"visualworld.original_frame_request","schema_version":1,"source":{}}',
        b"\xef\xbb\xbf{}",
        b'{"frames":[],"output_bytes":"1","schema":"x","schema_version":1.0,"source":{}}',
    ],
)
def test_request_rejects_noncanonical_duplicate_bom_and_float_json(payload: bytes) -> None:
    worker = _worker_namespace()
    invalid_request = cast(type[Exception], worker["_InvalidRequest"])
    with pytest.raises(invalid_request, match="invalid_request"):
        cast(Any, worker["_parse_request"])(payload)


class _Plane:
    def __init__(self, payload: bytes, line_size: int) -> None:
        self.payload = payload
        self.line_size = line_size

    def __bytes__(self) -> bytes:
        return self.payload


class _Reformatter:
    def __init__(self, frames: dict[object, object] | None = None) -> None:
        self.frames = frames or {}

    def reformat(self, frame: object, *, format: str) -> object:
        assert format == "rgb24"
        if not self.frames:
            return frame
        return self.frames.get(frame, frame)


def test_packed_rgb24_removes_decoded_row_padding() -> None:
    worker = _worker_namespace()
    plane = _Plane(bytes(range(1, 7)) + b"xx" + bytes(range(7, 13)) + b"yy", 8)
    converted = SimpleNamespace(width=2, height=2, planes=(plane,))

    original = object()
    assert cast(Any, worker["_packed_rgb24"])(
        original, _Reformatter({original: converted})
    ) == bytes(range(1, 13))


def test_descriptor_contract_requires_sealed_source_and_presealed_exact_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker_namespace()
    validate = cast(Any, worker["_validate_descriptors"])
    invalid_request = cast(type[Exception], worker["_InvalidRequest"])
    source_stat = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=12, st_ino=10, st_dev=1)
    output_stat = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=24, st_ino=11, st_dev=1)

    monkeypatch.setattr(
        os, "fstat", lambda descriptor: {3: source_stat, 4: output_stat}[descriptor]
    )

    def valid_fcntl(descriptor: int, command: int, *arguments: object) -> int:
        if command == cast(int, worker["_F_GET_SEALS"]):
            return {
                3: cast(int, worker["_REQUIRED_SEALS"]),
                4: cast(int, worker["_INITIAL_OUTPUT_SEALS"]),
            }[descriptor]
        return os.O_RDWR

    fcntl_module = cast(Any, worker["fcntl"])
    monkeypatch.setattr(fcntl_module, "fcntl", valid_fcntl)
    validate(12, 24)

    output_stat.st_size = 23
    with pytest.raises(invalid_request, match="invalid_request"):
        validate(12, 24)
    output_stat.st_size = 24

    def unsealed_output(descriptor: int, command: int, *arguments: object) -> int:
        if command == cast(int, worker["_F_GET_SEALS"]):
            return cast(int, worker["_REQUIRED_SEALS"]) if descriptor == 3 else 0
        return os.O_RDWR

    monkeypatch.setattr(fcntl_module, "fcntl", unsealed_output)
    with pytest.raises(invalid_request, match="invalid_request"):
        validate(12, 24)


def test_output_is_written_at_exact_offsets_then_write_and_seal_are_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker_namespace()
    publish = cast(Any, worker["_publish_output"])
    storage = bytearray(6)
    seals = cast(int, worker["_INITIAL_OUTPUT_SEALS"])
    calls: list[tuple[int, int]] = []

    def pwrite(descriptor: int, value: object, offset: int) -> int:
        assert descriptor == 4
        data = bytes(cast(memoryview, value))
        storage[offset : offset + len(data)] = data
        return len(data)

    def fake_fcntl(descriptor: int, command: int, *arguments: object) -> int:
        nonlocal seals
        assert descriptor == 4
        if command == cast(int, worker["_F_ADD_SEALS"]):
            addition = cast(int, arguments[0])
            calls.append((command, addition))
            seals |= addition
            return 0
        assert command == cast(int, worker["_F_GET_SEALS"])
        return seals

    monkeypatch.setattr(os, "pwrite", pwrite)
    monkeypatch.setattr(
        os,
        "fstat",
        lambda descriptor: SimpleNamespace(st_size=6),
    )
    monkeypatch.setattr(os, "fsync", lambda descriptor: None)
    monkeypatch.setattr(cast(Any, worker["fcntl"]), "fcntl", fake_fcntl)

    digest = publish(4, [b"abc", b"def"], 6)

    assert storage == b"abcdef"
    assert digest == hashlib.sha256(b"abcdef").hexdigest()
    assert calls == [(cast(int, worker["_F_ADD_SEALS"]), cast(int, worker["_FINAL_OUTPUT_SEALS"]))]
    assert seals == cast(int, worker["_REQUIRED_SEALS"])


class _FakeContainer:
    def __init__(self, frames: list[object]) -> None:
        self.flags = 0x10
        self.format = SimpleNamespace(name="mov")
        self.stream = SimpleNamespace(
            index=0,
            codec_context=SimpleNamespace(name="rawvideo", width=2, height=2),
            time_base=Fraction(1, 1000),
            duration=3,
            thread_type=None,
            thread_count=None,
        )
        self.streams = SimpleNamespace(video=[self.stream])
        self.frames = frames
        self.closed = False

    def decode(self, stream: object) -> list[object]:
        assert stream is self.stream
        return self.frames

    def close(self) -> None:
        self.closed = True


def _fake_frame(index: int, *, pixels: bytes | None = None) -> object:
    payload = pixels or bytes([index + 1]) * 12
    return SimpleNamespace(
        duration=1,
        height=2,
        key_frame=index == 0,
        planes=(_Plane(payload, 6),),
        pts=index,
        rotation=0,
        time_base=Fraction(1, 1000),
        width=2,
    )


def _fake_av(container: _FakeContainer) -> object:
    return SimpleNamespace(
        open=lambda *arguments, **keywords: container,
        video=SimpleNamespace(reformatter=SimpleNamespace(VideoReformatter=lambda: _Reformatter())),
    )


def test_decode_selects_exact_frame_refs_and_emits_only_pixel_free_metadata() -> None:
    worker = _worker_namespace()
    parsed = cast(Any, worker["_parse_request"])(_canonical(_request()))
    decoded = [_fake_frame(index) for index in range(3)]
    container = _FakeContainer(decoded)

    pixels, metadata, stream = cast(Any, worker["_decode_requested"])(
        _fake_av(container), parsed, object()
    )

    assert pixels == [bytes([1]) * 12, bytes([3]) * 12]
    assert [item["byte_offset"] for item in metadata] == ["0", "12"]
    assert [item["frame_ref"] for item in metadata] == _request()["frames"]
    assert [item["artifact"]["sha256"] for item in metadata] == [
        hashlib.sha256(bytes([1]) * 12).hexdigest(),
        hashlib.sha256(bytes([3]) * 12).hexdigest(),
    ]
    assert b"\x01" * 3 not in _canonical(metadata)
    assert stream == {
        "codec": "rawvideo",
        "format_name": "mov",
        "height": 2,
        "rotation_degrees": 0,
        "stream_index": 0,
        "time_base": {"denominator": "1000", "numerator": "1"},
        "width": 2,
    }
    assert container.closed is True


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("pts", 99),
        ("duration", 2),
        ("key_frame", False),
        ("width", 3),
        ("rotation", 90),
    ],
)
def test_decode_rejects_selected_frame_metadata_or_geometry_mismatch(
    attribute: str,
    value: object,
) -> None:
    worker = _worker_namespace()
    parsed = cast(Any, worker["_parse_request"])(_canonical(_request()))
    frames = [_fake_frame(index) for index in range(3)]
    setattr(frames[0], attribute, value)
    decode_failed = cast(type[Exception], worker["_DecodeFailed"])

    with pytest.raises(decode_failed, match="decode_failed"):
        cast(Any, worker["_decode_requested"])(_fake_av(_FakeContainer(frames)), parsed, object())


def test_decode_rejects_missing_requested_frame() -> None:
    worker = _worker_namespace()
    parsed = cast(Any, worker["_parse_request"])(_canonical(_request()))
    decode_failed = cast(type[Exception], worker["_DecodeFailed"])

    with pytest.raises(decode_failed, match="decode_failed"):
        cast(Any, worker["_decode_requested"])(
            _fake_av(_FakeContainer([_fake_frame(0), _fake_frame(1)])),
            parsed,
            object(),
        )


def test_measured_decode_cannot_satisfy_estimated_frame_time() -> None:
    worker = _worker_namespace()
    measured = {
        "basis": "measured",
        "time_base": {"denominator": "1000", "numerator": "1"},
        "value": "0",
    }
    estimated = {
        "basis": "estimated",
        "estimate": {
            "method": "previous_pts_plus_duration",
            "producer": {
                "configuration_sha256": "00" * 32,
                "name": "visualworld.test",
                "version": "1",
            },
        },
        "time_base": {"denominator": "1000", "numerator": "1"},
        "value": "0",
    }

    assert cast(Any, worker["_time_equal"])(measured, measured) is True
    assert cast(Any, worker["_time_equal"])(measured, estimated) is False


def test_worker_has_no_mutable_repo_import_and_error_records_are_static() -> None:
    worker = _worker_namespace()
    source = _WORKER.read_text()
    assert "from visualworld" not in source
    assert "import visualworld" not in source
    assert "error_type" not in source
    assert cast(Any, worker["_summary"])("decode_failed") == (
        b'{"schema_version":1,"status":"decode_failed"}\n'
    )
    assert cast(Any, worker["_summary"])("invalid_request") == (
        b'{"schema_version":1,"status":"invalid_request"}\n'
    )
