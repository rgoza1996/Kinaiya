# ADR 0075: Complete Agent observations and measured complete requests

Status: accepted contract; implementation and qualification in progress (#179).

## Requirement and baseline

Roadmap §8/M1 and AGENTS.md require usable task context and truthful evidence.
The frozen, consumed waterlog trial at `5797221` failed after 18 successful
provider responses without an edit or command. Native read returned the complete
2,814-byte test file, but two seed clipping passes delivered only 1,995 file
characters. An eight-message suffix also discarded earlier observations. This
proves native data loss; it does not establish the sole cause of repeated reads
or prove that repairing it qualifies M1.

## Shared interfaces for implementation

1. `packages.providers.completion_budget.PreparedComplete` owns one native
   request-local generation callback and its exact `PromptTokenCount`.
   `provider.prepare_complete(messages)` counts the **complete** request,
   including `stream=False`, tools/schema, reasoning and output reservation,
   without generating. It returns this object. No provider-wide Agent ticket is
   shared between concurrent tasks. Configuration/client/headers/tool snapshot
   and exact request identity are checked again before actual disclosure.
   `complete()` consumes once before awaiting; overflow, failed cleanup, stale
   identity or cancellation never permit replay. `discard()` releases a local
   callback only; preparing acquires no live stream requiring later cleanup.
2. Existing Converse `measure_prompt`/stream admission remains a separate
   interface. The shared count record permits a reservation below the verified
   context capacity; adapter-specific local limits remain enforced by their
   existing adapters. Agent's explicit Meta output reservation may be 8192.
   No model-name guess supplies a context limit. An unsupported/unconfigured
   complete-count capability fails explicitly before inference. Scripted
   non-inference fixtures alone return `fixture=True` and no token measurement;
   tests using this path make no provider capacity claim.
3. `services.agent.history_budget.select_history(base, exchanges, prepare,
   check, *, max_measurements=256)` selects chronological whole exchanges.
   `base` contains the complete trusted instructions/current objective/revision,
   source refs and grants. Each exchange is a nonempty list of complete
   role/content messages, including a logical action and its result where
   applicable. Native publication receipts intentionally replace the large raw
   publication action. Native steering notices may stand alone; current
   constraints also remain in the protected objective. The newest exchange is
   protected. The helper first prepares all exchanges, then drops one whole
   oldest exchange per count until a suffix fits. It never sums estimated token
   costs, assumes tokenizer monotonicity, clips text/JSON, or splits a pair.
   Protected overflow and exhausted measurement resources are explicit errors.
   Whole native UTF-8 input is bounded to 16 MiB and 512 exchanges as resource
   limits, not alleged model context capacity.
4. Selection returns `HistorySelection` with `prepared`, complete `messages`,
   `kept_exchanges`, `dropped_exchanges`, `measurement_attempts`. `check()` is an
   async native fence run before and after every prepare await. The adapter
   additionally checks the synchronous Agent export guard immediately before
   the count HTTP request and generation HTTP request. Recheck after all native
   awaits and before action dispatch. Cancellation/steering/reset, stale sources,
   task/provider/harness changes or metadata revocation cannot execute a stale
   response. All catalog dependency guards and sticky disclosure scopes remain
   task-wide even when their messages are evicted. Selection changes no native
   approvals, evidence, effect flags, finish checks or journal authority.
5. `read_file` keeps its existing 65,536-byte default and 262,144-byte maximum.
   A successful result is a complete, stable regular UTF-8 file, with a digest
   for those exact complete bytes. A file exceeding the requested or native
   bound is explicitly refused, with bounded native source/version metadata and
   recovery guidance. No prefix can carry a whole-file success receipt. This
   first repair does not introduce paging, implicit decoding replacement,
   automatic shell fallback or a new grant. A recoverable refusal may request a
   larger allowed bound; beyond the native maximum it must remain an honest
   refusal. Native observation text and accepted logical action JSON are kept
   complete in history, separate from concise durable evidence output.

## Native source admission at disclosure

Independent review reproduced revocation during the last journal await before
measurement. Checking cached task scope after that await did not prevent the
count request from exporting a newly revoked source. Adding another sequential
read would only move this race. Token counting is a disclosure, not a harmless
local estimate.

Every prepared Meta count and generation therefore invokes a native admission
after HTTPX request/auth hooks and before forwarding the frozen upload. The
Agent-only `/v1/transcript/agent-export-admission` reads the current session,
source identities/revisions and complete bounded disclosure chains together
under the journal Store lock. The receipt binds the native task/revision,
provider facts, actual uploaded body SHA256, count/generation purpose and fresh
nonce. It contains no prompt or source text. A missing capability, malformed or
mismatched receipt, unverified chain or denied scope refuses the upload. A
one-use native dispatch latch prevents admission reuse; a rejected or uncertain
attempt is not replayed. The local task/provider/tool/catalog guards run again
after admission awaits. Fixture-only generation acquires no external admission.

The atomic native admission is the linearization point for that one upload.
Later revocation can stop subsequent requests and action admission, but cannot
recall an already admitted in-flight disclosure. This is not a distributed
transaction or a new tool/effect permission. Native source scope now also folds
all bounded revisions of one exact speech-window slot, retaining an intermediate
restrictive scope; unrelated slots do not taint or exhaust that chain.

Prepared transports are tested against pinned HTTPX 0.28.1. They bind method,
URL, raw headers, immutable upload stream, body, extensions and selected
transport. Trusted hooks still run; this mechanism does not sandbox them.
Response-close ownership starts before response hooks. Prepared generation
accepts only identity encoding and at most 8 MiB of raw response bytes; this is
a native transport resource limit, not a model context limit. Cleanup failure
remains terminal even when steering arrives concurrently.

## Verification and compatibility

Source-bound tests must cover native file-to-actual-request tail delivery above
2,000 characters; JSON and multibyte UTF-8; exact bounds, changed files and
non-regular/symlink paths; whole-pair eviction and protected overflow; actual
count/generation equality with both Meta action formats and 8192 output tokens;
concurrent independent admissions; single-use/failure/cancellation; unsupported
counting; resource policy changes; and authority/disclosure changes at each new
await. Existing Converse and local-adapter gates remain required.

Previously unmeasured inference adapters are explicitly unavailable for the
measured Agent loop until they implement this optional complete-count contract.
This is a visible compatibility limitation, never a guessed tokenizer fallback.
No retry, adapted fixture or benchmark success is implied. Any subsequent real
repository trial needs a fresh declaration/case and retained failure evidence;
all applicable exact-head CI and independent review precede merge.
