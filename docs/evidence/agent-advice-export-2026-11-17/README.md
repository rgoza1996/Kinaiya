# Agent advice native export admission — #566 / #567

This increment continues draft PR #689 from `2879743ec409187bc5d2c1ad12e6ad854dc2dabe`.
The [native admission contract](../../decisions-agent-admission.md) defines the
source/task/privacy boundary. It is a prerequisite for optional Agent-facing
Decisions requests; no model request is executed and admission remains disabled
in the policy inspector. Both parent issues remain open.

## Verification collected

The same **912 tests pass on Python 3.13.18 and Python 3.14.10**, across 23 focused
unit/integration files. Exact commands, all 933 Python source pins and unchanged
input observations are retained in each final receipt. Scope includes the new
native SQLite/ASGI/client controls and the surrounding Agent submit, source export,
steering, mode, required Decisions runtime, independent policy, shared catalog,
protected Converse receipt and frozen-schema regressions. Existing Starlette
TestClient deprecation warnings are preserved; there are no test skips in this
focused union.

Ruff check passes; Ruff format check reports **1,239 files already formatted**.
`git diff --check` passes. [Equivalence](equivalence.json) preserves all **606
inherited schemas byte-for-byte**, adds exactly two native admission schemas,
and retains all **74 original inventory assertion ASTs**. No old assertion or
schema digest is relaxed.

## Original failures and correction

- **#690:** the first run has **46 setup errors** because the synthetic fixture
  published unsupported `agent.progress`. Native Session correctly refused that
  writer/kind. The fixture now publishes the existing `agent.state` event. Its
  repaired first union passes **46 tests**. The original oversized test ID/log
  bytes are retained unchanged; later ingress cases use explicit readable IDs.
  Per-field echo negatives also derive from the exact original request, so an
  unrelated helper default cannot satisfy a rejection assertion.
- **#691:** a two-Store WAL control fails **one test, 46 deselected**: a running
  task observation is paired with watermark 4, already containing its peer's
  cancellation, while the coherent running watermark is 3. The correction owns
  one fresh SQLite read snapshot around task/history/source/causal checks and
  the watermark. The next admission observes cancellation and refuses. An
  existing caller transaction stays untouched. The repaired union passes
  **48 tests**; the final 912 union additionally includes proof-field and schema
  checks. These are separate compositions; their counts are not additive.

[Verification manifest](verification.json) identifies eight exact runs and two
first-source captures. The first-source JSON stores the exact tracked diff from
the named baseline, the complete original new Python sources, and their pins.
Each `logs/*.log.json` reconstructs the full original log using UTF-8 encoding
and matches its SHA-256. Child pytest `exit_code` in the receipt is authoritative;
the orchestration wrapper itself exits zero even for a failed pytest child.

## Scope and remaining work

Native checks bind the original accepted task, current running revision, complete
source set, current revisions and retained causal scope. Instruction scope is
independent of the task's consent. The new endpoint is Decisions-only; the
original Agent export endpoint stays Agent-only. Counts/generation body hashes,
policy digest and nonces remain exact metadata bindings. No prompt is stored,
no settings/journal changes are made, and no proof grants task success or future
export permission.

The full project suite and local Python 3.11/3.12, frontend/device and live model
tests were not run for these new Python changes. No frontend/provider adapter,
SDK/package, owner installation, system policy or held #624 native run changed.
Parent #683 at `ea3ecccfd3d39c388dd99ac24850dcdb745660e2` now has all eight standard
CI jobs and six installer jobs passing, with four manual jobs skipped; those
checks qualify that exact parent, not this changed native admission code.

Before enabling advice, implement the authenticated Agent request/operation
owner, native runtime guards, measured provider callback, complete qualified
inputs, uncertain-result recovery and actual cleanup. Protected per-consumer
persistence/Options, standing authority and the full service/resource/external
agent matrix remain required. This evidence does not complete #566/#567/#623.
