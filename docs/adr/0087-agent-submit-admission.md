# ADR 0087: Native Agent submission admission and uncertain receipts

Status: Implemented with deterministic local qualification; exact-head CI is
required before merge. Tracking: [#199](https://github.com/rgoza1996/Kinaiya/issues/199).

## Requirement and failure

Roadmap §5.4 requires durable acknowledgments, idempotent handlers and reconciliation
of uncertain outcomes. A timeout cannot establish that a write failed. The
initial Agent submission currently enters its RAM cache before journal persistence
is acknowledged. A lost acknowledgment can therefore leave an apparently accepted
task with no worker, whether the native write committed or not.

The provider-independent retained receipt behavior from
[#192](https://github.com/rgoza1996/Kinaiya/issues/192) and
[ADR0077](0077-options-workspace-and-harness-models.md) remains required. Reading an
old receipt must not select a current provider, create work, acquire a new grant or
replace its historical model pin.

## Decision

The Session service arbitrates one logical submission slot for each session and
original client idempotency key. A SQLite uniqueness constraint covers concurrent
connections and different epochs. The slot and native journal entry commit in one
transaction. The internal append operation must not introduce a nested connection
context that commits the entry before its slot.

An admission response distinguishes `created` from `retained`. Only the direct,
valid `created` response can confer a one-use local opportunity to schedule a
worker, after fresh source/session checks and before cancellation, reset or close
suppresses launch. `created` is never a durable entitlement replayed to another
caller. A retained winner may have a different task ID, harness and model pin;
the winner's original metadata, including auxiliary model pins, remains authoritative.

The Agent reserves a frozen pending record before sending admission. It retains
the original task ID, provider reference, digest and complete native append body.
While persistence is unconfirmed, public status is paused with an empty summary
and the fixed uncertainty error. Exact overlapping retries do not issue another
admission request. Changed input remains a conflict.

Cancellation, timeout, malformed proof and shutdown suppress launch before any
further await. Positive reconciliation can establish a paused or terminal receipt;
it cannot restart execution. A successful lookup returning `unobserved` is only a
current observation: an older operation can still commit afterward. A later fresh
proposal must still arbitrate through the same native slot.

Pending work cannot steer, consume approvals or append follow-up task records.
An owner stop can suppress local execution while still reporting uncertainty;
it cannot claim durable cancellation without native evidence. Owned transport
operations and cleanup remain bounded, including late responses after close.
Elapsed deadlines are checked before accepting a completed result, not only while
waiting for one. A retired authority read cannot initiate another native read.

Preparation shares the management boundary for at most five seconds in total,
covering source authorization, native lookup and the frozen reservation. The
15-second admission and subsequent fresh authority check run outside that lock.
Uncertain pending identities and owned operations each have a 128-entry limit;
capacity refusal cannot evict a key whose native outcome remains unknown.

Retained records bind the complete validated initial native entry. A Task ID
collision cannot substitute another session's record, overwrite a live record
or let recovery silently choose the first entry. Conflicting native identities
refuse without changing either native history or an existing runtime task.

## Service contract

Both native endpoints require the authenticated Agent service identity, including
Agent-authorized work routed from a Converse session. Session channel does not
grant or revoke that service identity. Their
JSON bodies are strict UTF8, finite, closed and reject duplicate keys. Responses
contain nested journal objects and require identity content encoding.

| Session endpoint | Request cap | Total client deadline | Success |
| --- | --- | --- | --- |
| `POST /v1/transcript/agent-submits` | 65,536 bytes | 15 seconds | HTTP200, created or retained receipt |
| `POST /v1/transcript/agent-submits/lookup` | 4,096 bytes | 5 seconds | HTTP200, recorded or unobserved observation |

The response cap is 69,632 bytes, including the receipt envelope. Parsing and
owned response close must succeed before a successful observation is returned.
Transport failure, timeout, malformed data and failed close never become
`unobserved`. A created acknowledgment must match the entire frozen proposal,
including native identity and scope. Retained receipts must match the original
logical request, key and scope rather than a losing proposal's provider choices.

An occupied key with conflicting scope or content returns HTTP409 with the fixed
conflict diagnostic. Unavailable native admission returns HTTP503. Agent create
or stop with unconfirmed initial persistence returns HTTP503 with `Agent submission
persistence is unconfirmed`; it cannot return an HTTP201 acceptance. Ordinary
authentication failures remain HTTP401/403 and malformed native requests HTTP422.

## Upgrade and limits

Valid historical native submissions are backfilled without rewriting journal
history. Preserve the known missing/null model-pin formats and the existing
pre-scope local-only digest exception. Published pre-scope writers omitted both
requested and effective scope; validated historical omissions are interpreted
as local-only without inserting either field into the retained native proof.
New proposals still require both scope fields. Ambiguous duplicate keys or invalid native
provenance make Agent admission and lookup unavailable; independent Session reads
and Converse operations remain available. No row deletion, guessed winner or
automatic repair is permitted.

Validate the original stored request before any model coercion and compare its
complete append projection with the canonical entry and immutable SQL identity.
Corrupt potential submissions cannot disappear from migration because both JSON
cells failed parsing. Migration keeps only bounded pages in memory and uses the
native unique relation to identify conflicting history. Every failed native
transaction rolls back before an unrelated operation can use the connection;
an HTTP error alone does not establish rollback.
If rollback itself fails, close the unsafe connection rather than allowing a
later unrelated write to commit the partial transaction. That connection then
requires recovery; continued Session availability is not claimed for it.

Quiesce old Agent and Session writers before deployment. Generic submit append is
rejected before ordinary idempotency replay, including an exact historical
append retry. Other journal kinds retain their existing behavior. Mixed old
writers are unsupported because their generic append acknowledgment cannot
establish exclusive worker ownership.

This is not a durable Agent outbox, distributed execution lease or exactly-once
guarantee for arbitrary tool effects. A process can lose an uncommitted RAM
proposal; an already recorded canonical submission remains authoritative.

## Verification

Use actual SQLite connections, native HTTP services and controlled transport
faults without paid model inference. Verify before-commit failure, committed lost
acknowledgment, held late writes, overlapping retries, canonical retained identity,
source/scope changes, restart, cancel, reset and shutdown. Exercise failure at both
entry and slot insertion so separate transactions cannot pass a rollback test.
Client checks cover strict encoding, request/response limits, positive and negative
receipt bindings, total deadlines and owned response cleanup. Retain the existing
confirmed-receipt, source/disclosure, model-pin and completion fences.

The [retained verification record](../../evaluation/evidence/agent-submit-admission-2026-10-31.json)
binds the corrected functional source and tests. The lead's integrated submission
suite passed 396 cases with no failures, errors or skips; separate author and
independent review groups overlap that coverage and are reported separately.
Earlier failures, the intentionally interrupted full local run, fixture corrections
and actual Muse/Codex attribution remain recorded. Final Python 3.11/3.12,
Flutter, Android and iOS CI must pass on the exact PR head before merge or issue
closure. These checks do not establish a physical device or live-model qualification.
