# Media runtime and isolation experiment

This frozen issue #4 experiment compares FFmpeg's command-line transport with a
PyAV worker built from source against the same minimal FFmpeg libraries. It is
research code, not the application ingestion adapter.

## Immutable inputs

- FFmpeg 9.0.1 source archive SHA-256:
  `cf38e0e28c7e5605942c4a77755349b0145804a397af37eb1fb4c77cb237f635`
- FFmpeg detached-signature SHA-256:
  `b613a00005232a1245ace7080088781ac23a916119d3e5b0d6c042368eee0177`
- FFmpeg release-key fingerprint:
  `FCF986EA15E6E293A5644F10B4322F04D67658D8`
- PyAV 18.1.0 source archive SHA-256:
  `47bfc286e1bc9de7ab4681fc2b575cd2460a66919d31ffe1bd5aa54fae531a28`
- CPython 3.13.15 and uv 0.12.6 are the already approved issue #3 toolchain
  inputs. [Build constraints](build-constraints.txt) pin the additional source
  build requirements.

Download FFmpeg only from `https://ffmpeg.org/releases/` and verify the detached
signature against the complete fingerprint above. Download PyAV and the two
build wheels only from their exact PyPI release pages and verify their SHA-256
values before extraction or installation.

Configure FFmpeg with the arguments in
[ffmpeg-configure-args.txt](ffmpeg-configure-args.txt), plus a disposable absolute
`--prefix`. No GPL, version-3, nonfree, external codec, network, device, or
hardware-acceleration flag is enabled. Build shared libraries, set their
`pkg-config` directory while building the verified PyAV source distribution,
and keep both outside the repository.

The first-slice build intentionally exposes only the MP4/raw-H.264 demuxers and
H.264 decoder/parser. Run `./configure --prefix=/ABSOLUTE/RUNTIME/ffmpeg` with
the arguments from that file, then `make -j4` and `make install`. Build the
verified PyAV sdist with `PKG_CONFIG_PATH` set to that prefix's `lib/pkgconfig`
and only the hash-pinned wheels from [build-constraints.txt](build-constraints.txt).
The receipt records the resulting wheel and shared-library hashes.

## Exact replay inputs

Run the synthetic generator with an FFmpeg/ffprobe pair that provides `lavfi`
and `libx264`; the receipt fingerprints the exact generator binaries used:

```text
python3 generate_fixtures.py --ffmpeg /usr/bin/ffmpeg --output /DISPOSABLE/fixtures
```

The generated manifest contains every executed argument array, input checksum,
rights/privacy declaration, CFR and varying/discontinuous expected PTS sequence,
source packet PTS/DTS, rotation/SAR facts, and hostile fixture recipe. No binary
fixture is committed. The exact measured manifest is retained as
[measured-fixture-manifest.json](measured-fixture-manifest.json).

## Worker contract

The trusted parent opens a bounded regular file and passes only descriptor 3.
[worker_probe.py](worker_probe.py) disables generated/fill-in timestamps, asserts
the resulting libavformat flags, decodes the first allowlisted video stream once,
and emits length-delimited JSON-plus-RGB24 records. The JSON stores exact rational
PTS/DTS/duration, color fields, pixel hashes, and bounded side-data evidence.
Missing PTS remains JSON `null`. The companion
[inspect_records.py](inspect_records.py) rejects framing, sequence, byte-length,
or pixel-hash mismatches.

The probe labels stream SAR as a guess because PyAV 18.1.0 does not expose
per-frame SAR. Issue #11 must add and test the missing binding before treating SAR
as frame evidence. Unknown side data is recorded only as type, length, and hash.

## Linux isolation replay

The measured host staged the CPython, source-built PyAV, and minimal FFmpeg trees
as one root-owned read-only runtime. The parent then launched the worker with:

- a transient systemd service using a read-only `OpenFile=` descriptor;
- `User=nobody`, `NoNewPrivileges=yes`, 256 MiB `MemoryMax`, no swap, 64 tasks,
  200% CPU bandwidth, a 20-second deadline, and restrictive rlimits;
- bubblewrap user, PID, network, IPC, UTS, and mount namespaces;
- an empty read-only root, only the runtime and system C-library closure mounted
  read-only, a 32 MiB temporary filesystem, no host paths, no capabilities, and
  no later user namespaces.

This proves the direction, not a production launcher. Issue #11 owns `openat2`
staging, an inner seccomp/Landlock policy, aggregate CPU-budget polling,
`cgroup.kill`, bounded pipe draining, and structured error mapping. It must fail
closed when any mandatory Linux isolation capability is absent.

The generated binary fixtures are deliberately not committed. Their exact
generator parameters, hashes, observed outcomes, and benchmark values are in
the measured manifest and
[CPU-LITE receipt](../../docs/research/media-runtime-cpu-lite-receipt.json).
Do not substitute private video.

Stage the repository scripts and source-built runtime under one root-owned,
read-only runtime directory, then replay the bounded experiment on Linux:

```text
python3 run_worker_cases.py --runtime /opt/visualworld-runtime-probe --fixtures /DISPOSABLE/fixtures --output /DISPOSABLE/worker-cases.json
python3 full_worker_probe.py --runtime /opt/visualworld-runtime-probe --source /DISPOSABLE/fixtures/fixture.mp4 --records /DISPOSABLE/full-worker.records --result /DISPOSABLE/full-worker.json --unit-suffix replay
python3 isolation_probe.py controller --runtime /opt/visualworld-runtime-probe --nested-fixture /DISPOSABLE/fixtures/nested-reference.m3u8
python3 cancellation_probe.py --runtime /opt/visualworld-runtime-probe --source /DISPOSABLE/fixtures/fixture.mp4
```

`full_worker_probe.py` is the combined replay: systemd opens the source
read-only as descriptor 3, bubblewrap inherits that descriptor, and the real
worker decodes it under the recorded cgroup and namespace controls. These
scripts emit the exact namespace/systemd argument arrays and structured
outcomes. They are experimental evidence only; issue #11 owns the production
launcher and structured application errors.
