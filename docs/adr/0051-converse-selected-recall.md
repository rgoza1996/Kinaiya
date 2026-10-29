# Optional selected recall in Converse

Contract for #121 and clarification.md §12, following the optional core (#114)
and prompt measurement (#120). This first runtime slice uses owner-selected
existing nodes. Flutter note controls, automatic extraction/ranking, confidence,
soft edges, installed counter compatibility and improved recall remain separate.
The private prototype proposal is design provenance, not an implementation.

## Shared wire and authority

`packages/contracts/memory_context.py` defines versioned strict wire shapes.
`MemoryContextPin` requires explicit schema_version 1.0, implementation_id,
positive use_generation, namespace_revision, closure_sha256 and 1..8 distinct
selector_ids. The outer journal entry supplies session/thread/epoch and existing
runtime_id. The tag contains no copied note bodies. Cap encoded tag JSON at
2048 bytes; safe-integer/type/identifier/digest checks apply before use.

`MemoryRecallRequest` supplies the current Converse binding and 1..8 seed_ids.
`MemoryRecallResult` extends the existing complete MemoryQueryResult with
implementation_id `korina-dag-v1` and closure_sha256. Digest canonical JSON of
the base query result (schema_version, state, ordered nodes): sorted keys,
compact separators, unescaped strict UTF-8, no nonfinite numbers. Validate active
same-namespace nodes, unique identities/ordinals, complete older prerequisites,
depth <=16, enabled/ready state, digest and the original 16 KiB query bound.
The caller additionally checks every requested seed is present.

Session adds POST `/v1/converse-memory/recall` and `/validate`, authorized for
owner or Converse. Authenticate before any body receive, including duplicate
authorization/Origin checks. Reject queries, duplicate/nonfinite JSON, invalid
Unicode, wrong types and oversize inputs with fixed errors/no echoed content.
Body <=16 KiB, recall response <=17 KiB, validate response <=4 KiB; no-store.
The original six owner-only administration routes retain their exact authority.
No owner credential is forwarded. Memory is still installed only by the existing
explicit Session flag; absent/unavailable memory leaves journaling usable.

`MemoryStore.recall(request)` invokes the existing current-namespace complete
query; it does not truncate parents or select new seeds. `validate_context`
accepts MemoryContextValidate(binding,pin), rechecks the exact selected DAG and
digest/revision, and returns MemoryContextValidation(valid,state). Disabled
storage returns valid:false without reconciliation/content retrieval. Other
unavailable/stale/corrupt cases refuse with fixed errors as appropriate.

## Native publication validation

Store reserves payload.memory_context. Before idempotent return, check its exact
model/size plus role=converse, kind=conversation.converse, current Converse
channel, bounded valid runtime_id and explicit local_only/final text. Null,
unknown version and a tag on any other event/role are invalid.

Core provides `register_converse_context_validator(implementation_id, callback)`
for at most four trusted startup callbacks, once per name; no dynamic import or
untrusted registration. The callback takes session_id,thread_id,epoch,pin and
must return True. Unknown/unavailable validators reject tagged writes. Untagged
append neither loads the backend nor invokes a callback. Memory installation
registers `korina-dag-v1` only when its backend is available.

For a genuinely new tagged append, invoke that selected callback under the
existing Store RLock immediately before the entry insert, after ordinary
idempotence/causal checks and before any append DML. The DAG callback requeries
and reconciles the pinned seeds and compares revision/digest while the same lock
is held. Disabled, forgotten, corrected or quarantined context refuses. Preserve
committed invalidation/quarantine on refusal; test nested SQLite boundaries.
An identical previously committed append remains an idempotent receipt, not a
new memory use. Journal read/resolve and retained transcript content are unchanged.
The tag grants no permission or execution-completion authority.

## Pure history projections (Muse-owned interface)

Add `typed_history_projection(recent,source,info,context_turns,*,memory_context=None,
runtime_id=None)` in history.py and `window_context_projection(recent,source,info,
*,memory_context=None,runtime_id=None)` in delivery_context.py. Both return the
shared ContextProjection(messages,consumed_scopes,delivery,consumed_memory).
Keep existing compatibility functions and untagged behavior unchanged.

A tagged assistant is eligible only with a valid exact current pin, matching
runtime_id and outer scope, local provider, and explicit local_only scope.
Absent current pin, disabled/unavailable memory, malformed/unknown tag or any
mismatch omits it. Apply this gate before the existing suffix/row selection;
compute consumed_memory from only actually selected rows. Keep native snapshot,
user revisions, sticky disclosure floors, recent100, typed context_turns and
window eight-row/byte bounds unchanged. Window delivery qualifications must
refer only to retained assistant rows, with correct existing indices/receipts.
Do not backfill from outside the existing bounded snapshot or hide journal data.
The pure helper may live in new memory_history.py; it imports no backend.

## Runtime adapter and owner controls (lead-owned)

Inject a named adapter registry, initially supporting a local scoped Session
adapter. The adapter provides bounded recall and validation behind an explicit
interface; dialogue code receives a normalized snapshot/pin and does not depend
on the DAG store. Test replacing it with a compatible injected adapter. Requests
use only Converse service credentials, finite deadlines and real owned response
close; no hidden network/model/provider fallback. Failed cleanup stays retained.

POST `/v1/memory/state` on Converse accepts MemoryBinding. POST `/v1/memory/use`
accepts MemoryUseConfigure; both are owner-only, manually bounded/authenticated
before body parsing. Controls require expected_runtime_id and expected_generation
CAS and return MemoryUseState. Check the runtime identity before any await or
mutation, including off: generation zero/one can recur after a process restart,
so a generation alone cannot authorize a late control from the old runtime.
Configuration is process-local and starts off at every runtime creation. It is
separate from the persisted DAG enabled/retention controls. Disabled memory does
not delete notes. At most32 configured scopes; evict only disabled settled scopes.
Use a process-wide monotonic JSON-safe generation, never reuse it after eviction;
off remains possible at exhaustion, with future enables refused until restart.

On off, selection/adapter replacement, synchronously invalidate the use token
and fence affected typed/window/speech work before awaits. Use existing local
playback/operation cancellation, not overall Converse-off. Retain real read,
count, provider and in-flight journal owners; pending/unconfirmed work is visible
in MemoryUseState and cannot masquerade as settled. A timeout never proves an
append failed or stopped. Ordinary dialogue remains available once its existing
resource cleanup allows; new memory use refuses while prior ownership is unknown.

Prepare recall only for enabled nonempty selections and a local pinned provider.
No reads/updates/background work while use is off. First build ordinary context
excluding all tagged assistants. For a valid snapshot, project only matching
tagged assistants, add one clearly quoted untrusted-note data block, and count
the exact final complete prompt including personality/history/delivery/current
input and reserved output through measure_prompt. Unsupported/too-large/invalid
recall falls back to the ordinary projection before generation. Never partially
truncate a closure, retry dispatched generation or enlarge context. Propagate
cleanup errors rather than abandoning their real owners.

After count, validate the snapshot and local use/provider generation again
before dispatch. Tag every assistant chunk if fresh recall or any selected
tagged assistant was consumed, including guard substitutions; force local_only.
Typed/window prepared operations retain that immutable pin and selection token.
Local currentness, publication and speech admission enforce it. A changed pin
cannot be reused via transitive assistant history. In-flight native publication
is validated under the optional Session hook; already recorded old entries remain
truthful transcript data and are excluded from later prompts. Retained transcript
replay is not permission for fresh speech after a memory fence.

## Verification and ownership

Root owns contracts/schemas, adapter/control, runtime.py/windows.py/speech/API and
integration tests/docs. Codex owns Session memory.py, memory_api.py, Store registry
and reserved-tag validation plus new tests. Visible Muse Contributor xhigh owns
the pure history files/helper and new focused tests. Separate branches/worktrees;
independent review of original outputs and exact-head CI before merge.

Use synthetic providers/transports with the actual Session core and real runtime
control paths. Cover off/no reads, local-only disclosure, exact prompt budgets,
full dependency omission, typed/window/transitive filtering, forged/stale tags,
forget/source correction, reset/replacement, stale counts, held/failed close and
late append. Preserve original failures; do not claim installed tokenizer support,
improved human recall/latency, semantic erasure of repeated human facts, physical
silence or rollback of already dispatched journal/audio effects.

## First-slice operational limits

Enable the Session namespace and curate notes through the existing owner-only
administration API first. Runtime `use` requires that namespace binding, the
current expected_runtime_id and use_generation, implementation_id and selected node IDs; `state`
requires only the binding. The owner credential terminates at these controls.
The scoped Converse credential serves the local Session recall link. A remote
Session origin leaves this initial adapter unavailable and ordinary journaling
independent. A compatible injected adapter and trusted Session publication
validator can implement the same contracts without changing dialogue logic.

The optional count operation requires an already resident compatible local
provider. This change does not qualify the installed tokenizer or load a model.
The Session adapter has a five-second logical request deadline. Actual response
close can outlast that deadline and remains owned; provider count retains its
existing separate ownership/deadline. A failed or unknown publication remains
pending and inhibits fresh memory use in that scope until it can be settled or
Converse restarts. Restart gives a new runtime identity and starts use off; it
does not establish whether an earlier journal write committed. No automatic
extraction, background update, cloud disclosure or note selection is introduced.

State describes runtime configuration and retained ownership, not a backend or
model readiness probe. A configured adapter can still omit a later recall when
the optional Session backend or native counter is unavailable. Trusted in-process
adapter replacement disables its selected uses and retains at most32 retired
clients for owned shutdown; it cannot import an implementation from a request.
Provider pins include inner provider identity, disclosure metadata and configured
model/context limits, with checks before counting and after snapshot validation.
