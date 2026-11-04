# Media cgroup-v2 custody (physical prerequisite, not budgeting)

Related: `packages/resources/media_cgroup.py`, `packages/resources/_media_bootstrap.py`,
`packages/resources/media_processes.py`, [media processes](media-processes.md),
[private bootstrap](media-bootstrap.md).

This component is a real physical-custody prerequisite for held media
bootstrap. It owns an owner-configured dedicated delegated cgroup root,
creates one cryptorandom `kinaiya-media-<32hex>` leaf per actual retained
registered launch, supports atomic SELF enrollment before the sole exec
grant, and observes kernel cumulative descendant CPU snapshots including
exited/`setsid` descendants, freeze/kill events. It retains that original
through existing process/FD/spool settlement.

It is NOT native Segment budgeting, watchdog enforcement, native Session
permission/history/controller, runtime sealing, complete inner/zombie
cleanup, public native cleanup receipts, or demo qualification. It grants
no budget or authority. A future native budget/qualified-runtime owner
must arm before exec; this component grants none of that authority.

## Opt-in dedicated service

Configure a separately opted-in dedicated delegated service root with
`Delegate=cpu` and `DelegateSubgroup=supervisor` for explicit qualification.
The actual current process must already sit
in its immediate `supervisor` subgroup, outside all created decoder
leaves. The pool does not create services, enable controllers, or move the
supervisor. Qualification uses a new disposable service and a unique sibling
slice; it never reuses an existing app, Chromium, or user-manager group.
Production deployment still needs the separate native owner, watchdog and
qualified runtime described below.

Required dedicated unit shape:

```ini
[Service]
Delegate=cpu
DelegateSubgroup=supervisor
```

Pool validation requires Linux `x86_64` + cgroup2 (`fstatfs` magic
`0x63677270`), integer `capacity` 1..2, absolute no-symlink path owned by
the current UID, a real directory FD with stable device/inode/boot
identity (`/proc/sys/kernel/random/boot_id` + `CLOCK_BOOTTIME`), a true
current `/proc/self/cgroup` binding to `<root>/supervisor`, and an
existing `supervisor` child. It rejects an ordinary filesystem and the
mount itself or any existing app/Chromium/user-manager root. No
controller enablement, service creation, or fallback is performed.

## `cpu.stat` vs `cpu.max`, scope vs budget/public cleanup

- `snapshot()` reads the original `cpu.stat` (`usage_usec`) and
  `cgroup.events` (`populated`, `frozen`) via owner-opened FDs with
  bounded ASCII parsing (no duplicates, missing keys, negative or
  unbounded integers). Extra kernel keys are ignored only within bounds.
  Usage is cumulative/nondecreasing and includes all descendants;
  `BOOTTIME`/boot identity must not regress/change. Changed/missing/
  recreated group/root, counter regression, or read/close ambiguity
  raises `MediaCgroupError` (`Invalid native media cgroup operation`)
  and retains unknown with no zero baseline. No `/proc` CPU or child
  counters are used. `cpu.max` is never treated as a cumulative ceiling.
- `request_freeze`/`request_thaw` write the original `cgroup.freeze`;
  return does not prove frozen. Thaw refuses after scope fence/unknown.
  Freeze must be confirmed via `snapshot().frozen` by the caller.
- `fence()` is a synchronous negative local gate with no refund.
- `request_kill()` fences first and writes `1` to the original
  `cgroup.kill`; return is NOT cleanup.
- All kernel mutations use retained attribute FDs plus inode/boot/
  registry guards, never path relookup selecting another group.

## Original cleanup gate and unresolved charges

- `MediaWorkerPool.prepare(launch, grant, *, cgroup_scope=None)` default
  `None` preserves all legacy paths. A non-null exact `MediaCgroupScope`
  must belong to the SAME actual launch; binding occurs under the native
  lock before any spawn. Replay with a different scope conflicts; no
  legacy/cgroup mixing or new spawn occurs.
- Trusted bootstrap accepts the original six argv unchanged or an
  explicit additive eight-argv form ending
  `--kinaiya-cgroup-v1 <enrollmentFD>`. Before the existing exec-grant
  wait it validates the FD as owner-provided write-only cgroup2
  `cgroup.procs` (not ordinary/kill/freeze/source/control), writes
  exactly `b"0"` once to enroll SELF (never a parent PID), and closes it
  before grant wait and before target exec (including refusal paths).
  Only FD3/FD4 survive target exec. No target/config/media read occurs
  before grant.
- After verified bootstrap, prepared acquisition observes POSITIVE actual
  cgroup membership of that exact held PID while pidfd/start/nonce
  remain current. Scoped readiness uses one immutable absolute deadline from
  `WorkerSpec.startup_timeout_s`, established before original dispatch and
  never renewed by polling or replay. The existing five-second settle/close
  observation limits are unchanged. It rechecks the SAME membership
  immediately before first grant under lock/fence ordering. Timeout or
  fault retains the original/unknown with no replacement.
- Fenced/settled scoped launches retain their scope. Cleanup requests
  kill/freeze as needed and positively observes the SAME group's empty
  population/final CPU after setup/release joins and outer wait, BEFORE
  releasing spool/dependency custody. `kill` return, `populated==0`,
  EOF, or callback `True` alone is not cleanup. `close_after_settle`
  is separate and never called recursively from cleanup. A `setsid`
  descendant cannot survive with FD4 while the pool retires the grant;
  unknown scope keeps the grant/slot.
- `close_after_settle(launch)` only succeeds for the SAME launch whose
  pool positively settled, with a fresh `populated==False` group and no
  future setup/release/enrollment. It retains final CPU, unlinks the
  leaf, releases capacity, and returns a frozen `MediaCgroupRetirement`.
  Replay returns the SAME retirement. It releases only this custody
  component, not public cleanup/inner reap/source reservation.
- Existing matching leaves at startup are unresolved charges, never
  adopted/reaped/refunded. Uncertain acquisition retains its charge.
  `pool.close()` closes the root only when every scope is positively
  closed and no orphan/acquisition charge remains; otherwise it retains
  custody and returns `False`.

Each original acquisition is registered against the actual launch before
randomness, directory creation or descriptor opens. A failed acquisition retains
its name and every returned descriptor owner; replay cannot allocate another
leaf. Its reservation also prevents switching the launch to legacy execution.
An unused successful reservation still needs original pool settlement and a
fresh empty observation before separate cgroup retirement. An uncertain
reservation cannot release the original input grant or process slot.

Pool and scope identity resolve through original object registries, including
the creating PID and exact launch binding. Root path components and attribute
descriptor identities are retained and rechecked. Before a sole close attempt,
the owner discards its descriptor number; a failed close keeps the original
charge unknown and never retries a possibly reused integer. Successful
retirement replay returns the same historical retirement even after root close.
The lock order is worker pool then cgroup pool; each scope shares its cgroup
pool's lock, which spans final membership validation and execution-grant write.
No cgroup method acquires the worker lock or calls native currentness callbacks.
Boot, mount and process-membership reads use one pool-owned reusable observation
descriptor owner, enrolled before open and serialized under the same lock. It
can be reused only after positive close. A failed post-open identity check or an
ambiguous close retains that original owner and poisons the pool, including when
construction itself refuses; constructor cleanup cannot unregister it merely
because the separate directory handles closed successfully.

A fence prohibits new borrowing, dispatch, grant and thaw. It cannot revoke a
descriptor already inherited by an in-flight original bootstrap. That original
may finish historical self-enrollment after a fence, then observe grant EOF;
setup/release/outer-process joins precede the final empty observation. Such late
enrollment never permits target execution or a replacement.

Kernel accounting does not include bootstrap CPU consumed before enrollment.
This primitive alone does not prevent an unsandboxed same-UID workload from
altering a delegated tree or migrating tasks, and `populated=0` excludes zombie
reaping. Qualified runtime isolation and the native owner remain responsible for
those boundaries, complete inner cleanup and cumulative budget enforcement.

## Remaining requirements

All existing native Session start/resume/runtime, AT64-66, and demo
requirements remain pending. No enforcement or qualification is claimed
from this source alone. Existing native currentness fixtures remain
disclosed stand-ins, not Session permission proof.

## Recorded verification

On November 3, 2026, the normal media regression selection passed 1,118 tests
with no failures or skips. After correcting compatibility with the kernel's
optional `core_sched.force_idle_usec` field, all 46 opt-in kernel tests in
`test_media_cgroup_custody_live.py` and `test_media_cgroup_observation_live.py`
passed on Linux 7.0.0-42-generic, again with no failures or skips. Source and
selected dependency fingerprints remained unchanged during both runs.

The kernel run used its own transient user service with the delegation and
supervisor layout above. Set `KINAIYA_TEST_CGROUP_ROOT` to that dedicated service
root when invoking those tests from its supervisor. An absent setting skips
these opt-in tests; an invalid supplied setting fails. The completed service
and its unique slice were disposed afterward. Disposable teardown is not
credited as positive candidate cleanup.

The initial kernel run's 25 passes and 21 failures remain recorded separately:
the parser rejected a real namespaced counter, and a replacement-inode fixture
attempted a rename that cgroup v2 refused. The repaired test actually removes
its empty original and recreates the same name with a different inode. All
original refusal, retained-FD/source-charge and replacement checks remain.
The passing selection establishes only this component's exercised physical
custody behavior; it does not establish the pending native or demo requirements.

Initial hosted CI then exposed imports between test directories that the local
runner's extra test paths had masked. Shared helpers now live in one common
test module, preserving the original test bodies and assertions. With only the
repository root on `PYTHONPATH`, collection selected all 12,889 non-live tests;
the 79 affected process tests and all 46 kernel cases passed with no skips.
Collection is not execution of the full suite. The kernel rerun used another
new disposable service and slice, both inactive afterward.
