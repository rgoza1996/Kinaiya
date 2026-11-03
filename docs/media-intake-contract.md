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

## Ordinary event channel disconnects

The ordinary journal channel keeps its original application Task as the sole
journal sender, with one persistent receiver after authentication and media-mode
selection. A `WebSocketDisconnect` from receiving or sending ends this channel
normally, including Starlette's conversion of a transport `OSError` to code
1006. Other read, send and receiver errors retain their original failure. A
failed send never advances the journal cursor or establishes delivery.

Retirement requests receiver cancellation once and joins that original Task.
The original five-second cleanup observation does not establish completion:
an unresolved receiver stays in the same application's custody until it joins.
A blocked synchronous Store read or cancellation-resistant send can still delay
retirement. These channel observations grant no media cleanup authority.

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

## Native Session regression evidence

The [initial November 3 record](../evaluation/evidence/media-session-owner-2026-11-03.json)
retains the 176-case owner, lifecycle, ordinary event-channel and receipt-parser
qualification and the earlier 79-pass/one-failure result. Subsequent CI527 failed
fixture shutdown in the read-only current-status case: its original cleanup
Task returned false. That result remains a failure, with its precise cause
unproved.

The [connection-retirement record](../evaluation/evidence/media-connection-retirement-2026-11-03.json)
qualifies the correction at source `aa5223a`: **180 passed**, including the prior
176 unchanged cases and four new cases. A physical connection join can precede
its original native publication. Source cleanup therefore joins that same
retirement Task through publication and requires its positive result, even
after hub registry removal. A failed original retains source custody even if
its SQL publication committed. No replacement retirement Task, deadline
extension or retry is introduced.

The four new cases were independently reviewed before execution. Against the
unchanged old production source, two failed and two passed; all four then
passed with the correction. These controlled failures demonstrate the guarded
interleavings, not their incidence in CI527. The separate five-case focused run
overlaps the 180-case run; their counts are not additive. Intentionally failed
originals keep unconfirmed cleanup and reservation; disposing fixture handles
does not turn those outcomes into positive native cleanup.

These checks do not establish a timing cause or qualify decoder/model/device
behavior, physical cleanup of arbitrary operations, the standing64 capacity
gate or the complete Omni demo. Changed-head CI and parent integration remain
required.

### Fresh-parse audit preservation and local timing

The [fresh-parse record](../evaluation/evidence/media-fresh-parse-audit-2026-11-03.json)
adds 16 preservation cases for canonical native history and corrupt request,
catalog and receipt storage. All 16 pass against the preceding implementation;
the candidate passes the broader **196-case** suite, including the unchanged
180 cases above. The focused 112 cases overlap this suite and are not additional
unique coverage.

Every audit still parses and validates the complete persisted graph. Within that
same synchronous call chain, private helpers encode/hash the fresh, unexposed
parse without immediately reconstructing it again. There is no cross-call cache,
skipped row, weaker public serializer, new cleanup authority or longer deadline.
Arbitrary typed inputs continue through the strict public codec.

In one instrumented local run per revision, the same history case closed in
4.550 seconds before the change and 3.499 seconds afterward, within the unchanged
five-second deadline. Four closing audits accounted for 4.489 and 3.440 seconds,
respectively. These are descriptive local observations: instrumentation and host
load affect timing, nested spans overlap, and the observations do not prove the
earlier CI529 failure's exact cause or guarantee CI/platform latency. CI529's
unconfirmed-shutdown failure remains recorded; changed-head CI is required.

The opt-in [diagnostic plugin](../evaluation/media_cleanup_diagnostic.py) publishes
the instrument for reproduction. It now distinguishes the native observer from
the synchronous worker it delegates to, correlates nested audit spans, and
retains then-pending observations when `close` returns. This extends the earlier
completed-span-only instrument; the historical measurements above were collected
with that earlier revision. The plugin uses private owner methods and requires
a compatible source revision.
Run it in a fresh, dedicated pytest process from a checkout of the desired
revision with this plugin on the Python path, using the locked development
environment:

```sh
mkdir -p .dev-run
PYTHONPATH="evaluation:.:tests/integration:tests/unit" \
  KINAIYA_MEDIA_PHASE_OUTPUT=".dev-run/media-cleanup-phases.json" \
  .venv/bin/python -B -m pytest -q -m 'not live' -p no:cacheprovider \
  -p media_cleanup_diagnostic \
  -o asyncio_default_fixture_loop_scope=function \
  tests/integration/test_session_media_owner.py::test_all_index_ack_history_survives_recent_cache_boundary
```

The report retains categorical aggregates, the last 512 completed spans, at most
512 pending spans and eight close-return snapshots. Recorder-local sequence
numbers correlate observer, worker and audit spans; they are not native identity
or ownership proof. Close snapshots privately select observations of the same
owner object and retain deadline remaining/overrun when available. The worker
wrapper calls the original function once with unchanged arguments. Its clock,
lock and wrapper overhead, and retained recorder references, can affect timing
and object lifetime.

A missing span says nothing about work still in flight; any recorder loss
invalidates completeness claims. Worker/audit CPU is measured on the same
thread; async CPU is unmeasured, nested totals overlap, and category/task/row
state alone never proves physical cleanup or timeout cause. The categorical report's content
limits do not apply to ordinary pytest logs/JUnit. This command uses synthetic native
Auth/ASGI/SQLite/spool machinery without a decoder, model or audio device. The
parent media foundation has merged; decoder launch and the complete Omni demo
remain unavailable at this boundary.

Later [CI579](https://github.com/rgoza1996/Kinaiya/actions/runs/41715054223)
at `07a2157` retained an unconfirmed native shutdown in the history case on
Python 3.11: 12,111 passed, one failed, six skipped and two deselected. Python
3.12 and the platform jobs passed. Its bounded snapshot showed source cleanup
and one native job pending before the final close barrier; it did not identify
the exact pending worker or establish the cause. The five-second production
deadline and history assertions remain unchanged. New phase observations are
diagnostic evidence, not a repair or a replacement for passing acceptance/CI.

### Reuse within one synchronous audit

Each locked history audit has a private receipt memo. A receipt enters it only
after fresh strict parsing, canonical encoding and every persisted-field check.
Reuse requires an exact, type-sensitive match for all fields read by that parser;
SQL, reference, history, request, fence and link checks still run. The memo never
crosses a call or the transaction's mutation yield: pre- and post-audits create
separate instances. Parsed models and their mutable children stay inside the
audit; ordinary native lookups continue to parse fresh values.

The optimization retains at most 64 entries and 4 MiB of estimated key content.
This is not a total Python heap bound. Unsupported or oversized keys and evicted
entries take the original fresh-parse path. These limits neither evict native
history nor change admission limits, cleanup authority or the five-second
shutdown deadline. Qualification and diagnostic observations remain separate.
