# Exact-PTS frame sampling validation

- Issue: V01-12
- Result: pass on the required CPU-LITE profile
- Policy: `nearest_eligible_pts` at configurable exact rational target FPS
- Evidence: [machine-readable receipt](pts-frame-sampling-cpu-lite-receipt.json)

## Implemented policy

The first eligible source PTS anchors target zero. Later targets are exact
`origin + n / target_fps` rational times. For each adjacent pair, the closer
source PTS is selected; an exact midpoint or equal-PTS tie chooses the lower
decode index, including across pages. One source frame is emitted at most once,
even across a long VFR gap, and original
`FrameRef` values are returned unchanged. A positive final frame duration gives
an exclusive end-of-stream horizon; no frame or timestamp is synthesized.

Each call accepts at most 64 candidates from one stream. Resumed pages repeat
the complete prior frame at index zero and carry an immutable cursor binding the
source, time base, sampling configuration, origin, prior frame, equal-PTS tie
representative, next target, last emitted identity, and total candidates. The default total cap is 100,000
candidates and the default exact time-span cap is 3,600 seconds. Cancellation
returns a structured error without publishing a result or advanced cursor.

## Acceptance result

Decoded CFR and rotation fixtures selected exact PTS `0, 200, 400, 600`; the VFR
fixture selected exact PTS `0, 100, 350, 500`. One-shot, repeated, and bounded
paged runs produced identical frame identities and retained the original exact
timestamps. Cancellation, page-cap, and duration-cap cases returned their
expected structured failures.

On the 4-vCPU, 16-GB CPU-LITE VM, one maximum-size 64-candidate batch selected
11 frames in 487,105 ns wall time and 483,037 ns process CPU time, or 131,388
candidates/second. Process peak RSS was 34,119,680 bytes. These values establish
the first baseline; they are not a cross-machine performance claim or tuning
target.

## Limits

This validates deterministic sampling of supplied decoded candidates. It does
not add adaptive or motion-aware sampling, fabricate missing PTS, tune for a
specific machine, or make detector-recall claims. Those remain later milestone
work.
