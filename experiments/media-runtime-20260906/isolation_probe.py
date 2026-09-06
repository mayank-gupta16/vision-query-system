# SPDX-License-Identifier: Apache-2.0
"""Replay the issue #4 Linux namespace and cgroup boundary probes."""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Any

SYSTEMD_PROPERTIES = (
    "User=nobody",
    "Group=nogroup",
    "NoNewPrivileges=yes",
    "MemoryMax=268435456",
    "MemorySwapMax=0",
    "TasksMax=64",
    "CPUQuota=200%",
    "RuntimeMaxSec=20s",
    "LimitNOFILE=64",
    "LimitNPROC=64",
    "LimitFSIZE=8388608",
    "UMask=0077",
)


def namespace_argv(
    runtime: Path,
    program: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    argv = ["/usr/bin/bwrap"]
    environment_args = [
        item
        for key, value in sorted((extra_env or {}).items())
        for item in ("--setenv", key, value)
    ]
    argv.extend(
        [
            "--unshare-user",
            "--uid",
            "65534",
            "--gid",
            "65534",
            "--unshare-net",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--disable-userns",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--setenv",
            "PATH",
            "/runtime/python/bin",
            "--setenv",
            "HOME",
            "/nonexistent",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "PYTHONPATH",
            "/runtime/venv/lib/python3.13/site-packages",
            "--setenv",
            "LD_LIBRARY_PATH",
            "/runtime/ffmpeg/lib",
            *environment_args,
            "--ro-bind",
            str(runtime),
            "/runtime",
            "--dir",
            "/lib",
            "--ro-bind",
            "/usr/lib/x86_64-linux-gnu",
            "/lib/x86_64-linux-gnu",
            "--ro-bind",
            "/usr/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--size",
            "33554432",
            "--tmpfs",
            "/tmp",
            "--remount-ro",
            "/",
            "--chdir",
            "/",
            *program,
        ]
    )
    return argv


def _read_denied(path: Path) -> bool:
    try:
        path.read_bytes()
    except OSError:
        return True
    return False


def _write_denied(path: Path) -> bool:
    try:
        path.write_text("forbidden", encoding="utf-8")
    except OSError:
        return True
    return False


def _connect_denied(family: socket.AddressFamily, address: Any) -> bool:
    attempt = socket.socket(family, socket.SOCK_STREAM)
    attempt.settimeout(0.25)
    try:
        attempt.connect(address)
    except OSError:
        return True
    finally:
        attempt.close()
    return False


def _udp_denied(address: tuple[str, int]) -> bool:
    attempt = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    attempt.settimeout(0.25)
    try:
        attempt.connect(address)
        attempt.send(b"visualworld-isolation-probe")
    except OSError:
        return True
    finally:
        attempt.close()
    return False


def _dns_denied() -> bool:
    def _timeout(_signum: int, _frame: object) -> None:
        raise TimeoutError

    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.setitimer(signal.ITIMER_REAL, 0.5)
    try:
        socket.getaddrinfo("example.invalid", 80)
    except (OSError, TimeoutError):
        return True
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return False


def _status_fields() -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator and key in {"CapEff", "NoNewPrivs"}:
            fields[key] = value.strip()
    return fields


def _run_inner_probe(args: argparse.Namespace) -> int:
    scratch = Path("/tmp/visualworld-write-check")
    scratch.write_text("ok", encoding="utf-8")
    symlink = Path("/tmp/visualworld-symlink-escape")
    symlink.symlink_to(args.host_sentinel)
    status_fields = _status_fields()
    checks = {
        "effective_uid_non_root": os.geteuid() == 65534,
        "effective_gid_non_root": os.getegid() == 65534,
        "host_sentinel_read_denied": _read_denied(Path(args.host_sentinel)),
        "proc_root_escape_denied": _read_denied(
            Path("/proc/1/root") / args.host_sentinel.lstrip("/")
        ),
        "symlink_escape_denied": _read_denied(symlink),
        "runtime_write_denied": _write_denied(Path("/runtime/forbidden")),
        "root_write_denied": _write_denied(Path("/forbidden")),
        "tmp_write_allowed": scratch.read_text(encoding="utf-8") == "ok",
        "host_ipv4_denied": _connect_denied(socket.AF_INET, ("127.0.0.1", args.ipv4_port)),
        "host_ipv6_denied": _connect_denied(socket.AF_INET6, ("::1", args.ipv6_port, 0, 0)),
        "host_unix_path_denied": _connect_denied(socket.AF_UNIX, args.unix_socket_path),
        "host_unix_abstract_denied": _connect_denied(
            socket.AF_UNIX, "\0" + args.abstract_socket_name
        ),
        "host_udp_denied": _udp_denied(("127.0.0.1", args.udp_port)),
        "public_dns_transport_denied": _udp_denied(("8.8.8.8", 53)),
        "dns_resolution_denied": _dns_denied(),
        "capabilities_empty": int(status_fields.get("CapEff", "1"), 16) == 0,
        "no_new_privileges": status_fields.get("NoNewPrivs") == "1",
    }
    namespaces = {
        name: os.readlink(f"/proc/self/ns/{name}")
        for name in ("cgroup", "ipc", "mnt", "net", "pid", "user", "uts")
    }
    result = {
        "schema_version": 1,
        "checks": checks,
        "namespaces": namespaces,
        "status_fields": status_fields,
        "status": "pass" if all(checks.values()) else "fail",
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "pass" else 1


def _tcp_listener(family: socket.AddressFamily, address: Any) -> socket.socket:
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(address)
    listener.listen()
    return listener


def _listener_hit(listener: socket.socket, *, datagram: bool = False) -> int:
    readable, _, _ = select.select([listener], [], [], 0)
    if not readable:
        return 0
    if datagram:
        listener.recvfrom(4096)
    else:
        connection, _ = listener.accept()
        connection.close()
    return 1


def _json_line(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        if line.startswith("{"):
            value = json.loads(line)
            if isinstance(value, dict):
                return value
    raise ValueError("probe JSON output missing")


def _fd3(source_fd: int) -> None:
    os.dup2(source_fd, 3, inheritable=True)


def _run_controller(args: argparse.Namespace) -> int:
    runtime = args.runtime.resolve()
    nested_fixture = args.nested_fixture.resolve()
    source_fd = os.open(nested_fixture, os.O_RDONLY)
    try:
        with tempfile.TemporaryDirectory(prefix="visualworld-isolation-", dir="/root") as temporary:
            host_root = Path(temporary)
            sentinel = host_root / "host-sentinel"
            sentinel.write_text("host-private", encoding="utf-8")
            unix_path = host_root / "host.sock"
            abstract_name = f"visualworld-{os.getpid()}"

            ipv4 = _tcp_listener(socket.AF_INET, ("127.0.0.1", 0))
            ipv6 = _tcp_listener(socket.AF_INET6, ("::1", 0, 0, 0))
            unix_socket = _tcp_listener(socket.AF_UNIX, str(unix_path))
            abstract_socket = _tcp_listener(socket.AF_UNIX, "\0" + abstract_name)
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.bind(("127.0.0.1", 0))
            listeners = [ipv4, ipv6, unix_socket, abstract_socket, udp]
            try:
                inner_program = [
                    "/runtime/python/bin/python3.13",
                    "/runtime/isolation_probe.py",
                    "probe",
                    "--host-sentinel",
                    str(sentinel),
                    "--ipv4-port",
                    str(ipv4.getsockname()[1]),
                    "--ipv6-port",
                    str(ipv6.getsockname()[1]),
                    "--unix-socket-path",
                    str(unix_path),
                    "--abstract-socket-name",
                    abstract_name,
                    "--udp-port",
                    str(udp.getsockname()[1]),
                ]
                bwrap_argv = namespace_argv(runtime, inner_program)
                unit = f"visualworld-isolation-probe-{os.getpid()}"
                systemd_argv = [
                    "/usr/bin/systemd-run",
                    "--quiet",
                    "--pipe",
                    "--wait",
                    "--collect",
                    f"--unit={unit}",
                    *(f"--property={item}" for item in SYSTEMD_PROPERTIES),
                    *bwrap_argv,
                ]
                completed = subprocess.run(
                    systemd_argv,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                probe_result = _json_line(completed.stdout)
                host_hits = {
                    "ipv4_tcp": _listener_hit(ipv4),
                    "ipv6_tcp": _listener_hit(ipv6),
                    "unix_path": _listener_hit(unix_socket),
                    "unix_abstract": _listener_hit(abstract_socket),
                    "ipv4_udp": _listener_hit(udp, datagram=True),
                }
            finally:
                for listener in listeners:
                    listener.close()

            nested_listener = _tcp_listener(socket.AF_INET, ("127.0.0.1", 9))
            nested_program = [
                "/runtime/python/bin/python3.13",
                "/runtime/worker_probe.py",
            ]
            nested_bwrap_argv = namespace_argv(runtime, nested_program)
            nested_process = subprocess.Popen(
                nested_bwrap_argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(source_fd,),
                start_new_session=True,
                preexec_fn=lambda: _fd3(source_fd),
            )
            nested_stdout, nested_stderr = nested_process.communicate(timeout=5)
            nested_hit = _listener_hit(nested_listener)
            nested_listener.close()
    finally:
        os.close(source_fd)

    nested_stderr_text = nested_stderr.decode("utf-8", errors="replace")
    result = {
        "schema_version": 1,
        "systemd_properties": list(SYSTEMD_PROPERTIES),
        "systemd_run_argv": systemd_argv,
        "namespace_argv": bwrap_argv,
        "systemd_returncode": completed.returncode,
        "probe": probe_result,
        "host_listener_connections": host_hits,
        "nested_reference": {
            "namespace_argv": nested_bwrap_argv,
            "returncode": nested_process.returncode,
            "stdout_bytes": len(nested_stdout),
            "stderr_bytes": len(nested_stderr),
            "stderr": nested_stderr_text.strip(),
            "host_listener_connections": nested_hit,
        },
    }
    result["status"] = (
        "pass"
        if completed.returncode == 0
        and probe_result.get("status") == "pass"
        and not any(host_hits.values())
        and nested_process.returncode == 21
        and nested_hit == 0
        else "fail"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe = subparsers.add_parser("probe")
    probe.add_argument("--host-sentinel", required=True)
    probe.add_argument("--ipv4-port", type=int, required=True)
    probe.add_argument("--ipv6-port", type=int, required=True)
    probe.add_argument("--unix-socket-path", required=True)
    probe.add_argument("--abstract-socket-name", required=True)
    probe.add_argument("--udp-port", type=int, required=True)
    controller = subparsers.add_parser("controller")
    controller.add_argument("--runtime", type=Path, required=True)
    controller.add_argument("--nested-fixture", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "probe":
        return _run_inner_probe(args)
    return _run_controller(args)


if __name__ == "__main__":
    raise SystemExit(main())
