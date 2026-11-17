# Adaptive Live Call replacement ownership (#623)

This local increment fixes a replacement Converse surface acquiring a microphone
in another capture mode while the original Adaptive Live Call still retains an
unresolved speaker-stop Future. The release gate remains false; complete #623
acceptance and rollout remain open.

Baseline: `3ca1e442483f170b81f60637e18dc6eaf14c9e8c` on
`codex/623-adaptive-call-integration`. The [manifest](manifest.json) binds final
source hashes, exact commands, runtime versions and original evidence. All 923
Python and 338 frontend input hashes match the final receipts. All 599 exported
schemas remain unchanged ([export record](schemas.json)).

| Check | Result | Receipt |
| --- | --- | --- |
| Composed host/capture/consumer/contract/actual Dart integration, 29 pytest targets | 450 passed on Python 3.13.18 | [Command and pins](logs/issue-623-replacement-final-313-receipt.json) |
| Same composed scope | 450 passed on Python 3.14.10 | [Command and pins](logs/issue-623-replacement-final-314-receipt.json) |
| Flutter capture/settings/voice/native audio/playback/visualizer/UI regression, 23 targets | 244 passed | [Command and pins](logs/issue-623-replacement-frontend-final-receipt.json) |
| Seven browser audio entrypoints | 237 passed | [Command and pins](logs/issue-623-replacement-browser-final-receipt.json) |
| Dart analyzer, three affected files, `--fatal-infos` | No issues | [Command and pins](logs/issue-623-replacement-analyzer-clean-receipt.json) |
| Ruff check | Passed | [Command and pins](logs/issue-623-replacement-ruff-final-receipt.json) |
| Ruff format check | 1,218 files already formatted | [Command and pins](logs/issue-623-replacement-format-final-receipt.json) |
| Release web build, existing cache, `--no-pub --no-web-resources-cdn` | Passed; Wasm compiler dry run passed | [Command and pins](logs/issue-623-replacement-web-build-receipt.json) |

Both Python runs cover the same cases, including twelve actual native/Dart
scenarios. Browser counts include checks registered by imported fixture modules.
The Wasm result is a dry run, not a Wasm build or runtime test. Existing Starlette
deprecation and CupertinoIcons font warnings remain in the original logs.

The retained registry now fences all Call modes and one-shot recording before
microphone acquisition, including after asynchronous setup. Replacement
presentation observes enrollment/retirement without replacing or releasing the
original owner. New-conversation admission also requires no retained Call.

Actual native controls cover two replacement paths:

- Dispose the original widget/client while its local speaker stop is held.
  Confirm native input cleanup and the stopped mic while the speaker/FIFO remain
  retained. A fresh widget with a fresh adapter and explicit Legacy Silence has
  disabled Call/Tell controls and cannot acquire. When the same original speaker
  Future settles, that surface enables Call and makes exactly one acquisition.
- Replace the client inside the same mounted widget while the speaker stop is
  held. The independent original recovery channel confirms native cancellation
  after ordinary-client closure. The public deadline and another Stop action
  preserve retention, one original cancellation and no positive audio replay.
  The same speaker Future must settle before retirement and new Call readiness.

The first UI failure was [recorded before repair](https://github.com/rgoza1996/Kinaiya/issues/623#issuecomment-6947200251).
The [extended control](https://github.com/rgoza1996/Kinaiya/issues/623#issuecomment-6947241684)
confirmed an actual second microphone acquisition with expected arms `0`, actual
`1`; its original UI assertions remain. A later fixture-only correction removes
two [redundant null assertions](https://github.com/rgoza1996/Kinaiya/issues/623#issuecomment-6947327104).
First failed logs, source snapshots and repair runs are retained separately from
final-source qualification. No product assertion was weakened to hide a failure.

The first [schema-export invocation](schema-export-first.json) selected an older
editable checkout because the invoker omitted the worktree import path. It
failed before changing any schema. That [failure was recorded](https://github.com/rgoza1996/Kinaiya/issues/623#issuecomment-6947510472)
before using explicit `PYTHONPATH` for this worktree, as the qualified pytest
runner already does. The repaired export passes with all 599 hashes unchanged;
no dependency or product-source change was needed.

Fourteen run records and seven original source snapshots remain preserved.
Each `.log.json` stores exact original UTF-8 text and its raw byte SHA-256;
encoding its `text` field recovers the original bytes. The manifest separately
records archive digests and unchanged receipt digests. Only the eight final
runs qualify final source; earlier passes and failures are historical evidence.

These are software architecture controls using actual loopback HTTP, fresh
SQLite, synthetic Whistle IPC, a mocked legacy-local TTS transport and synthetic
microphone/speaker adapters. They do not qualify native owner/process
replacement, every reconnect/dependency-loss path, managed TTS workers, physical
audio, room/device echo cancellation or provider latency. No dependencies were
installed, SDKs upgraded, live/paid models called, held native CI retried or
installed owners promoted. Model quality remains diagnostic; architecture and
harness correctness determine acceptance.
