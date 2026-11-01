# 0104: Native prompt context observations

Status: native and Flutter source independently reviewed; static checks and offline web build passed. CI and preview qualification pending.

## Requirement and ownership

The Context and history group in `docs/options-delivery-plan.md` needs actual
input/capacity/output-reserve observations and a clear distinction between the
prompt and the retained transcript. ADR0057 owns Converse suffix selection;
ADR0075 owns Agent whole-exchange selection and native export admission. This
increment observes those existing operations. It does not create a compaction
engine, new disclosure grant, transcript-retention policy or data exporter.

Each native runtime owns a process-local ring of at most128 immutable metadata
observations. Restart discards it. The native process instance is explicit;
there is no claim that an empty ring means no work occurred. Each observation
is at most8,000 UTF-8 JSON bytes; invalid or oversized metadata is omitted
without changing inference admission. No prompt, objective text, personality,
source body, tool result, endpoint, credential or audio is retained here.

## Observation boundary

Converse records `prepared` after exact final prompt remeasurement and native
currentness/disclosure checks, immediately before its stream is constructed.
This proves a finalized preparation, not successful dispatch or generation.
Both typed and speech-window paths use the same observation owner. Cached
window preparation does not recount or create another observation on inspection.

Agent records `prepared` after `select_history` returns its actual measured
`PreparedComplete`. Fixture-only, unmeasured preparation creates no token
observation. If the existing `complete_generation` native export-admission
callback then validates an exact matching request digest, a second immutable
`generation_export_admitted` observation shares the operation identity. That
phase proves the native admission boundary only; it does not prove the remote
provider accepted, generated or billed the request. No additional journal,
provider or counting request is introduced by observation.

The strict contract records role, process sequence, observation/operation IDs,
UTC time, Session/thread/epoch, source IDs, optional task/objective revision and
harness, captured model/provider/locality/reasoning and exact native model
settings/catalog pins. Unmanaged embeddings have an explicit unknown native
selection revision. The measurement carries exact request/binding digests,
input tokens, active context capacity, output reservation and the checked
headroom `capacity - input - reservation`. These are observations for that
preparation, not current KV allocation, readiness or billing.

## Prompt selection and transcript

Converse counts whole eligible stored messages, including existing assistant
chunks, within the native pages examined for that preparation. It may stop
paging after a suffix fit; `history_complete:false` therefore explicitly leaves
older unexamined history unknown. Kept+dropped equals examined eligible
messages. Pure projection supplies these counts without another tokenizer call.
The frozen native history watermark is included when available.

Agent reports the actual whole-exchange selection result and measurement
attempts. `history_complete:true` means the task's captured exchange list was
examined, not that Agent loaded the entire Session transcript. Action/result
pairs remain whole; protected instructions/objective and current source remain
under their existing owners. Neither role deletes transcript rows when it
omits them from a prompt. Optional memory fallback remains unchanged.

## Passive owner API

- `GET /v1/converse/options/context`
- `GET /v1/agent/options/context`

Both require owner authentication and return `Cache-Control: no-store`. They
return the newest32 observations, the process instance, native sequence,
retained count, storage mode and measured/legacy-unmeasured/unknown mode.
Strict root/nested DTOs, safe integer counters and the8,000-byte per-observation
limit bound a response below256 KiB. Reads perform no prompt compilation,
count, inference, discovery, adapter construction, load or readiness probe.

Each response row separately marks whether its captured native configuration
matches the currently inspected saved role/harness declaration, is obsolete,
or cannot be established. Catalog inspection is metadata-only. An unrelated
catalog revision does not replace the captured exact generation. The status is
not source freshness, current inference eligibility or a new scope grant.
Unmanaged bindings remain unknown. API absence on an older embedding is
unavailable, not an inferred zero-token result.

## Embedded Options and reserve editing

The Context page is inside the traditional Options workspace, top aligned.
Converse and Agent read independently. Reconnects, view closure and stale
callbacks fence rendering by client identity and connection generation. Reads
use a separate owned transport with at most4 actual requests per facade and16
globally across reconnects, including synchronous factory reservations. The
five-second result deadline and300ms cleanup wait never release an unsettled
real HTTP owner. Response parsing is bounded to256 KiB, depth16 and8,192 nodes;
duplicate keys, unsafe numbers and malformed UTF-8 are rejected.

The page displays captured input/capacity/reserve/headroom and the actual suffix
policy. Its provider-settings action opens the existing embedded Providers page.
Supported `max_tokens`/`context_tokens` edits retain the existing provider catalog
generation/CAS behavior. Saving a profile does not silently retarget an accepted
task or Converse selection: the owner reviews/selects the new generation in
Models or Harness Customization. Historical observations are never a mutable
global reserve slider. Host-only reserves remain native host configuration.

Automatic summary compaction, transcript/audio/diagnostic retention, general
export and reviewed deletion remain separate full-goal work. Existing memory
capture/revise/select/forget semantics (ADR0085) are unchanged.

## Evidence

Implementation checks are static formatting/analysis, contract schema export
and diff inspection only. No new tests, runtime/provider calls, device access,
private runtime data inspection or model qualification were performed for this
increment. CI and preview evidence must identify the source revision separately.
