# Native logical utterance boundary (#623, #681)

This local checkpoint continues `180d7712b7d9261d5cc010a4928c6f149c89ff54`
on `codex/623-adaptive-call-integration`. Adaptive Live remains disabled.
The actual CharacterView Call owner and its cancellation/recovery integration
are unfinished; these checks do not qualify the complete Call feature.

The additive `native_live_utterance_v1` capability and closed request advance
the logical utterance after the exact original final, native publication
acknowledgment and actual decoder/HTTP cleanup. They retain the same native
microphone registration and STT lease, preserve earlier source permission and
continue absolute audio offsets. Ordinary v2 begin cannot change utterances.
Model completion does not establish an acoustic end, and a backchannel does
not request an utterance boundary. The contract is documented in
[live stream capture](../../live-stream-capture.md#explicit-logical-utterance-boundary).

The [manifest](manifest.json) records exact test/build commands, 921 unchanged
Python input hashes, 330 unchanged frontend input hashes, dependency locks,
18 run receipts and their exact UTF-8 log envelopes. The final Python runs use
the same input hashes on both interpreters. The isolated web build uses the
same frontend bytes and unchanged dependency lock. All 594 prior exported
schemas remain byte-identical; exactly two closed schemas are added.

| Current check | Result |
| --- | --- |
| Focused native composition, utterance, ownership and actual Dart-to-native HTTP/IPC tests on Python 3.13.18 | 272 passed |
| The same scope on Python 3.14.10 | 272 passed |
| Native stream client, incremental PCM, bridge fixture, client and capture-mode Flutter tests | 42 passed |
| Analyzer over four affected Dart files | No issues |
| Full Ruff checks and formatting | Passed; 1,212 files already formatted |
| Isolated release web build and Wasm dry run, without dependency installation | Passed in 29.5 seconds |

The Python runs retain the existing Starlette warning; the web build retains
the existing missing CupertinoIcons font warning. The final Python log hashes
are `c241313324b97ddfae033fa90723f833429776a6ecc45f6d15be17987487efb2`
and `6a0c4122824f21ecf04d18b0a4e61da1a384f0a29484c694d17c41bb8fa076f6`.
Frontend and web-build log hashes are
`0e63da6cbe0d658d726a403a609ab4eea6fada9f04e3ec0638509fb1208d634b`
and `985758a93a7391851f98f44ca48fedc91d77e61174cf789bdada9bd6fb6a0522`.

Native controls cover successive utterances, prior source permission, unchanged
mic/STT ownership, scope substitutions, malformed bodies, owner authorization,
initial-name collision, a genuinely held decoder drain, concurrent duplicate
delivery, lost acknowledgment with metadata-only recovery and refusal after
an explicit capture end. The actual Dart transport additionally crosses fresh
native HTTP, SQLite and synthetic Whistle IPC through ordinary decoder rollover
and the explicit logical boundary, then verifies original-owner cancellation.
The committed wire fixture was exported from that actual native implementation.

During this work, #681 exposed a false availability report for buffered-only,
closed or replaced STT clients. It was filed before repair. The selected STT
owner now passively checks its original owned-stream protocol without acquiring
a provider, credential, lease or capture. Supported input stays available;
buffered selections remain selectable without a fallback or a model probe.
The issue remains open pending integration.

The retained first controls include nine missing-endpoint failures and an
implementation fixture failure caused by overwriting the new utterance in the
fixture's merge order. The fixture was repaired without removing its original
assertions. #681 retains its initial failing probe, expanded one-pass/three-fail
controls and subsequent four-pass repair. One attempted check named a
nonexistent STT test file: it exited 4 with no tests run and is not a pass.
The earlier 239-case Python runs predate the #681 fix and are historical only.

Original controls, native source references and byte-exact reconstructable
patches are retained under `sources/`. Additional log envelopes preserve the
first missing-PYTHONPATH schema export, Dart analyzer diagnostic and Ruff import
diagnostics. These failures were recorded in the issue tracker before repair;
they are not replaced by later successful results.

No physical microphone, live model endpoint, installed-owner promotion,
package/SDK installation or held #624 native retry was used. Full Call wiring,
original cancellation through disconnect/replacement, capture during playback,
echo/mute/accessibility/no-audio controls, timing comparison and qualified
default rollout remain required under #623.
