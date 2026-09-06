# Original-pixel geometry and crop validation

- Issue: V01-13
- Result: pass on the required CPU-LITE profile
- Evidence: [machine-readable receipt](original-pixel-crops-cpu-lite-receipt.json)

## Implemented policy

`DetectorTransform` records an exact affine mapping from a detector input to
encoded source pixels. The detector's content rectangle may describe a full
stretch resize or the active image inside letterbox padding. Clockwise display
rotation is normalized to 0, 90, 180, or 270 degrees. Other rotations fail
closed in version 1.

Detector and source boxes are half-open integers. All four detector-box corners
are transformed with rational arithmetic; minima round down, maxima round up,
and the result clamps to source bounds. A region wholly outside source content
is rejected rather than producing empty evidence. The same transform,
producer-space box, and final source box are retained in the version-1
`Geometry` record.

`extract_rgb24_crop` copies packed RGB24 rows from the encoded source raster.
It never scales, rotates, interpolates, or converts the pixels. Crop values hide
their bytes from representations and expose only an SHA-256/byte-count
`Artifact` descriptor. The small validation sink accepts only bounded relative
ASCII destinations below an already-created absolute root. That root must be
private and owned by the effective UID; every opened destination parent must
share that owner and deny writes to group and other. The sink opens components
relative to retained descriptors without following symlinks, creates the final
file exclusively, removes its own incomplete inode on ordinary failure or
interruption, and never includes caller paths or bytes in errors. The trusted
caller must serialize same-principal namespace mutations while the call owns
its descriptors. Hostile same-UID mutation and custody after return are outside
this small sink's boundary; the later EvidenceStore owns the accepted kernel
writer lock, staging, CAS commit, recovery, and durable-custody protocol.

## Acceptance result

All twelve companion-manifest synthetic moving-region crops matched their
byte-level SHA-256 goldens, including the 90-degree display-rotation fixture.
Exact-size display mappings recovered the source boxes exactly. Mapping boxes
through a half-size detector recovered every boundary within one source pixel.
Traversal, symlink-parent, non-private-root, and writable-parent attempts were
rejected; no outside file appeared, and the written in-root artifact matched its
descriptor. Injected write/chmod/sync interruptions left no owned partial file
or open descriptor, while inode-aware cleanup preserved an unrelated replacement.

On the 4-vCPU, 16-GB CPU-LITE VM, the 60-second/5-FPS copy baseline processed
300 crops from a reusable 1920x1080 source raster. Each mapped 960x540 detector
box to a 1280x720 source crop, copying 829,440,000 bytes in aggregate. The exact
mapping, copy, required artifact SHA-256, wall, process-CPU, throughput, digest,
and positive peak-RSS observations are retained in the machine-readable receipt.
The harness fails rather than treating an absent or non-positive RSS/timing
observation as bounded. These values establish a baseline, not a cross-machine
performance claim or optimization target.

## Limits

This issue accepts manually or fake-specified integer boxes and packed RGB24
source frames. It does not add a detector, segmentation, best-frame scoring,
color conversion, arbitrary-angle resampling, persistent artifact storage, or
end-to-end ingestion orchestration.
