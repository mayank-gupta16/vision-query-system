# SPDX-License-Identifier: Apache-2.0
"""Version-1, vendor-neutral ingestion records from ADR-0004."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from typing import BinaryIO, ClassVar, NoReturn, cast

MAX_RECORD_BYTES = 256 * 1024
MAX_DEPTH = 16
MAX_OBJECT_MEMBERS = 128
MAX_ARRAY_ITEMS = 64
MAX_GENERAL_STRING_BYTES = 4_096
MAX_TOKEN_BYTES = 128
MAX_STREAMS = 32
MAX_PRODUCERS = 64
MAX_I31 = 2**31 - 1
MAX_U32 = 2**32 - 1
MAX_U64 = 2**64 - 1
MIN_I64 = -(2**63)
MAX_I64 = 2**63 - 1

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_UNSIGNED_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_SIGNED_DECIMAL_RE = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/-]*\Z")


class RecordValidationError(ValueError):
    """A bounded validation failure that never echoes the untrusted record."""

    def __init__(self, code: str, path: str = "$") -> None:
        self.code = code
        self.path = path
        super().__init__(f"{code} at {path}")


def _fail(code: str, path: str = "$") -> NoReturn:
    raise RecordValidationError(code, path)


def _mapping(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail("expected_object", path)
    return cast(dict[str, object], value)


def _array(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        _fail("expected_array", path)
    return cast(list[object], value)


def _exact_fields(
    value: object,
    required: set[str],
    path: str,
    optional: set[str] | None = None,
) -> dict[str, object]:
    mapping = _mapping(value, path)
    keys = set(mapping)
    allowed = required | (optional or set())
    if not required.issubset(keys):
        _fail("missing_field", path)
    if not keys.issubset(allowed):
        _fail("unknown_field", path)
    return mapping


def _general_string(value: object, path: str) -> str:
    if not isinstance(value, str):
        _fail("expected_string", path)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail("invalid_unicode", path)
    if len(encoded) > MAX_GENERAL_STRING_BYTES:
        _fail("string_too_long", path)
    if unicodedata.normalize("NFC", value) != value:
        _fail("string_not_nfc", path)
    return value


def _token(value: object, path: str, allowed: frozenset[str] | None = None) -> str:
    token = _general_string(value, path)
    if not token.isascii():
        _fail("token_not_ascii", path)
    if len(token.encode("ascii")) > MAX_TOKEN_BYTES or not _TOKEN_RE.fullmatch(token):
        _fail("invalid_token", path)
    if allowed is not None and token not in allowed:
        _fail("unknown_enum", path)
    return token


def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool:
        _fail("expected_boolean", path)
    return value


def _bounded_int(value: object, minimum: int, maximum: int, path: str) -> int:
    if type(value) is not int:
        _fail("expected_integer", path)
    integer = value
    if not minimum <= integer <= maximum:
        _fail("integer_out_of_range", path)
    return integer


def _decimal(
    value: object,
    *,
    signed: bool,
    minimum: int,
    maximum: int,
    path: str,
) -> str:
    decimal = _general_string(value, path)
    pattern = _SIGNED_DECIMAL_RE if signed else _UNSIGNED_DECIMAL_RE
    if not pattern.fullmatch(decimal):
        _fail("invalid_decimal", path)
    integer = int(decimal)
    if not minimum <= integer <= maximum:
        _fail("decimal_out_of_range", path)
    return decimal


def _sha256(value: object, path: str) -> str:
    digest = _general_string(value, path)
    if not _SHA256_RE.fullmatch(digest):
        _fail("invalid_sha256", path)
    return digest


def _typed_id(value: object, prefix: str, path: str) -> str:
    identifier = _general_string(value, path)
    if not re.fullmatch(rf"{prefix}_[0-9a-f]{{64}}", identifier):
        _fail("invalid_identifier", path)
    return identifier


def _validate_shape(value: object, depth: int = 1, path: str = "$") -> None:
    if depth > MAX_DEPTH:
        _fail("maximum_depth_exceeded", path)
    if isinstance(value, dict):
        if len(value) > MAX_OBJECT_MEMBERS:
            _fail("too_many_object_members", path)
        for key, item in value.items():
            _general_string(key, path)
            _validate_shape(item, depth + 1, f"{path}.{key}")
    elif isinstance(value, list):
        if len(value) > MAX_ARRAY_ITEMS:
            _fail("too_many_array_items", path)
        for index, item in enumerate(value):
            _validate_shape(item, depth + 1, f"{path}[{index}]")
    elif value is None or type(value) in {bool, int}:
        return
    elif isinstance(value, str):
        _general_string(value, path)
    else:
        _fail("unsupported_json_value", path)


def _canonical_bytes(value: dict[str, object]) -> bytes:
    _validate_shape(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise RecordValidationError("canonicalization_failed") from error
    if len(encoded) > MAX_RECORD_BYTES:
        _fail("canonical_record_too_large")
    return encoded


def _identifier(prefix: str, projection: dict[str, object]) -> str:
    return f"{prefix}_{hashlib.sha256(_canonical_bytes(projection)).hexdigest()}"


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_key")
        result[key] = value
    return result


def _reject_json_number(_: str) -> NoReturn:
    _fail("floating_point_forbidden")


def _parse_json(data: bytes) -> dict[str, object]:
    if len(data) > MAX_RECORD_BYTES:
        _fail("encoded_record_too_large")
    if data.startswith(b"\xef\xbb\xbf"):
        _fail("bom_forbidden")
    try:
        text = data.decode("utf-8", errors="strict")
        value: object = json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except RecordValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise RecordValidationError("invalid_json") from error
    _validate_shape(value)
    return _mapping(value, "$")


@dataclass(frozen=True, slots=True)
class Fingerprint:
    digest: str
    bytes: str
    algorithm: str = "sha256"

    def __post_init__(self) -> None:
        _token(self.algorithm, "fingerprint.algorithm", frozenset({"sha256"}))
        _sha256(self.digest, "fingerprint.digest")
        _decimal(
            self.bytes,
            signed=False,
            minimum=0,
            maximum=MAX_U64,
            path="fingerprint.bytes",
        )

    def to_mapping(self) -> dict[str, object]:
        return {"algorithm": self.algorithm, "bytes": self.bytes, "digest": self.digest}

    @classmethod
    def from_mapping(cls, value: object, path: str = "fingerprint") -> Fingerprint:
        item = _exact_fields(value, {"algorithm", "bytes", "digest"}, path)
        return cls(
            algorithm=_token(item["algorithm"], f"{path}.algorithm"),
            bytes=_general_string(item["bytes"], f"{path}.bytes"),
            digest=_general_string(item["digest"], f"{path}.digest"),
        )


@dataclass(frozen=True, slots=True)
class TimeBase:
    numerator: str
    denominator: str

    def __post_init__(self) -> None:
        _decimal(
            self.numerator,
            signed=False,
            minimum=1,
            maximum=MAX_U32,
            path="time_base.numerator",
        )
        _decimal(
            self.denominator,
            signed=False,
            minimum=1,
            maximum=MAX_U32,
            path="time_base.denominator",
        )

    def to_mapping(self) -> dict[str, object]:
        return {"denominator": self.denominator, "numerator": self.numerator}

    @classmethod
    def from_mapping(cls, value: object, path: str = "time_base") -> TimeBase:
        item = _exact_fields(value, {"denominator", "numerator"}, path)
        return cls(
            numerator=_general_string(item["numerator"], f"{path}.numerator"),
            denominator=_general_string(item["denominator"], f"{path}.denominator"),
        )


@dataclass(frozen=True, slots=True)
class Producer:
    name: str
    version: str
    configuration_sha256: str

    def __post_init__(self) -> None:
        _token(self.name, "producer.name")
        _token(self.version, "producer.version")
        _sha256(self.configuration_sha256, "producer.configuration_sha256")

    def to_mapping(self) -> dict[str, object]:
        return {
            "configuration_sha256": self.configuration_sha256,
            "name": self.name,
            "version": self.version,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "producer") -> Producer:
        item = _exact_fields(value, {"configuration_sha256", "name", "version"}, path)
        return cls(
            name=_token(item["name"], f"{path}.name"),
            version=_token(item["version"], f"{path}.version"),
            configuration_sha256=_general_string(
                item["configuration_sha256"], f"{path}.configuration_sha256"
            ),
        )


@dataclass(frozen=True, slots=True)
class MediaTime:
    value: str
    time_base: TimeBase
    basis: str = "measured"
    estimate_method: str | None = None
    estimate_producer: Producer | None = None

    def __post_init__(self) -> None:
        _token(self.basis, "media_time.basis", frozenset({"measured", "estimated"}))
        _decimal(
            self.value,
            signed=True,
            minimum=MIN_I64,
            maximum=MAX_I64,
            path="media_time.value",
        )
        if self.basis == "measured":
            if self.estimate_method is not None or self.estimate_producer is not None:
                _fail("estimate_forbidden", "media_time.estimate")
        elif self.estimate_method is None or self.estimate_producer is None:
            _fail("estimate_required", "media_time.estimate")
        else:
            _token(
                self.estimate_method,
                "media_time.estimate.method",
                frozenset({"previous_pts_plus_duration"}),
            )

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "basis": self.basis,
            "time_base": self.time_base.to_mapping(),
            "value": self.value,
        }
        if self.basis == "estimated":
            if self.estimate_method is None or self.estimate_producer is None:
                _fail("estimate_required", "media_time.estimate")
            result["estimate"] = {
                "method": self.estimate_method,
                "producer": self.estimate_producer.to_mapping(),
            }
        return result

    @classmethod
    def from_mapping(cls, value: object, path: str = "media_time") -> MediaTime:
        base = _mapping(value, path)
        basis = _token(base.get("basis"), f"{path}.basis", frozenset({"measured", "estimated"}))
        required = {"basis", "time_base", "value"}
        if basis == "estimated":
            required.add("estimate")
        item = _exact_fields(base, required, path)
        estimate_method: str | None = None
        estimate_producer: Producer | None = None
        if basis == "estimated":
            estimate = _exact_fields(item["estimate"], {"method", "producer"}, f"{path}.estimate")
            estimate_method = _token(estimate["method"], f"{path}.estimate.method")
            estimate_producer = Producer.from_mapping(
                estimate["producer"], f"{path}.estimate.producer"
            )
        return cls(
            value=_general_string(item["value"], f"{path}.value"),
            time_base=TimeBase.from_mapping(item["time_base"], f"{path}.time_base"),
            basis=basis,
            estimate_method=estimate_method,
            estimate_producer=estimate_producer,
        )


def compare_media_time(left: MediaTime, right: MediaTime) -> int:
    """Compare exact timestamp values without converting to floating point."""
    left_value = int(left.value) * int(left.time_base.numerator) * int(right.time_base.denominator)
    right_value = (
        int(right.value) * int(right.time_base.numerator) * int(left.time_base.denominator)
    )
    return (left_value > right_value) - (left_value < right_value)


@dataclass(frozen=True, slots=True)
class SourceStream:
    stream_index: int
    width: int
    height: int
    rotation_degrees: int
    time_base: TimeBase
    media_type: str = "video"

    def __post_init__(self) -> None:
        _bounded_int(self.stream_index, 0, MAX_I31, "stream.stream_index")
        _bounded_int(self.width, 1, MAX_I31, "stream.width")
        _bounded_int(self.height, 1, MAX_I31, "stream.height")
        _bounded_int(self.rotation_degrees, -MAX_I31, MAX_I31, "stream.rotation_degrees")
        _token(self.media_type, "stream.media_type", frozenset({"video"}))

    def to_mapping(self) -> dict[str, object]:
        return {
            "height": self.height,
            "media_type": self.media_type,
            "rotation_degrees": self.rotation_degrees,
            "stream_index": self.stream_index,
            "time_base": self.time_base.to_mapping(),
            "width": self.width,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "stream") -> SourceStream:
        item = _exact_fields(
            value,
            {"height", "media_type", "rotation_degrees", "stream_index", "time_base", "width"},
            path,
        )
        return cls(
            stream_index=_bounded_int(item["stream_index"], 0, MAX_I31, f"{path}.stream_index"),
            width=_bounded_int(item["width"], 1, MAX_I31, f"{path}.width"),
            height=_bounded_int(item["height"], 1, MAX_I31, f"{path}.height"),
            rotation_degrees=_bounded_int(
                item["rotation_degrees"], -MAX_I31, MAX_I31, f"{path}.rotation_degrees"
            ),
            time_base=TimeBase.from_mapping(item["time_base"], f"{path}.time_base"),
            media_type=_token(item["media_type"], f"{path}.media_type"),
        )


@dataclass(frozen=True, slots=True)
class Rational:
    numerator: str
    denominator: str

    def __post_init__(self) -> None:
        _decimal(
            self.numerator,
            signed=True,
            minimum=MIN_I64,
            maximum=MAX_I64,
            path="rational.numerator",
        )
        _decimal(
            self.denominator,
            signed=False,
            minimum=1,
            maximum=MAX_U32,
            path="rational.denominator",
        )

    def to_mapping(self) -> dict[str, object]:
        return {"denominator": self.denominator, "numerator": self.numerator}

    @classmethod
    def from_mapping(cls, value: object, path: str) -> Rational:
        item = _exact_fields(value, {"denominator", "numerator"}, path)
        return cls(
            numerator=_general_string(item["numerator"], f"{path}.numerator"),
            denominator=_general_string(item["denominator"], f"{path}.denominator"),
        )

    def fraction(self) -> Fraction:
        return Fraction(int(self.numerator), int(self.denominator))


@dataclass(frozen=True, slots=True)
class ProducerSpace:
    width: int
    height: int
    box_xyxy: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        _bounded_int(self.width, 1, MAX_I31, "producer_space.width")
        _bounded_int(self.height, 1, MAX_I31, "producer_space.height")
        _validate_box(self.box_xyxy, self.width, self.height, "producer_space.box_xyxy")

    def to_mapping(self) -> dict[str, object]:
        return {"box_xyxy": list(self.box_xyxy), "height": self.height, "width": self.width}

    @classmethod
    def from_mapping(cls, value: object, path: str = "producer_space") -> ProducerSpace:
        item = _exact_fields(value, {"box_xyxy", "height", "width"}, path)
        width = _bounded_int(item["width"], 1, MAX_I31, f"{path}.width")
        height = _bounded_int(item["height"], 1, MAX_I31, f"{path}.height")
        box = _parse_box(item["box_xyxy"], width, height, f"{path}.box_xyxy")
        return cls(width=width, height=height, box_xyxy=box)


@dataclass(frozen=True, slots=True)
class AffineCoefficients:
    a: Rational
    b: Rational
    c: Rational
    d: Rational
    e: Rational
    f: Rational

    def to_mapping(self) -> dict[str, object]:
        return {
            "a": self.a.to_mapping(),
            "b": self.b.to_mapping(),
            "c": self.c.to_mapping(),
            "d": self.d.to_mapping(),
            "e": self.e.to_mapping(),
            "f": self.f.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "coefficients") -> AffineCoefficients:
        item = _exact_fields(value, {"a", "b", "c", "d", "e", "f"}, path)
        return cls(
            **{
                key: Rational.from_mapping(item[key], f"{path}.{key}")
                for key in ("a", "b", "c", "d", "e", "f")
            }
        )


def _validate_box(box: tuple[int, int, int, int], width: int, height: int, path: str) -> None:
    if not isinstance(box, tuple) or len(box) != 4:
        _fail("invalid_box", path)
    x_min, y_min, x_max, y_max = box
    for index, coordinate in enumerate(box):
        _bounded_int(coordinate, 0, MAX_I31, f"{path}[{index}]")
    if not (x_min < x_max <= width and y_min < y_max <= height):
        _fail("box_out_of_bounds", path)


def _parse_box(value: object, width: int, height: int, path: str) -> tuple[int, int, int, int]:
    items = _array(value, path)
    if len(items) != 4:
        _fail("invalid_box", path)
    box = tuple(
        _bounded_int(item, 0, MAX_I31, f"{path}[{index}]") for index, item in enumerate(items)
    )
    typed = cast(tuple[int, int, int, int], box)
    _validate_box(typed, width, height, path)
    return typed


@dataclass(frozen=True, slots=True)
class Geometry:
    source_width: int
    source_height: int
    box_xyxy: tuple[int, int, int, int]
    measurement: str
    transform_kind: str = "identity"
    producer_space: ProducerSpace | None = None
    coefficients: AffineCoefficients | None = None
    space: str = "source_pixels"

    def __post_init__(self) -> None:
        _token(self.space, "geometry.space", frozenset({"source_pixels"}))
        _bounded_int(self.source_width, 1, MAX_I31, "geometry.source_width")
        _bounded_int(self.source_height, 1, MAX_I31, "geometry.source_height")
        _validate_box(
            self.box_xyxy,
            self.source_width,
            self.source_height,
            "geometry.box_xyxy",
        )
        _token(
            self.measurement,
            "geometry.measurement",
            frozenset({"measured", "calibrated", "estimated", "inferred", "unknown"}),
        )
        _token(
            self.transform_kind,
            "geometry.transform_to_source.kind",
            frozenset({"identity", "affine_rational"}),
        )
        if self.transform_kind == "identity":
            if self.producer_space is not None or self.coefficients is not None:
                _fail("identity_transform_has_producer_space", "geometry")
        elif self.producer_space is None or self.coefficients is None:
            _fail("affine_transform_missing_producer_space", "geometry")
        else:
            self._validate_affine_box()

    def _validate_affine_box(self) -> None:
        if self.producer_space is None or self.coefficients is None:
            _fail("affine_transform_missing_producer_space", "geometry")
        x_min, y_min, x_max, y_max = self.producer_space.box_xyxy
        coefficients = self.coefficients
        a, b, c = (
            coefficients.a.fraction(),
            coefficients.b.fraction(),
            coefficients.c.fraction(),
        )
        d, e, f = (
            coefficients.d.fraction(),
            coefficients.e.fraction(),
            coefficients.f.fraction(),
        )
        corners = ((x_min, y_min), (x_min, y_max), (x_max, y_min), (x_max, y_max))
        xs = [a * x + b * y + c for x, y in corners]
        ys = [d * x + e * y + f for x, y in corners]
        transformed = (
            min(xs).__floor__(),
            min(ys).__floor__(),
            max(xs).__ceil__(),
            max(ys).__ceil__(),
        )
        if transformed != self.box_xyxy:
            _fail("affine_box_mismatch", "geometry.box_xyxy")

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "box_xyxy": list(self.box_xyxy),
            "measurement": self.measurement,
            "source_height": self.source_height,
            "source_width": self.source_width,
            "space": self.space,
        }
        if self.transform_kind == "identity":
            result["transform_to_source"] = {"kind": "identity"}
        else:
            if self.coefficients is None or self.producer_space is None:
                _fail("affine_transform_missing_producer_space", "geometry")
            result["producer_space"] = self.producer_space.to_mapping()
            result["transform_to_source"] = {
                "coefficients": self.coefficients.to_mapping(),
                "kind": "affine_rational",
            }
        return result

    @classmethod
    def from_mapping(cls, value: object, path: str = "geometry") -> Geometry:
        base = _mapping(value, path)
        transform = _mapping(base.get("transform_to_source"), f"{path}.transform_to_source")
        kind = _token(
            transform.get("kind"),
            f"{path}.transform_to_source.kind",
            frozenset({"identity", "affine_rational"}),
        )
        required = {
            "box_xyxy",
            "measurement",
            "source_height",
            "source_width",
            "space",
            "transform_to_source",
        }
        if kind == "affine_rational":
            required.add("producer_space")
            transform = _exact_fields(
                transform, {"coefficients", "kind"}, f"{path}.transform_to_source"
            )
        else:
            transform = _exact_fields(transform, {"kind"}, f"{path}.transform_to_source")
        item = _exact_fields(base, required, path)
        width = _bounded_int(item["source_width"], 1, MAX_I31, f"{path}.source_width")
        height = _bounded_int(item["source_height"], 1, MAX_I31, f"{path}.source_height")
        return cls(
            source_width=width,
            source_height=height,
            box_xyxy=_parse_box(item["box_xyxy"], width, height, f"{path}.box_xyxy"),
            measurement=_token(item["measurement"], f"{path}.measurement"),
            transform_kind=kind,
            producer_space=(
                ProducerSpace.from_mapping(item["producer_space"], f"{path}.producer_space")
                if kind == "affine_rational"
                else None
            ),
            coefficients=(
                AffineCoefficients.from_mapping(
                    transform["coefficients"], f"{path}.transform_to_source.coefficients"
                )
                if kind == "affine_rational"
                else None
            ),
            space=_token(item["space"], f"{path}.space"),
        )


@dataclass(frozen=True, slots=True)
class Artifact:
    sha256: str
    bytes: str
    media_type: str = "application/vnd.visualworld.rgb24"

    def __post_init__(self) -> None:
        _sha256(self.sha256, "artifact.sha256")
        _decimal(
            self.bytes,
            signed=False,
            minimum=0,
            maximum=MAX_U64,
            path="artifact.bytes",
        )
        _token(
            self.media_type,
            "artifact.media_type",
            frozenset({"application/vnd.visualworld.rgb24"}),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"bytes": self.bytes, "media_type": self.media_type, "sha256": self.sha256}

    @classmethod
    def from_mapping(cls, value: object, path: str = "artifact") -> Artifact:
        item = _exact_fields(value, {"bytes", "media_type", "sha256"}, path)
        return cls(
            sha256=_general_string(item["sha256"], f"{path}.sha256"),
            bytes=_general_string(item["bytes"], f"{path}.bytes"),
            media_type=_token(item["media_type"], f"{path}.media_type"),
        )


class _Record:
    schema: ClassVar[str]
    schema_version: ClassVar[int] = 1
    identity_version: ClassVar[int] = 1

    def to_mapping(self) -> dict[str, object]:
        raise NotImplementedError

    def identity_projection(self) -> dict[str, object]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Source(_Record):
    source_id: str
    fingerprint: Fingerprint
    streams: tuple[SourceStream, ...] = ()

    schema: ClassVar[str] = "visualworld.source"

    def __post_init__(self) -> None:
        _typed_id(self.source_id, "src", "source_id")
        if len(self.streams) > MAX_STREAMS:
            _fail("too_many_streams", "streams")
        if not all(isinstance(stream, SourceStream) for stream in self.streams):
            _fail("invalid_stream", "streams")
        indexes = [stream.stream_index for stream in self.streams]
        if len(indexes) != len(set(indexes)):
            _fail("duplicate_stream_index", "streams")
        if self.source_id != _identifier("src", self.identity_projection()):
            _fail("identifier_mismatch", "source_id")

    @classmethod
    def create(cls, fingerprint: Fingerprint, streams: tuple[SourceStream, ...] = ()) -> Source:
        projection = {"fingerprint": fingerprint.to_mapping(), "identity_version": 1}
        return cls(
            source_id=_identifier("src", projection), fingerprint=fingerprint, streams=streams
        )

    def identity_projection(self) -> dict[str, object]:
        return {"fingerprint": self.fingerprint.to_mapping(), "identity_version": 1}

    def to_mapping(self) -> dict[str, object]:
        return {
            "access": {"classification": "private", "retention": "source_controlled"},
            "fingerprint": self.fingerprint.to_mapping(),
            "identity_version": 1,
            "origin": {"kind": "local_file", "locator_stored": False},
            "schema": self.schema,
            "schema_version": 1,
            "source_id": self.source_id,
            "streams": [stream.to_mapping() for stream in self.streams],
        }

    @classmethod
    def from_mapping(cls, value: object) -> Source:
        item = _record_fields(
            value, {"access", "fingerprint", "origin", "source_id", "streams"}, cls.schema
        )
        origin = _exact_fields(item["origin"], {"kind", "locator_stored"}, "origin")
        _token(origin["kind"], "origin.kind", frozenset({"local_file"}))
        if _boolean(origin["locator_stored"], "origin.locator_stored"):
            _fail("stored_locator_forbidden", "origin.locator_stored")
        access = _exact_fields(item["access"], {"classification", "retention"}, "access")
        _token(access["classification"], "access.classification", frozenset({"private"}))
        _token(access["retention"], "access.retention", frozenset({"source_controlled"}))
        stream_values = _array(item["streams"], "streams")
        if len(stream_values) > MAX_STREAMS:
            _fail("too_many_streams", "streams")
        return cls(
            source_id=_general_string(item["source_id"], "source_id"),
            fingerprint=Fingerprint.from_mapping(item["fingerprint"]),
            streams=tuple(
                SourceStream.from_mapping(stream, f"streams[{index}]")
                for index, stream in enumerate(stream_values)
            ),
        )


@dataclass(frozen=True, slots=True)
class FrameRef(_Record):
    frame_id: str
    source_id: str
    stream_index: int
    decode_index: str
    pts: MediaTime
    duration: MediaTime | None = None
    key_frame: bool | None = None

    schema: ClassVar[str] = "visualworld.frame_ref"

    def __post_init__(self) -> None:
        _typed_id(self.frame_id, "frm", "frame_id")
        _typed_id(self.source_id, "src", "source_id")
        _bounded_int(self.stream_index, 0, MAX_I31, "stream_index")
        _decimal(
            self.decode_index,
            signed=False,
            minimum=0,
            maximum=MAX_U64,
            path="decode_index",
        )
        if self.duration is not None and not isinstance(self.duration, MediaTime):
            _fail("invalid_duration", "duration")
        if self.key_frame is not None and type(self.key_frame) is not bool:
            _fail("invalid_key_frame", "key_frame")
        if self.frame_id != _identifier("frm", self.identity_projection()):
            _fail("identifier_mismatch", "frame_id")

    @classmethod
    def create(
        cls,
        source_id: str,
        stream_index: int,
        decode_index: str,
        pts: MediaTime,
        duration: MediaTime | None = None,
        key_frame: bool | None = None,
    ) -> FrameRef:
        projection: dict[str, object] = {
            "decode_index": decode_index,
            "identity_version": 1,
            "pts": pts.to_mapping(),
            "source_id": source_id,
            "stream_index": stream_index,
        }
        return cls(
            frame_id=_identifier("frm", projection),
            source_id=source_id,
            stream_index=stream_index,
            decode_index=decode_index,
            pts=pts,
            duration=duration,
            key_frame=key_frame,
        )

    def identity_projection(self) -> dict[str, object]:
        return {
            "decode_index": self.decode_index,
            "identity_version": 1,
            "pts": self.pts.to_mapping(),
            "source_id": self.source_id,
            "stream_index": self.stream_index,
        }

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "decode_index": self.decode_index,
            "frame_id": self.frame_id,
            "identity_version": 1,
            "pts": self.pts.to_mapping(),
            "schema": self.schema,
            "schema_version": 1,
            "source_id": self.source_id,
            "stream_index": self.stream_index,
        }
        if self.duration is not None:
            result["duration"] = self.duration.to_mapping()
        if self.key_frame is not None:
            result["key_frame"] = self.key_frame
        return result

    @classmethod
    def from_mapping(cls, value: object) -> FrameRef:
        item = _record_fields(
            value,
            {"decode_index", "frame_id", "pts", "source_id", "stream_index"},
            cls.schema,
            {"duration", "key_frame"},
        )
        return cls(
            frame_id=_general_string(item["frame_id"], "frame_id"),
            source_id=_general_string(item["source_id"], "source_id"),
            stream_index=_bounded_int(item["stream_index"], 0, MAX_I31, "stream_index"),
            decode_index=_general_string(item["decode_index"], "decode_index"),
            pts=MediaTime.from_mapping(item["pts"], "pts"),
            duration=(
                MediaTime.from_mapping(item["duration"], "duration") if "duration" in item else None
            ),
            key_frame=(_boolean(item["key_frame"], "key_frame") if "key_frame" in item else None),
        )


@dataclass(frozen=True, slots=True)
class EvidenceRef(_Record):
    evidence_id: str
    frame_id: str
    artifact: Artifact
    geometry: Geometry | None
    kind: str = "original_frame"
    retention: str = "derived_private"

    schema: ClassVar[str] = "visualworld.evidence_ref"

    def __post_init__(self) -> None:
        _typed_id(self.evidence_id, "evi", "evidence_id")
        _typed_id(self.frame_id, "frm", "frame_id")
        _token(self.kind, "kind", frozenset({"original_frame"}))
        _token(self.retention, "retention", frozenset({"derived_private"}))
        if self.geometry is not None and not isinstance(self.geometry, Geometry):
            _fail("invalid_geometry", "geometry")
        if self.evidence_id != _identifier("evi", self.identity_projection()):
            _fail("identifier_mismatch", "evidence_id")

    @classmethod
    def create(
        cls,
        frame_id: str,
        artifact: Artifact,
        geometry: Geometry | None,
        kind: str = "original_frame",
        retention: str = "derived_private",
    ) -> EvidenceRef:
        projection: dict[str, object] = {
            "artifact_sha256": artifact.sha256,
            "frame_id": frame_id,
            "geometry": geometry.to_mapping() if geometry is not None else None,
            "identity_version": 1,
            "kind": kind,
        }
        return cls(
            evidence_id=_identifier("evi", projection),
            frame_id=frame_id,
            artifact=artifact,
            geometry=geometry,
            kind=kind,
            retention=retention,
        )

    def identity_projection(self) -> dict[str, object]:
        return {
            "artifact_sha256": self.artifact.sha256,
            "frame_id": self.frame_id,
            "geometry": self.geometry.to_mapping() if self.geometry is not None else None,
            "identity_version": 1,
            "kind": self.kind,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact": self.artifact.to_mapping(),
            "evidence_id": self.evidence_id,
            "frame_id": self.frame_id,
            "geometry": self.geometry.to_mapping() if self.geometry is not None else None,
            "identity_version": 1,
            "kind": self.kind,
            "retention": self.retention,
            "schema": self.schema,
            "schema_version": 1,
        }

    @classmethod
    def from_mapping(cls, value: object) -> EvidenceRef:
        item = _record_fields(
            value,
            {"artifact", "evidence_id", "frame_id", "geometry", "kind", "retention"},
            cls.schema,
        )
        geometry_value = item["geometry"]
        return cls(
            evidence_id=_general_string(item["evidence_id"], "evidence_id"),
            frame_id=_general_string(item["frame_id"], "frame_id"),
            artifact=Artifact.from_mapping(item["artifact"]),
            geometry=(None if geometry_value is None else Geometry.from_mapping(geometry_value)),
            kind=_token(item["kind"], "kind"),
            retention=_token(item["retention"], "retention"),
        )


@dataclass(frozen=True, slots=True)
class Sampling:
    target_fps: Rational
    policy: str = "nearest_eligible_pts"

    def __post_init__(self) -> None:
        _token(self.policy, "sampling.policy", frozenset({"nearest_eligible_pts"}))
        if int(self.target_fps.numerator) <= 0:
            _fail("target_fps_must_be_positive", "sampling.target_fps.numerator")

    def to_mapping(self) -> dict[str, object]:
        return {"policy": self.policy, "target_fps": self.target_fps.to_mapping()}

    @classmethod
    def from_mapping(cls, value: object, path: str = "sampling") -> Sampling:
        item = _exact_fields(value, {"policy", "target_fps"}, path)
        return cls(
            policy=_token(item["policy"], f"{path}.policy"),
            target_fps=Rational.from_mapping(item["target_fps"], f"{path}.target_fps"),
        )


@dataclass(frozen=True, slots=True)
class RunOutputs:
    sample_count: str
    sample_index_sha256: str

    def __post_init__(self) -> None:
        _decimal(
            self.sample_count,
            signed=False,
            minimum=0,
            maximum=MAX_U64,
            path="outputs.sample_count",
        )
        _sha256(self.sample_index_sha256, "outputs.sample_index_sha256")

    def to_mapping(self) -> dict[str, object]:
        return {
            "sample_count": self.sample_count,
            "sample_index_sha256": self.sample_index_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "outputs") -> RunOutputs:
        item = _exact_fields(value, {"sample_count", "sample_index_sha256"}, path)
        return cls(
            sample_count=_general_string(item["sample_count"], f"{path}.sample_count"),
            sample_index_sha256=_general_string(
                item["sample_index_sha256"], f"{path}.sample_index_sha256"
            ),
        )


_CONTRACTS: dict[str, object] = {
    "evidence_ref": 1,
    "frame_ref": 1,
    "run_manifest": 1,
    "source": 1,
}


@dataclass(frozen=True, slots=True)
class RunManifest(_Record):
    run_id: str
    source_id: str
    producers: tuple[Producer, ...]
    sampling: Sampling
    state: str
    outputs: RunOutputs | None = None

    schema: ClassVar[str] = "visualworld.run_manifest"

    def __post_init__(self) -> None:
        _typed_id(self.run_id, "run", "run_id")
        _typed_id(self.source_id, "src", "source_id")
        if len(self.producers) > MAX_PRODUCERS:
            _fail("too_many_producers", "producers")
        if not all(isinstance(producer, Producer) for producer in self.producers):
            _fail("invalid_producer", "producers")
        _token(
            self.state,
            "state",
            frozenset({"preparing", "committed", "failed", "cancelled"}),
        )
        if self.state == "committed" and self.outputs is None:
            _fail("committed_outputs_required", "outputs")
        if self.state != "committed" and self.outputs is not None:
            _fail("incomplete_outputs_forbidden", "outputs")
        if self.run_id != _identifier("run", self.identity_projection()):
            _fail("identifier_mismatch", "run_id")

    @classmethod
    def create(
        cls,
        source_id: str,
        producers: tuple[Producer, ...],
        sampling: Sampling,
        state: str,
        outputs: RunOutputs | None = None,
    ) -> RunManifest:
        projection = {
            "contracts": dict(_CONTRACTS),
            "identity_version": 1,
            "producers": [producer.to_mapping() for producer in producers],
            "sampling": sampling.to_mapping(),
            "source_id": source_id,
        }
        return cls(
            run_id=_identifier("run", projection),
            source_id=source_id,
            producers=producers,
            sampling=sampling,
            state=state,
            outputs=outputs,
        )

    def identity_projection(self) -> dict[str, object]:
        return {
            "contracts": dict(_CONTRACTS),
            "identity_version": 1,
            "producers": [producer.to_mapping() for producer in self.producers],
            "sampling": self.sampling.to_mapping(),
            "source_id": self.source_id,
        }

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "contracts": dict(_CONTRACTS),
            "identity_version": 1,
            "producers": [producer.to_mapping() for producer in self.producers],
            "run_id": self.run_id,
            "sampling": self.sampling.to_mapping(),
            "schema": self.schema,
            "schema_version": 1,
            "source_id": self.source_id,
            "state": self.state,
        }
        if self.outputs is not None:
            result["outputs"] = self.outputs.to_mapping()
        return result

    @classmethod
    def from_mapping(cls, value: object) -> RunManifest:
        item = _record_fields(
            value,
            {"contracts", "producers", "run_id", "sampling", "source_id", "state"},
            cls.schema,
            {"outputs"},
        )
        contracts = _exact_fields(item["contracts"], set(_CONTRACTS), "contracts")
        for name in _CONTRACTS:
            if _bounded_int(contracts[name], 1, MAX_I31, f"contracts.{name}") != 1:
                _fail("unknown_contract_version", f"contracts.{name}")
        producer_values = _array(item["producers"], "producers")
        if len(producer_values) > MAX_PRODUCERS:
            _fail("too_many_producers", "producers")
        return cls(
            run_id=_general_string(item["run_id"], "run_id"),
            source_id=_general_string(item["source_id"], "source_id"),
            producers=tuple(
                Producer.from_mapping(producer, f"producers[{index}]")
                for index, producer in enumerate(producer_values)
            ),
            sampling=Sampling.from_mapping(item["sampling"]),
            state=_token(item["state"], "state"),
            outputs=(RunOutputs.from_mapping(item["outputs"]) if "outputs" in item else None),
        )


Record = Source | FrameRef | EvidenceRef | RunManifest


def _record_fields(
    value: object,
    body_fields: set[str],
    expected_schema: str,
    optional: set[str] | None = None,
) -> dict[str, object]:
    common = {"identity_version", "schema", "schema_version"}
    item = _exact_fields(value, common | body_fields, "$", optional)
    if _token(item["schema"], "schema") != expected_schema:
        _fail("schema_mismatch", "schema")
    if _bounded_int(item["schema_version"], 1, MAX_I31, "schema_version") != 1:
        _fail("unknown_schema_version", "schema_version")
    if _bounded_int(item["identity_version"], 1, MAX_I31, "identity_version") != 1:
        _fail("unknown_identity_version", "identity_version")
    return item


def dumps_record(record: Record) -> bytes:
    """Serialize one validated record using the restricted RFC 8785 profile."""
    if not isinstance(record, (Source, FrameRef, EvidenceRef, RunManifest)):
        _fail("unsupported_record")
    return _canonical_bytes(record.to_mapping())


def identity_bytes(record: Record) -> bytes:
    """Return the exact canonical identity projection used for the typed ID."""
    if not isinstance(record, (Source, FrameRef, EvidenceRef, RunManifest)):
        _fail("unsupported_record")
    return _canonical_bytes(record.identity_projection())


def loads_record(data: bytes) -> Record:
    """Parse one bounded strict-JSON v1 ingestion record."""
    if not isinstance(data, bytes):
        _fail("record_must_be_bytes")
    value = _parse_json(data)
    schema = _token(value.get("schema"), "schema")
    version = _bounded_int(value.get("schema_version"), 1, MAX_I31, "schema_version")
    if version != 1:
        _fail("unknown_schema_version", "schema_version")
    readers: dict[str, Callable[[object], Record]] = {
        Source.schema: Source.from_mapping,
        FrameRef.schema: FrameRef.from_mapping,
        EvidenceRef.schema: EvidenceRef.from_mapping,
        RunManifest.schema: RunManifest.from_mapping,
    }
    try:
        reader = readers[schema]
    except KeyError as error:
        raise RecordValidationError("unknown_schema", "schema") from error
    return reader(value)


def load_record(reader: BinaryIO) -> Record:
    """Read at most 256 KiB plus one byte before parsing a record."""
    try:
        data = reader.read(MAX_RECORD_BYTES + 1)
    except OSError as error:
        raise RecordValidationError("record_read_failed") from error
    if not isinstance(data, bytes):
        _fail("record_reader_must_be_binary")
    return loads_record(data)


__all__ = [
    "MAX_RECORD_BYTES",
    "AffineCoefficients",
    "Artifact",
    "EvidenceRef",
    "Fingerprint",
    "FrameRef",
    "Geometry",
    "MediaTime",
    "Producer",
    "ProducerSpace",
    "Rational",
    "Record",
    "RecordValidationError",
    "RunManifest",
    "RunOutputs",
    "Sampling",
    "Source",
    "SourceStream",
    "TimeBase",
    "compare_media_time",
    "dumps_record",
    "identity_bytes",
    "load_record",
    "loads_record",
]
