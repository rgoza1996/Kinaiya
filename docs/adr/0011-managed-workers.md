# ADR 0011: Owner-controlled local worker lifecycle

Status: experimental implementation for GitHub #9 / Roadmap 14.2-14.4, pending
lead review, policy/coordinator wiring, and live voice handoff.

## Context

Roadmap 14.2-14.4 requires resource policies, safe handoff (unload Converse,
admit Agent work, verify release, restore profile), and no deadlock on
approvals. This ADR covers only the lowest layer: spawning, tracking, and
reaping local worker subprocesses defined solely by trusted owner
configuration. Policy, coordination, wiring, and live voice handoff stay with
the lead and are explicitly out of scope here.

## Decision

New module `packages/resources/processes.py` (stdlib only, no import-time side
effects) with lazy re-exports in `packages/resources/__init__.py`:

- `WorkerSpec`: frozen dataclass (`id`, `argv`, `cwd`, `env`, timeouts).
  Validates ids, nonempty argv with absolute existing executable argv[0],
  absolute existing `cwd`, bounds on argv/env counts and sizes, timeout ranges,
  and rejects NUL bytes and malformed env names. `argv`/`cwd`/`env` come only
  from owner configuration at construction; no API accepts them at runtime,
  model/HTTP tool arguments never reach spawning, and no shell is ever used.
- `WorkerManager(specs, *, readiness=None)`: at most 16 unique workers. One
  asyncio lock serializes `start`/`stop`/`restart`/`close`; `status()` is
  synchronous and never awaits a probe or hung worker. Children spawn via
  `asyncio.create_subprocess_exec(start_new_session=True)` with a minimal
  explicit `PATH`/`LANG` plus provided `env` (parent credentials never
  inherited; owners may opt cache paths into `env`). Stdout/stderr drain in the
  background with 64 KiB tails per stream so flood output neither blocks the
  child nor exhausts memory.
- `readiness(spec, process)` is lead-supplied and verifies actual ready state.
  Without it, a live worker reports `running`/`unverified` with `ready=false`;
  a loaded model is never pretended. Startup probing, early exit, and probe
  failure are bounded by `startup_timeout_s`; failures clean up the owned
  process group and report `early-exit`, `startup-timeout`, or
  `readiness-failed`.
- `stop` sends TERM to the owned process group, waits `stop_timeout_s`, then
  KILLs surviving owned members (including children outliving an exited
  leader), reaps the leader, drains pipes, and verifies no live owned group
  member remains before reporting `stopped`. Anything else is `stop-incomplete`
  with residual pids — never a false stopped state. `restart` always creates a
  new owned process. `close` stops every owned worker even if one cleanup
  raises, then aggregates failures into one `RuntimeError`.
- Ownership is guarded by Linux `/proc` start-time identity so a reused PID is
  never signalled; unknown ids are refused; arbitrary PIDs are never accepted.
- `status()` exposes `id/state/ready/owned_pid`, observed RSS bytes or null,
  bounded error category/log tails, transition timing, and measured stop
  evidence (RSS before, TERM/KILL/reap outcome) — never command or environment
  values. Process exit proves its mappings went away, not exact system-wide
  freed memory. `gpu` reports `unknown/unverified`: no GPU exists on this host
  and the module claims no GPU scheduling or allocator release.

## Verification

`tests/integration/test_resource_processes.py` (33 tests, green in ~8 s with
the root `.venv` Python/pytest and `PYTHONPATH=.`; Ruff clean) spawns real
synthetic Python workers only: 40 MiB allocation with measured RSS/group RSS,
file-based readiness, stop/restart with new PID and RSS disappearance,
TERM-ignoring descendant killed via KILL escalation, sentinel process
surviving `close`, startup/probe/cancellation cleanup with empty groups,
serialized concurrent start/stop, bounded flood tails, secret-free status, and
partial-close attempting all workers. Timing is bounded (each test well under
10 s); a session fixture closes every manager.

## Limits (honest)

- Zombies read 0 RSS but that is not proof a descendant was reaped; the module
  reports live-member scans and says so instead of over-claiming.
- `/proc` measurement is Linux-only; elsewhere RSS fields return null, not zero.
- Admission policy using these facts, coordinator wiring, Converse handoff,
  and any voice/UX behavior remain lead-owned and unverified here. This is not
  complete M6, not a real GPU/conversation handoff, and makes no API budget or
  model pricing changes.
