# Current-main native Converse qualification, November 14, 2026

Issue #483 remains **open**. The first two actual native outputs still draft an
agenda instead of acknowledging the separate Agent handoff. The unrelated
direction question is answered correctly. This records negative quality evidence;
it is not a product repair or a claim of audible delivery.

The clean application baseline was
`cd47ba3eadc7d4fab7806e62c64d1e34cf487451`, after the editable Converse/Decisions
preferences and explicit `steer_pending` task observation reached main. Three
fresh public inputs and their expected behavior were declared in
[issue #483 before the run](https://github.com/rgoza1996/Kinaiya/issues/483#issuecomment-6889902486).
There were no retries, persona changes, replacement outputs or additional model
calls after the results were inspected.

| Input | First result | Text acceptance |
| --- | --- | --- |
| Ask Agent for an imaginary book club agenda | Drafts an agenda with welcome, book discussion, reflections and next-meeting planning | **Fail:** not receipt-only handoff |
| Add a poetry reading to that request | ASR changes this to “And a poetry writing to that same request,”; reply repeats an agenda without poetry | **Fail:** still drafts the deliverable; ASR discrepancy retained |
| Ask the opposite direction from east | “West is the opposite direction from east.” | **Pass** for this unrelated text answer only |

The unedited six input/output WAVs and exact output text are retained beside
[observations.json](observations.json). The output durations are 12.48, 12.32 and
3.60 seconds of 24 kHz mono PCM16. Input speech is public synthetic `ffmpeg`
`flite`/`slt` audio at 16 kHz, not the owner's earlier private recording.

The actual Store → NativeAudioOwner ASR → WindowConverseController →
RealtimeAudioProvider path ran against the existing isolated compatible engine
overlay at source `8ca4060b7e30e76baaa5670ee1ec1b760b7f8552`. The model and projector,
engine executable/library, clean application diff and qualification helper hashes
are recorded in the JSON. The overlay's earlier eighteen disabled-tool protocol
controls are retained in the [previous qualification](../native-converse-2026-11-14/README.md);
those controls were not rerun or treated as dialogue-quality evidence here.

A real ephemeral Agent service accepted the synthetic task and pending steer;
its explicitly controlled provider held one request and performed no tools or
other effects. Twelve actual authenticated TaskContext HTTP responses show the
task running at objective revision 1, with `steer_pending=false` initially and
`true` after the follow-up. There is no execution evidence, applied revision 2 or
completed outcome. Synthetic Decisions dispositions released each Converse turn;
this does not qualify Decisions model accuracy or Agent work quality.

Post-run verification reconstructed every output PCM stream and text from the
retained raw WebSocket events. All six WAV/PCM hashes, three ASRs, three final
text streams, three native publication text/PCM bindings and twelve task
observations matched. Raw local receipt hashes and event counts are retained in
the JSON; private absolute paths, launch arguments and large base64 logs are
omitted. The engine exited `0`; no run or cleanup failure was recorded.

The installed engine, owner services/configuration, microphone and cloud
providers were not used or changed. Audio has **not** been listened to; browser,
speaker and device delivery remain unqualified. Earlier first failures remain
preserved. The earlier private input echo has neither been reproduced nor
repaired by these fresh fixtures.
