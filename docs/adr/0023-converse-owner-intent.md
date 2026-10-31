# ADR 0023: Durable Converse owner intent across resource restoration

Status: experimental implementation for [#9](https://github.com/rgoza1996/Kinaiya/issues/9),
Roadmap 14.3–14.4 and AT14. Extends [ADR 0013](0013-resource-handoff.md)
and [ADR 0022](0022-managed-laya-resources.md). Full M6 and physical voice
acceptance remain open.

## Problem and authority

On main `1a1c162`, an owner could receive `200 {"enabled":false}` from
`POST /v1/converse/enabled` while resource restoration was starting a worker.
The restoring controller subsequently set the same flag to true. Owner choice
and temporary resource availability shared one mutable value. The reproduced
failure is retained in [#9](https://github.com/rgoza1996/Kinaiya/issues/9#issuecomment-6457640027).

The owner alone selects whether Converse may operate. Resource readiness may
further restrict that choice; automatic task completion cannot enlarge it.
The effective gate is `owner_enabled && resource_enabled`.

## Durable choice and independent availability

Both native control paths use `converse-intent.json` in the same owner data
directory. Its bounded v1 record contains a strict boolean `enabled` and a
monotonic integer `revision`. Resource actions and Converse owner requests
serialize only the small atomic file update, using a nonblocking Linux file
lock, private regular files, directory-relative operations, fsync and replace.
Reads reload the current record. Unsafe, absent or corrupt records fail closed;
they are not reset to an enabled default during operation. This is local owner
configuration, separate from the shared conversational journal.

The native Converse API authenticates the actor independently of its body:

- The owner credential persists a new choice before any delivery/journal await.
- The resources credential changes only temporary resource availability.
- Other service credentials are refused. Extra/coerced control fields are refused.

Health and successful control responses retain `enabled` as effective availability
and add `owner_enabled`, `owner_intent_revision` and `resource_enabled`.
An owner-on response can truthfully return `enabled:false` while resources remain
paused; it records the request without claiming workers are already ready.
A failed persistence/delivery acknowledgment returns 503 and requires inspection.

The launcher supplies the persistent store to Converse, including standalone
mode. A managed Converse starts with its resource gate closed. The controller
opens it only after verified worker readiness. On a Converse-only restart,
reconciliation repeats that readiness handshake without changing owner intent.
A resource-controller restart still preserves uncertain-lease inspection rules.

The existing durable resource policy's `desired_converse:false` seeds an absent
intent file during migration. A choice held only in an old process's RAM cannot
be recovered after that process has exited; preserve that choice through the
old durable resource owner control before replacing an old running installation.
The new record survives both service restarts. It is outside Agent's workspace,
and Converse control source is now protected from Agent workspace access.

## Restoration and reconciliation

The resource controller keeps its existing serialized worker/action state and
idempotency ledger. It rereads owner intent around blocking startup boundaries
and before/after reopening Converse. An off received during startup causes owned
workers to be stopped/reaped and the transition to settle off. Owner HTTP
acknowledgment does not wait for the controller's restoration lock. A loader
already in progress can take time to return; disabling inference is immediate
at the owner gate, while worker release remains pending until independently
confirmed.

The controller observes native owner changes on its local maintenance loop
(target interval 250 ms, subject to control I/O and current worker operations).
It handles each explicit revision once, preserving a newer on request arriving
during handoff. Failed owner-on restoration is not repeatedly retried without a
new explicit request. A stale resource-action replay returns its existing receipt
and cannot rewrite a newer owner choice. Handoff rechecks current provider
locality, including after Converse reconfiguration; an unmanaged local text
worker still refuses handoff.

[ADR 0078](0078-conditional-converse-owner-intent.md) specifies an additive
expected-owner-revision precondition for a new resource converse action,
separate from replay protection and resource readiness.

Resource status projects current owner intent even while a worker action holds
the controller lock. `policy.owner_intent_revision` is independent of the worker
`revision`; Flutter checks both and rejects retreating owner revisions. This
lets it stop capture/playback on an owner-off update before worker progress
advances. It never rearms the microphone automatically on restoration.

## Speech and late inference

An owner-off cancels known producers and invalidates playback generations.
Direct turns and prepared window replies also retain their admission's owner
revision and resource availability generation. They recheck those boundaries
around asynchronous journal/provider work. An off/on cycle cannot revive a
previously admitted response or authorize an old speech chunk. Existing task
outcome and session/epoch checks continue to apply.

These are server/client control checks. They do not prove that physical speakers
or microphones have stopped, and cannot recall data already transmitted to a
provider. There is no provider switch or new cloud fallback.

## Verification and limits

The [native process evidence](../../evaluation/evidence/converse-owner-intent-2026-10-27.json)
records an actual independently launched Converse HTTP process, a real owned
40 MiB memory-worker fixture, an acknowledged owner-off during held restoration,
verified group release, later explicit on and process cleanup. It uses no model
or voice inference. Its first fixture correctly refused handoff because it
advertised an unmanaged local text provider; the corrected fixture uses explicitly
nonlocal synthetic provider metadata without invoking that endpoint.

Deterministic coverage includes owner/service authentication, strict bodies,
standalone persistence, legacy migration, both service restarts, Converse-only
restart, controller/Converse outages, manual/task-finished/owner-on restoration
races, a newer on during handoff, failed-restoration retry limits, stale speech,
prepared replies, suspended admission, file/lock attacks and Flutter status.

```bash
PYTHONPATH=. pytest -q tests/unit/test_converse_intent.py \
  tests/integration/test_converse_owner_controls.py \
  tests/integration/test_converse_owner_processes.py \
  tests/integration/test_resource_handoff.py \
  tests/integration/test_speech_windows.py tests/integration/test_playback_recovery.py
cd apps/kinaiya_flutter
flutter test test/resource_state_test.dart
```

The on-disk mechanism targets the existing experimental Linux host. The data
directory and OS owner are trusted; this does not resist an administrator
rewriting policy. The native process fixture does not measure GPU contention,
real speech, audible latency or full Converse→Laya→Agent delegation. Those
broader gates remain open alongside M6 scheduling/device-loss work.
