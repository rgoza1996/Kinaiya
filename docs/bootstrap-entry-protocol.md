# Trusted bootstrap entry before native identity verification

Tracks [#577](https://github.com/rgoza1996/Kinaiya/issues/577) and
[#620](https://github.com/rgoza1996/Kinaiya/issues/620); diagnostic preservation
follow-up [#645](https://github.com/rgoza1996/Kinaiya/issues/645).
Legacy timing preservation: [#646](https://github.com/rgoza1996/Kinaiya/issues/646).
This is a startup-protocol change, separate from the historical diagnostic-only
slice in [command observations](worker-bootstrap-command-observations.md).

## Why exec-pipe EOF is insufficient

CPython3.12.16's [subprocess parent](https://raw.githubusercontent.com/python/cpython/v3.12.16/Lib/subprocess.py)
waits for the exec-error pipe to reach EOF. Linux7.0
[begin_new_exec](https://raw.githubusercontent.com/torvalds/linux/v7.0/fs/exec.c)
closes close-on-exec descriptors before returning to the ELF loader, whose
[create_elf_tables](https://raw.githubusercontent.com/torvalds/linux/v7.0/fs/binfmt_elf.c)
later initializes argument/environment bounds. The
[procfs cmdline reader](https://raw.githubusercontent.com/torvalds/linux/v7.0/fs/proc/base.c)
explicitly returns zero bytes before environment initialization.

The source-derived inference is that exec-pipe EOF alone cannot establish that
the trusted Python bootstrap has entered userland. Immediately verifying cmdline
can encounter that initialization interval. This is a possible mechanism for
the recorded empty-read failures, not proof that every historical generic/media
refusal has the same cause. Coarse `pre_command_mismatch` alone remains
insufficient to establish an empty read.

## Private entry framing v1

Both native parents append the exact trailing option
`--kinaiya-bootstrap-entry-v1` to the existing trusted helper invocation. The
generic helper retains its original grant/config/nonce arguments. The media
helper retains those arguments plus control/source and, when selected, its
existing cgroup enrollment option. Unsupported argument forms refuse execution.

After its argument checks, the trusted helper writes exactly
`00 6b696e616979612d626f6f7473747261702d656e7472792d7631 00` to its existing
stdout pipe, then waits on the original grant. Media also completes its required
cgroup enrollment and grant-descriptor validation first. It does not read the
configuration or source body or run configured code to send this prefix.

The native parent consumes exactly this bounded prefix before its existing
identity verifier runs. It does not poll procfs, repeat a failed observation,
sleep to guess initialization, or dispatch another process. The prefix is a
scheduling boundary, not process identity, execution permission, a source grant,
native settlement, or application/model readiness. All original command,
parent/group/session/start-time and pidfd checks still precede the sole grant.
Their predicates and short-circuit order are unchanged.

The prefix uses the already owned stdout pipe; no descriptor or public service
endpoint is added. It is consumed before stdout draining starts, so configured
worker output is preserved. Existing media stderr draining, read-only source,
FD3/FD4 remap, grants, cgroup ownership and cleanup remain required. Legacy
direct helper invocations remain accepted for low-level compatibility; native
parents require v1 and never fall back when the prefix is absent or malformed.

## Budgets, refusal and cancellation

Generic entry waiting consumes the configured WorkerSpec startup budget, and
application readiness retains the original start timestamp. Scoped media entry
uses its existing absolute cgroup startup deadline. Legacy unscoped preparation
retains its original absence of an implicit startup timer; its original task,
descriptor, source and fence ownership still bound the operation. No configured
budget is widened and no new timer or model-quality threshold is introduced.

EOF, malformed/partial framing or deadline exhaustion refuses before identity
verification and grant. Generic refusal closes the original grant once and
retains the actual Process for its existing unverified reap/pipe cleanup; cached
exit diagnostics sample that same Process before close and after the original
reap attempt. A warning sink cannot change refusal or create another waiter.
Cancellation during generic entry uses the existing cancelled-spawn settlement
with already-closed inherited descriptors omitted from its retained list.

Media entry is awaited by the retained original start task, using its existing
descriptor-aware readiness callback. Cancellation, fence or failure retains the
original start/process/source/pipe owners for the existing cleanup. A fence is
rechecked after entry before identity work; the later original grant checks
remain authoritative. An observer cancellation or timeout never proves physical
cleanup or releases another original's capacity.

## Local qualification and remaining limits

Two frozen actual-parent tests gate first-party fixture copies before bootstrap
entry. Both fail on baseline7299f8b9 because the verifier runs before the gate is
released; both pass with entry framing, including unchanged configured stdout
and exactly one configured execution. These controls prove phase ordering, not
reproduction of a particular historical kernel interleaving.

The first broader run reports 420 passed/four failures: early-exit fixtures
expose missing cached refusal-exit warnings. Issue #645 records that regression
before repair; the corrected diagnostic/phase subset passes ten cases with the
existing warning/cleanup oracles unchanged. New boundary controls first report
ten passes/two observer-fixture failures: the worker tests awaited cleanup while
deliberately withholding the gate needed for that cleanup. Observing grant close
before releasing that gate preserves the strict refusal/ownership assertions;
all twelve cases then pass.

The expanded run reports 590 passes/one failure: an unchanged cgroup oracle
exposes an unintended timer on legacy None/unscoped preparation. Issue #646
records it before removing that new timer. The corrected control explicitly
preserves the unscoped original until entry or fence. The final expanded run
passes all 591 cases on Python 3.13.18, and 39 targeted entry, diagnostic and
cgroup-custody cases pass on Python 3.12.3. Ruff checks and formatting pass.
The final decorator formatting change preserves the Python AST exactly.

An offline wheel build includes the new protocol module and both helpers;
365 hashed RECORD members verify. A disposable installed-wheel worker launched
from `/tmp` with isolated Python imports executes once, preserves configured
stdout including an embedded entry prefix, and positively reaps its original.
No dependency, native guard, host policy or model selection changes accompany
this protocol. Commands, hashes and first/final outcomes are retained in the
[evidence capsule](evidence/bootstrap-entry-2026-11-16/verification.json).

After integrating main32d5fb7a (test/documentation-only changes), the combined
entry, custody and selection-capture controls pass 248 cases on each explicit
Python 3.12.3 and 3.13.18 interpreter, with one existing Starlette warning each.
All production and frozen entry-test source hashes remain identical.

Exact-head hosted CI and applicable native target qualification remain required.
These controls establish the new phase boundary; they do not retrospectively
prove a cause for every #577/#620 failure or qualify physical cgroup isolation,
GPU/model behavior or the complete resource-handoff matrix.
