# Completion-driven live Decisions worker (#623)

Status: implementation increment under qualification. Adaptive Live remains off.
The native worker and discovery APIs do not complete the Call, decoder, Converse/
Agent consumer, backchannel, timing or rollout integration required by #623.
Construction starts no inference, microphone, STT or playback. Existing finalized
and Rolling Windows workers retain their explicit contracts.

## Durable discovery and bounded session turns

`LiveDecisionsPendingQuery` and `LiveDecisionsPendingPage` use explicit schema 2.0
and `native_live_decisions_v2`. Discovery returns at most 32 original native source
bindings after a canonical eligible sequence. It selects the latest annotation
per utterance, current session/thread/epoch and positive current capture
registration, excluding sources with a native outcome. Each returned source is
revalidated against its complete original source/capture pair and permission.
The hint grants no inference, provider, Agent or playback authority.

Identical raw hypotheses retain the same canonical sequence. Paging cannot turn
them into fresh input. Completed but unpublished sources remain discoverable;
cursor advancement never acknowledges or disposes them. Restart begins discovery
again from native history. Missing outcome evidence remains unknown.

The RAM queue holds at most 32 scoped candidates, coalescing each session/thread/
epoch/utterance to its latest eligible source. Discovery continues across bounded
pages when the queue fills, yielding between pages. An underrepresented session
can displace a RAM candidate from a more represented session; that candidate stays
in native history without an ACK, deletion, outcome or new charged allowance.
Session turns prevent one session's backlog from taking every next inference.
The original eligible source is refreshed again before model acquisition.

Only authenticated Decisions may POST `/v2/transcript/live-decisions/pending`.
LocalJournal uses the same role rule. The 4 KiB request uses the existing closed
identity-JSON envelope, five-second collection bound, post-body credential check
and uncached response. Query counters reject coercion; pages require bounded,
strictly ordered original sources and an exact original-query echo. Two additive
schemas have a separate byte pin; all 566 prior schema files retain their bytes.

## Completion, wake and actual ownership

`LiveDecisionsWorker` owns one actual inference slot. It snapshots/subscribes to
the original journal wake, scans durable candidates and starts eligible work.
Actual inference completion immediately triggers reconciliation and another scan;
there is no fixed sampling delay before fresh healthy input. A running original
wake Task survives inference completion. Only that wait's actual terminal result
permits its replacement. A wake grants only a recheck, never admission.

The 1,000 ms maximum idle heartbeat covers idle or unavailable-wake observation.
Failed wake acquisition/observation has a retry boundary; it cannot immediately
create an unbounded replacement-wait loop. Source-specific metadata/recovery
failures defer that exact source/selection for one second. A fresh source is a
different identity and is considered immediately. Seen/deferred observations are
bounded to 256 entries each; eviction cannot reset native charged history.

Each actual classifier Task holds its native model lease and constructs its own
`NativeLiveDecisionAttempt`. Direct awaits and `asyncio.timeout` preserve that
Task's admission identity. The default classification budget is 5 seconds, configurable
within 0.1–30 seconds. The worker reuses the existing retained-window context selector,
native task-source resolution and full-current-input request builder. Its context
and primary-input bounds remain explicit; no new semantic/style projection,
confidence floor, output rewrite or silent model/default change is added.

The [native inference contract](live-decisions-attempts.md) exclusively charges
and publishes the original result. Only known absence or a positively settled
retryable error can fund another call under the frozen native allowance. A strict
output-validation failure after an actual response is a terminal protocol
refusal, with exact raw response and charge retained. It cannot fund a reroll.
Both routing axes remain unchanged. Actual Agent availability is worker status;
downstream native consumers enforce their own permissions and revision checks.

Source, capture, model and disclosure are rechecked before result publication.
Stale valid completions remain diagnostic history. A model completing does not
prove that the speaker finished or that a backchannel should play.

## Exact-result recovery

A replacement reads the original native attempt. Completed output can be reused
only with the same backend/settings revision, selection, catalog revision,
provider/model/local declaration and protocol. Only the old service incarnation
identifier may differ; recovery never transfers the old provider opportunity.
Current capture/eligibility, the actual new lease and every original disclosed
source/floor are rechecked. Changed model/capture/disclosure preserves the old
completion and charge while refusing its use.

The original publication proof is derived from that stored completion. Recovery
first looks up its exact receipt. A positive original needs no publication call.
A missing receipt is unknown, not a failed-write assertion. With all current
checks established, recovery may submit that same immutable proof to Session's
idempotent native publisher. Session derives the unchanged result and serializes
its sole journal effect; this cannot create a different outcome or duplicate an
existing one. This metadata reconciliation never repeats inference or dispatches
Agent/playback effects. Read-only lookup itself performs no mutation. Lost-ACK
controls independently count provider calls, HTTP requests, exact proof bytes,
native charges and journal effects on both sides of commit.

Admitted/uncertain/legacy-uncertain, refused and exhausted native states grant no
new call. Model selection changes do not reset that history or repair an answer.

## Stop and failure evidence

Stop fences new scheduling and cancels the original active classifier once.
Its actual model lease and HTTP response close retain the slot through cleanup.
A failed close leaves the lease, original task/result and slot retained, reporting
unconfirmed cleanup. Fresh input cannot fund a replacement into that slot.
Model-owner busy state remains truthful.

The two-second close facade returns false when cleanup is pending or unconfirmed;
the retained original drain continues. A completed original journal wait is
collected without cancel-and-replace churn. JournalClient separately retains its
actual metadata acquisition/read/sole-close owners through facade cancellation.
No logical stop/currentness flag proves physical GPU, remote-provider, microphone,
STT or playback retirement.

The [qualification record](evidence/live-decisions-worker-2026-11-16/README.md)
links actual SQLite/ASGI/request-bound provider controls, exact input/log pins,
preserved first failures and the legacy context-selector comparison. These are
synthetic architecture checks. Physical audio, representative end-to-end timing
comparisons, same-capture rollover, consumers, backchannel/echo/mute and qualified
default rollout remain required. Model quality is diagnostic evidence.
