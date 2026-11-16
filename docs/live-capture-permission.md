# Native live capture permission (#623)

Status: native permission increment under qualification. Adaptive Live remains
off. These internal APIs start no microphone, STT worker, inference or playback.
They supply one required guard for the completion-driven path; they do not grant
provider disclosure, Agent execution or physical resource authority.

## Original registration and negative fence

`native_live_capture_v2`, schema version 2.0, binds a registration to the native
Session backend/controller, Converse owner/capture and session/thread/epoch.
The authenticated Converse service is the sole mutation role. A transcript,
model response, owner UI request or Decisions result cannot create that role.

Registration produces an immutable native receipt with a random registration
identity and the current journal sequence boundary. A source must follow that
boundary and match the exact owner/capture/scope before capture permission can
allow it. A registration cannot retroactively adopt an older transcript. The
worker must additionally validate the original eligible source, model lease,
disclosure and current native authority before inference and publication.

The fence binds its exact original registration, request identity and reason.
It may arrive before registration completes, storing a negative tombstone with
no registration receipt. A delayed registration returns that same closed state;
it cannot reopen the original. A fence after registration preserves its receipt.
Exact retries return the original evidence. Different identities, scope or fence
requests refuse instead of replacing an earlier original. Lookup is read-only.

`capture_open` means only that this registered native permission remains current.
It requires a positive receipt, no fence, the current controller and current
Converse session/thread/epoch. `capture_permits_source` additionally requires a
positive exact transcript/capture pair after registration. Missing receipt is
unknown. Neither false permission nor a fence proves that earlier model, HTTP,
microphone, STT, PCM or playback work stopped. Their original owners must still
settle. Late valid recognized text remains retainable after fencing.

## Native ownership and bounded recovery

Trusted Session startup explicitly activates a new native capture controller while
retaining the backend identity and original ledger. Replacement invalidates the
old controller; reopening cannot synthesize permission for old captures.
Passive Store construction and read-only metadata do not initialize or claim a
controller. Repeating startup activation cannot reclaim a replaced controller.
Historical receipt lookup remains attributable in the replacement controller,
with capture permission closed. Reset closes old-epoch permission; deletion
removes scoped metadata and leaves earlier writes unknown. Closed/unavailable
controllers refuse. Corrupt/missing native state is never repaired by resetting
the ledger; unrelated legacy Session operations remain available.

The registry serializes each owned mutation with the original Store lock and
`BEGIN IMMEDIATE`. An existing transaction is refused before entering another
SQLite connection context, preserving its owner's uncommitted work. All record
columns are bounded in SQLite before Python allocation, then strict metadata,
native columns and full original-request digests must agree.

There are at most 64 current open permissions and 10,000 retained registration/
fence records. Fencing frees only logical permission capacity. It does not refund
physical work. Records are not evicted to obtain capacity; exhaustion refuses.
Requests are bounded to 4 KiB, responses to 16 KiB, with duplicate-key, nonfinite/
fractional-counter, unsafe structure and coercion refusal. Body collection has a
five-second deadline and rechecks credentials before any effect. Responses are
uncached. JournalClient retains the original send/read/sole close through caller
cancellation; no automatic mutation retry or lookup polling is introduced.

## Internal endpoints and evidence

Under `/v2/transcript/live-captures`, GET `backend` and POST `lookup`/`source`
require Converse or Decisions. POST `register`/`fence` require Converse.
LocalJournal applies the same role rules. Invalid input returns 422; invalid
envelope 400, unsupported encoding 415, excess body 413, body deadline 408,
unavailable original controller 503, and original-operation conflict 409.
No status claims that an uncertain earlier request had no effect.

See [the qualification receipt](evidence/live-capture-permission-2026-11-16/README.md)
for actual SQLite/ASGI/client controls, source and log pins, and the retained first
transaction failure. All prior schema bytes remain unchanged; seven additive
permission schemas have an independent byte pin.

The [native v2 inference increment](live-decisions-attempts.md) implements charged
admission and exact result publication against these original capture receipts.
Still required for full #623: completion/wake scheduling, same-capture decoder rollover, Converse/
Agent consumers, backchannel/echo/mute ownership, timings and qualified rollout.
Synthetic permission checks do not qualify physical audio or model quality.
