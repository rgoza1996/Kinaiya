# Independent Call capture-permission observer (#623)

This increment prevents a held local speaker-stop Future from blocking capture
permission observation after native Decisions loss. The release gate stays
false; complete #623 qualification and rollout remain open.

Baseline: `349cd2e4d9e0a7cbf787f860c9cc1f90b59c8b2f` on
`codex/623-adaptive-call-integration`. The [manifest](manifest.json) retains exact
commands, final source hashes, runtime identities and original evidence. All 923
Python and 338 frontend hashes match final receipts. All 599 exported schemas
are unchanged ([export record](schemas.json)).

| Check | Result | Receipt |
| --- | --- | --- |
| Composed host/capture/consumer/contract/actual Dart integration, 29 pytest targets | 453 passed on Python 3.13.18 | [Command and pins](logs/issue-623-permission-final-313-receipt.json) |
| Same composed scope | 453 passed on Python 3.14.10 | [Command and pins](logs/issue-623-permission-final-314-receipt.json) |
| Full `flutter test --no-pub --reporter expanded` | 1,556 passed; six skipped | [Command and pins](logs/issue-623-permission-full-frontend-receipt.json) |
| Seven browser audio entrypoints | 237 passed | [Command and pins](logs/issue-623-permission-browser-final-receipt.json) |
| Full `flutter analyze --no-pub` | No issues | [Command and pins](logs/issue-623-permission-analyzer-final-receipt.json) |
| Ruff check | Passed | [Command and pins](logs/issue-623-permission-ruff-final-receipt.json) |
| Ruff format check | 1,221 files already formatted | [Command and pins](logs/issue-623-permission-format-final-receipt.json) |
| Release web build, existing cache, `--no-pub --no-web-resources-cdn` | Passed; Wasm compiler dry run passed | [Command and pins](logs/issue-623-permission-web-build-receipt.json) |

Both Python runs cover the same cases, including fifteen actual native/Dart
scenarios. The unconfigured full Flutter command skips six controls requiring
native endpoint environment variables; four live-stream controls run separately
through that native/Dart composition. Capture Mode and Core Subsystems live
controls were not rerun here. Browser counts include registrations from imported
fixture modules. Wasm is a dry run, not a Wasm build/runtime test. Existing
Starlette deprecation and CupertinoIcons font warnings remain in the logs.

The original Call now owns a separate capture-permission observer on its
enrolled metadata channel. It retains one actual lookup at a time and one
cancellable nominal one-second control heartbeat. It does not await journal
speech delivery or a speaker-stop Future, and input/model work does not wait for
the heartbeat. Stop fences admission, cancels/wakes the heartbeat, and joins the
observer plus its actual HTTP reads before releasing Call capacity. Closed,
missing or unavailable permission initiates original local/native cleanup.

The forced actual native/Dart control plays one synthetic backchannel and delays
an actual native positive lookup response at the fixture adapter's send Future.
The original native bytes are preserved. It then stops the real Decisions
worker, observes native capture cleanup and actual generation-stop publication,
and releases the older response unchanged. Journal delivery reaches the held
local speaker-stop Future. The microphone must still stop without fresh PCM;
speaker playback, the FIFO and Call capacity remain retained through the public
deadline until that same speaker Future settles. This holds an adapter Future;
it does not claim the already-read socket remains open.

The [first failure](https://github.com/rgoza1996/Kinaiya/issues/623#issuecomment-6948477146)
reaches `playbackStopWaits > 0` but fails `!audio.armed`: serial journal delivery
blocks the next permission read behind speaker cleanup. Four first source
snapshots and the original log are retained. The independent observer passes
that control together with immediate backchannel loss, idle/no-PCM loss and the
existing held-stop regression. A later [fixture-only style correction](https://github.com/rgoza1996/Kinaiya/issues/623#issuecomment-6948561989)
adds braces to three cleanup conditionals. Product assertions are preserved.

Eleven run records and five original source snapshots remain preserved. Each
`.log.json` stores exact UTF-8 text and its raw byte SHA-256; encoding `text`
recovers the original output. The manifest separately records archive and
unchanged receipt digests. Only eight runs qualify final source; earlier runs
remain first-failure/repair evidence.

This is software ownership and native permission evidence using real loopback
HTTP, fresh SQLite, synthetic Whistle IPC and explicitly mocked legacy-local
TTS. It does not qualify every transport/dependency fault, a killed/replaced
service process, managed TTS workers, physical audio, room/device echo
cancellation or provider latency. No dependencies were installed, SDKs upgraded,
live/paid models called, held native CI retried or installed owners promoted.
Model quality remains diagnostic; architecture/harness correctness determines
acceptance.
