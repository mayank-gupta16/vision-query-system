# SPDX-License-Identifier: Apache-2.0
"""Validate the bounded record stream emitted by worker_probe.py."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path
from typing import Any

MAGIC = b"VWFRAME1"


def inspect(path: Path) -> dict[str, Any]:
    frames = 0
    pixels = 0
    first_pts: dict[str, Any] | None = None
    last_pts: dict[str, Any] | None = None
    null_pts = 0
    display_matrices: list[list[int]] = []
    with path.open("rb") as stream:
        if stream.read(len(MAGIC)) != MAGIC:
            raise ValueError("invalid magic")
        while sizes := stream.read(8):
            if len(sizes) != 8:
                raise ValueError("truncated record lengths")
            metadata_size, pixel_size = struct.unpack(">II", sizes)
            if metadata_size > 65_536:
                raise ValueError("metadata record too large")
            metadata_raw = stream.read(metadata_size)
            pixel_raw = stream.read(pixel_size)
            if len(metadata_raw) != metadata_size or len(pixel_raw) != pixel_size:
                raise ValueError("truncated record")
            metadata = json.loads(metadata_raw)
            if metadata["frame_index"] != frames:
                raise ValueError("non-sequential frame index")
            if hashlib.sha256(pixel_raw).hexdigest() != metadata["pixel_sha256"]:
                raise ValueError("pixel hash mismatch")
            expected = metadata["width"] * metadata["height"] * 3
            if pixel_size != expected:
                raise ValueError("pixel size mismatch")
            if metadata["pts"] is None:
                null_pts += 1
            else:
                if first_pts is None:
                    first_pts = metadata["pts"]
                last_pts = metadata["pts"]
            for side_data in metadata["side_data"]:
                if side_data["type"] == "DISPLAYMATRIX":
                    display_matrices.append(side_data["matrix_i32_native"])
            frames += 1
            pixels += pixel_size
    return {
        "status": "ok",
        "frames": frames,
        "pixel_bytes": pixels,
        "first_pts": first_pts,
        "last_pts": last_pts,
        "null_pts": null_pts,
        "display_matrices": display_matrices,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("records", type=Path)
    result = inspect(parser.parse_args().records)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
