# Native Agent advice export admission (#566 / #567)

The next request-execution prerequisite is a distinct authenticated Session
boundary for Agent-facing Decisions input. It qualifies metadata immediately
before an actual prompt-count or generation upload. It performs no inference,
stores no prompt or receipt, and does not enable the optional advice consumer.
The configuration inspector still reports `request_admission: unavailable`.

## Contract and ownership

`POST /v1/transcript/agent-advice-export-admission` requires the **Decisions**
service credential. Its closed `AgentAdviceExportRequest` binds the original
canonical `AgentTaskIdentity`, objective revision, all current native task source
revisions, retained context references, task/request and policy-instruction
disclosure scopes, provider identity, policy digest, logical advice operation,
actual uploaded-body digest, upload purpose and per-upload nonce. The request
has a 65,536-byte resource bound. Duplicate/ambiguous JSON, coerced counters,
unknown fields, unsupported content encoding and invalid versions refuse.

Only a current **running** task can qualify. Session verifies the original native
submit winner, applied objective/steer history, the complete task source set,
current source revisions and retained causal disclosure closure. The existing
Agent export admission supplies its sticky source/privacy floor and standing-task
guard; no source is omitted to make disclosure pass. Instructions require their
own disclosure scope, combined with the native task and retained input scopes.

The policy digest, instruction scope and provider identity are declarations from
the trusted Decisions caller, bound into the exact response. Session does not
observe the Decisions host configuration or measure the uploaded prompt. The
internal [Decisions operation owner](decisions-agent-operations.md) now captures
its actual policy/provider and installs this callback at each measured
count/generation dispatch.
An authenticated Agent request and current Agent runtime/task guards remain
required before that consumer can be enabled.

Task, history, sources and response watermark come from one owned SQLite read
snapshot under the original Store lock. A different WAL connection can commit
during the read; the coherent snapshot may precede that overlapping change.
The next admission observes the new state. An existing caller transaction is
refused and left untouched. The function rolls back only its own read snapshot;
it never commits journal or settings changes.

An accepted native steering intent fences further uploads for its current
objective revision until the worker applies or definitively rejects it. The
same read snapshot checks the protected intent and the matching Agent journal
events. Application admits the new revision and its complete sources. A
rejection can restore the original revision only with the same intent event,
digest, actor and revision. A missing or mismatched resolution keeps the fence
in place. The Agent operating mode retains its existing policy for already
accepted work.

## Result and failure semantics

`AgentAdviceExportAdmission` echoes the exact original request and its canonical
SHA-256, native running state and task scope, plus the existing complete export
proof. The response is bounded at 196,608 bytes and served with `no-store`.
`JournalClient.agent_advice_export_admission` checks media/encoding/length limits,
strict native fields, complete body/source/scope binding and the original request.
The local adapter enforces the same Decisions-only role boundary. The original
`agent-export-admission` API remains Agent-only.

Malformed input returns 422, unsupported media/encoding 415, excess input 413,
an ingress deadline 408, changed task/revision/source membership 409, refused
disclosure 403, and missing/uncertain native authority 503. Error responses contain
constant diagnostics. Authentication retains the native 401/403 behavior.

This is a point-in-time native observation, not a reusable export permit, an
execution permission, model-output assessment or task-success claim. Repeating
this read grants no model retry. The internal owner supplies original enrollment,
exact recovery, measured execution and retained cleanup. Authenticated Agent and
service integration, protected policy persistence/Options and the full service
and external-agent matrix remain unfinished. Standing-task advice needs its own standing
authority integration; ordinary-task metadata cannot bypass that boundary.

Qualification preserves the first unsupported-fixture failure (#690) and the
two-owner mixed-snapshot regression (#691). Tests cover native SQLite, current
task/source lifecycle, privacy floors, role separation and strict ASGI/client
boundaries; they do not assess model answer quality.
