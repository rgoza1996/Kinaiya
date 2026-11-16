# Native capture permission evidence (#623)

This increment supplies a required native permission guard for the future live
worker. Adaptive Live remains off. It starts no microphone, inference or playback.
The parent is 4c548cbb226cb038fbbbc2cf656848643fd1475f. The
[contract](../../live-capture-permission.md) and [verification.json](verification.json)
record exact bounds, commands, source/log pins and remaining integration.

Current focused qualification passes **170 cases on Python 3.13.18 in 25.52s**
and the same **170 on Python 3.14.10 in 25.74s**. This includes the previous
publication/schema controls, the native lifecycle/HTTP controls and the unchanged
legacy passive-reader oracle. The broader Session/wake/media/standing/finalized
Decisions/window/native-stream regression passes **229 cases in 149.14s**, with
one existing Starlette warning. All 869 declared Python inputs remain unchanged
during each current run. Seven additive schema exports have a separate frozen
pin; all 550 prior schema files retain their bytes and the original 184-file
schema-family count/hash stays unchanged.

Actual SQLite/ASGI/client controls cover original registration and read-only
lookup, cancellation before a delayed registration, exact lost-ACK recovery,
registration/fence races, scope/reset/delete/controller boundaries, protected
metadata corruption, capacity, auth/credential rotation and closed wire bounds.
Late recognized text remains retained after fencing. Fencing revokes logical
permission and makes no process, HTTP, microphone or playback-stop claim.

Two first failures were recorded in #623 before repairs:

- Three transaction controls fail in 0.52s: register, fence and initial registry
  construction refused an already-active SQLite transaction after entering its
  connection context, rolling back the original owner's work. The
  [first control](first-transaction-control.py.txt) retains those assertions.
  The owned transaction helper now refuses before entering another connection
  context. The final initialization control uses explicit startup activation and
  preserves the original uncommitted-name/transaction assertions.
- The earlier broader run reports one failure and 228 passes in 150.93s:
  constructing a passive Store replaced native capture controller identity.
  [The original standing-owner oracle](original-passive-store-control.py.txt)
  detects that write. Its entire source file remains byte-identical to the parent;
  no fixtures or assertions were edited. Controller activation now occurs only at
  explicit trusted Session startup. Passive construction/reads leave it untouched,
  and a replaced controller cannot reclaim itself through repeated activation.

A preliminary check of the passive repair had 35 passes and 19 fixture setup
errors after an unused-import fix removed a fixture alias. Those controls did not
run; the direct native fixture corrects that setup, with its log retained separately.
The original failures are not relabeled as successful qualification.

No model, physical device, provider-default, dependency, installed-owner, host
policy or SDK change is included. Charged v2 inference, exact result publication,
completion scheduling, decoder rollover, consumers, echo/mute, timings and rollout
remain required for full #623. Model quality is not an acceptance gate.
