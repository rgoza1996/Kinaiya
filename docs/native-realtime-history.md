# Native role-bearing history (#483 / #626)

The `realtime_v1` adapter negotiates the optional `native_history.v1` session
extension. A capable endpoint receives the original first system instruction
and separate prior text turns with their actual `system`, `user` or `assistant`
roles. The current transcript is validated but omitted: original current PCM
remains the user input. The adapter never inserts raw role-token strings,
regenerates history, replays historical audio or silently truncates context.

Endpoints without the advertisement retain the documented limited mode: prior
text/order/role labels are JSON data inside system instructions. That data
preservation does not establish native chat-role fidelity. There is no probing
model call, silent model/default switch, transcription/TTS fallback, persona
tuning or model-quality acceptance gate.

## Negotiation and acknowledgment

An initial realtime `session.created` advertises:

```json
{
  "native_history": {
    "contract": "native_history.v1",
    "max_messages": 4094,
    "max_utf8_bytes": 1048576
  },
  "history_count": 0,
  "history_sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
}
```

These fields supplement the existing session identity, audio formats and output
settings. The contract fixes the three supported text roles. Bounds are exact
non-boolean integers; a smaller advertised bound is honored without folding or
dropping rows. Unknown, malformed or changing advertised contracts refuse.

The client freezes its caller-owned containers before any transport/authority
hook. After validating the initial empty-history binding, `session.update`
contains the exact advertised `native_history` object, the original first system
string in `instructions`, and an ordered `history` array of exact two-field
`role`/`content` records. The final current transcript is absent. History can
contain an additional authorized system row; the adapter does not relabel it.

Before sending any PCM, the client requires the existing exact session/settings
acknowledgment plus the same capability and exact history count/digest.
Canonical history bytes are compact UTF-8 JSON with sorted object keys,
unescaped Unicode, no whitespace separators and no non-finite values. Array
order and every string are preserved. SHA-256 is lowercase hexadecimal.
The engine normalizes object-key order to `content`, then `role`, before hashing.
This cryptographic binding avoids echoing up to a megabyte of historical text
through the existing 256 KiB received-event guard.

The existing 1 MiB preparation limit, including its fixed legacy-framing reserve,
still applies. The endpoint additionally limits first-system UTF-8 bytes plus
canonical history JSON bytes. Existing message, PCM, output, event, deadline and
cleanup bounds remain in force. Context capacity is not a measured token-fit
claim. Over-bound requests refuse completely; they are not rebuilt with less
history or retried as limited JSON after a failed negotiation.

## Engine behavior

The downstream candidate for source `8ca4060…` stages strict history before
changing configuration. It renders first-system plus native history through the
selected model's actual chat template, then derives the current user/audio and
assistant-generation framing. Internal placeholders are bounded, collision
checked and independent of historical literal `@@USER@@` / `@@REPLY@@` text.
The old fixed-placeholder method can put current input in the wrong place; a
compiled original-method negative control retains that failure.

History is immutable once the first audio or message starts. Transcription
sessions neither advertise nor accept the extension, and cannot inherit a
nonempty realtime history through a type switch. Malformed roles/types/fields,
unsupported versions and over-bound updates refuse without adopting the proposed
history. Disabled-tool acknowledgments remain required.

The candidate is evaluated in an isolated source/build overlay. It does not
replace an installed engine or edit an owner's runtime/configuration. It uses
the existing build's other objects/libraries; that is not a clean-rebuild or
new upstream release qualification.

## Reply provenance and verification

`NativeAudioReply.history_mode` is `native_roles_v1` after verified negotiation
or `system_json` for a limited endpoint. The generated event retains it as
`native_audio_history`, bound into the original native reply cache. It describes
the input representation, not model answer quality, heard playback or Agent
effects. Invalid representation metadata refuses before publication.

The first provider oracles retain **20 failed / 1 passed**. The digest/metadata
contract revision retains its separate **14 failed / 11 passed** baseline. The
provider, history and native-audio metadata modules then pass **216 tests**.
The C++ helper passes **40 checks**, including the actual LFM3 template, literal
markers, role/order reconstruction, malformed rows, refusal immutability and the
original-method negative control. Python and C++ independently agree on the
Unicode/quoted/newline history digest.

The predeclared private CPU-only native run passes **13 protocol controls**,
including a 300 KB fitting history whose acknowledgment remains bounded. One
original public-PCM provider call returns a completed text/audio unit with
`native_roles_v1`; captured frames match the original input PCM and returned
text/PCM exactly. The original socket closes, the source hashes remain fixed,
the intended overlay is mapped, and the original engine exits zero. No answer
or style score, reroll, microphone, ASR/Agent execution, browser delivery or
owner-runtime promotion is inferred. Local first failures, helpers, source pins,
raw frames and receipts remain under `output/issue-626-native-history/`.
