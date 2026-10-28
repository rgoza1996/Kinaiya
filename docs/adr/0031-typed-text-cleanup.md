# ADR 0031: Retain bounded typed Converse provider cleanup

Status: implementation for [#76](https://github.com/rgoza1996/Kinaiya/issues/76),
following the opt-in window stream ownership in
[#75](https://github.com/rgoza1996/Kinaiya/issues/75). Integration and independent
review evidence must be recorded before issue closure. This does not complete
M2 or change any running provider or default configuration.

A typed turn previously cancelled its producer and gathered it without a
deadline. A real HTTP response may already be closing inside `__anext__` at
natural EOF or provider failure. Cancelling that close, then invoking an
already-exhausted generator's `aclose`, cannot establish transport release.
The single `_active[session]` cancellation index also allowed a new turn to
replace the only reference to an old provider operation.

The runtime now retains typed operations in a separate registry. Each session
has at most one retained typed owner. Owners and admission reservations share
32 session slots. An operation remains owned through producer termination and
its independent iterator-close task. A failed or self-cancelled close stays
quarantined until runtime restart; a timeout never releases it. Successful
late cleanup releases only its own identity, so it cannot clear a newer task.
`_active` remains a cancellation index rather than proof of release.

Request identity, current session/thread/epoch, owner/resource admission and
idempotent replay are validated before legitimate old work is cancelled.
Replay does not cancel or invoke inference, and its output does not retain the
admission lock. A new request reserves capacity before any new user append or
provider creation. Same-session replacement fences the old generation and
cancels its producer once, then waits for actual release. Saturation and
unconfirmed cleanup return explicit refusal before the new user entry or
provider side effect. Other sessions may proceed within the same bound.

The admission-lock wait, wait for old provider release and terminal cleanup
wait share at most one second per request. This bounds waiting for ownership;
it is not a deadline for ordinary model inference or native journal I/O.
Interrupt, owner-off, resource pause and response disconnect set the operation
stop token immediately. Cancellation performs no diagnostic journal I/O. An
independent session monitor also observes external owner changes and native
session resets while a provider has not emitted its first delta. Availability,
generation and stop tokens fence publication before and after asynchronous
journal work; the native journal rejects stale epoch/thread writes.

The typed producer opts into the existing task-local HTTP close protection
before calling `__anext__`, covering normal EOF and provider error as well as
explicit outer `aclose`. Compatible and Meta request bodies, credentials,
disclosure filtering and ordinary Agent provider behavior remain unchanged.
Cleanup waits for producer termination before calling iterator `aclose`, so
those calls cannot run concurrently. A conforming iterator without `aclose`
remains supported; its producer must terminate before release. This cannot
prove release of resources for which that iterator exposes no close protocol.

The typed SSE response explicitly closes its body iterator even when ASGI send
is cancelled while the generator is paused at `accepted` or `text`. This uses
the same retained cleanup owner and bounded wait. It does not depend on async
generator garbage collection. Service shutdown also requests typed cancellation
and shares one bounded wait across all retained cleanups. If cleanup is still
pending, the process retains ownership for the remainder of its lifetime.

Window and typed admission consult small read-only readiness hooks. The window
hook delegates to the controller's existing release predicate, including its
actual task, HTTP cleanup and delivery-stop barrier state. Window WAIT, stop
and off controls do not wait on admission. A typed reservation prevents a new
window from slipping in while native validation or prior cleanup is pending;
a final check immediately before the window provider call handles a preparation
already in progress. A retained window close likewise blocks a typed provider.
This protects the two Converse entry points for the same session. It is not a
generic provider scheduler, process-wide remote inference lock, remote kernel
preemption or physical audio acknowledgment.

Unauthenticated health exposes only aggregate `text_operations` counts:
`active`, `cleanup_pending`, `cleanup_failed`, `admitting` and fixed `capacity`.
An admission reservation can overlap its session's older retained cleanup;
the capacity counts distinct sessions rather than summing those two counts.
It includes no session IDs, prompts, credentials or provider error messages.
Owner-off and interruptions do not erase these retained diagnostics.

The focused integration tests use public synthetic input and actual
`CompatibleTextProvider`/`MetaResponsesProvider` adapters over HTTPX
`MockTransport`. Explicit transport barriers exercise held input, natural EOF,
provider error, failed and self-cancelled close; repeated controls; paused SSE
send; no-close iterators; late output; replay and invalid scope; global
saturation; reservation and lock contention; and both directions of window
reuse. No model, paid service, frozen reference process or private conversation
is used. These are deterministic ownership checks rather than live provider,
network socket, audio latency or sustained load evidence.
