# Deterministic CLI vertical-slice validation

## Scope

Issue #17 exposes the already-reviewed fake/manual coordinator path through five
commands: `probe`, `ingest`, `inspect-run`, `list-samples`, and `show-evidence`.
The slice has zero third-party runtime dependencies and intentionally accepts no
real media, model output, server traffic, natural-language query, or ambient
network capability.

## Contract and security checks

- Successful data commands write exactly one canonical ASCII JSON document to
  standard output and return `0`; help and version retain their conventional
  plain-text output.
- Usage failures write a stable JSON error to standard error and return `2`;
  operational failures return `1`; cancellation returns `130`.
- Argument-parser failures discard the parser's input-bearing message. Runtime
  failures expose only bounded error codes, operations, and retryability, never
  rejected input, local paths, exception text, or pixels.
- `list-samples` uses committed-run ownership joins rather than source-wide
  reads, so records from another run cannot be mixed into the response. Dangling
  or mismatched ownership fails as corrupt.
- `show-evidence` writes only to an explicit absolute destination under an
  existing private directory, refuses links and overwrite, creates mode `0400`,
  and refuses destinations inside the store itself.
- Checked-in help, probe, usage-error, and cancellation goldens lock the public
  surface. The wheel gate repeats the full quickstart from a fresh offline
  runtime outside the checkout.

## Exact-crop result

The built-in fixture is one 2×2 packed-RGB24 frame with bytes `0..11`. The
default half-open box `(1, 0, 2, 2)` exports exactly six source bytes. The
exported SHA-256 is
`ee0c0ca710ef28a12edd6f7d1454fc6516e434d4e33c733f886bee5f3e62233c`.
The CLI reports the hash and dimensions but never prints the bytes or output
path.

## CPU-LITE result

The 2026-09-07 run used CPython 3.13.15 on the mandatory Linux x86_64 CPU-LITE
profile (4 vCPU, 16,221,589,504 bytes RAM, no GPU). Six fresh-process commands
completed in 569,547,335 ns total. The slowest individual command was below
117 ms, child peak RSS was 26,001,408 bytes, and the committed store occupied
200,710 logical bytes. All functional, redaction, timing, RSS, and disk bounds
passed. These are boundedness observations for this fixture, not real-video
throughput claims.

The complete redacted measurements and implementation hashes are in the
[machine-readable receipt](cli-vertical-slice-cpu-lite-receipt.json).
