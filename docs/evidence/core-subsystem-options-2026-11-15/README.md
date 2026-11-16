# Core Options verification (#566 / #567)

Runtime source: clean commit
`f8bbd1fc2a653be68b130a384892eb1c90df0f96`, stacked on Core owner draft
`98434af9dae0f12b25bc93938fe87c2199b965b4`. The later evidence/documentation
commit does not change runtime code.

[Check history](check-history.json) preserves first failures and final results:
missing feature imports; missing mock-facade injection; actual narrow-shell
overflow; two missing authority guards; and a fixture's cross-thread SQLite
ownership error. The scroll helper was corrected to settle layout before a
strict tap, with real selection/containment assertions retained. Menu rows also
allow intrinsic height. Existing test files were not rewritten.

The final complete Flutter suite passes **1,495 tests / one skipped**.
The environment-gated live case separately passes **one test in 3.09 seconds**.
Analysis is clean; web release build, Ruff and schema export/diff checks pass.
The build retains its nonfatal existing Cupertino font-family warning and a
successful Wasm dry run; this is not Wasm runtime qualification.

With the pinned Flutter SDK on PATH, run the actual interoperability fixture:

```sh
PYTHONPATH=. python -m pytest -q tests/integration/test_core_subsystem_dart.py
```

It starts the original Session/Core owner on its own server thread and runs
`test/core_subsystems_live_test.dart` against real loopback HTTP. Six mode
changes plus host reset commit seven revisions. Six positive exact replays add
no revision; seven lookups verify Python/Dart request digests. A stale change
receives 409 and its lookup proves a fence. Agent credentials receive 403.
Host config bytes stay exact and the original thread joins. CI runs this
separately in the Flutter job; Python-only jobs may skip it without the SDK.

## Built app and interrupted original

The actual release app used normal Advanced connection to a private temporary
Session/Core with public synthetic credentials and no Agent/model services.
Core remained accessible and saved all six modes, reporting next-launch state
and running services as unobserved. Captures cover 1280×720 and 320×640:

![All subsystems saved](desktop.jpg)

The original temporary server exited **143 / SIGTERM** during host reset.
Signal origin is unproven; no final cleanup receipt exists. The UI retained its
exact original as unconfirmed, without a replacement write.
[First interruption](first-interruption.json) and
[initial source pins](first-started.json) retain that result.

A replacement reused the same protected store, host file, origin and backend.
Generation advanced from one to two. Exact lookup returned **unobserved +
mutation_fenced=true** for the generation-one original. The UI retained its
draft, disabled Save, and required inspected state before a new request. A fresh
generation-two host reset committed revision seven under a different key.
[Native proof and receipts](browser-recovery-proof.json) retain both originals
and their distinct outcomes.

![Host defaults confirmed](host-reset.jpg)

[Replacement receipt](replacement-receipt.json) verifies unchanged source hashes
and host bytes, returned server run, joined watcher, zero model calls and zero
Agent tasks. Both PIDs were observed absent; the replacement exited zero.
Viewport override was reset and the created browser tab closed.

The `.py.txt` helpers retain actual commands/paths, including the interrupted
helper. The replacement records Core requests incrementally and preserves an
owned SIGTERM handler so shutdown can emit its receipt. It does not weaken
native guards or repair the first run's evidence. Raw output remains under
`output/issue-566-core-options/`.

No installed owner service/configuration, model/default, execution permission,
audio device or third-party Agent changed. Full consumer-policy and lifecycle
acceptance remains open under #566/#567.

## Reporter compatibility follow-up (#634)

The first PR #632 workflow at `385d0af503ae6ff966726d6d862ec9afa3d12edc`,
run `43750683316`, Flutter job `132923724847`, passed 1,495 Flutter cases
(one live-only skip) and the preceding companion native interoperability case.
The new Core native Dart subprocess also exited zero and reported one passing
case. Its Python wrapper then failed because GitHub's default reporter prints
`1 test passed.` instead of the expected local `All tests passed!`.

The unchanged wrapper reproduced the same failure locally with `CI=true
GITHUB_ACTIONS=true`: one failure in 3.12 seconds. Selecting the documented
`--reporter expanded` preserves the success-text and exit-code checks and all
native assertions. The actual Dart/Core fixture then passed in that same
environment (one case, 2.86 seconds) and in the normal environment (one case,
2.66 seconds). No product or SDK behavior changed. Full exact-head CI has not
yet completed for this follow-up. Raw first/final logs remain under the local
`output/issue-566-core-options/` evidence directory.

## Isolated SDK fixtures (#637)

The common copy helper and companion case are the same source as PR #638,
`2915b6281d1019b24043aafac92d1768eb3c6f04`. Core's independent input-hash
oracle first passes with the existing warm source cache (one case / 3.00s);
its `--no-pub` invocation does not reproduce the companion's analyzer migration.
The first genuinely fresh copy then fails (one case / 1.36s), because that flag
assumed an already generated package configuration. Normal Flutter preparation
now runs in the copy within the unchanged 90-second process budget. The copy
contains only current declared inputs; its lock and all 407 original Flutter
files must remain unchanged.

Python 3.12.3 passes all four helper cases, the complete 35-case companion
module, and the actual Core case: **40 passed / 35.53s**. Python 3.13.18 passes
the four helper cases and both actual native SDK cases: **6 passed / 16.23s**.
The focused cases overlap the full group and are not added to its total. All
native save/replay/lookup/403/409, host-byte and original-owner/thread-close
assertions remain. One existing Starlette deprecation warning is retained.
See [fixture receipts](fixture-isolation.json) for first/final log hashes and
source pins. No SDK, dependency, runtime or authority change is made.

The earlier exact-head run `43754636981` passed Python 3.11/3.12 (15,208 cases,
10 skipped, 49 live deselected and two subtests each) plus four non-Python jobs.
Python 3.14 failed one standing-authority setup before its target test:
15,207 passed, one failed, 10 skipped, 49 deselected and two subtests.
[#639](https://github.com/rgoza1996/Kinaiya/issues/639) preserves that observation;
its underlying cause remains unproven. Source isolation does not claim to fix
it. The resulting head requires fresh CI and full #566/#567 remain unfinished.
