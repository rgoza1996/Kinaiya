# Session reset/deletion and retained Call cleanup (#623, #686)

This increment returns a structured 409 when Session deletion removes the native
registration required by a retained positive decoder proof. It preserves the
original operation and positive-proof validator. The independent retirement
channel still establishes obsolete-scope closure and actual original cleanup.
Adaptive Live remains disabled; full feature acceptance remains open.

Baseline: `05738eab7d18b193f807baf59c24c9914f69173e` on
`codex/623-adaptive-call-integration`. The [manifest](manifest.json) records
exact commands, 925 Python and 340 frontend input hashes and five changed code
files. [Production equivalence](production-equivalence.json) verifies all 604
schema files and all 185 production frontend inputs remain byte-identical.

| Check | Result | Receipt |
| --- | --- | --- |
| Composed host, capture, consumer, contract and native/Dart integration; 32 targets | 557 passed on Python 3.13.18 | [Command and pins](logs/issue-686-scope-final-313-receipt.json) |
| Same composition | 557 passed on Python 3.14.10 | [Command and pins](logs/issue-686-scope-final-314-receipt.json) |
| Full Flutter, existing cache, `--no-pub` | 1,559 passed; six skipped | [Command and pins](logs/issue-686-full-frontend-receipt.json) |
| Full Flutter analysis | No issues | [Command and pins](logs/issue-686-analyzer-final-receipt.json) |
| Ruff check | Passed | [Command and pins](logs/issue-686-ruff-final-receipt.json) |
| Ruff format check | 1,226 files already formatted | [Command and pins](logs/issue-686-format-final-receipt.json) |

Both Python runs include twenty-one actual native/Dart scenarios. The four new
Call controls reset/delete the real native Session during quiet capture and
while playing one synthetic backchannel. They leave the frontend AppState scope
unchanged and supply no fresh PCM or bridge event. The mic stops; the native
retirement proof alone cannot release the original speaker, FIFO or Call slot.
Those remain retained through the public deadline until that same local speaker
Future settles. No positive replay or automatic rearm occurs.

Native reset retains registration but closes its previous epoch. Deletion removes
the row; positive lookup then refuses without manufacturing old metadata. The
retirement channel can still observe obsolete scope and join actual native
cleanup. The fixture snapshots the synthetic reply before deletion and separately
asserts its durable row is gone afterward. It does not relabel a deleted row as
retained history. Only the exact positive lookup endpoint may return the expected
409; all other live operations retain their original status/count assertions.

[Issue #686](https://github.com/rgoza1996/Kinaiya/issues/686) records the original
native ASGI validation failure before repair. A first guard incorrectly used
attribute access on the serialized native dictionary; that repair regression,
its source, and the corrected run are preserved. Ten run records and six first
source snapshots remain. Each `.log.json` retains exact UTF-8 text and its raw
byte SHA-256; encoding `text` recovers the original bytes. Archive and unchanged
receipt digests are separate. Six runs qualify final source; earlier runs remain
failure or narrower repair evidence.

The previous [237 browser checks](../native-capture-retirement-2026-11-17/logs/issue-685-browser-complete-receipt.json)
and [release web/Wasm compiler dry run](../native-capture-retirement-2026-11-17/logs/issue-685-retirement-web-build-receipt.json)
apply to the unchanged production frontend bytes. They were not rerun or relabeled
as new all-340-input checks; only a frontend test helper changes. Wasm remains a
compiler dry run, not a build/runtime qualification.

The unconfigured full Flutter command skips six endpoint-dependent controls;
four stream controls run separately through the native/Dart composition. Capture
Mode/Core Subsystems live controls were not rerun by that full command. Existing
Starlette deprecation remains recorded. Native HTTP/SQLite/Whistle IPC and
software-audio controls do not qualify killed/replaced service processes,
physical audio/echo cancellation, every adapter or representative provider
timing. No installs, SDK upgrades, model calls, held #624 retry or installed-owner
promotion were performed. Architecture and harness correctness determine
acceptance; model behavior is diagnostic. See the
[native contract](../../live-stream-capture.md) and
[Call integration](../../adaptive-call-integration.md).
