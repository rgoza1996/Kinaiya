# Registered media children and spool grants

Related: [complete media protocol](media-protocol.md),
[private bootstrap](media-bootstrap.md), [track planning](media-decoder-plan.md),
[issue #204](https://github.com/rgoza1996/Kinaiya/issues/204).

`packages/resources/media_processes.py` implements a private native process
owner. It connects the pre-grant media bootstrap to the sealed upload spool.
It adds no public route, source authorization, decoder readiness or completion
authority. Actual decoder, sandbox/runtime qualification, native publication
owners and media consumers still require integration.

`MediaLaunchBinding` has exactly seven fields: `operation_id`, `child_instance`,
`source`, `source_id`, `consumer`, `fence`, and `binding_sha256`. Registration
captures independent validated snapshots. A matching replay returns the same
registered `MediaLaunch`; changed reuse of its child identity conflicts.
References and digests do not themselves establish permission.

`MediaWorkerPool(fixed_spec, limits, *, current, retire_dependencies)` requires
two owner-configured callbacks. `current(binding)` is synchronous, read-only,
non-reentrant validation of actual native admission, lease, permission and
selected profile. It must return an actual boolean. The asynchronous
`retire_dependencies(binding, child_or_none)` must positively join the original
inner/sandbox, descendant and publication owners. Production cannot substitute
a caller assertion or a successful mock callback for those dependencies.

The public methods of this private owner are:

- `register(binding)`: reserve the original synchronously before scheduling
  effects. Allow one active or unresolved child per operation and the selected
  global child limit. Retain replay history after positive settlement.
- `start(launch, grant)`: dispatch that exact original at most once. Replays
  observe its retained spawn/result; cancelling an observer does not fence or
  cancel the original. A returned child proves only its outer grant.
- `fence(launch)`: immediately reject new effects and control I/O.
- `settle(launch)` and `close()`: observe retained cleanup for at most five
  seconds. Explicit settlement fences; pool close fences every original.
  Timeout or caller cancellation retains original work for later observation.

`MediaChild.channel` exposes only asynchronous `send_chunk(bytes)` and
`recv_chunk(max_bytes)`. Each chunk/receive bound is 1–65536 bytes, with one
original sender and receiver each. Fence immediately rejects new I/O and
requests cancellation; retained cleanup joins the exact operations and callbacks
before sole socket close. Old queued callbacks must not affect
a later owner of the same descriptor number. Channel EOF is not source EOF,
decoder completion or physical cleanup.

The private spool port is `SpoolUpload._open_child_grant(launch)`. It accepts
only the actual registered launch, exact source, current binding and allowed
phase. Under the spool mutex, sealed/current/capacity checks precede atomic
grant binding and reader registration; registration precedes descriptor open.
Wrong-source, foreign or already-bound refusal cannot poison a healthy upload.
The grant shares the existing sixteen active/unresolved spool-reader slots.
It exposes no public descriptor-transfer API; ordinary `SpoolReader` remains
unchanged.

The spool alone closes the source descriptor. Closing its parent copy after
spawn leaves the child membership charged. Only positive settlement of that
same pool original and its dependencies permits retiring the child use and
finishing pending deletion. An ambiguous close discards its descriptor number
before the syscall, retains the claim and never recloses a reused number.

The pool retains the actual spawn future, PID incarnation/pidfd, grant writer,
control I/O, drains and reaper. Pre-grant verification owns acquired handles
before validation. Currentness and the synchronous fence are rechecked directly
before writing the grant and positively closing its writer. Unsupported native
ownership primitives fail closed. A spawn dispatched without a returned process
facade stays unknown; absence of the facade cannot establish no child.

Positive local settlement requires all original local owners and the native
dependency gate. Outer reap, an empty process group, a response label or a
timeout alone cannot settle it. Uncertain owners retain capacity. Registration
and descendant-adoption attempts reserve metadata under a one-GiB logical
ceiling, without eviction or refund; this quota is not a measured RSS bound.
Generic WorkerManager behavior is preserved through four additive private
entries that only this registered media owner selects.

Errors have fixed messages and closed codes: `bad_packet`, `bound`, `capacity`,
`conflict`, `expired`, or `unavailable`. `MediaLocalSettlement` is immutable
internal `settled`/`unknown` observation, not a public CleanupReceipt, sandbox
qualification, successful decode or authoritative Agent task completion.

Verification uses disposable synthetic CPU children, read-only files and Unix
sockets. It includes original replay, late/cancel-resistant spawn, rejected
pidfd close ambiguity, exact descriptor reuse with a new event-loop reader,
parent-close versus child retirement, shared reader capacity, unknown native
dependencies and generic-worker compatibility. See the retained failed run,
fixture corrections and bounded results in
[the evidence record](../evaluation/evidence/media-processes-2026-11-02.json).
The complete AT64–AT66 and sign-language demonstration remain pending.
