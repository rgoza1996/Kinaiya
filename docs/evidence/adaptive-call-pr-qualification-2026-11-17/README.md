# Gated Call integration: complete Flutter regression

The implementation is `fb5e41b2905ce46310c0b7f58300764292edb86c` (tree
`1416bbde803dc804442555ba45b02ccdccae0748`) on
`codex/623-adaptive-call-integration`, based on main
`50272e8a9dcc3786994ce63651126f88fddbc22f`. This record broadens the local
[replacement qualification](../adaptive-call-replacement-2026-11-17/README.md)
before publishing the actual gated host/Call integration. It does not enable
Adaptive Live or complete #623.

| Check | Result | Receipt |
| --- | --- | --- |
| `flutter test --no-pub --reporter expanded` | 1,556 passed; six skipped | [Command and source pins](logs/issue-623-integration-full-frontend-receipt.json) |
| `flutter analyze --no-pub` | No issues | [Command and source pins](logs/issue-623-integration-full-analyze-receipt.json) |

All 338 frontend input hashes match both receipts and the implementation.
The [manifest](manifest.json) retains their original log and receipt digests.
The lossless `.log.json` format recovers the raw output by UTF-8 encoding `text`.
No application source or dependency changed during either check.

Six controls require native endpoint environment variables absent from the
unconfigured full Flutter command: three in `adaptive_live_call_native_test.dart`,
one in `live_stream_native_live_test.dart`, one in `capture_modes_live_test.dart`
and one in `core_subsystems_live_test.dart`. The four live-stream controls are
separately exercised by twelve actual native/Dart scenarios inside each passing
450-case Python 3.13/3.14 composition. Capture Mode and Core Subsystems live
controls were not rerun here; their existing unit/fixture controls passed in the
full suite. A skipped endpoint control is not a physical/native qualification.

The local release web build, Wasm compiler dry run, browser checks, schema
export, lint and focused results remain in the linked qualification record.
Complete owner/process replacement, remaining dependency-loss/no-audio and
supported-adapter qualification, physical echo prevention, representative timing
comparisons and default migration/rollout remain open. Model quality remains
diagnostic; architectural correctness determines acceptance.
