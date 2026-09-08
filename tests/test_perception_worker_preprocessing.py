# SPDX-License-Identifier: Apache-2.0
"""Mutation-sensitive pixel and configuration oracles for the perception worker."""

from __future__ import annotations

import os
import runpy
from math import prod
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest


def _worker_namespace() -> dict[str, object]:
    worker = Path(__file__).resolve().parents[1] / "workers/perception_worker.py"
    return runpy.run_path(os.fspath(worker))


class _Plane:
    def __init__(self, payload: bytes, line_size: int) -> None:
        self._payload = payload
        self.line_size = line_size

    def __bytes__(self) -> bytes:
        return self._payload


class _Reformatter:
    def __init__(self, converted: object) -> None:
        self._converted = converted
        self.calls: list[tuple[object, str]] = []

    def reformat(self, frame: object, *, format: str) -> object:
        self.calls.append((frame, format))
        return self._converted


def test_packed_rgb24_removes_each_decoded_row_padding() -> None:
    worker = _worker_namespace()
    packed_rgb24 = cast(Any, worker["_packed_rgb24"])
    frame = object()
    first = bytes(range(1, 10))
    second = bytes(range(11, 20))
    plane = _Plane(first + b"\xa1\xa2\xa3" + second + b"\xb1\xb2\xb3", 12)
    converted = SimpleNamespace(width=3, height=2, planes=(plane,))
    reformatter = _Reformatter(converted)

    assert packed_rgb24(frame, reformatter) == first + second
    assert reformatter.calls == [(frame, "rgb24")]


class _Array:
    def __init__(self, data: bytes, shape: tuple[int, ...], *, contiguous: bool) -> None:
        if prod(shape) != len(data):
            raise AssertionError("test array shape does not match its data")
        self._data = data
        self.shape = shape
        self.contiguous = contiguous

    def reshape(self, shape: tuple[int, ...]) -> _Array:
        return _Array(self._data, shape, contiguous=self.contiguous)

    def tobytes(self) -> bytes:
        return self._data


class _NumpyOracle:
    uint8 = object()

    def __init__(self) -> None:
        self.frombuffer_calls: list[tuple[bytes, object]] = []
        self.rot90_calls: list[int] = []
        self.contiguous_inputs: list[_Array] = []

    def frombuffer(self, payload: bytes, *, dtype: object) -> _Array:
        self.frombuffer_calls.append((payload, dtype))
        return _Array(payload, (len(payload),), contiguous=True)

    def rot90(self, array: _Array, *, k: int) -> _Array:
        self.rot90_calls.append(k)
        height, width, channels = array.shape
        if channels != 3:
            raise AssertionError("oracle expects RGB24")
        raw = array.tobytes()
        rows = [
            [raw[(y * width + x) * 3 : (y * width + x + 1) * 3] for x in range(width)]
            for y in range(height)
        ]
        for _ in range(k % 4):
            rows = [list(column) for column in zip(*rows, strict=True)][::-1]
        rotated = b"".join(pixel for row in rows for pixel in row)
        return _Array(rotated, (len(rows), len(rows[0]), 3), contiguous=False)

    def ascontiguousarray(self, array: _Array) -> _Array:
        self.contiguous_inputs.append(array)
        return _Array(array.tobytes(), array.shape, contiguous=True)


_A = b"\x01\x02\x03"
_B = b"\x11\x12\x13"
_C = b"\x21\x22\x23"
_D = b"\x31\x32\x33"
_E = b"\x41\x42\x43"
_F = b"\x51\x52\x53"


@pytest.mark.parametrize(
    ("rotation", "expected_shape", "expected_pixels", "expected_k"),
    [
        (0, (1, 2, 3, 3), _A + _B + _C + _D + _E + _F, []),
        (90, (1, 3, 2, 3), _D + _A + _E + _B + _F + _C, [3]),
        (180, (1, 2, 3, 3), _F + _E + _D + _C + _B + _A, [2]),
        (270, (1, 3, 2, 3), _C + _F + _B + _E + _A + _D, [1]),
    ],
)
def test_generated_asymmetric_rgb_oracle_produces_contiguous_nhwc_batch(
    rotation: int,
    expected_shape: tuple[int, int, int, int],
    expected_pixels: bytes,
    expected_k: list[int],
) -> None:
    worker = _worker_namespace()
    inference_batch = cast(Any, worker["_inference_batch"])
    pixels = _A + _B + _C + _D + _E + _F
    np = _NumpyOracle()

    batch = cast(_Array, inference_batch(pixels, 3, 2, rotation, np))

    assert batch.shape == expected_shape
    assert batch.tobytes() == expected_pixels
    assert batch.contiguous is True
    assert np.frombuffer_calls == [(pixels, np.uint8)]
    assert np.rot90_calls == expected_k
    assert len(np.contiguous_inputs) == 1


class _Fluent:
    def __init__(self, calls: list[tuple[object, ...]], role: str) -> None:
        self._calls = calls
        self._role = role

    def _record(self, method: str, *arguments: object) -> _Fluent:
        self._calls.append((self._role, method, *arguments))
        return self

    def set_element_type(self, value: object) -> _Fluent:
        return self._record("set_element_type", value)

    def set_layout(self, value: object) -> _Fluent:
        return self._record("set_layout", value)

    def set_color_format(self, value: object) -> _Fluent:
        return self._record("set_color_format", value)

    def set_spatial_dynamic_shape(self) -> _Fluent:
        return self._record("set_spatial_dynamic_shape")

    def convert_color(self, value: object) -> _Fluent:
        return self._record("convert_color", value)

    def resize(self, value: object) -> _Fluent:
        return self._record("resize", value)


class _Input:
    def __init__(self, calls: list[tuple[object, ...]]) -> None:
        self._calls = calls
        self._tensor = _Fluent(calls, "tensor")
        self._preprocess = _Fluent(calls, "preprocess")
        self._model = _Fluent(calls, "model")

    def tensor(self) -> _Fluent:
        self._calls.append(("input", "tensor"))
        return self._tensor

    def preprocess(self) -> _Fluent:
        self._calls.append(("input", "preprocess"))
        return self._preprocess

    def model(self) -> _Fluent:
        self._calls.append(("input", "model"))
        return self._model


class _Processor:
    def __init__(self, calls: list[tuple[object, ...]], built: object) -> None:
        self._calls = calls
        self._input = _Input(calls)
        self._built = built

    def input(self) -> _Input:
        self._calls.append(("processor", "input"))
        return self._input

    def build(self) -> object:
        self._calls.append(("processor", "build"))
        return self._built


def test_openvino_preprocessing_configuration_is_exact() -> None:
    worker = _worker_namespace()
    preprocessed_model = cast(Any, worker["_preprocessed_model"])
    calls: list[tuple[object, ...]] = []
    model = object()
    built = object()
    processor = _Processor(calls, built)

    def pre_post_processor(value: object) -> _Processor:
        calls.append(("preprocess", "PrePostProcessor", value))
        return processor

    def layout(value: str) -> str:
        calls.append(("openvino", "Layout", value))
        return f"layout:{value}"

    ov = SimpleNamespace(Type=SimpleNamespace(u8="u8"), Layout=layout)
    preprocess = SimpleNamespace(
        ColorFormat=SimpleNamespace(RGB="RGB", BGR="BGR"),
        ResizeAlgorithm=SimpleNamespace(RESIZE_LINEAR="RESIZE_LINEAR"),
        PrePostProcessor=pre_post_processor,
    )

    assert preprocessed_model(model, ov, preprocess) is built
    assert calls == [
        ("preprocess", "PrePostProcessor", model),
        ("processor", "input"),
        ("input", "tensor"),
        ("tensor", "set_element_type", "u8"),
        ("openvino", "Layout", "NHWC"),
        ("tensor", "set_layout", "layout:NHWC"),
        ("tensor", "set_color_format", "RGB"),
        ("tensor", "set_spatial_dynamic_shape"),
        ("processor", "input"),
        ("input", "preprocess"),
        ("preprocess", "convert_color", "BGR"),
        ("preprocess", "resize", "RESIZE_LINEAR"),
        ("processor", "input"),
        ("input", "model"),
        ("openvino", "Layout", "NCHW"),
        ("model", "set_layout", "layout:NCHW"),
        ("processor", "build"),
    ]
