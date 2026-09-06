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
per-frame SAR. Issue #9 must add and test the missing binding before treating SAR
as frame evidence. Unknown side data is recorded only as type, length, and hash.

## Linux isolation replay

The measured host staged the CPython, source-built PyAV, and minimal FFmpeg trees
as one root-owned read-only runtime. The parent then launched the worker with:

- a transient systemd service using a read-only `OpenFile=` descriptor;
- `User=nobody`, `NoNewPrivileges=yes`, 256 MiB `MemoryMax`, no swap, 32 tasks,
  200% CPU bandwidth, a 20-second deadline, and restrictive rlimits;
- bubblewrap user, PID, network, IPC, UTS, and mount namespaces;
- an empty read-only root, only the runtime and system C-library closure mounted
  read-only, a 32 MiB temporary filesystem, no host paths, no capabilities, and
  no later user namespaces.

This proves the direction, not a production launcher. Issue #9 owns `openat2`
staging, an inner seccomp/Landlock policy, aggregate CPU-budget polling,
`cgroup.kill`, bounded pipe draining, and structured error mapping. It must fail
closed when any mandatory Linux isolation capability is absent.

The generated fixtures are deliberately not committed. Their exact generator
parameters, hashes, raw outcomes, and benchmark samples are in the
[CPU-LITE receipt](../../docs/research/media-runtime-cpu-lite-receipt.json).
Do not substitute private video.
