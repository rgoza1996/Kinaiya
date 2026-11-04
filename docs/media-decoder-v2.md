# Explicit native decoder v2 handshake

Related: #204, #357 and the [media protocol](media-protocol.md).

The v2 contract supplies the networkless decoder with the information absent
from the original v1 probe/open records: the selected profile before probing,
actual first-PTS observations, an explicit probe-index to native-track/format
mapping, the original Seal reference, and segment budget/cursor bindings.
These records and the framed codec grant no admission, currentness, permission,
charged budget or physical cleanup authority. Native owner composition and
actual executable qualification remain separate requirements.

## Explicit version selection

Select `pyav17_timed_fd_v2` with `decoder_fd_v2` in a pinned DecoderProfile.
The profile itself stays version 1; its new driver and protocol values form a
required pair. Existing v1 driver/profile declarations and v1 request/response
records remain supported by their existing interfaces. The planner supports
its prior PyAV v1 pair plus the new PyAV v2 pair; FFmpeg planning is not enabled.

The new request/response functions are `decoder_request_v2_bytes/json` and
`decoder_response_v2_bytes/json` in `services.common.media_decoder_wire`.
They use the existing uint32be header length, canonical JSON and exact declared
binary body framing. They accept the exact registered v2 record types only.
The v1 functions do not inspect a version and silently upgrade. Ordinary
65,536-byte control, depth, node and body bounds still apply. Receipt and Found
wrappers remain version 1 even when their nested record is version 2.

## Probe and open

Every DecodeRequestV2 requires operation, source, Seal, consumer, fence, original
child-owner, profile hash and sequence bindings. A `probe` carries the selected
profile and a null plan; `open` carries the open plan and a null profile; `next`
and `cancel` carry both nullable fields as null. The probe's selected profile
must name the v2 driver/protocol pair and match the request's hash.

ProbeObservation contains the original source, Seal, child-owner and profile
bindings, the existing ProbeResult metadata and one ProbeClock for every probe
index. Clocks retain signed original first PTS, a positive exact rational
original timebase and explicit provenance. Untimed images use zero PTS with
their actual declared positive timebase. Missing clocks, duplicate indices,
wrong timebases and incorrect provenance refuse; kind or array order cannot
supply a missing association.

DecodeOpenPlan carries that exact observation, DecodeReady, one explicit
DecodeSelection per probe index, Segment, a native budget reference and its
DecodeBudget snapshot, the next source sequence, and one next-sequence cursor
per native track. A selection names its probe index, complete native Track and
complete selected Format. Multiple audio or video tracks remain distinct,
including different IDs, formats and clocks. Native track records must match
DecodeReady exactly. The segment and cursors must cover its exact track set;
source, Seal, profile, revision, generation, child and running segment-budget
bindings must agree. The request's Seal is explicit because the child cannot
derive a native receipt reference from media bytes.

The full native planner preserves the existing selected PCM raw/WAV formats,
16/24/32-bit codecs, supported rates/channels and PNG/JPEG rgb24 image/frame
formats. It checks the selected profile's formats/transforms, original and
selected geometry, all original tracks and the exact common origin computed
from observed timed first PTS. A structural record alone does not establish
these facts.

## Native planning and authority

`build_decode_plan_from_open` makes a fresh closed snapshot of its inputs,
checks the v2 source/Seal/profile/segment correspondence, and delegates to the
existing full `build_decode_plan`. Its returned DecodePlan and digest retain
the existing initial descriptor/clock correspondence semantics. They are not
an open-plan payload digest, a continuation authorization or a budget receipt.
Resolving native references, validating the original charged probe/segment
budget, checking current authorization and retaining actual launch/reader/
provider/publication ownership are the native owner's responsibility.

No probe allowance is created by putting a profile in a request. Native probe
admission and accounting must precede actual probing. Cancel acknowledgement,
segment boundary, true source EOF, publication completion and physical process
cleanup remain different observations. The v2 records do not enable start or
resume on their own or qualify AT64–AT66 or the sign-language demonstration.
