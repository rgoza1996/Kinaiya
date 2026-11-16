# Completion-driven worker evidence (#623)

This increment implements bounded durable discovery and the actual native live
inference scheduler. It leaves Adaptive Live off. Host/Call integration, decoder
rollover, Converse/Agent consumers, backchannel microphone/echo/mute ownership,
representative timing comparisons and default rollout remain required for #623.

Parent: `aa4fe3bf9a2d94ad4c4e89ef2c5ae21cdb8dca5d` (PR #666). The
[worker contract](../../live-decisions-worker.md), [verification.json](verification.json)
and [Python input pins](python-inputs.json) retain exact commands, environment,
source/log hashes, results and preserved first controls.

Current composed qualification passes **194 cases on Python 3.13.18 in 42.91s**
and the same **194 on Python 3.14.10 in 42.87s**. All 886 declared Python inputs
remain unchanged during each run. Two additive discovery schemas have independent
pin `39d6e3c27ba4e888aa8a4d08e887cf4a832c532c18bcf3fc8e69b781242727bf`;
all 566 previous schema bytes and the original 184-schema aggregate are unchanged.

Actual SQLite/ASGI/JournalClient and request-bound provider controls establish:

- Latest-input coalescing after actual inference completion, one actual model
  lease/call at a time, unchanged-raw exclusion and one retained original HTTP
  journal wait across completion. Stale results stay in the original ledger.
- Bounded passive discovery across pages and restart; no RAM cursor/candidate
  eviction acknowledges native input. A large first-session backlog cannot
  exclude another native session from the queue and next session turn.
- Exact completed-result recovery after lost publication ACKs before and after
  commit: one original model call, unchanged charge/completion, the same proof
  bytes and exactly one native journal effect. Positive lookup avoids another
  publication call; unknown lookup can reconcile only that exact native proof.
- Recovery rechecks current model selection, capture and every originally
  disclosed context floor. Each changed-authority control preserves the completed
  result/charge and produces no new model call or publication attempt.
- Cancellation retains the actual lease and sole close. Failed physical close
  retains the original slot/lease even against fresh input and reports false
  cleanup settlement. Positive metadata lookup does not refund that resource.
- Actual settled HTTP transport errors use only the native frozen allowance,
  closing each original before the next attempt. Malformed/version/label output
  is retained and terminally refused after one call, without a model reroll.
- Metadata/model/wake outages retain queued work and stop ownership, bound retry
  churn, and grant no inference. Native roles, request bounds and schema roots
  remain explicit.

The shared context selector and hypothesis function were extracted for reuse.
The [independent AST comparison](independent-source-audit.json) proves that the
hypothesis is unchanged and the selector body changes only explicit journal/error
injection. Rolling Windows passes its original `JournalUnavailable` factory.
The broader existing finalized/window/provider/disclosure/wake/native-permission/
passive-owner regression passes **1,008 cases in 300.83s**, with one existing
Starlette deprecation warning. Its 886 Python inputs match the focused matrix
and final source. This includes existing Rolling Windows tests against the shared
context selector.

The original failures are preserved as first source/control artifacts, not
relabeled as passing qualification:

- Interface integration: 2 failures/19 passes in 14.83s. The new caller supplied
  unsupported classify keywords. It now uses the existing task/history interface;
  no prompt, budget or assertion was changed to conceal the mismatch.
- Invalid wire retry: 3 failures in 1.69s. ValueError/validation output failures
  were treated as retryable and spent two calls on unchanged input. An explicit
  post-response protocol refusal now preserves raw output/charge after one call.
- Failed discovery continuation: 1 failure in 1.47s. An old `has_more` hint caused
  immediate retries before the scheduled stop owner ran. The hint is cleared on
  metadata failure; the original one-call/yield assertion remains.
- Model metadata outage: 1 failure in 1.46s. A stamp read outside the guarded step
  followed queue removal, killing the loop and losing that RAM entry. The stamp
  is now acquired before removal inside the guard; exact queued work remains.
- Session fairness: 1 failure in 0.91s. A full first-session queue stopped native
  discovery before the second session. Bounded continued paging, RAM balancing
  and session turns preserve native history and the original capacity assertions.
- Wake outage: 1 failure in 0.46s. An immediately failed wait was recreated five
  times across five bounded scans. The original retry-clock control now observes
  one failed wait while native scans/stop handling continue.

Ruff lint and the 878-file formatting check pass. No model quality, physical audio,
GPU, acoustic latency, deployment, installed-owner, dependency or host-policy
qualification is claimed. These counts overlap the existing native protocol tests
and broader regression; they are not additive coverage totals.
