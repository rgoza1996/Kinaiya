# Native capture retirement and controller replacement (#623, #685)

This increment gives the gated Call a separately versioned retirement protocol.
An exact native fence, changed backend/controller or obsolete scope can confirm
logical permission closure. Actual registration, HTTP, decoder/STT, microphone
and speaker cleanup remain separately owned. The release gate stays false;
complete Adaptive Live qualification and rollout remain open.

Baseline: `17a2350bc41c4bc8def052b2e1a8de5cc33e9222` on
`codex/623-adaptive-call-integration`. The [manifest](manifest.json) retains exact
commands, 925 Python and 340 frontend source hashes, unchanged dependency locks,
SDK identity and 82 web artifact hashes. The [schema record](schemas.json)
confirms all 599 existing files are byte-identical; five additive contracts bring
the export to 604. All 74 original legacy-inventory assertion ASTs and its
184-schema digest remain unchanged.

| Check | Result | Receipt |
| --- | --- | --- |
| Composed host, input, consumer, recovery, contract and actual Dart integration; 32 pytest targets | 551 passed on Python 3.13.18 | [Command and pins](logs/issue-685-retirement-final-313-receipt.json) |
| Same composition | 551 passed on Python 3.14.10 | [Command and pins](logs/issue-685-retirement-final-314-receipt.json) |
| Full `flutter test --no-pub --reporter expanded` | 1,559 passed; six skipped | [Command and pins](logs/issue-685-full-frontend-receipt.json) |
| Full `flutter analyze --no-pub` | No issues | [Command and pins](logs/issue-685-analyzer-first-receipt.json) |
| Seven browser audio entrypoints | 237 passed | [Command and pins](logs/issue-685-browser-complete-receipt.json) |
| Ruff check | Passed | [Command and pins](logs/issue-685-ruff-final-receipt.json) |
| Ruff format check | 1,225 files already formatted | [Command and pins](logs/issue-685-format-final-receipt.json) |
| Release web build using existing cache and `--no-pub --no-web-resources-cdn` | Passed; Wasm compiler dry run passed | [Command and pins](logs/issue-685-retirement-web-build-receipt.json) |

The Python compositions each include seventeen actual native/Dart scenarios
against fresh loopback HTTP, SQLite and synthetic Whistle IPC. New controls
replace the real Session capture controller during quiet input and while an
original local speaker-stop Future is held. The mic stops without fresh PCM or
a local bridge event. Native input cleanup cannot release the speaker, FIFO or
Call slot; the same original speaker Future must settle. No automatic rearm,
positive audio replay or fabricated native fence occurs.

The native pending-registration control separately holds the original journal
Task across controller replacement. Closure can be confirmed while cleanup is
still unconfirmed; releasing that same Task permits retirement without entering
STT. Both old and new protocols retain cancellation-before-begin and held-decoder
controls. The old abort contract still reports unknown for a fenceless controller
replacement; it is not silently reinterpreted.

Two history-pressure controls issue 66 subsequent negative originals using the
normal production history limit. An old persisted registration actually leaves
temporary Converse history and recovers through the authenticated, passive native
identity query. An original with no durable native row retains its sole proof
inside the existing total96 record ceiling. Exhaustion refuses new work instead
of erasing those originals. Recovery queries reject unauthorized roles, malformed
or oversized bodies, and mismatched scopes without ledger mutations.

[Issue #685](https://github.com/rgoza1996/Kinaiya/issues/685) records each defect
before repair. The original positive-registration failure, pending-registration
overconstraint and loss of the only proof under history pressure remain retained.
A fixture compilation failure from a captured nullable variable is also preserved;
the new helper receives its own speaker-stop Completer without weakening existing
behavioral assertions. An initial browser invocation ran 131 cases; the corrected
seven-entrypoint command ran 237. Both exact scopes remain recorded.

Seventeen run records and nineteen first source snapshots remain preserved. Each
`.log.json` stores exact UTF-8 output and its raw byte SHA-256; encoding `text`
recovers the original bytes. Archive and receipt digests are separate. Eight runs
qualify final source; earlier runs remain failure, repair or partial-scope evidence.
The [shared wire fixture provenance](native-fixture-source.json) identifies the
actual native response used by the Python and Dart codec controls.

The unconfigured full Flutter command skips six endpoint-dependent controls.
Four stream controls run separately through the native/Dart composition; Capture
Mode and Core Subsystems live controls were not rerun by that full command.
Existing Starlette deprecation and CupertinoIcons font warnings remain recorded.
Wasm is a compiler dry run, not a Wasm build/runtime qualification.

This qualifies a surviving Converse owner with a replaced Session capture
controller. It does not qualify killed/replaced service processes, every transport
fault or playback adapter, managed TTS workers, physical audio/echo cancellation,
representative provider timing or default rollout. No dependencies were installed,
SDKs upgraded, live/paid models called, held #624 native run retried or installed
owners promoted. Architecture/harness correctness determines acceptance; model
behavior is diagnostic. See the [native contract](../../live-stream-capture.md)
and [Call integration](../../adaptive-call-integration.md).
