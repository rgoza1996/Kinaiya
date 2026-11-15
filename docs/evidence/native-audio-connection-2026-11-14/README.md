# Explicit native ASR connection qualification

This records the bounded transport repair in [#595](https://github.com/rgoza1996/Kinaiya/issues/595)
and another **failed handoff-quality comparison** for [#483](https://github.com/rgoza1996/Kinaiya/issues/483).
It does not qualify a replacement for the owner's model or change live settings.

## Original failure

At application `17e1fe70f9cc19bbb7f7d0375344a304e52bd340`, the existing
`intent_url` default selected the installed Audio model's separate live-ASR
handler. Its matching transcription ACK preceded original PCM, but the handler
then emitted a transcript delta without the committed input identity required
by the buffered adapter. Kinaiya refused. The later attempts were refused by
the native owner; only one actual ASR connection occurred and no conversational
reply or Agent request occurred. The first manifest, wire, public inputs and
logs remain unchanged; their bounded record is in
[observations.json](observations.json). Its cleanup refusal is preserved, and
the original engine exit was `0`. This is not successful dialogue evidence.

The engine source `8ca4060b7e30e76baaa5670ee1ec1b760b7f8552` selects that
live-ASR path when its ASR preset is nonempty and the URL intent is transcription.
That handler's `final: true` branch ends input without a committed identity.
The chat-capable handler instead supports the same explicit transcription
session update and emits the committed identity before transcription output.

## Qualified connection path

At clean application commit `3d5724fa9e969f7d8abb77b1959335366bc10cf2`, the
explicit `session_update` choice opens the original realtime URL, then requires
the matching transcription session ACK before exporting original PCM. It
preserves strict committed-identity, transcript, currentness and cleanup checks.
It does not retry the failed operation or automatically choose another mode.

The model and projector were the already installed LFM3-Audio-2B Q4_K_M/Q8_0;
their full hashes and the compiled engine/library hashes are recorded. The
engine uses the same private disabled-tool ACK overlay documented in
[the earlier qualification](../native-converse-2026-11-14/README.md). Neither
installed engine nor model was changed. The model is an alternate local
checkpoint, not reference-equivalence proof for Omni.

The same three predeclared public setup inputs were used once with this new
connection setting. They are consumed fixtures, not an unseen holdout. Actual
ASR, first assistant outputs, complete prepared messages, twelve original native
task-context responses and publication records are retained. All six WAVs are
unaltered; input and output PCM hashes match, published text matches the native
endpoint text, and publication retains the current local-only task-data floor.

| Input | Actual observation | Handoff/answer result |
| --- | --- | --- |
| Ask Agent for an imaginary art-gallery opening checklist | ASR changes “Agent” to “cage”; native dialogue drafts the checklist | Handoff failed |
| Change opening time to ten in the morning | ASR is correct; dialogue promises an update, then discusses inability to edit conversation content | Handoff failed |
| Ask how many sides a rectangle has | ASR slightly changes wording; dialogue answers four | Factual answer passed |

The actual native Agent task has one controlled, held provider request. The
steering receipt remains pending at revision 1, as all later native metadata
reports. No tool, revision-2 application, program receipt, task outcome or
semantic work completion occurred. The artificial routing releases Converse;
it does not qualify Decisions model accuracy.

The engine exited `0` and no cleanup error was recorded for the new connection
run. Audio has not been listened to, and browser/device speech delivery remains
unqualified. No cloud, microphone, private input, model download, text/TTS
fallback, owner deployment or live configuration change was involved. The
connection compatibility result does not close #483.
