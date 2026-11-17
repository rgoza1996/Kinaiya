# Original live cancellation and retained recovery (#623)

This local checkpoint continues `74a1ade24bbf051730025c5b5a129fe74444e0c9`
on `codex/623-adaptive-call-integration`. Adaptive Live remains disabled and
the actual CharacterView Call owner remains unfinished. This qualification
covers the native negative-original protocol and its independent frontend
recovery channel, not the complete Call feature.

`native_live_abort_v1` binds the exact initial begin and cancellation reason.
It can fence a delayed start before registration, while Converse is off and
without STT selection, registration or positive admission. Its permanent native
Session fence survives temporary registry retirement. Exact metadata lookup
recovers a lost acknowledgment without repeating a mutation. The original
identity-only cancel request still requires an owned capture.

Permission closure and actual native cleanup are separate. Native closure,
readback and bounded retirement now share the same cleanup predicate. A
retained registration failure before STT entry can settle after its positive
native fence; an explicit pre-STT flag supplies that evidence. Pending native
tasks, decoder cleanup or an unsettled STT lease continue to hold ownership.
Browser acquisition/stop, client HTTP, downstream inference and speech/playback
remain independent obligations. See the
[protocol and lifecycle contract](../../live-stream-capture.md#cancellation-before-the-initial-start-arrives).

| Current check | Result |
| --- | --- |
| Composed host/capture/utterance/negative/permission/native-wire scope on Python 3.13.18 | 387 passed |
| The same scope on Python 3.14.10 | 387 passed |
| Frontend stream/recovery, PCM, bridge, client and capture-mode tests | 47 passed |
| Analyzer over four affected Dart files | No issues |
| Full Ruff checks and formatting | Passed; 950 files already formatted |
| Isolated release web compiler and Wasm dry run, with no dependency installation | Passed in 31.5 seconds |

Both Python runs retain the existing Starlette deprecation warning. The web
build retains the existing missing CupertinoIcons font warning. Final native
log hashes are `b88b614aa8ab9e46caac4872aa32e4267657df8f09d96e4b18decebd25fe7d7c`
and `19a820b48f3157059b383c5fe288cdc31051077ea6773130de8534206a9e149f`.
The frontend and web-build hashes are
`517c12f459605e070b6bad7f7d3300d97c5c9ce08bec5f1a778b935e1cdc6687`
and `ff6f485e4e8b78a4eda688ecc3ab782b0ce0dcda04d7275a653b90354e1264aa`.

The [manifest](manifest.json) retains exact commands, 923 unchanged Python
inputs, 331 unchanged frontend inputs and the dependency locks. The isolated
web source has exactly the same frontend bytes and unchanged lock. All 596
prior exported schemas remain byte-identical; exactly three closed schemas
are added. There are 22 exact UTF-8 log envelopes and 12 original source
references, including byte-exact patches reconstructed with `--fuzz=0`.
All 27 assertion statements in the first controls remain in the final controls.

Native controls cover cancellation before start, genuinely held backend and
registration work, one retained fence through timeout/duplicate delivery,
cancelled HTTP facades, lost fence acknowledgment and unavailable metadata,
retirement after more than 64 completed captures, owner authorization, changed
initial scope, owner-off without STT, and genuinely held decoder cleanup.
The actual Dart client crosses fresh native HTTP, SQLite and synthetic Whistle
IPC, then uses its original recovery connection after the normal client closes.
Frontend checks also retain a genuinely late negative HTTP response beyond both
closed facades. The native wire fixture was exported from actual native HTTP
and SQLite with public synthetic inputs; the exporter source is retained.

First failures are retained: 13 controls at the missing endpoints; two expanded
recovery gaps (20 passed); one owner-off admission gap (60 passed); a test-only
curly-brace analyzer diagnostic; and the shutdown/readback mismatch. Each was
recorded in #623 before repair. The earlier 387-case runs preceded the added
shutdown assertion and shared-predicate repair; the composed runs above use
the final, identical inputs on both interpreters. No original assertion was
removed or weakened to obtain these results.

No physical microphone, live model endpoint, SDK/package installation,
installed-owner promotion, system-policy change or held #624 native retry was
used. Remaining #623 work includes actual Call capture/playback ownership,
echo/mute/accessibility/no-audio/process replacement, representative timing
comparison and qualified default rollout. Model behavior remains diagnostic.
