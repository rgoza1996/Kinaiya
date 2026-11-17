# Decisions consumer policy inspection — #566 / #567

Branch `codex/566-decisions-consumers` starts at
`ea3ecccfd3d39c388dd99ac24850dcdb745660e2`, the draft PR #683 head.
This increment is configuration-only: an optional, independently owned Agent
advisory policy references Core's catalog and is inspected through an owner-only
native endpoint. Every observation reports `request_admission: unavailable`.
The [consumer contract and remaining integration](../../decisions-consumer-policies.md)
define the scope; neither #566 nor #567 is complete.

## Collected verification

- Fourteen focused unit/integration files: **466 passed on Python 3.13.18** and
  **466 passed on Python 3.14.10**. These are the same 466 tests on two interpreters,
  including 51 new policy/API/launcher cases. The exact commands and all 929 Python
  input pins are in each final receipt; the inputs stayed unchanged throughout
  each run and match the final source composition.
- Native SQLite and ASGI controls preserve the original Converse prompt,
  selection, exact original-request lookup and protected receipt rows while
  inspecting absent, configured and invalid optional Agent policies.
- Metadata controls cover exact Core generations, rotation, adapter/protocol and
  reasoning constraints, UTF-8 bounds, canonical digest consistency, owner-only
  access, refusal of writes, and cloud instruction disclosure. Constructor/key
  traps prove inspection does not open adapters or resolve credentials. No model
  request is issued. Exact Wonder Standard `max` remains selectable in metadata;
  models retain their existing adapter-specific effort rules.
- Ruff check passed; Ruff format check reports **1,233 files already formatted**.
  `git diff --check` passed. [Equivalence evidence](equivalence.json) confirms all
  **604 inherited schema files byte-identical**, two explicit additive exports,
  and all **74 original inventory assertion ASTs unchanged**.

## Original failure and repair — #688

The first Python 3.13 run reports **3 failed, 38 passed**. Its credential-import
fixture passed the operation identifier and credential DTO `key` to the same
helper argument, preventing all three Meta disclosure controls from reaching the
native catalog. This was recorded in #688 before correction. The helper parameter
was renamed without relaxing the assertions. The repaired/expanded first union
passed **50 tests**; the later final union includes the added digest-negative
control and all surrounding regressions. These counts describe separate source
compositions and must not be added together.

[Verification manifest](verification.json), `logs/*.log.json`, their exact receipts
and `first-sources/*.txt` preserve the first failure and its original sources.
The log wrapper's `text.encode("utf-8")` reconstructs the raw bytes and matches
its SHA-256. Pytest's recorded child `exit_code` is authoritative; the local
pytest orchestration wrapper itself exits zero even for a failed child.

## Limits

The full project suite, local Python 3.11/3.12, Flutter/build/device tests and live
providers were not run for this Python configuration-only increment. No frontend
or provider adapter implementation changed; the relevant startup, required
Decisions readiness, prompt, selection, setup and shared-catalog regressions are
included above. An existing Starlette TestClient deprecation warning is preserved
in both final logs. No packages, SDKs, models or system settings were installed
or changed.

Agent request execution, task/source-bound upload admission, lifecycle/recovery,
protected per-consumer persistence and Options UI, plus the complete managed
resource/external-agent matrix remain open. The existing Converse dependency and
its readiness checks are unchanged. PR #683 remains independently draft/gated;
this stacked increment does not qualify or promote its full Adaptive Live scope.
