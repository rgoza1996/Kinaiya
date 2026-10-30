# 0057: Converse history follows the active context window

Issue #134 replaces the live eight-message and typed twelve-message host defaults
with a measured context budget. These were compatibility bounds, not model
capacity. Routing/state traffic in the latest 100 journal events could also hide
dialogue. This contract precedes implementation; native counting capability alone
does not establish completion or improved conversation quality.

## Shared invariants

Converse selects context; Session supplies authoritative, paginated records.
Preserve exact session/thread/epoch, authenticated origin, native revisions and
sticky disclosure, selected-memory pins and playback qualifications. Keep existing
assistant chunks as separate messages in this change. Their real template/token
cost, rather than a message count, determines retention. Do not rewrite the
transcript, infer heard words or change Agent/Decisions ownership.

Production host construction enables measured context budgeting. Unsupported
measurement is a visible refusal, never an implicit fixed-row or estimated-token
fallback. Direct embedded-runtime compatibility remains separately identifiable;
it is not the production host's budgeting path or evidence for this feature.

## Native history paging

Add `JournalClient.context_history` and the equivalent owner/Converse-only
`LocalJournal` operation, backed by `GET /v1/transcript/context-history`.
Parameters are `source_event_id`, optional `through_sequence` and
`before_sequence`, and page `limit` (1..200). The native source must be an
authenticated user `conversation.user` or `conversation.window` in the current
Converse session/thread/epoch. The store derives scope from that source.

The first page captures a committed watermark. Subsequent pages carry that exact
watermark and an exclusive descending keyset cursor. The versioned response
contains source identity, session/thread/epoch, `through_sequence`, descending
`entries`, nullable `next_before_sequence`, and explicit `exhausted`. Clients
validate scope, watermark, strict ordering and cursor progress. Malformed,
changed, stale or nonprogressing pages fail. A reset during paging refuses.

Base pages contain only user/window/Converse dialogue. For each base page,
fetch all matching qualifications through the same watermark using
`context_qualifications(source_event_id, through_sequence, base_event_ids,
before_sequence=None, limit=100)` and owner/Converse-only
`POST /v1/transcript/context-qualifications`. Its strict bounded JSON request
accepts 1..200 distinct base identities. The versioned response repeats those
identities in request order and uses its own descending cursor/exhaustion flag.
It validates base membership in the frozen source-derived query. Drain its
complete revision/receipt/control closure before projecting any requested base.
Generation stops may precede late obsolete assistant chunks, so fetching only
events newer than the oldest base is insufficient. Routing, resource and execution traffic
does not consume this page allowance. Window heads are selected at the frozen
watermark before disclosure filtering, retaining the same highest
window-index/revision/sequence semantics. Qualification records through that
watermark remain available before an older base entry is selected. Do not
backfill an old public draft after a private/invalid correction.

Pages remain bounded to 256 KiB of native entry JSON. A byte-limited short page is
not exhaustion. Do not consume an unreturned row; an individually oversized row
fails explicitly. Add suitable scope/kind/sequence indexes. Storage/page resource
limits are transport protections, not dialogue-token capacity. The awaited
builder has a ten-second overall deadline, a 256-measurement guard and a
16 MiB unique-native-record guard and refuses incomplete retrieval instead
of silently treating it as complete history.

## Complete-prompt selection

Introduce an awaited context builder shared by typed and live host paths. It
reads pages newest first and retains complete eligible messages in chronological
order. Reuse the current revision, disclosure and selected-memory gates; remove
the eight/twelve-row and 2,000-character cuts in this measured path. Current input
and protected personality/instructions are preserved byte-for-byte.

Compile each candidate's complete final prompt, including playback metadata and
any admitted selected-recall closure, then call the provider's exact
`measure_prompt(messages)`. Counts use `PromptTokenCount` (request and provider
binding digests, active context capacity and output reservation). Exact equality
fits: input tokens + reserved output tokens <= active context tokens. Never sum
separately counted messages or estimate template overhead.

Add recent eligible history while it fits. At overflow, remove the oldest whole
history message and rebuild chronological indices and playback metadata; retain
a suffix, without skipping an oversized newest message to fill with older text. If no
history message fits, retain the protected-only prompt; do not truncate the
newest stored message. The eviction unit is one stored message, including
existing assistant sentence chunks.
An oversized protected prompt refuses before generation. The complete playback
block participates in token measurement; do not independently truncate its
qualifications to retain otherwise unqualified assistant text. Whitelisting and
bounded receipt summaries remain authoritative.

Memory preparation must support an awaited builder receiving the current pin
and complete recall message. Keep lease cancellation, namespace/current-provider
checks, validation and publication ownership around every new await. If selected
recall cannot fit, omit that optional complete closure under the existing memory
contract and rebuild ordinary history; do not slice a dependency set. A changed
provider, reset, owner-off or memory invalidation cannot publish an old prompt.

## LM Studio native provider

Add an explicit local `kind: lmstudio` adapter; generic compatible HTTP behavior
is unchanged. Configuration selects a loopback `/llm` WebSocket endpoint, exact
already-loaded model alias, output bound, finite timeout and nonthinking mode.
The optional WebSocket dependency is pinned and imported lazily. Missing
dependencies or unsupported bodies/configuration fail clearly. Agent-only and
other-provider startup imports no model runtime or SDK.

Use the published auth-version-1 native RPC/channel contract. Select exactly one
alias with `listLoaded`, then bind count **and generation** to its immutable
`instanceReference`. Never use `model()`/get-or-load, fuzzy REST resolution, JIT,
downloads, settings mutation, CPU fallback or cloud. The current instance's
`llm.load.contextLength` is authoritative; the advertised model maximum is not.

The same validated text-only history and protected prediction-config stack feed
`applyPromptTemplate`, `countTokens` and native prediction. Nonthinking explicitly
sets `llm.prediction.reasoning.enableThinking: false`; output reservation equals
the protected max-predicted-token setting. Use an explicit stop-at-limit overflow
policy. Include all effective base/request formatting configuration in the
measurement binding. Pin the first binding/capacity/output reservation across
every candidate, ordinary/recall pass and final refresh. Recheck instance and
configuration before dispatch and publication. Keep the formatted-template
digest privately with the final counted request; a template change with identical
token count still refuses before generation. A measurement is a snapshot, not a memory-allocation reservation.

Validate bounded, strict protocol JSON and expected call/channel identities.
Reject reasoning fragments, malformed/duplicate terminals, unexpected closure,
length exhaustion and unsupported terminal reasons. Input overflow prevention
and generated-length exhaustion are separate: stop-at-limit refuses input
sliding/truncation, while an exhausted reply is not labeled a completed reply.
A bounded prefix buffer recognizes reasoning delimiters across native fragments. `complete` collects the same
native stream. Retain actual per-request cancellation/terminal/connection cleanup;
unconfirmed native cleanup blocks reuse and raises `ProviderCleanupError`, rather
than treating socket closure as proof that prediction stopped. Never cancel a
different request or claim ownership of the external LM Studio process. Existing
unmanaged local-text handoff refusal remains.

The capability probe counted 25 tokens for one public synthetic fully formatted
nonthinking prompt on the loaded 2,048-token instance, without generation/loading.
That probe does not establish production parsing, generation cleanup or arbitrary
template correctness. The implementation needs independent source review and
bounded actual public synthetic integration on its final source.

Published contracts:
[native backend interface](https://github.com/lmstudio-ai/lmstudio-js/blob/4b9af903e866808013524eece1e9ceb7a7f18c68/packages/lms-external-backend-interfaces/src/llmBackendInterface.ts),
[instance-bound model](https://github.com/lmstudio-ai/lmstudio-js/blob/4b9af903e866808013524eece1e9ceb7a7f18c68/packages/lms-client/src/llm/LLM.ts),
[supported low-level protocol](https://github.com/lmstudio-ai/lmstudio-python/blob/16796832c1665985c73f5a9e938cbf17b40adf02/src/lmstudio/sync_api.py).

## Ownership and verification

Provider work owns the new LM Studio module and synthetic protocol tests in an
isolated worktree. Session work owns paging DTO/store/API/client and its tests in
another worktree. Root owns the awaited context builder, history/delivery/memory
integration, launcher/dependency wiring and this contract. Independent review and
all applicable exact-head CI precede integration. Do not edit the frozen live
human-test checkout while implementing.

Tests cover >8 short entries, multi-sentence assistant chunks, operational-event
flooding, paging/byte boundaries, complete revision chains/disclosure floors,
window revisions, namespace/reset/watermark races, delivery indices, selected
memory fallback/revocation, full-template/multilingual token overhead, exact fit
and one-token overflow, oversized current input, unsupported/stale counters,
instance replacement, cancellation and retained cleanup. Native evidence must
separately prove no JIT/model loading, GPU continuity and final production prompt
retention. Physical conversation feel and broader model quality remain human
acceptance work.

## Configuration and compatibility

Install the pinned `lmstudio` extra (included in `requirements-dev.lock`) and select:

```json
{"kind":"lmstudio","base_url":"http://127.0.0.1:1234","model":"already-loaded-alias","max_tokens":128,"reasoning_effort":"none"}
```

This adapter never loads or unloads that external model. The current local GPU
profile keeps its existing full GPU/KV configuration and loaded context. Do not
confuse the loaded context with the checkpoint's advertised maximum. Context
capacity uses strict JSON-safe integers; there is no 32,768-token clamp. Worker
profiles retain their independently validated load limits.

The launcher enables measured history for typed and live Converse. Existing
managed GGUF whole-chat counters can supply the same result contract. A generic
compatible/cloud/Qwen adapter without exact counting refuses with
`ContextBudgetUnavailable`; adding model-specific exact counting remains required
before that adapter can generate through this host path. Direct embedded runtime
construction retains its explicitly named legacy compatibility default. Health
reports `context_history: measured` or `legacy_embedded` so these paths cannot be
mistaken for each other. No client request can opt into a hidden fallback.

Errors distinguish unsupported counting, oversized protected input, stale model/
conversation, and incomplete retrieval. Counters and page guards never write
transcript text to logs. Memory use remains separately owner-selected; ordinary
history budgeting does not enable DAG recall or change retention settings.
