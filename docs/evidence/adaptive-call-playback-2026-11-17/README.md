# Gated Adaptive Live Call playback (#623)

This local increment fixes ignored native playback-stop controls and premature
Call retirement while the original speaker-stop Future is unresolved. Adaptive
Live remains gated off. #623 acceptance, integration and rollout remain open.

Baseline: `0d0a2bb214117e9f9cf9b8a151c7b9534e5b61ed` on
`codex/623-adaptive-call-integration`. The [manifest](manifest.json) retains
commands, runtime identities, final source hashes, unchanged input pins and
original first-failure evidence. All 599 exported schemas are unchanged
([export record](schemas.json)).

| Check | Result | Receipt |
| --- | --- | --- |
| Composed native host/capture/consumer/contract/actual Dart integration, 29 pytest targets | 448 passed on Python 3.13.18 | [Command and pins](logs/issue-623-playback-final-313-receipt.json) |
| Same composed scope | 448 passed on Python 3.14.10 | [Command and pins](logs/issue-623-playback-final-314-receipt.json) |
| Flutter capture/settings/voice/native audio/playback/visualizer/UI regression, 23 targets | 244 passed | [Command and pins](logs/issue-623-playback-frontend-final-receipt.json) |
| Seven browser audio entrypoints | 237 passed | [Command and pins](logs/issue-623-playback-browser-final-receipt.json) |
| Dart analyzer, six affected files | No issues | [Command and pins](logs/issue-623-playback-analyzer-final-receipt.json) |
| Ruff check | Passed | [Command and pins](logs/issue-623-playback-ruff-final-receipt.json) |
| Ruff format check | 1,217 files already formatted | [Command and pins](logs/issue-623-playback-format-final-receipt.json) |
| Release web build with existing cache, `--no-pub --no-web-resources-cdn` | Passed; Wasm dry run passed | [Command and pins](logs/issue-623-playback-web-build-receipt.json) |

The two Python runs cover the same cases, including ten actual native/Dart
scenarios. The 923 Python inputs and 338 frontend inputs match their final
receipts and remain unchanged through each run. Browser counts describe executed
checks, including registrations from imported fixture modules. The Wasm result
is a compiler dry run; no Wasm build or runtime test was performed. Existing
Starlette deprecation and CupertinoIcons font warnings remain in the logs.

| Requirement or defect | Evidence |
| --- | --- |
| Native generation stop must stop current playback while capture continues | Actual authenticated interrupt, original scoped journal control, exact PCM bytes/rate, same armed mic/epoch and subsequent acknowledged PCM |
| Ordinary delivery, malformed metadata or another runtime cannot grant stop authority | Three typed playback-control tests cover scope/runtime mismatches, receipt fields, causes, unsafe/fractional/bool counters and monotonic jumps |
| Synthesis EOF must not mean played | Actual native speech stream ends while the local player remains held; only original local completion produces the native played receipt |
| Mute must stop capture and playback without claiming delivery | Actual interaction surface at 1.6 text scale and reduced motion, accessible Mute/Unmute labels, native cancellation and no played receipt |
| Native mic cleanup must not release Call before original speaker cleanup | Actual `cleanup_confirmed=true`, stopped mic and held local speaker Future; Call/FIFO remain retained through the public deadline, then retire after that same Future settles |
| Backchannel speaker output must not enter the software input path | Public bridge stop/natural-end controls preserve exact mic PCM, FIFO packet, sequence/offset, epoch and one acquisition; echo cancellation/noise suppression requests are checked |

Native scenarios use fresh loopback HTTP, SQLite and synthetic Whistle IPC.
The real `/v1/converse/speech` path uses an explicitly mocked legacy-local TTS
transport, one public synthetic text and eight PCM bytes. This qualifies native
speech authorization, transport and local playback lifecycle; it does not qualify
a managed TTS worker, physical hardware, room/device echo cancellation, model
quality or provider latency. No dependencies were installed, SDKs upgraded,
live/paid models called, physical microphones acquired, held native CI retried
or installed owners promoted.

First defects were recorded before repair on #623: [ignored native stop](https://github.com/rgoza1996/Kinaiya/issues/623#issuecomment-6946155421),
[Semantics fixture lifetime](https://github.com/rgoza1996/Kinaiya/issues/623#issuecomment-6946361842),
[premature speaker retirement](https://github.com/rgoza1996/Kinaiya/issues/623#issuecomment-6946607412)
and [five fixture style diagnostics](https://github.com/rgoza1996/Kinaiya/issues/623#issuecomment-6946739612).
The first logs, original sources and repair runs remain preserved. The fixture
repair disposes its Semantics handle inside the test body; the analyzer repair
adds braces and uses super parameters. Product assertions are not weakened.
Late native interrupt/delivery refusals in failed-test disposal remain truthful
refusals, not stop confirmations or reasons to retry a mutation.

Each `.log.json` stores exact original UTF-8 text and its raw byte SHA-256.
Encoding its `text` field recovers the original bytes; the manifest separately
records the archive digest. Receipts are copied unchanged. Only eight final
runs qualify the final source; earlier passes are diagnostic/repair evidence.

Remaining work includes complete dependency-loss/no-audio, owner/process
replacement and reconnect paths, remaining supported playback adapters and
accessibility behavior, physical echo prevention, representative
same-conversation timing comparison, default migration and rollout. Model answer,
classification, persona and speech quality remain diagnostic; architecture and
harness correctness determine acceptance.
