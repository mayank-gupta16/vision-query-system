# SPDX-License-Identifier: Apache-2.0
"""Generate the disposable synthetic media used by the issue #4 experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

FRAME_COUNT = 30
TIME_BASE = {"numerator": 1, "denominator": 90_000}
METADATA_BYTES = 131_072
SOURCE_LIMIT_BYTES = 67_108_864


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _probe_packets(ffprobe: str, fixture: str, *, cwd: Path) -> dict[str, Any]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_packets",
        "-show_entries",
        "packet=pts,dts,duration",
        "-of",
        "json",
        fixture,
    ]
    completed = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    result = json.loads(completed.stdout)
    return {"command": command, "packets": result["packets"]}


def _encoder_args(*, variable_rate: bool) -> list[str]:
    return [
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "10",
        "-keyint_min",
        "10",
        "-bf",
        "2",
        "-sc_threshold",
        "0",
        "-threads",
        "1",
        "-x264-params",
        f"threads=1:force-cfr={0 if variable_rate else 1}",
        "-flags:v",
        "+bitexact",
        "-fflags",
        "+bitexact",
        "-map_metadata",
        "-1",
    ]


def _ffmpeg_prefix(ffmpeg: str) -> list[str]:
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]


def _commands(ffmpeg: str) -> dict[str, list[str]]:
    prefix = _ffmpeg_prefix(ffmpeg)
    cfr = [
        *prefix,
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=10",
        "-frames:v",
        str(FRAME_COUNT),
        "-vf",
        "setpts=N+10,setsar=4/3",
        *_encoder_args(variable_rate=False),
        "-video_track_timescale",
        "90000",
        "-movflags",
        "+faststart",
        "fixture.mp4",
    ]
    varying = [
        *prefix,
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=10",
        "-frames:v",
        str(FRAME_COUNT),
        "-vf",
        "setpts=N+10+floor(N/3),setsar=4/3",
        *_encoder_args(variable_rate=True),
        "-fps_mode:v",
        "vfr",
        "-video_track_timescale",
        "90000",
        "-movflags",
        "+faststart",
        "varying-pts.mp4",
    ]
    no_pts = [
        *prefix,
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=10",
        "-frames:v",
        "5",
        *_encoder_args(variable_rate=False),
        "-f",
        "h264",
        "no-pts.h264",
    ]
    rotated = [
        *prefix,
        "-display_rotation:v:0",
        "90",
        "-i",
        "fixture.mp4",
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
        "rotated.mp4",
    ]
    oversize = [
        *prefix,
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=4096x2160:r=1",
        "-frames:v",
        "1",
        *_encoder_args(variable_rate=False),
        "-video_track_timescale",
        "90000",
        "oversize.mp4",
    ]
    excessive_metadata = [
        *prefix,
        "-i",
        "fixture.mp4",
        "-f",
        "ffmetadata",
        "-i",
        "excessive-metadata.ffmeta",
        "-map",
        "0:v:0",
        "-map_metadata",
        "1",
        "-c:v",
        "copy",
        "-movflags",
        "+faststart",
        "excessive-metadata.mp4",
    ]
    return {
        "fixture.mp4": cfr,
        "varying-pts.mp4": varying,
        "no-pts.h264": no_pts,
        "rotated.mp4": rotated,
        "oversize.mp4": oversize,
        "excessive-metadata.mp4": excessive_metadata,
    }


def _write_python_fixtures(output: Path) -> None:
    (output / "malformed.bin").write_bytes(bytes(range(256)) * 16)
    fixture = (output / "fixture.mp4").read_bytes()
    (output / "truncated.mp4").write_bytes(fixture[:4096])
    (output / "nested-reference.m3u8").write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n"
        "http://127.0.0.1:9/private.mp4\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    with (output / "source-too-large.bin").open("wb") as stream:
        stream.truncate(SOURCE_LIMIT_BYTES + 1)


def _file_record(path: Path) -> dict[str, Any]:
    return {"name": path.name, "bytes": path.stat().st_size, "sha256": _sha256(path)}


def generate(ffmpeg_path: Path, output: Path) -> dict[str, Any]:
    ffmpeg = str(ffmpeg_path.resolve())
    ffprobe_path = ffmpeg_path.resolve().with_name("ffprobe")
    ffprobe = str(ffprobe_path)
    output.mkdir(parents=True, exist_ok=True)
    commands = _commands(ffmpeg)
    generated_names = [*commands]
    extra_names = [
        "excessive-metadata.ffmeta",
        "malformed.bin",
        "truncated.mp4",
        "nested-reference.m3u8",
        "source-too-large.bin",
    ]
    for name in [*generated_names, *extra_names, "manifest.json"]:
        (output / name).unlink(missing_ok=True)

    metadata = ";FFMETADATA1\ncomment=" + ("x" * METADATA_BYTES) + "\n"
    (output / "excessive-metadata.ffmeta").write_text(metadata, encoding="utf-8")
    for command in commands.values():
        _run(command, cwd=output)
    _write_python_fixtures(output)

    version = subprocess.run(
        [ffmpeg, "-version"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    ffprobe_version = subprocess.run(
        [ffprobe, "-version"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    packet_probes = {
        name: _probe_packets(ffprobe, name, cwd=output)
        for name in ("fixture.mp4", "varying-pts.mp4")
    }
    files = sorted(
        (_file_record(output / name) for name in [*generated_names, *extra_names]),
        key=lambda item: str(item["name"]),
    )
    manifest = {
        "schema_version": 1,
        "generator": "experiments/media-runtime-20260906/generate_fixtures.py",
        "owner_source": "VisualWorld project-generated synthetic test inputs",
        "acquisition_method": "generated locally from reviewed source code",
        "rights": "project-generated; no third-party video, faces, plates, or private data",
        "privacy": "synthetic patterns only",
        "allowed_use": "VisualWorld testing and benchmarking",
        "ffmpeg": {
            "path": ffmpeg,
            "sha256": _sha256(Path(ffmpeg)),
            "version_output": version,
        },
        "ffprobe": {
            "path": ffprobe,
            "sha256": _sha256(ffprobe_path),
            "version_output": ffprobe_version,
        },
        "commands": commands,
        "packet_probes": packet_probes,
        "python_generated": {
            "malformed.bin": "bytes(range(256)) repeated 16 times",
            "truncated.mp4": "first 4096 bytes of fixture.mp4",
            "nested-reference.m3u8": "fixed manifest with 127.0.0.1:9 reference",
            "source-too-large.bin": f"sparse file of {SOURCE_LIMIT_BYTES + 1} bytes",
            "excessive-metadata.ffmeta": f"fixed {METADATA_BYTES}-byte comment",
        },
        "expected": {
            "fixture.mp4": {
                "frames": FRAME_COUNT,
                "width": 320,
                "height": 240,
                "time_base": TIME_BASE,
                "pts": [(index + 10) * 9000 for index in range(FRAME_COUNT)],
                "has_reordered_dts": True,
                "sample_aspect_ratio": {"numerator": 4, "denominator": 3},
            },
            "varying-pts.mp4": {
                "frames": FRAME_COUNT,
                "time_base": TIME_BASE,
                "pts": [(index + 10 + index // 3) * 9000 for index in range(FRAME_COUNT)],
                "has_discontinuities": True,
                "has_reordered_dts": True,
            },
            "no-pts.h264": {"frames": 5, "pts": [None] * 5},
            "rotated.mp4": {"rotation_degrees": 90},
            "oversize.mp4": {"width": 4096, "height": 2160},
            "excessive-metadata.mp4": {"container_comment_bytes": METADATA_BYTES},
        },
        "files": files,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = generate(args.ffmpeg, args.output)
    print(json.dumps({"files": len(manifest["files"]), "status": "ok"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
