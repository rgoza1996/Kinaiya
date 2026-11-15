# Native Audio comparison after connection repair, November 15, 2026

Issue #483 remains **open**. The already-installed Audio model now reaches
dialogue with the explicit `session_update` transcription connection. It gives a
receipt-only first handoff and answers an unrelated question correctly, but
changes one requested spare flashlight to a pair in the follow-up. These are
three first outputs from a declared comparison, not a repair of the owner's
Omni deployment or evidence of successful Agent work.

The clean application source was
`199c91624f91920a47683f3b8a506acc79cecd2c`. The earlier Audio comparison stopped
at ASR; it supplied no dialogue-quality result. This run uses the connection
choice repaired in #595, with unchanged built-in persona and three previously
unused public inputs [declared before inference](https://github.com/rgoza1996/Kinaiya/issues/483#issuecomment-6891460903).
No output was retried, replaced or selected from multiple generations.

| Input | First native reply | Text acceptance |
| --- | --- | --- |
| Ask Agent for an imaginary astronomy club equipment checklist | "I will ask the agent to create a brief equipment checklist for an imaginary astronomy club night." | **Pass** for receipt-only handoff; no checklist or completed-work claim |
| Add a spare flashlight | "I will add a pair of flashlights to the equipment checklist for the astronomy club night." | **Fail:** changes the requested correction despite ASR preserving "a spare flashlight" |
| Ask the name of a shape with three sides | "A shape with three sides is called a triangle." | **Pass** for the independent text answer |

All six input/output WAVs are unedited and retained beside
[observations.json](observations.json). Input is public synthetic local
FFmpeg/flite speech at 16 kHz. Outputs are 24 kHz mono PCM16 lasting 4.40, 4.08
and 3.52 seconds. Follow-up ASR ends with a comma instead of the input's period;
that original result is preserved. Audio has **not** been listened to. Text
agreement does not establish spoken-word agreement or physical audibility.

The actual Store → NativeAudioOwner ASR → WindowConverseController →
RealtimeAudioProvider path used the existing isolated disabled-tool ACK overlay
at engine source `8ca4060b7e30e76baaa5670ee1ec1b760b7f8552`. The installed
LFM3-Audio-2B Q4_K_M model and Q8_0 projector, executable/library, helper and
clean source hashes are recorded in the JSON. The earlier overlay protocol
controls were not rerun or counted as new quality evidence.

A genuine ephemeral Agent service accepted the task and pending steer. Its one
controlled provider request stayed held and performed no tools or other
effects. Twelve authenticated TaskContext HTTP responses show the task running
at objective revision 1; `steer_pending` changes from false to true after the
follow-up. They prove neither applied revision 2 nor execution success.
Synthetic Decisions dispositions released the Converse turns; this does not
qualify Decisions model accuracy.

A separate retained-byte verification reconstructed all three original ASRs,
assistant text streams and output PCM streams from the raw WebSocket events.
All six WAV/PCM hashes, three native text/PCM publications and twelve task
observations matched. Raw receipt hashes and event counts are exported without
private paths or large base64 logs. The engine exited 0, with no recorded run or
cleanup failure. The helper's exact SHA-256 is
`c53760c2e918ec857f19dc2e10867ed0bacddbc46b5f30e6c336e102bd602f35`.

## Reference source comparison

[reference-sources.json](reference-sources.json) pins seven public source/config
files to LiquidAI Omni revision
`21d75123f86c1f7ffb4bef1ea202710c4e7ebe67`, with URLs and hashes.
[reference-source-review.json](reference-source-review.json) records the
comparison with the clean engine source and retained actual Omni server props.
No downloaded Python code was executed, and no weights or dependencies were
downloaded.

The served template equals the published template except its final newline.
Both sources append the explicit interleaved marker after the assistant prefix
and wrap Omni input-audio embeddings once in media markers. The inspected audio
preprocessing formulas agree on padding, periodic Hann/window/hop, Slaney mel
normalization, log floor and final normalization. Retained actual server props
already use greedy text sampling. This is source inspection, **not numerical
equivalence** of features, encoder tensors, quantized logits or speech; loaded
projector sampling overrides were not numerically compared. No demonstrated
template/mode/preprocessing-source correction was identified.

No owner model, installed engine, runtime/configuration, microphone or cloud
provider was changed or used. There was no text/TTS fallback or new browser or
device rehearsal. Earlier private echo and current negative Omni handoff results
remain unresolved, as does the Audio follow-up failure. Full-precision reference
inference, audio listening and device delivery remain unqualified.
