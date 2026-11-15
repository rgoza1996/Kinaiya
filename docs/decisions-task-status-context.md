# Read-only terminal task context (#485)

Status questions can refer to work after it has finished or failed. The window
classifier previously removed terminal tasks from its only task input. Its
editable default preference already distinguished status questions from new
work, but it had no separate native terminal observations to consult.

Window request v4 keeps `tasks` as the existing active target candidates and
adds an optional `task_history` array of at most eight terminal observations.
Each row carries task ID, objective revision, native `succeeded`/`failed`/
`cancelled` state and a permitted description of at most 240 characters. It
contains no evidence body, output, command, error text or semantic-success claim.
The worker selects the last eight eligible terminal records in the existing
Agent status order; this is bounded context, not a complete inventory or a
guarantee of recency across adapter implementations. There is no new endpoint,
service credential, execution permission or model-specific routing rule.

Selection requires the exact source session, thread and epoch. Observations
with more than 32 original task source references are omitted before resolution.
Native source resolution and sticky disclosure floors also apply to a single
terminal task. Forbidden rows, including their IDs and descriptions, are removed
before provider-body encoding or reuse hashing. A missing, deleted or non-user
original source omits its terminal row even for local processing; the scope
resolver's local-only fallback cannot establish provenance for this new field.

The shared authorization-first builder determines both actual provider input
and reuse identity. Included native history source identities/revisions/scopes
join the existing currentness check, including when a retained classification
is reused after publication failure. A source becoming restricted during call
admission refuses before provider entry. The ordinary overflow fold removes
conversation history and retains five active candidates, while retaining the
included terminal observations. An over-bound final request refuses; it is not
silently rebuilt without that task context.

Compatible text providers receive the JSON field directly. Laya/Jev, native d1
and the shared SystemOne projection validate terminal row types, bounds and
states, and render them in a separate read-only block. Their typed target
question is constructed exclusively from active `tasks`. Supplying terminal
history cannot create an additional typed target. The native dispatcher also
continues rejecting steer/interrupt against a terminal target if an untrusted
text model invents one.

The fixed contract explains this read-only boundary. Owner interpretation
preferences, confidence policies and model selection remain separate and
unchanged. Code does not force a status question to Ignore or a work request to
Queue; a valid model selection retains both independent axes and the existing
source/enrollment/permission checks. The task-status read is an existing Agent
adapter operation, not a new pagination or transport-ownership contract.

## Verification and remaining gate

`test_decisions_task_status_context.py` uses the actual Store, window worker,
provider transports and dispatcher with explicitly controlled Agent states and
model outputs. It checks active/succeeded/failed status, thanks, explicit new
work, correction and cancellation across both axes; terminal target rejection;
cloud privacy and hidden-data digest invariance; scope/bounds/overflow; fresh
and reused-call revocation; and malformed/native typed-choice behavior.

The initial three succeeded/failed/cancelled projection oracles all failed on
unchanged main, with no `task_history`. Original logs are retained. The first
expanded typed-adapter fixture supplied an already-consumed HTTP response; it
was corrected to a raw ByteStream without changing the production reader.
An additional candidate oracle found unresolved terminal provenance reaching a
local model through that fallback. Its first failure is retained, and the new
projection now omits that unverifiable row.

These controlled checks establish input and dispatch behavior, not model
accuracy. The [predeclared native d1 matrix](evidence/decisions-task-status-native-2026-11-15/README.md)
retains one accepted matching result and eleven below-floor refusals. Actual
routing quality, exact integrated-head CI and final acceptance review remain
required for #485. The old private rehearsal and separate Agent outcome-durability
discrepancy are not rewritten or claimed reconciled by this change.
