# SPDX-License-Identifier: Apache-2.0
"""Replay correctness, hostile-input, and first-frame probes for issue #4."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import signal
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from inspect_records import MAGIC, inspect
from isolation_probe import namespace_argv

EXPECTED_EXIT_CODES = {
    "fixture.mp4": 0,
    "varying-pts.mp4": 0,
    "no-pts.h264": 0,
    "rotated.mp4": 0,
    "excessive-metadata.mp4": 0,
    "oversize.mp4": 20,
    "source-too-large.bin": 20,
    "malformed.bin": 21,
    "truncated.mp4": 21,
    "nested-reference.m3u8": 21,
}


def _fd3(source_fd: int) -> None:
    os.dup2(source_fd, 3, inheritable=True)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _summary(stderr: bytes) -> dict[str, Any] | None:
    for line in reversed(stderr.decode("utf-8", errors="replace").splitlines()):
        if line.startswith("{"):
            result = json.loads(line)
            if isinstance(result, dict):
                return result
    return None


def _records(stdout: bytes) -> dict[str, Any] | None:
    if not stdout.startswith(MAGIC):
        return None
    with tempfile.NamedTemporaryFile() as temporary:
        temporary.write(stdout)
        temporary.flush()
        return inspect(Path(temporary.name))


def _run_case(
    runtime: Path,
    source: Path,
    *,
    worker_args: list[str] | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    program = [
        "/runtime/python/bin/python3.13",
        "/runtime/worker_probe.py",
        *(worker_args or []),
    ]
    argv = namespace_argv(runtime, program)
    source_fd = os.open(source, os.O_RDONLY)
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(source_fd,),
        start_new_session=True,
        preexec_fn=lambda: _fd3(source_fd),
    )
    os.close(source_fd)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    elapsed = time.monotonic() - started
    return {
        "namespace_argv": argv,
        "returncode": process.returncode,
        "wall_seconds": elapsed,
        "timed_out": timed_out,
        "stdout_bytes": len(stdout),
        "stdout_sha256": _digest(stdout),
        "stderr_bytes": len(stderr),
        "summary": _summary(stderr),
        "records": _records(stdout),
    }


def _read_exact_until(descriptor: int, size: int, deadline: float, buffer: bytearray) -> None:
    while len(buffer) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("first frame deadline")
        readable, _, _ = select.select([descriptor], [], [], remaining)
        if not readable:
            raise TimeoutError("first frame deadline")
        chunk = os.read(descriptor, size - len(buffer))
        if not chunk:
            raise EOFError("worker exited before first frame")
        buffer.extend(chunk)


def _measure_first_frame(runtime: Path, source: Path) -> dict[str, Any]:
    argv = namespace_argv(
        runtime,
        ["/runtime/python/bin/python3.13", "/runtime/worker_probe.py"],
    )
    source_fd = os.open(source, os.O_RDONLY)
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(source_fd,),
        start_new_session=True,
        preexec_fn=lambda: _fd3(source_fd),
    )
    os.close(source_fd)
    assert process.stdout is not None
    buffer = bytearray()
    deadline = started + 5.0
    try:
        _read_exact_until(process.stdout.fileno(), len(MAGIC) + 8, deadline, buffer)
        if bytes(buffer[: len(MAGIC)]) != MAGIC:
            raise ValueError("invalid worker magic")
        metadata_size, pixel_size = struct.unpack(">II", buffer[len(MAGIC) :])
        first_record_bytes = len(MAGIC) + 8 + metadata_size + pixel_size
        _read_exact_until(process.stdout.fileno(), first_record_bytes, deadline, buffer)
        first_frame_seconds = time.monotonic() - started
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=0.5)
    return {
        "namespace_argv": argv,
        "first_frame_seconds": first_frame_seconds,
        "first_record_bytes": first_record_bytes,
        "returncode_after_cancel": process.returncode,
    }


def _pts(records: dict[str, Any]) -> list[int | None]:
    return [
        None if item["pts"] is None else int(item["pts"]["value"]) for item in records["timestamps"]
    ]


def _time_bases(records: dict[str, Any]) -> list[dict[str, int] | None]:
    return [
        None if item["pts"] is None else item["pts"]["time_base"] for item in records["timestamps"]
    ]


def _packets_reordered(manifest: dict[str, Any], name: str) -> bool:
    packets = manifest["packet_probes"][name]["packets"]
    return any(packet.get("pts") != packet.get("dts") for packet in packets)


def run(runtime: Path, fixtures: Path) -> dict[str, Any]:
    manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))
    cases = {name: _run_case(runtime, fixtures / name) for name in EXPECTED_EXIT_CODES}
    cases["decoded-byte-cap"] = _run_case(
        runtime,
        fixtures / "fixture.mp4",
        worker_args=["--max-decoded-bytes", "1000"],
    )
    cases["frame-cap"] = _run_case(
        runtime, fixtures / "fixture.mp4", worker_args=["--max-frames", "1"]
    )

    cfr_records = cases["fixture.mp4"]["records"]
    varying_records = cases["varying-pts.mp4"]["records"]
    missing_records = cases["no-pts.h264"]["records"]
    rotated_records = cases["rotated.mp4"]["records"]
    metadata_records = cases["excessive-metadata.mp4"]["records"]
    if not all(
        isinstance(item, dict)
        for item in (
            cfr_records,
            varying_records,
            missing_records,
            rotated_records,
            metadata_records,
        )
    ):
        raise RuntimeError("expected successful fixture did not produce records")

    expected_cfr = manifest["expected"]["fixture.mp4"]
    expected_varying = manifest["expected"]["varying-pts.mp4"]
    expected_missing = manifest["expected"]["no-pts.h264"]
    time_base = manifest["expected"]["fixture.mp4"]["time_base"]
    correctness = {
        "cfr_pts_exact": _pts(cfr_records) == expected_cfr["pts"],
        "cfr_time_base_exact": all(item == time_base for item in _time_bases(cfr_records)),
        "cfr_source_packets_reordered": _packets_reordered(manifest, "fixture.mp4"),
        "varying_pts_exact": _pts(varying_records) == expected_varying["pts"],
        "varying_time_base_exact": all(item == time_base for item in _time_bases(varying_records)),
        "varying_source_packets_reordered": _packets_reordered(manifest, "varying-pts.mp4"),
        "missing_pts_remain_null": _pts(missing_records) == expected_missing["pts"],
        "rotation_exact": rotated_records["rotation_degrees_derived"] == [90.0],
        "display_matrix_retained": len(rotated_records["display_matrices"]) == 30
        and all(
            matrix == [0, -65536, 0, 65536, 0, 0, 0, 0, 1073741824]
            for matrix in rotated_records["display_matrices"]
        ),
        "stream_sar_labelled_guess": cfr_records["stream_sar_guesses"]
        == [{"denominator": 3, "numerator": 4}],
        "excessive_container_metadata_output_bounded": metadata_records["max_metadata_record_bytes"]
        <= 65_536,
    }
    exit_codes = {
        name: cases[name]["returncode"] == expected
        for name, expected in EXPECTED_EXIT_CODES.items()
    }
    exit_codes["decoded-byte-cap"] = cases["decoded-byte-cap"]["returncode"] == 20
    exit_codes["frame-cap"] = cases["frame-cap"]["returncode"] == 20
    no_timeouts = not any(bool(item["timed_out"]) for item in cases.values())
    first_frame = _measure_first_frame(runtime, fixtures / "fixture.mp4")
    result = {
        "schema_version": 1,
        "manifest_sha256": hashlib.sha256((fixtures / "manifest.json").read_bytes()).hexdigest(),
        "cases": cases,
        "correctness": correctness,
        "expected_exit_codes_observed": exit_codes,
        "no_timeouts": no_timeouts,
        "first_frame": first_frame,
    }
    result["status"] = (
        "pass" if all(correctness.values()) and all(exit_codes.values()) and no_timeouts else "fail"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.runtime.resolve(), args.fixtures.resolve())
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.write_text(encoded, encoding="utf-8")
        print(json.dumps({"output": str(args.output), "status": result["status"]}))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
