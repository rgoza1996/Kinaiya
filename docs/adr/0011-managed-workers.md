# ADR 0011: Owner-controlled local worker lifecycle

Status: experimental implementation for GitHub #9 / Roadmap 14.2-14.4, pending
lead review, policy/coordinator wiring, and live voice handoff. Do not use on
real voice/services yet.

## Context

Roadmap 14.2-14.4 requires resource policies, safe handoff (unload Converse,
admit Agent work, verify release, restore profile), and no deadlock on
approvals. This ADR covers only the lowest layer: spawning, tracking, and
reaping local worker subprocesses defined solely by trusted owner
configuration. Policy, coordination, wiring, and live voice handoff stay with
the lead and are explicitly out of scope here.

Lead review of 8f6bbb7 found the numeric group-signalling boundary unsafe: TERM
was gated on `/proc` start-time identity, but the later KILL loop signalled
numeric pgids/members unconditionally, so a reused PID/group could receive a
foreign signal. This revision removes every numeric signal path.

## Decision

New module `packages/resources/processes.py` (stdlib only, no import-time side
effects) with lazy re-exports in `packages/resources/__init__.py`:

- `WorkerSpec`: frozen dataclass (`id`, `argv`, `cwd`, `env`, timeouts).
  Validates ids, nonempty argv with absolute existing executable argv[0],
  absolute existing `cwd`, bounds on argv/env counts and sizes, a bound on
  total env bytes, timeout ranges, and rejects NUL bytes and malformed env
  names. `argv` stays an immutable tuple and `env` is copied then frozen into
  an immutable mapping, so no caller can modify spawn config through the
  public spec reference. `argv`/`cwd`/`env` come only from owner configuration
  at construction; no API accepts them at runtime, model/HTTP tool arguments
  never reach spawning, and no shell is ever used.
- `WorkerManager(specs, *, readiness=None)`: at most 16 unique workers. One
  asyncio lock serializes `start`/`stop`/`restart`/`close`; `status()` is
  synchronous and never awaits a probe or hung worker. Children spawn via
  `asyncio.create_subprocess_exec(start_new_session=True)` with a minimal
  explicit `PATH`/`LANG` plus provided `env` (parent credentials never
  inherited; owners may opt cache paths into `env`). Stdout/stderr drain in the
  background with 64 KiB tails per stream so flood output neither blocks the
  child nor exhausts memory. Spawn requires Linux pidfd support
  (`os.pidfd_open` + `signal.pidfd_send_signal`); without it `start` fails
  closed with `unsupported-platform` before any child exists.
- Ownership is proved with stable handles, not numbers. The leader pidfd is
  opened at spawn (a held child cannot be recycled before reap). Group members
  are adopted only while the verified leader is alive — immediately after
  spawn and immediately before stop — with kernel start-time/session/group
  rechecks around `pidfd_open` plus an fdinfo bind-back check, so PID reuse
  cannot redirect adoption. After the leader exits, only already-adopted
  handles may be signalled. Numeric `/proc` scans are measurement-only
  diagnostics and are never signalled, by any path.
- `readiness(spec, process)` is lead-supplied and verifies actual ready state.
  Without it, a live worker reports `running`/`unverified` with `ready=false`;
  a loaded model is never pretended. A probe success is rechecked against
  actual process liveness before `ready=true` is declared. Startup probing,
  early exit, and probe failure are bounded by `startup_timeout_s`; failures
  clean up through stable handles and report `early-exit`,
  `startup-timeout`, or `readiness-failed`.
- `stop` captures members while the leader is verifiably alive, sends TERM
  through stable handles only, waits `stop_timeout_s`, then KILLs surviving
  tracked members through their handles (covering children outliving an
  exited leader), reaps the leader, drains pipes, and verifies via handles
  plus a diagnostic scan before reporting `stopped`. Anything else is an
  explicit failure — `stop-incomplete` for known-owned survivors,
  `ownership-unverified` for unattributable occupants or unavailable
  enumeration — never a false stopped state and never unsafe group signalling.
  Handles are released only after verified cleanup (or terminally by `close`).
  A cleanly stopped worker records no owned group, so idempotent `stop` does
  not inspect or signal a formerly owned numeric group. `start`/`restart` of
  a failed or old incarnation first verifies cleanup and refuses with
  `cleanup-incomplete`/`ownership-unverified` instead of overwriting the
  record and orphaning descendants. `restart` always creates a new owned
  process. `close` stops every owned worker even if one cleanup raises, then
  aggregates failures into one `RuntimeError`.
- Cancellation during spawn is shielded: if the child completed, ownership is
  retained and the child is cleaned up boundedly before the cancellation
  propagates; cleanup uncertainty is reported as failure, never success.
- `status()` exposes `id/state/ready/owned_pid`, observed RSS bytes or null,
  bounded error category, transition timing, measured stop evidence (RSS
  before, TERM/KILL/reap/ownership outcome), and bounded log byte counts —
  never command values, environment values, or raw child output. Tails are
  omitted by default (empty strings) because literal scrubbing cannot prove
  output secret-free (secrets can split across streams or truncate at the
  tail boundary). Evidence and transition dictionaries are deep copies so a
  caller cannot mutate recorded facts. A cached `ready=true` is reconciled to
  `ready=false`/`readiness=failed` whenever the real process has exited.
  `gpu` reports `unknown/unverified`: no GPU exists on this host and the
  module claims no GPU scheduling or allocator release.

## Verification

`tests/integration/test_resource_processes.py` (33 tests, green in ~8 s with
the root `.venv` Python/pytest and `PYTHONPATH=.`; Ruff clean) spawns real
synthetic Python workers only: 40 MiB allocation with measured RSS/group RSS,
file-based readiness, stop/restart with new PID and RSS disappearance,
TERM-ignoring descendant killed via stable-handle KILL escalation, sentinel
process surviving `close`, startup/probe/cancellation cleanup with empty
groups, serialized concurrent start/stop, bounded flood tails, secret-free
status, and partial-close attempting all workers (fault now injected at the
pidfd layer). Timing is bounded (each test well under 10 s); a session fixture
closes every manager.

`tests/integration/test_resource_boundaries.py` (10 tests) adds the unsafe
boundaries as regressions with disposable synthetic fixtures (cleaned
explicitly in `finally`): simulated identity loss plus a spoofed stale group
member proves `stop`/`close` never signal a sentinel numerically or through
any handle (fdinfo bind check), refuses with `ownership-unverified`, and
blocks restart until recovery; a real child printing a long synthetic secret
split across stdout/stderr writes proves the secret absent from status;
ready-after-exit reconciles to false/failed; repeated stop is safe with
`/proc` listing disabled; cancellation during spawn retains cleanup ownership
with no strays; inaccessible `/proc` fails closed instead of proving an empty
group; env/evidence mutation cannot change trusted state; missing pidfd
support refuses spawn.

## Limits (honest)

- Cleanup is verified only for observed managed groups: members adopted while
  the verified leader was alive, signalled through stable handles. A worker
  that escapes its group (double-fork plus `setsid`) is not detected or
  contained, and a hostile owner-supplied worker is not proven contained.
- After verified cleanup the numeric pgid may later be reused by a foreign
  group; scans stay diagnostic-only, so such reuse can report
  `ownership-unverified` instead of success. That fail-closed ambiguity is
  intentional and documented, not a proof of foreign membership.
- A dedicated subreaper/cgroup host may be needed for fully generic cleanup;
  this module does not claim one and must not be assumed to provide it.
- Zombies read 0 RSS but that is not proof a descendant was reaped; the module
  reports handle liveness plus scans and says so instead of over-claiming.
- `/proc` identity and pidfds are Linux-only; without pidfd support the module
  refuses to spawn rather than falling back to numeric signalling.
- Admission policy using these facts, coordinator wiring, Converse handoff,
  and any voice/UX behavior remain lead-owned and unverified here. This is not
  complete M6, not a real GPU/conversation handoff, and makes no API budget or
  model pricing changes. No live-resource admission until the lead verifies.
