# Native Converse architecture audit (#483)

The owner clarified on November 15, 2026 that Kinaiya owns the architecture and
harness, not model answer quality. The original repeated task, subsequent
unwanted drafting and reference-model observations remain failures of the
desired dialogue behavior. They are diagnostic observations, not a release or
issue-closure gate requiring Kinaiya to make that model answer differently.

The architecture review uses Kinaiya main `0ae03656f096e8cddcdd50a02f77ec55df687cf6`
and native engine source `8ca4060b7e30e76baaa5670ee1ec1b760b7f8552`.

| Boundary | Evidence and limit |
| --- | --- |
| Current audio input | `RealtimeAudioProvider.respond` sends the original PCM in an explicitly acknowledged realtime session. Its final current transcript is checked but omitted from instructions; the separate transcription intent cannot supply an assistant reply. |
| Prior context | `_instructions` retains all admitted history text, order and role labels within the declared all-or-refuse limit. It serializes that history as JSON inside system instructions. This preserves data but does **not** preserve native chat roles. |
| Endpoint capabilities | `tools/server/server-realtime.cpp::on_item_create` accepts only user messages containing `input_text`; assistant items are rejected. Blindly changing the provider to send assistant history as items would not repair this selected endpoint. |
| Template and audio frontend | The [retained reference comparison](evidence/native-converse-reference-comparison-2026-11-15/README.md) records byte-equal templates and close frontend numerical agreement. Both runs used the same flattened instructions. It does not qualify structured historical roles or prove the cause of any semantic answer. |
| Reply custody | The provider admits text/audio only from matching assistant response/item identities, validates final completion and accumulated output, and refuses malformed, stale or over-bound output. It does not rewrite an answer to produce an acknowledgment or claim Agent success. |
| Task observations | The [task-context contract](converse-task-context.md) keeps native task metadata separate from permission, execution evidence and semantic success. It retains source/disclosure/currentness checks and distinguishes generated history from confirmed playback. |

The concrete harness limitation is tracked in
[#626](https://github.com/rgoza1996/Kinaiya/issues/626): versioned role/history
capability handling and faithful native history when supported. Until that is
resolved, do not describe the existing reference comparison as proof that
Kinaiya has no multi-turn protocol limitation. #483 retains this architecture
follow-up; further persona/style tuning, confidence changes or model sweeps are
not its acceptance gate.

`tests/unit/test_realtime_audio_provider.py` covers actual encoded history and
original PCM, all-or-refuse bounds, matching session/response/item acknowledgments,
separate transcription, stale admission, cleanup and independent next turns.
`tests/integration/test_converse_task_context.py` covers the actual dialogue
paths' task metadata and disclosure boundaries. Those controlled tests verify
contracts; they do not measure answer, voice or classification quality. The
current-main audit runs both modules unchanged: **160 passed in 34.22 seconds**.
The documentation diff check also passes.

No model inference, engine replacement, owner configuration change, history
rewrite, audio replay, semantic override or fallback is part of this audit.
