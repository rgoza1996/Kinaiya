# Converse task observations and editable instructions

This change supplies the framework data requested by [#486](https://github.com/rgoza1996/Kinaiya/issues/486)
and the editable default preference in [#559](https://github.com/rgoza1996/Kinaiya/issues/559),
as a prerequisite for another [#483](https://github.com/rgoza1996/Kinaiya/issues/483)
native dialogue qualification. It does not establish that LFM3 follows the
preference or that generated task-status answers are universally truthful.
Model behavior remains separate from native authority, as requested in #557.

## Observation contract

`POST /v1/agent/task-context` accepts only the Converse service token. Owner,
Decisions and Agent tokens cannot use this additional capability. It grants no
task-list, task-detail, submission, steering, cancellation or tool capability.
The constructor-owned client accepts only a literal HTTP `127.0.0.1` endpoint
with an explicit port. Environment proxies cannot receive its service token.

The strict `TaskContextRequest` identifies an authenticated, current, finalized
Converse user event or latest speech window by event ID, sequence, revision,
session, thread and epoch. An optional distinct list of 1–16 task IDs restricts
refreshes to previously consumed observations. Caller-supplied task IDs do not
grant access to another session, thread or epoch.

The strict `converse_task_context.v1` response contains:

- The exact source binding, backend-instance identity and UTC observation time.
- At most 16 related runtime task observations, newest submission first.
- Task identity/revision, observed runtime state, submission confirmation, pending steer,
  completion-publication pending flag and current disclosure floor.
- Counts of current-revision native evidence by the nine existing evidence
  kinds, plus remaining required-work count.
- Whether a matching, settled native task-program execution receipt was
  observed, was not observed, or could not be established.
- Explicit `semantic_verification: "not_assessed"` and
  `observation_basis: "native_runtime_snapshot"` on every row.

No task objective, summary, error, transcript, evidence output, file path,
artifact contents, provider credential or tool arguments enter this response.
The read does not mutate tasks, append journal rows, run tools, call an Agent
model, wait for Agent completion, repair history or retry work.

Runtime state and persisted publication remain different facts. In particular,
`completion_publication_pending: false` does **not** prove that an outcome was
published, and an `artifact` evidence count does **not** attest a particular UI
publication or external effect. A file write or consultation is partial work;
a pending steer does not establish that a new objective revision was applied;
a program receipt proves only its recorded execution. It does not prove that
signed content was correctly translated or that the objective was fulfilled.
The existing independently validated `outcome_notice` path retains its own
authenticated evidence and success checks. This API never replaces that gate.

The request cap is 8,192 bytes; the response cap is 65,536 bytes. Duplicate JSON
keys, non-finite numbers, unknown fields, bool-as-integer values and invalid
scope/identity combinations are refused. Counters use the exact JSON integer
range through 9,007,199,254,740,991. A snapshot refuses rather than partially
folding more than 4,096 applied, pending-steer and context dependencies or scanning a runtime
pool above 65,536 records. Native snapshot work is bounded to two seconds; the
client facade is bounded to 2.5 seconds and retains HTTP cleanup ownership.

Errors are bounded, constant descriptions: 401 invalid credentials, 403 wrong
service, 413 oversized body, 422 invalid request, 409 stale source, and 503
unavailable native observations. The client does not infer absence from an
error. Restarted Agent instances cannot silently replace the original frame.
This is an observation interface; there is no new durable store or migration.

## Dialogue and disclosure

Typed, retained-window and buffered native-audio turns prepare one immutable
observation frame. Permitted observations are serialized as framework data in
the **first system message**, after the owner-selected personality. Hidden rows
and their count/identities are omitted. An empty visible list is explicitly a
bounded observation, not a complete task inventory or evidence of Agent absence.
Unavailable lookup is explicit and ordinary Converse remains independently usable.

The actual augmented prompt is measured on text paths. Native audio retains its
complete qualified history and original PCM; it invents no text-token count and
does not fall back to text generation or TTS. No keyword router, confidence
threshold, forced model answer or mandatory waiting silence is added.

Consumed task scopes participate in the complete request's disclosure floor.
The original client, provider and backend/task identities are retained. A
bounded refresh checks task disclosure before each measurement and inference
entry. If those observations become unavailable or forbidden during preparation,
that prepared request is refused; its metadata is not silently dropped and
re-inferred. These are observations admitted at checked boundaries, not a claim
of atomic cross-service revocation after an external request has begun.

Pending steering sources participate in those fresh reads even before their
IDs join an applied task revision (#597). The snapshot captures the original
pending selection, its source IDs and scope; replacement during awaited reads
refuses the snapshot. A metadata read never applies the steer or mutates the
task's retained scope merely to compute its current disclosure floor.

Before reply publication, task scopes are refreshed again. Later restriction,
missing observations or backend replacement can only narrow the publication
floor; failed reads never restore a public floor for already consumed data.
Replies that consumed task observations are additionally retained as
`local_only`: this runtime snapshot has no durable history qualifier capable of
rechecking its independent task sources after restart or later scope edits.
That conservative floor prevents the derived reply from escaping through a
subsequent cloud history request or cloud TTS. Fresh public task observations
may still enter a separately permitted inference request. Local providers keep
the complete qualified history. Full cloud reuse/speech of task-derived replies
requires a durable, current provenance path and remains open under #486.
Existing source, provider, playback, memory, permission and outcome checks remain
in force. An assistant paraphrase does not create or alter an Agent receipt.

## Owner configuration

The built-in persona now prefers a brief acknowledgment and awaiting Agent's
substantive answer for delegated questions/commands. It still permits listening,
corrections and unrelated conversation while Agent works. Actual acceptance,
refusal, failure and completion remain independent native facts.

An optional root `converse_prompt` in the host JSON configuration replaces the
entire personality text. It is independent of the pinned voice/setup document:

```json
{
  "converse_prompt": {
    "contract_version": 1,
    "text": "Speak in the character I choose. For delegated work, acknowledge briefly and await Agent's substantive answer. Keep listening and converse naturally about other topics while it works.",
    "content_scope": "public_synthetic"
  }
}
```

Omission or `null` selects the built-in persona. Explicit `text: ""` is an empty
override, not a request to restore the default. Custom text defaults to
`local_only` unless the owner explicitly supplies another valid scope. Text is
bounded to 65,536 UTF-8 bytes and cannot contain NUL. Unknown fields, invalid
version/types/scopes and oversized text fail before provider construction, with
an error that does not echo the instructions. This setting is read on service
startup; no running owner configuration is changed by the implementation.
Native observation framing and authority checks remain framework-owned even
when the owner chooses a different conversational preference.

## Verification mapping and limits

| Requirement | Implementation | Verification |
| --- | --- | --- |
| Authenticated read-only task observations, no private task text | `services/agent/task_context.py`, strict `packages/contracts/task_context.py` | `tests/integration/test_converse_task_context.py`: real Agent/SQLite, ASGI and genuine socket HTTP, auth/control denial, no writes/model work, source/revision/epoch fences |
| Typed/window/native prompt framing and history | `services/converse/task_context.py`, runtime, windows, context budget | Same integration module: actual client, first system message, measured prompt equality, full fitting history, original PCM and no text fallback |
| Privacy changes, unavailable data, original backend/client | Task frame disclosure/publication refreshes and conservative local retention floor | Same integration module: private-row omission, restriction during count/generation, restart and offline cases, environment-proxy isolation, later task-derived history reuse |
| Editable handoff default without forced model behavior | `converse_prompts.py`, `services/converse/prompts.py`, launcher/runtime | `test_converse_prompt_config.py`: blank/custom/default scope, malformed configuration, actual launcher wiring; integration's five illustrative delegated/follow-up/long-running/failure/unrelated cases on typed and window paths |
| Preserve native success and execution boundaries | Existing `outcome_notice` and task-program authority | `test_handoff_receipts.py`, `test_task_program.py`, `test_task_program_runner.py`; integration verifies regex-evading dialogue cannot manufacture an outcome |
| Additive wire compatibility | Five explicit schema exports | `test_task_context_schema_exports.py` pins these five separately; legacy 184-schema bytes and the seven #371 exports retain their independent pins |

Controlled provider outputs and status fixtures establish deterministic
framework behavior only. They do not demonstrate actual model understanding,
audio intelligibility, browser playback, device delivery, routing accuracy,
semantic translation quality or general qualification of the local engine.
The earlier negative #483 outputs remain in
[the evidence bundle](evidence/native-converse-2026-11-14/README.md).
