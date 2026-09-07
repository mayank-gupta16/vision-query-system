# v0.1 end-to-end and hostile-input regressions

## Scope

Issue #18 adds the release-gate regression suite for the deterministic v0.1
vertical slice. It generates the public 2×2 packed-RGB24 fixture in memory at
test time and exercises the actual CLI, coordinator, EvidenceStore, and
WorldStore. It adds no media, model, perception dataset, runtime dependency, or
network capability.

The versioned
[fixture manifest](../../fixtures/v01-regression/manifest.json) binds the public
probe golden, source/frame/run/evidence identities, run-manifest and sample-index
hashes, exact six-byte evidence artifact, WorldStore counts, and deletion counts.
Its rights record is Apache-2.0 with no external sources, and its privacy record
declares fully generated public synthetic data with no people, faces, plates,
personal data, or retained temporary outputs.

## Regression boundary

The suite verifies:

- first ingest, close/reopen, inspect, run-owned listing, exact evidence export,
  and idempotent retry against the locked golden;
- an interruption after artifact promotion, explicit recovery, and a subsequent
  byte-identical committed retry;
- an interruption after deletion artifacts are removed, recovery from the
  durable deletion plan, zero remaining world/evidence records, and a reduced
  deletion receipt;
- oversized frame rejection before effects followed by a valid retry;
- relative/traversal paths, a linked store root, and a linked export target fail
  closed without changing their targets;
- SQL-like/control-sequence identifiers, malicious record JSON, and command/URL
  text placed in a corrupt artifact remain inert and absent from terminal output;
- corrupt evidence is never exported, all temporary files are removed, and the
  machine receipt contains only aggregate facts and implementation hashes; and
- socket, URL-open, process-launch, and shell capabilities are denied throughout
  the scenarios, with zero attempted use.

The no-egress control applies to the application scenarios. CI checkout,
toolchain acquisition, and the separately invoked dependency audit retain their
documented network boundaries.

## CPU-LITE result

The 2026-09-07 CPython 3.13.15 run used Linux x86_64 CPU-LITE (4 vCPU,
16,221,589,504 bytes RAM, no GPU). All 25 functional/security checks passed.
The three scenarios completed in 228,117,264 ns total, process peak RSS was
35,459,072 bytes, and the 1 ms sampled peak temporary-store footprint was
946,401 logical bytes. The 10-second, 512 MiB, and 32 MiB PR bounds all passed.
These values are suite-cost observations for the tiny synthetic fixture, not
media-throughput claims.

The complete redacted measurements and source/fixture hashes are retained in the
[machine-readable receipt](v01-regressions-cpu-lite-receipt.json).
