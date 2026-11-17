# Internal advice service boundary — #566 / #567

This increment continues draft PR #689 from
`5e7559b5e750881ad77103ba286d6a3564c0e975`. The
[HTTP contract](../../decisions-agent-advice-api.md) describes Agent-only request,
exact lookup/cancel, runtime inspection and bounded service-lifecycle ownership.
The launcher does not install this owner; public request admission remains
unavailable. Agent client/runtime integration and both parent issues remain open.

## Final verification

The final post-marker source passes **1,613 tests on Python 3.13.18 and 1,613 on
Python 3.14.10** across 36 files. Each run has one existing Starlette deprecation
warning, no skips and unchanged pins for all 939 Python inputs. Ruff check and
formatting pass (**1,249 files already formatted**); schema export and
`git diff --check` pass. Exact commands, outcomes and source pins accompany the
retained logs and source captures below.

## Preserved failures and repairs

- **#697:** the first four-file union has **one failure, 209 passes**. A lost HTTP
  acknowledgment followed by cancellation of a held measured native prediction
  produces unconfirmed cleanup. An unchanged-control diagnostic records the exact
  native cancel packet reaching the synthetic wire's unsupported-send assertion.
  Production correctly refuses to treat socket closure as native completion.
  Only the fixture is repaired: its terminal is bound to the original model and
  configuration, and controls distinguish held and absent terminal proof. The
  repaired immediate union has **212 passes**; the original cleanup assertion is
  unchanged. No production cancellation check is weakened.
- **#698:** later broad runs each have **one failure, 1,612 passes, one warning**
  on Python 3.13.18 and 3.14.10. The newly added ingress-deadline control lacks its
  `asyncio` import after an earlier style auto-fix. Restoring that test import
  gives **one pass, 53 deselected** on each interpreter, using the actual existing
  five-second ingress deadline. Its 408/no-enrollment assertion stays unchanged.
  The complete repaired union then passes **1,613 tests** on each interpreter.
- **#699:** final compatibility inspection finds the native eager-task-factory
  control accesses an API absent from Python 3.11. An exact source-expression
  probe fails under the existing Python 3.11.15 and resolves under Python 3.12.3.
  A feature marker now skips only that native case where its API is absent, while
  retaining all original assertions and ordinary/unknown Task-factory controls.
  Exact marker probes produce true on 3.11 and false on 3.12. These stdlib probes
  are **not** project tests or dependency-environment qualification. The 3.11 CI
  lane and the held #624 native run remain unchanged. The feature's introduction
  is documented in the [Python API reference](https://docs.python.org/3.12/library/asyncio-task.html#eager-task-factory).

The final post-marker results, exact commands, full Python input pins, first-source
captures, raw-log encodings and hashes are recorded in [verification.json](verification.json).
Each `logs/*.log.json` reconstructs its original UTF-8 bytes and SHA-256. The
receipt's child `exit_code` is authoritative; the pytest orchestration wrapper
exits zero even when its child fails. The original 45 HTTP-control assertion ASTs,
124 operation-control assertions and 74 inventory assertions remain intact.

## Scope and remaining qualification

Coverage includes real Session SQLite/ASGI admission and actual measured LM Studio
protocol with synthetic frames, role denial before enrollment, credential
revalidation after awaited ingress, exact original recovery after a lost HTTP
acknowledgment, no model replay, capacity/retention, malformed/oversized ingress,
deadline/disconnect, full raw output including 4 MiB NUL expansion, controller
identity and confirmed versus pending/unconfirmed shutdown. No live/paid model,
physical audio, model-quality acceptance, SDK/package installation, owner-runtime
promotion or system-policy change is involved.

All **612 inherited schemas** remain byte-identical; two additive service schemas
produce 614 total. The default app's absent advice route and original policy
inspector's unavailable admission are explicitly checked.

Predecessor standard run **44141733548** is not fully qualified. Its iOS job
**134246029345** is cancelled with no runner or executed steps; authoritative
annotations identify hosted macOS capacity pressure. **#700** retains the exact
job/annotation responses. No retry or platform substitution was requested. The
predecessor's six installer jobs passed; individual other standard-job states
remain separate from current-source qualification. The earlier lost-finish-ack
failure #696 remains unresolved despite later passing observations.

The complete project suite and pinned local Python 3.11/3.12, frontend/device and
external-agent qualification were not run for this increment. Authenticated Agent
client/current-task guards, full qualified inputs, remote Resources/cleanup
recovery, standing authority, launcher enablement, protected policy
persistence/Options and the full service/provider/external-agent matrix remain
unfinished. Native `finish_already_accepted` mode semantics must be preserved.
