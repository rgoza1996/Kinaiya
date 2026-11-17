# Gated Adaptive Live Call ownership (#623)

This local qualification connects the actual Call widget to one original native
capture, contiguous browser PCM, passive journal updates and retained negative
recovery. Adaptive Live remains gated off. Complete #623 acceptance and rollout
remain open; this record does not qualify physical audio or model quality.

The baseline is `341c7fda6e51b8cf34e986a97fc1fb62b6b290c1` on
`codex/623-adaptive-call-integration`. The [manifest](manifest.json) records exact
commands, SDK/runtime identities, 923 Python inputs, 336 frontend inputs, lock
digests, compiled web artifact hashes and all run/source digests. Both Python
versions and all final frontend/build checks use the same recorded source bytes.
All 599 exported schemas remain unchanged ([export record](schemas.json)).

| Check | Final result | Receipt |
| --- | --- | --- |
| Native host/capture/consumer/contract/actual Dart integration; 29 pytest targets | 444 passed on Python 3.13.18 | [Command and pins](logs/issue-623-call-ready-final-313-receipt.json) |
| Same composed scope | 444 passed on Python 3.14.10 | [Command and pins](logs/issue-623-call-ready-final-314-receipt.json) |
| Flutter capture/settings/voice/native audio/speech/visualizer regression; 19 targets | 204 passed | [Command and pins](logs/issue-623-call-ready-frontend-final-receipt.json) |
| Seven browser audio entrypoints | 233 passed | [Command and pins](logs/issue-623-call-ready-browser-final-receipt.json) |
| Dart analyzer; 11 affected files | No issues | [Command and pins](logs/issue-623-call-ready-analyzer-final-receipt.json) |
| Ruff check | Passed | [Command and pins](logs/issue-623-call-ready-ruff-final-receipt.json) |
| Ruff format check | 1,216 files already formatted | [Command and pins](logs/issue-623-call-ready-format-final-receipt.json) |
| Release web build with existing cache, `--no-pub --no-web-resources-cdn` | Passed; Wasm dry run passed | [Command and pins](logs/issue-623-call-ready-web-build-receipt.json) |

These counts describe each check's scope; the two Python runs cover the same
cases. Native integration uses real loopback HTTP, fresh SQLite and synthetic
Whistle IPC, with a synthetic microphone adapter. The held-stop test advances
Flutter's public deadline while retaining an actual unresolved stop Future; it
does not measure physical hardware latency. No dependencies were installed,
SDKs upgraded, live/paid models called, physical microphones acquired, native
CI retried or installed owners promoted. Existing Starlette deprecation and
CupertinoIcons font warnings remain visible in their original logs.

| Requirement or defect | Implementation and verifying evidence |
| --- | --- |
| Fresh speaking observation independent of frozen packets | Optional `IncrementalSpeakingAudio` and exact-epoch public bridge getter; unchanged first JS control source and final browser checks |
| Contiguous input, native ACK, same mic/STT lease through logical boundaries | `AdaptiveLiveCapture`; actual Call/native consumer and original transport integration cases |
| Retain original stop through deadline and parent disconnect; exclude late PCM | Actual held-stop Call case; native request counts, lease retirement and original FIFO release |
| Valid decimal journal payload metadata must not stop capture | Independent bounded journal codec and strict identity counters; six client controls including 200 seeded round trips, then actual native consumer |
| Separate next-utterance proof from subsequent PCM scope | Original operation's `segment` projection; actual empty full reply followed by a third acknowledged PCM packet |
| Recover refused rotation and lost rotation receipt without guessing cleanup | Exact stopped original FIFO readback or original idempotent receipt; both actual native controls |
| Provider EOF must preserve a buffered noninterrupting reply | Separate native `live_no_audio_ready` fact; actual held-reply case proves no finish/next-utterance dispatch, and native readiness controls cover both full-reply actions |
| Disconnect before/during wake body must not become ASGI 500 (#682) | Native bounded 400 refusal before waiter enrollment; two actual ASGI disconnect controls within the 50-case wake scope |

The [Call ownership contract](../../adaptive-call-integration.md) and
[native consumer contract](../../live-converse-consumption.md) describe authority,
ordering, limits, no-audio readiness and retained cleanup.

First failing outputs, diagnostic runs and repair runs remain under `logs/`;
27 original source snapshots remain under `sources/`. The manifest marks only
the eight final runs as final-source qualification. Earlier 441-case runs and
the first release build predate the held-reply repair and are historical.
Native Python assertion ASTs remain unchanged for all 21 wake-control and 118
consumer-control assertions in their first snapshots; the complete first JS
control source also remains unchanged. Fixture corrections are explicit: real
loopback HTTP for Flutter native transports, JSON media types in mocks, and the
seeded generator's choice-list bound. The initial fixtures and failed outputs
remain retained. No behavioral expectation was relaxed to conceal a failure.

Remaining acceptance includes full playback/backchannel capture, echo/mute,
accessibility, complete no-audio/dependency-loss and process replacement/reconnect
coverage, representative same-conversation timing comparisons, and qualified
default migration/rollout. Synthetic architecture traces do not establish local
or cloud latency distributions. Model answer, classification, persona and speech
quality remain diagnostic observations; architecture and harness correctness
determine acceptance.
