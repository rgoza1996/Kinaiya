# Quiet Call cleanup after live Decisions loss (#623)

This increment fixes a gated Adaptive Live Call remaining retained after native
capture permission and resources have closed, when no fresh PCM arrives. The
release gate remains false; full #623 acceptance and rollout remain open.

Baseline: `05f73fae87664e2f9ccbcb0888b3a1bd68bd2672` on
`codex/623-adaptive-call-integration`. The [manifest](manifest.json) retains exact
commands, final source hashes, runtime identities and first-failure evidence.
All 923 Python and 338 frontend input hashes match the final receipts. All 599
exported schemas remain unchanged ([export record](schemas.json)).

| Check | Result | Receipt |
| --- | --- | --- |
| Composed host/capture/consumer/contract/actual Dart integration, 29 pytest targets | 452 passed on Python 3.13.18 | [Command and pins](logs/issue-623-dependency-final-313-receipt.json) |
| Same composed scope | 452 passed on Python 3.14.10 | [Command and pins](logs/issue-623-dependency-final-314-receipt.json) |
| Full `flutter test --no-pub --reporter expanded` | 1,556 passed; six skipped | [Command and pins](logs/issue-623-dependency-full-frontend-receipt.json) |
| Seven browser audio entrypoints | 237 passed | [Command and pins](logs/issue-623-dependency-browser-final-receipt.json) |
| Full `flutter analyze --no-pub` | No issues | [Command and pins](logs/issue-623-dependency-analyzer-final-receipt.json) |
| Ruff check | Passed | [Command and pins](logs/issue-623-dependency-ruff-final-receipt.json) |
| Ruff format check | 1,220 files already formatted | [Command and pins](logs/issue-623-dependency-format-final-receipt.json) |
| Release web build, existing cache, `--no-pub --no-web-resources-cdn` | Passed; Wasm compiler dry run passed | [Command and pins](logs/issue-623-dependency-web-build-receipt.json) |

Both Python runs cover the same cases, including fourteen actual native/Dart
scenarios. The full Flutter command skips six endpoint-dependent controls
because their native environment variables are absent; four live-stream controls
are exercised separately by that native/Dart composition. Capture Mode and Core
Subsystems live controls were not rerun in the unconfigured Flutter command.
Browser counts include registrations from imported fixture modules. The Wasm
result is a compiler dry run, not a Wasm build/runtime test. Existing Starlette
deprecation and CupertinoIcons font warnings remain visible in the logs.

The original passive observer now reads the current decoder's exact native
capture permission after each bounded scoped transcript drain. The enrolled
metadata channel sends no PCM or begin/finish operation. A fence need not append
a transcript entry, so unchanged bounded wake returns still lead to permission
inspection while the microphone is quiet. Closed, missing or unavailable
permission initiates local stop and original native cancellation/readback;
actual HTTP and cleanup lifetimes remain retained through public deadlines.
This observation adds no inference pacing or model retry.

Two actual native/Dart controls use fresh loopback HTTP, SQLite, synthetic
Whistle IPC and the original `RequiredLiveDecisions` monitor:

- Play one synthetic backchannel, stop the real live Decisions worker, then
  supply no further PCM or bridge event.
- Arm a native capture, stop that worker, and supply no PCM at any point.

The authenticated test-only stop route observes the worker's real readiness API
and waits for native cleanup. It reports closed capture permission, completed
native cleanup and preserved legacy availability. The local Call must then stop
its mic/player and release its original FIFO. Another explicit Call must observe
unavailable capability without another acquisition or a mode fallback. The idle
case starts no STT PCM, Decisions inference, TTS or Converse reply. The original
held-speaker regression separately preserves retention until its own Future
settles.

The [first failure](https://github.com/rgoza1996/Kinaiya/issues/623#issuecomment-6947982076)
retains both timed-out cleanup assertions and their native request traces; no
client cancellation occurs until explicit test teardown. After the product
repair, a [fixture synchronization failure](https://github.com/rgoza1996/Kinaiya/issues/623#issuecomment-6948047588)
read an empty history before the real unavailable-capability response returned.
The correction waits for that observable status and preserves every original
stop, no-rearm, no-fallback and status assertion. Native delivery409 refusals
after the fence remain truthful refusals and are neither weakened nor retried.

Twelve run records and six original source snapshots remain preserved. Each
`.log.json` stores exact UTF-8 text and its raw byte SHA-256; encoding `text`
recovers the original bytes. The manifest separately records archive and
unchanged receipt digests. Only eight runs qualify final source; earlier runs
remain first-failure/repair evidence.

These controls qualify software ownership and native permission observation.
They do not qualify a killed/replaced service process, every dependency or
transport fault, managed TTS workers, physical audio, room/device echo
cancellation or provider latency. No dependencies were installed, SDKs upgraded,
live/paid models called, held native CI retried or installed owners promoted.
Model quality remains diagnostic; architecture/harness correctness determines
acceptance.
