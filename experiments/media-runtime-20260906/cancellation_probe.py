# SPDX-License-Identifier: Apache-2.0
"""Cancel a worker while its deliberately undrained output pipe is blocked."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path

from isolation_probe import namespace_argv

PIPE_BYTES = 4096
CANCEL_AFTER_SECONDS = 0.25
TERMINATION_LIMIT_SECONDS = 0.5


def _fd3(source_fd: int) -> None:
    os.dup2(source_fd, 3, inheritable=True)


def _token_pids(token: str) -> list[int]:
    needle = token.encode()
    result: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environment = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in environment:
            result.append(int(entry.name))
    return sorted(result)


def _kill_pids(pids: list[int]) -> None:
    for pid in pids:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def run(runtime: Path, source: Path) -> dict[str, object]:
    token = f"visualworld-cancel-{os.getpid()}-{time.monotonic_ns()}"
    program = ["/runtime/python/bin/python3.13", "/runtime/worker_probe.py"]
    argv = namespace_argv(
        runtime.resolve(),
        program,
        extra_env={"VISUALWORLD_PROBE_TOKEN": token},
    )
    source_fd = os.open(source.resolve(), os.O_RDONLY)
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(source_fd,),
        pipesize=PIPE_BYTES,
        start_new_session=True,
        preexec_fn=lambda: _fd3(source_fd),
    )
    os.close(source_fd)

    time.sleep(CANCEL_AFTER_SECONDS)
    alive_before_cancel = process.poll() is None
    started = time.monotonic()
    os.killpg(process.pid, signal.SIGTERM)
    forced_kill = False
    try:
        process.wait(timeout=TERMINATION_LIMIT_SECONDS)
    except subprocess.TimeoutExpired:
        forced_kill = True
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=TERMINATION_LIMIT_SECONDS)
    termination_seconds = time.monotonic() - started

    survivors = _token_pids(token)
    if survivors:
        forced_kill = True
        _kill_pids(survivors)
    stdout, stderr = process.communicate(timeout=TERMINATION_LIMIT_SECONDS)
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        process_group_gone = True
    else:
        process_group_gone = False

    checks = {
        "worker_alive_when_cancelled": alive_before_cancel,
        "blocked_output_observed": alive_before_cancel and len(stdout) >= PIPE_BYTES,
        "termination_within_limit": termination_seconds <= TERMINATION_LIMIT_SECONDS,
        "no_forced_kill": not forced_kill,
        "process_group_gone": process_group_gone,
        "no_token_survivors": not survivors,
        "stdout_bounded": len(stdout) <= PIPE_BYTES,
        "stderr_bounded": len(stderr) <= PIPE_BYTES,
    }
    return {
        "schema_version": 1,
        "namespace_argv": argv,
        "pipe_bytes": PIPE_BYTES,
        "cancel_after_seconds": CANCEL_AFTER_SECONDS,
        "termination_limit_seconds": TERMINATION_LIMIT_SECONDS,
        "termination_seconds": termination_seconds,
        "returncode": process.returncode,
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "checks": checks,
        "status": "pass" if all(checks.values()) else "fail",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.runtime, args.source)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
