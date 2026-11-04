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

Select `pyav17_timed_fd_v2` with `decoder_fd_v2` in a pinned
`MediaDecoderProfileV2` and its exact `MediaDecoderProtocolV2`. These are separate
closed DTOs, registered under `DecoderProfileV2` and `DecoderProtocolV2` hash
domains. The profile's metadata `v` stays 1; its exact class, driver, protocol
and self-hash domain identify the new generation. Its self hash omits only
`profile_sha256`, with all nested fields retained.

The original `MediaDecoderProfile` and `MediaDecoderProtocol` retain their
original closed fields, literals, validation and exported schema bytes. V1
handlers do not accept or reinterpret V2 profiles. The planner accepts the
exact original PyAV v1 profile/pair or exact new PyAV v2 profile/pair; FFmpeg
planning is not enabled. The native Session constructor and persisted catalog
currently resolve the original `DecoderProfile` only. Explicit V2 native
catalog registration and launch integration remain pending.

PR441's original CI602 run exposed six changed v1 profile/protocol schema files
and an omitted 21-schema inventory exclusion. The repair separates the V2
types instead of changing the original 69-schema byte pin or the original
184-schema count/hash. Its exact additive inventory is nine records with
Receipt/Found wrappers: the seven handshake records plus the two V2 profile
and protocol records. Prior executable results below remain tied to their
recorded revisions; this schema/type repair needs its own qualification.

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

## Selected finite decoder timing and payload semantics

These are the selected rules for the executable v2 child. The bounded child
qualification below does not establish native owner or deployment authority.

A positive Segment selects whole PCM samples by their actual presentation
start in the logical half-open interval `[start, end)`. Arbitrary rational
boundaries do not need to lie on the selected PCM sample grid. Splitting uses
the ceiling sample index and retains the exact unselected suffix for the next
contiguous segment. Packet start/end report the actual sample-grid interval;
the last whole sample can extend past the logical segment end by less than
one selected sample. Metadata is never rounded to the requested boundary, and
no fractional sample, silence, dropped sample or duplicate sample is created.
All existing packet duration, byte and source limits still apply. A zero-width
logical interval contains no PCM sample starts.

The first physical open honors the supplied native publication cursors and
logical interval, including when the segment index is zero. That index does
not establish empty durable source history or a zero seek position. A genuinely
fresh native source starts its publication sequences at one; the native owner
must resolve and authorize actual history, generation, position and budget.
The child decodes any required prefix under the existing charge and preserves
original sample/frame positions. Retained continuation still requires exact
prior cursors and adjacency; a fresh nonzero-index open still requires the
explicit `one_segment_child` profile. None of these child inputs grants seek,
reset, history or continuation authority.

Elementary still images and video frames are point observations at their
actual original presentation instant: packet start equals packet end. Positive
segments select those points half-open; a zero-width point segment includes
the point exactly equal to that position. An untimed still image can therefore
be decoded at `[0, 0]` without inventing a positive duration. Only actual
all-track decoder exhaustion establishes source EOF. A segment descriptor or
a point packet alone cannot establish it.

PCM sample_start/sample_count count the selected output PCM track. SourcePTS
retains an actually observed original input-frame anchor and original timebase.
Resampler output timing can locate the output samples within the retained
bounded original-anchor intervals; it cannot replace original SourcePTS.
Known forward timestamp gaps already present in a finite recording are
preserved without inserting silence. Resampling state may be flushed and
restarted at such a gap while global output sample indices remain continuous.
This recorded timing is distinct from a lost native capture interval, owner
seek or reset, whose native generation/fence rules remain required. Backward
or overlapping timing that cannot be represented by this initial decoder
remains explicitly unsupported until its discontinuity handling is qualified.

The child must reserve aggregate pending wire-payload capacity before
materializing another candidate. Retained replay and carry payloads count,
including actual copied bytes; immutable shared backing is not confused with
pointer size. Decoder/codec/PCM processing scratch is a separate address-space
obligation, not a claim that a Python payload counter measures total heap or
RSS. Native queue/reservation and positive original settlement remain external
owner responsibilities.

## Bounded executable qualification

The [November 3 evidence record](../evaluation/evidence/media-decoder-v2-child-2026-11-03.json)
records **30 passing real-media cases** at `abeb079`, using the actual PyAV17.1.0
child through MediaWorkerPool, its private bootstrap and original FD3/FD4. They
check raw/WAV PCM16/24/32 bytes, original input-frame anchors, timed MP4 frames,
PNG/JPEG output, zero-width still images, rational sample cuts, a recorded AAC
timestamp gap, two independently mapped audio tracks, supplied publication
cursors, retained continuation, prefix reconstruction and actual original
process waits. The replacement pair reuses the same original sealed spool
upload and profile only after positive retirement of the prior process.

The original cursor restriction produced 11 passes and 16 failures with the
same 27-case oracle; those failures remain recorded. Correcting the restriction
produced 27 passes, then the expanded 30-case run passed. These overlapping
runs are not additive. A separate 638-case contract/provider interaction run
passed at `962ac16`; it uses controlled HTTPX/protocol bridge fixtures.

Native references, currentness and dependency callbacks in the child fixtures
are controlled test metadata. They do not resolve actual Session admission,
native charged budgets, sealed runtime/sandbox qualification, publication or
public cleanup receipts. The full codec/rate/channel/transform/clock/resource
matrix, native Session controller/history/provider composition, live capture,
Omni speech/duplex and end-to-end demonstration remain separate requirements.
Ordinary Session start/resume remains unavailable at this boundary.
