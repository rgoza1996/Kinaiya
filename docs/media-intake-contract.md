# Experimental media intake contracts

Related: [issue #204](https://github.com/rgoza1996/Kinaiya/issues/204),
[Roadmap AT64–AT66](../Roadmap.md#26-acceptance-test-matrix).

`packages/contracts/media.py` implements closed control records for source
intake, decoder descriptions and decoder requests. `services/common/media_wire.py`
implements bounded framed ingress. `services/session/media_spool.py` implements
private local storage and original reader ownership. These components do not
enable a decoder, authorize media access, or establish a working Omni pipeline.
Native Session composition and actual backend qualification remain required.

## Representation and limits

Ordinary control records fit 65,536 UTF-8 bytes, depth 24 and 8,192 value/container
nodes; object keys do not count as nodes. Keys are unique, every declared field
is required, nullable fields remain explicit, and numbers are exact safe integers.
There is no implicit coercion, format conversion, sampling or large-record flag.
Receipts omit only their designated outer digest when hashing; decoder profile
and runtime hashes omit only their explicitly named root self-digest. Nested
hashes remain part of their enclosing value.

The codec inspects actual typed values and metadata before reconstruction.
Unknown typed fields are `bad_packet` before their descendants are traversed.
An oversized declared list is bounded before expanding its children. Raw JSON
resource checks precede closed-record validation. These distinct cases do not
promise the same error precedence for every multiply-invalid input.

Media routes return the protocol's fixed errors: an invalid bearer is
`403 denied`, and an upload without its original WebSocket association is
`409 stale`. Both refuse intake before spool progress. Upload reservation
bounds precede sequence checks; a sparse-order test must otherwise fit its
reserved byte range. These classifications preserve the native refusal gates.

The encoded-record limit is not a peak memory guarantee. Bounded declared
objects can materialize a larger encoded representation before that final
envelope is refused. Native process and queue reservations are separate checks.

## Probe codec categories

The following clarification was selected on November 2, 2026 after the first
integrated test run. The initial descriptor grammar listed a global codec enum
without explicitly assigning codecs to track kinds. The codec now also checks
this category compatibility:

| Track kind | Representable codecs |
| --- | --- |
| `audio` | `pcm_s16le`, `pcm_s24le`, `pcm_s32le`, `aac`, `opus` |
| `image` | `png`, `mjpeg` |
| `video` | `png`, `mjpeg`, `h264`, `hevc`, `vp9`, `av1` |

PNG and MJPEG remain representable video frame codecs. This table checks
descriptor consistency; it does not qualify a container/codec pair or advertise
decoder support. Native owners must verify the actual original source, selected
profile, runtime, transforms and decoder observations. Empty or unknown profile
metadata remains unqualified.

## Source time and intervals

Keep signed original PTS and timebases. Rebased nonnegative positions use exact
rational arithmetic with a native-selected common origin; frame index is not a
clock. Untimed images use the explicit zero-origin convention without inventing
a positive duration or forcing their declared timebase to one.

The descriptor grammar permits equal Segment endpoints and caps the interval at
60 seconds. It has no positive-duration lower bound. Native execution still
checks the original tracks, source, interval, charged budget and actual progress;
a zero-width descriptor alone establishes neither useful work nor source EOF.
Do not tighten the shared interval helper to fabricate duration for still images.
`segment_end`, original input EOF, publication completion and physical cleanup
are separate observations.

## Storage ownership

The selected spool root must already exist, be private and owner-controlled.
Directory components and regular files are opened without following symlinks.
The spool keeps its original OS lock and charges uncertain uploads after errors
or restart. Replay checks actual retained bytes; it never rewrites an old chunk.
The recent progress cache is bounded while old replay proofs and deleted source
IDs remain on disk within the metadata budget.

The initial helper allows 16 active or unresolved original readers per upload.
Positive close frees a slot; ambiguous close does not. Ordinary close does not
release retained source bytes. Deletion waits for original readers, then checks
owned inodes, unlinks, flushes and persists the tombstone before releasing its
local reservation. A protected namespace is required: stat followed by unlink
is not an atomic inode-conditional operation against a hostile privileged actor.

These are local helper observations. They are not native SourceAdmission,
UploadAck, Seal or DeleteReceipt authority, nor decoder/provider/publication
cleanup. Native owners must compose every promised original dependency and
recheck permission, session, source, lease, scope and reservation before effects
and acknowledgment.

## Session reset and deletion composition

An enabled native media owner enrolls the original Session change and fences
matching in-memory sources and preparations before waiting for the Store lock.
The owner then controls the actual SQLite transaction, authorization recheck,
commit/rollback outcome and original result. It never joins an already-open
caller transaction. A reset advances the captured original epoch exactly once;
replaying its retained token returns its original result without new DML.

Deletion commits its negative fence first. Outside the transaction it requests
retirement of the original operations, then checks all original claims in a
separate guarded transaction before deleting Session/history. A pending cleanup
refusal retains that committed fence and history. An explicit retry of the same
original skips the fence DML. An uncertain transaction retains its claim and
prevents automatic retry or a successful close claim.

If a registered reader-cleanup continuation already positively joined and
deleted its original upload, Session retirement retains that exact completed
Task when its probe, connection and original operations are also settled. It
does not start another physical cleanup for that source. Other pending or
unknown claims still block final deletion; this adds no general first-request
completion guarantee and does not bypass the native final transaction checks.

The owner-only HTTP routes enroll before their first await. A native controller
retains one original worker across cancelled or timed-out HTTP observations;
five seconds bounds the observation, not physical completion. The selected
bookkeeping limit is 16 pending/unresolved changes and 64 recent terminal
originals. Exhausting pending-change or native-worker slots returns the fixed
`capacity` error (HTTP 429). Conflicting commands and additional in-flight
observers return `conflict` (HTTP 409). Its fixed 10,813,440-byte lifetime reserve
belongs to the existing 1 GiB metadata budget. With no registered owner,
persisted `media_` tables still
prevent reset/deletion from bypassing original custody.

This is the selected integration contract. It does not qualify decoder launch,
real capture, model inference, device behavior or the complete Omni pipeline.

## Remaining decoder launch work

The existing general worker launcher does not yet supply the original media
duplex control endpoint and read-only source descriptor required by this driver.
The initial descriptor protocol also needs explicit probe-index to allocated
track/format mapping, original clock observations, initial sequence/budget
binding and a sealed-runtime launch contract. A profile hash or an unstated
array order cannot supply those values to a networkless child.

Decoder execution remains unavailable until those interfaces, per-segment
resource enforcement and original peer/control/body/publication settlement are
implemented and verified. Parsing, storage checks and descriptor tests do not
pass AT64–AT66 or establish a sign-language demonstration.
