# Internal Agent advice operations (#566 / #567)

`AgentAdviceOperations` owns measured execution and cleanup for the optional
Agent consumer. With an explicit host policy and both roles selected, the
[internal HTTP API](decisions-agent-advice-api.md) exposes it to the native
[Agent worker](decisions-agent-advice-client.md).

## Original request and input ownership

The closed version-1 `AgentAdviceCall` binds a controller instance, logical request
key, policy SHA-256, original native task identity, current objective revision,
complete declared task source revisions, retained context references, disclosure
scope and role-bearing text messages. The input has a declared 4 MiB canonical
UTF-8 bound and 2,048-message bound. Messages and policy instructions are forwarded
in full without semantic rewriting or silent truncation. An oversized or malformed
request refuses before enrollment. The authenticated Agent bridge constructs
the complete input from the current ordinary worker and checks its binding
again before dispatch and before returning advice.

Enrollment detaches mutable caller inputs and reserves the original logical key
before task creation. An exact repeated submission or passive lookup observes the
same original; another digest under that key or another controller instance
conflicts. Defaults retain at most eight active originals and 64 total originals;
terminal entries are not evicted to fund replay. Constructor limits may be lower,
and never exceed 128. Unknown task registration, provider construction or cleanup
retains active capacity and cannot authorize another attempt.

The original task starts behind an enrollment gate in a fresh context. Its task
identity, loop and coroutine must match before the gate opens. Inherited execution
tools, grants and primary-model callbacks do not cross that boundary. A trusted
Resources actual-operation parent, when present, separately retains the original.

## Measured execution and disclosure

Only the enrolled original captures the independent Agent policy and exact Core
catalog recipe and credential reference. Inspection remains metadata-only. The
actual fresh adapter uses the Agent resource group and does not require Converse's
wake state. Catalog rotation cannot retarget an already captured operation; a
later unavailable declaration refuses without selecting another model.

The same original task prepares and consumes one measured `PreparedComplete`.
Before each actual count or generation upload, its callback requests the
[native Session admission](decisions-agent-admission.md) for that exact body hash,
purpose, nonce, task/source set, provider identity and instruction policy. The
complete proof and original request must match. A native source/revision or
disclosure refusal prevents the next upload and does not fund a reroll. This
increment exercises the actual LM Studio protocol and Session ASGI boundary with
synthetic native wire responses, not a live or paid model.

## Observation, cancellation and cleanup

An original owns its root task, proof HTTP exchanges and adapter's sole close
through its own actual-operation hub. Its parent Resources owner remains held
until that original's cleanup is confirmed, independently of unrelated parent
work. Cancelling or timing out an observer cannot cancel or replace the original.
Cancelling the original records a request; it does not prove the work stopped.

The proof client's strict JSON, encoding, body-length, size and exact-echo checks
remain in force. It retains the actual raw read and sole response close after
observer cancellation. Root-task completion or a confirmed adapter close alone
cannot settle an operation while its proof response close remains pending.
Pending cleanup reports `closing`/`pending`; failed cleanup reports
`cleanup_unconfirmed`/`unconfirmed`, retains capacity and fails the enclosing
Resources owner. A bounded controller close observes these originals without
forgetting them or claiming retirement.

Returned model text is retained exactly, including empty text, whitespace, NUL
and text claiming permissions or success. Its hash, byte count, actual model pin
and effective disclosure scope accompany the observation. The output has a
declared 4 MiB bound; excess output or invalid text is an explicit resource or
protocol failure. No semantic/style filter or model-quality gate is introduced.
Every observation identifies output as `untrusted_model_data` and task status as
`not_assessed`. Later native task changes do not rewrite historical raw output.

## Remaining integration

Standing-task authority, the Options editor, and full
consumer/provider/resource/external-agent qualification remain part of #566/#567.

Supporting failures #692–#695 and their unchanged first-run evidence are described
in the [operation verification record](evidence/agent-advice-operations-2026-11-17/README.md).
