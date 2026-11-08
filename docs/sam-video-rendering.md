# Native SAM video rendering

The opt-in task-media owner pins the original MP4 SHA-256 and independently
measured dimensions, frame rate, frame count and duration. The provider sends
the complete source video under that exact source/prompt grant. A SAM response
does not supply a path, command, code, credential or permission.

`SamVideoProjector` renders only that source. A native worker runs in a private
user/PID/network namespace with no inherited environment, credentials, Agent
workspace or physical devices. Its writable directory contains copied inputs
and bounded outputs. The system runtime libraries, trusted worker and pinned
official parser are read only. Ubuntu library alternatives are also read only;
the system linker cache and GPU directories are not exposed.

Before the official parser can allocate rasters, the worker bounds every mask
token's decoded area and every source coordinate grid, including the protocol's
full whitespace set. It refuses malformed output, unsupported diagnostics,
out-of-range frames, inconsistent coordinates, empty masks and resource excess.
The codec is copied unchanged from `meta-models/meta-sam` commit
`9348a8a791d3e5b6417232227e32e10464cdf49f`. Per-file provenance, SAM License and
upstream third-party notices are retained in `packages/vendor/meta_sam_parser`.

The worker paints validated per-frame masks onto transparent PNG frames and
uses the existing system FFmpeg to composite green highlights onto the exact
video, retaining its audio. Original and highlighted bytes are registered by a
shared native owner with task/session/epoch/revision/scope and digest receipts.
Generated code receives only an opaque asset receipt. Publication neither
establishes task success nor validates an ASL interpretation.

The original spawn and child process remain owned through cancellation; the
PID namespace is retired before cancellation propagates. Failed render inputs
and logs stay in the private render directory for diagnosis. Successful work
directories are removed only after native registration and current-owner checks.

Bounds: source at most 1280×720, 1800 frames, 60 seconds; response text 48 MiB,
8000 mask tokens, 262144 pixels per raster, 64 MiB total raster pixels and
256 MiB projected overlay pixels. The isolated worker has address-space, CPU,
file-size and descriptor limits; its parent has a 240-second wall deadline.
Rendered video is at most 32 MiB. These are qualification bounds, not claims of
arbitrary-video support.

Synthetic tests exercise a real isolated decode and render, verify visible
green mask pixels and untouched blue background, and cover source, quota,
coordinate, BOM-whitespace and provenance failures. They make no provider call
and do not establish actual ASL segmentation or transcription accuracy.
