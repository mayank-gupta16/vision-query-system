# SPDX-License-Identifier: Apache-2.0
"""Machine-readable CLI for the deterministic version-1 fake/manual slice."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import NoReturn, TextIO, cast

from visualworld import __version__
from visualworld.coordinator import (
    CoordinatorError,
    IngestionConfig,
    IngestionCoordinator,
    ManualEvidenceInput,
)
from visualworld.geometry import CropError, Rgb24Crop, write_rgb24_crop
from visualworld.ingestion import (
    EvidenceRef,
    Fingerprint,
    FrameRef,
    MediaTime,
    Rational,
    RecordValidationError,
    RunManifest,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
)
from visualworld.ports import (
    MAX_PORT_BATCH_ITEMS,
    FakeFrameSampler,
    FakeVideoSource,
    PortError,
    PortErrorCode,
)
from visualworld.storage import LocalEvidenceStore
from visualworld.world_store import LocalWorldStore

CLI_RESULT_SCHEMA = "visualworld.cli-result"
CLI_SCHEMA_VERSION = 1
_FIXTURE_NAME = "deterministic-rgb24-v1"
_FIXTURE_MARKER = b"visualworld.cli.fixture.v1"
_FIXTURE_WIDTH = 2
_FIXTURE_HEIGHT = 2
_FIXTURE_PIXELS = bytes(range(_FIXTURE_WIDTH * _FIXTURE_HEIGHT * 3))
_DEFAULT_BOX = (1, 0, 2, 2)


class _UsageError(ValueError):
    """An argparse rejection whose untrusted message is deliberately discarded."""


class _OutputError(RuntimeError):
    """A terminal write failure whose implementation detail must remain private."""


class _CliError(RuntimeError):
    def __init__(self, code: str, operation: str, *, retryable: bool = False) -> None:
        self.code = code
        self.operation = operation
        self.retryable = retryable
        super().__init__(f"{code} at cli.{operation}")


def _silence_stream(stream: TextIO) -> None:
    """Redirect a failed standard stream so interpreter shutdown cannot retry it."""

    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return
    try:
        null_descriptor = os.open(os.devnull, os.O_WRONLY)
        if null_descriptor == descriptor:
            return
        try:
            os.dup2(null_descriptor, descriptor)
        finally:
            os.close(null_descriptor)
    except OSError:
        pass


def _write_text(text: str, stream: TextIO) -> None:
    try:
        stream.write(text)
        stream.flush()
    except (AttributeError, OSError, ValueError):
        _silence_stream(stream)
        raise _OutputError from None


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _UsageError from None

    def _print_message(self, message: str, file: object | None = None) -> None:
        if message:
            stream = sys.stderr if file is None else cast(TextIO, file)
            _write_text(message, stream)


def canonical_json(value: object) -> str:
    """Encode one deterministic ASCII-only JSON value for terminal output."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _success(command: str, result: dict[str, object]) -> dict[str, object]:
    return {
        "command": command,
        "result": result,
        "schema": CLI_RESULT_SCHEMA,
        "schema_version": CLI_SCHEMA_VERSION,
        "status": "ok",
    }


def _failure(
    command: str,
    code: str,
    operation: str,
    *,
    retryable: bool = False,
) -> dict[str, object]:
    return {
        "command": command,
        "error": {
            "code": code,
            "operation": operation,
            "retryable": retryable,
        },
        "schema": CLI_RESULT_SCHEMA,
        "schema_version": CLI_SCHEMA_VERSION,
        "status": "error",
    }


def _fixture() -> tuple[Source, tuple[FrameRef, ...], bytes]:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint(
            hashlib.sha256(_FIXTURE_MARKER).hexdigest(),
            str(len(_FIXTURE_MARKER)),
        ),
        (SourceStream(0, _FIXTURE_WIDTH, _FIXTURE_HEIGHT, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(
            source.source_id,
            0,
            str(index),
            MediaTime(str(index * 200), time_base),
        )
        for index in range(2)
    )
    return source, frames, _FIXTURE_PIXELS


def _probe_payload() -> dict[str, object]:
    source, frames, _ = _fixture()
    video = FakeVideoSource(source, frames)
    probed = video.probe()
    candidates = video.read_frames(stream_index=0, limit=MAX_PORT_BATCH_ITEMS)
    return {
        "default_manual_region": {
            "box_xyxy": list(_DEFAULT_BOX),
            "frame_id": frames[1].frame_id,
        },
        "fixture": _FIXTURE_NAME,
        "frames": [frame.to_mapping() for frame in candidates],
        "source": probed.to_mapping(),
    }


def probe_result() -> dict[str, object]:
    """Return the stable result document emitted by ``visualworld probe``."""

    return _success("probe", _probe_payload())


def _argument(arguments: argparse.Namespace, name: str) -> str:
    value = getattr(arguments, name, None)
    if type(value) is not str:
        raise _CliError("invalid_request", name)
    return value


def _store_path(value: str, *, must_exist: bool) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise _CliError("invalid_request", "store")
    if must_exist:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            raise _CliError("not_found", "store") from None
        except OSError:
            raise _CliError("operation_failed", "store") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise _CliError("invalid_request", "store")
    return path


def _output_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise _CliError("invalid_request", "output")
    return path


def _run_probe(arguments: argparse.Namespace) -> dict[str, object]:
    del arguments
    return _probe_payload()


def _run_ingest(arguments: argparse.Namespace) -> dict[str, object]:
    root = _store_path(_argument(arguments, "store"), must_exist=False)
    raw_box = getattr(arguments, "box", None)
    if (
        not isinstance(raw_box, (list, tuple))
        or len(raw_box) != 4
        or not all(type(value) is int for value in raw_box)
    ):
        raise _CliError("invalid_request", "box")
    box = tuple(raw_box)
    source, frames, pixels = _fixture()
    video = FakeVideoSource(source, frames)
    sampler = FakeFrameSampler((frames[1].frame_id,))
    evidence_store = LocalEvidenceStore(root, max_payload_bytes=len(pixels))
    world_store = LocalWorldStore(root)
    coordinator = IngestionCoordinator(evidence_store, world_store)
    result = coordinator.ingest(
        video,
        sampler,
        IngestionConfig(
            Sampling(Rational("5", "1")),
            max_frame_bytes=len(pixels),
        ),
        (ManualEvidenceInput(frames[1].frame_id, pixels, box),),
    )
    return {
        "disposition": result.disposition.value,
        "evidence_ids": [item.evidence_id for item in result.evidence],
        "run_id": result.manifest.run_id,
        "sample_count": len(result.frames),
        "source_id": result.manifest.source_id,
    }


def _run_inspect(arguments: argparse.Namespace) -> dict[str, object]:
    root = _store_path(_argument(arguments, "store"), must_exist=True)
    record = LocalWorldStore(root).get(_argument(arguments, "run_id"))
    if not isinstance(record, RunManifest):
        raise _CliError("invalid_request", "inspect_run")
    return {"manifest": record.to_mapping()}


def _complete_page(
    first: tuple[FrameRef, ...] | tuple[EvidenceRef, ...],
    has_more: Callable[[], bool],
    operation: str,
) -> None:
    if len(first) == MAX_PORT_BATCH_ITEMS and has_more():
        raise _CliError("limit_exceeded", operation)


def _run_list_samples(arguments: argparse.Namespace) -> dict[str, object]:
    root = _store_path(_argument(arguments, "store"), must_exist=True)
    run_id = _argument(arguments, "run_id")
    world = LocalWorldStore(root)
    manifest = world.get(run_id)
    if not isinstance(manifest, RunManifest) or manifest.state != "committed":
        raise _CliError("not_found", "list_samples")
    frames = world.list_run_frames(run_id, limit=MAX_PORT_BATCH_ITEMS)
    evidence = world.list_run_evidence(run_id, limit=MAX_PORT_BATCH_ITEMS)
    if frames:
        _complete_page(
            frames,
            lambda: bool(
                world.list_run_frames(
                    run_id,
                    after_stream_index=frames[-1].stream_index,
                    after_decode_index=frames[-1].decode_index,
                    limit=1,
                )
            ),
            "list_samples",
        )
    if evidence:
        _complete_page(
            evidence,
            lambda: bool(
                world.list_run_evidence(
                    run_id,
                    after_evidence_id=evidence[-1].evidence_id,
                    limit=1,
                )
            ),
            "list_samples",
        )
    if manifest.outputs is None or len(frames) != int(manifest.outputs.sample_count):
        raise _CliError("corrupt", "list_samples")
    by_frame: dict[str, list[dict[str, object]]] = {frame.frame_id: [] for frame in frames}
    for item in evidence:
        owned = by_frame.get(item.frame_id)
        if owned is None:
            raise _CliError("corrupt", "list_samples")
        owned.append(item.to_mapping())
    return {
        "run_id": manifest.run_id,
        "samples": [
            {
                "evidence": by_frame[frame.frame_id],
                "frame": frame.to_mapping(),
            }
            for frame in frames
        ],
    }


def _run_show_evidence(arguments: argparse.Namespace) -> dict[str, object]:
    root = _store_path(_argument(arguments, "store"), must_exist=True)
    destination = _output_path(_argument(arguments, "output"))
    try:
        if destination.resolve(strict=False).is_relative_to(root.resolve(strict=True)):
            raise _CliError("invalid_request", "output")
    except OSError:
        raise _CliError("operation_failed", "output") from None
    world = LocalWorldStore(root)
    record = world.get(_argument(arguments, "evidence_id"))
    if not isinstance(record, EvidenceRef) or record.geometry is None:
        raise _CliError("invalid_request", "show_evidence")
    content = LocalEvidenceStore(root).get(record.artifact.sha256)
    x_min, y_min, x_max, y_max = record.geometry.box_xyxy
    crop = Rgb24Crop(x_max - x_min, y_max - y_min, content)
    artifact = write_rgb24_crop(
        crop,
        artifact_root=destination.parent,
        relative_destination=destination.name,
    )
    if artifact != record.artifact:
        raise _CliError("corrupt", "show_evidence")
    return {
        "evidence": record.to_mapping(),
        "export": {
            "bytes": int(artifact.bytes),
            "height": crop.height,
            "media_type": artifact.media_type,
            "sha256": artifact.sha256,
            "width": crop.width,
        },
    }


def _add_store(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--store",
        required=True,
        metavar="ABSOLUTE_STORE",
        help="absolute private local-store directory",
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the command parser without reading process arguments."""

    parser = _SafeArgumentParser(
        prog="visualworld",
        description="VisualWorld deterministic local evidence CLI.",
        epilog="Commands emit canonical JSON; evidence bytes are written only with --output.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", title="commands")

    probe = commands.add_parser(
        "probe",
        help="inspect the built-in deterministic RGB24 fixture",
        allow_abbrev=False,
    )
    probe.set_defaults(handler=_run_probe)

    ingest = commands.add_parser(
        "ingest",
        help="ingest one manual region from the deterministic fixture",
        allow_abbrev=False,
    )
    _add_store(ingest)
    ingest.add_argument(
        "--box",
        nargs=4,
        type=int,
        default=_DEFAULT_BOX,
        metavar=("X_MIN", "Y_MIN", "X_MAX", "Y_MAX"),
        help="half-open source-pixel box (default: 1 0 2 2)",
    )
    ingest.set_defaults(handler=_run_ingest)

    inspect_run = commands.add_parser(
        "inspect-run",
        help="read one run manifest",
        allow_abbrev=False,
    )
    _add_store(inspect_run)
    inspect_run.add_argument("--run-id", required=True, metavar="RUN_ID")
    inspect_run.set_defaults(handler=_run_inspect)

    list_samples = commands.add_parser(
        "list-samples",
        help="list frame and evidence records owned by one run",
        allow_abbrev=False,
    )
    _add_store(list_samples)
    list_samples.add_argument("--run-id", required=True, metavar="RUN_ID")
    list_samples.set_defaults(handler=_run_list_samples)

    show_evidence = commands.add_parser(
        "show-evidence",
        help="export exact packed RGB24 evidence without terminal pixel output",
        allow_abbrev=False,
    )
    _add_store(show_evidence)
    show_evidence.add_argument("--evidence-id", required=True, metavar="EVIDENCE_ID")
    show_evidence.add_argument(
        "--output",
        required=True,
        metavar="ABSOLUTE_FILE",
        help="new file beneath an existing private directory",
    )
    show_evidence.set_defaults(handler=_run_show_evidence)
    return parser


def _write_document(document: dict[str, object], stream: TextIO) -> None:
    _write_text(canonical_json(document) + "\n", stream)


def _finish_failure(
    command: str,
    code: str,
    operation: str,
    *,
    retryable: bool = False,
    exit_code: int = 1,
) -> int:
    with suppress(_OutputError):
        _write_document(
            _failure(command, code, operation, retryable=retryable),
            sys.stderr,
        )
    return exit_code


def _finish_success(command: str, payload: dict[str, object]) -> int:
    try:
        _write_document(_success(command, payload), sys.stdout)
    except _OutputError:
        return _finish_failure(command, "operation_failed", "emit_output")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one bounded command and return a stable process exit code."""

    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
    except _OutputError:
        return _finish_failure("arguments", "operation_failed", "emit_output")
    except _UsageError:
        return _finish_failure(
            "arguments",
            "usage_error",
            "parse_arguments",
            exit_code=2,
        )
    command = getattr(arguments, "command", None)
    if command is None:
        try:
            parser.print_help()
        except _OutputError:
            return _finish_failure("arguments", "operation_failed", "emit_output")
        return 0
    handler = getattr(arguments, "handler", None)
    if type(command) is not str or not callable(handler):
        return _finish_failure(
            "arguments",
            "usage_error",
            "parse_arguments",
            exit_code=2,
        )
    selected = handler
    try:
        payload = selected(arguments)
    except KeyboardInterrupt:
        return _finish_failure(command, "cancelled", command, exit_code=130)
    except _CliError as error:
        return _finish_failure(
            command,
            error.code,
            error.operation,
            retryable=error.retryable,
        )
    except CoordinatorError as error:
        return _finish_failure(
            command,
            error.code.value,
            error.stage.value,
            retryable=error.retryable,
        )
    except PortError as error:
        code = error.code.value
        exit_code = 1
        if error.code is PortErrorCode.CANCELLED:
            code = "cancelled"
            exit_code = 130
        return _finish_failure(
            command,
            code,
            error.operation,
            retryable=error.retryable,
            exit_code=exit_code,
        )
    except (CropError, RecordValidationError, TypeError, ValueError):
        return _finish_failure(command, "invalid_request", command.replace("-", "_"))
    except Exception:
        return _finish_failure(command, "operation_failed", command.replace("-", "_"))
    return _finish_success(command, payload)
