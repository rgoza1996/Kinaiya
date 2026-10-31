# ADR0068: Agent publications displayed by Converse

Status: bounded experimental implementation for [#165](https://github.com/rgoza1996/Kinaiya/issues/165), under M9 [#12](https://github.com/rgoza1996/Kinaiya/issues/12). The parent milestone and AT61 real-call gate remain open.

## Requirement and decision

The owner asked for generated intelligent UI in the updated Penpot composition, then clarified that Converse displays it. Agent constructs generic structured artifacts. Decisions routes ordinary text or speech requests to Agent; it neither invents frontend data nor grants publication authority. The accepted main-stage layout keeps the real transcript above the corner portrait, retains ordinary composer/voice controls, and permits pin, close and reopen. The native client renders the specification; generated executable frontend code is excluded.

Use the [versioned contract](../intelligent-ui-contract.md), rather than hardcoded demonstration scenarios. Nine native component types cover bounded text, status, tables, charts, diagrams, source metadata, numeric/choice controls and an Ask affordance. Unknown content retains a readable summary fallback. The saved Interactive views switch disables rich rendering while keeping data and ordinary controls usable. Existing six- through nine-key presentation snapshots preserve all prior choices and default the added preference on.

## Authority, persistence and recovery

The Agent supervisor exposes `publish_ui` only with configured native storage. Its SQLite database stays outside the worker workspace. The model supplies bounded components, never artifact/session/task identity, revision, digest or evidence. Native publication receipts prove only UI publication. Final verification checks the current task/revision and stored digest. Failed or uncertain publication/journaling cannot produce a confirmed completion or automatic retry. Converse uses fixed publication-only outcome wording when no independent work-effect proof exists, including after plugin or MCP computation.

Muse's strict text-response schema carries publication arguments in one bounded `ui_spec_json` string. The provider adapter validates closed UTF-8 JSON, translates valid logical assistant history, and returns the logical native action. Successful publications retain a native receipt with ID, digest, revisions and the complete component ID/type manifest within 4 KiB of UTF-8 JSON. They do not replay the large assistant payload or clip it into malformed JSON. This transport representation grants no publication authority; storage still validates every component and native pin.

Patches use content-revision compare-and-swap. Presentation interactions use scoped native owner authentication, expected content revision and idempotency. They change presentation state only. Ask prefills the existing composer, preserves its contents and combines disclosure scope; it never submits a message or runs work. A timeout retains the exact pending action for explicit reconciliation. A bounded private RAM record preserves unresolved keys across matching-backend/scope reconnects; different credentials, endpoints or session/epoch cannot inherit them. Stale callbacks retain the displayed artifact and connection binding instead of acting on whichever result is current later. A definitive rejection restores native selection rather than leaving an optimistic local highlight.

Currentness is checked again after speech-authority awaits, including pinned source revisions, final session state and pending local cancel/steer. These checks define the captured admission boundary, not an atomic distributed transaction with future Session changes. Scoped reads and frontend connection/session/epoch ownership keep old results from becoming another session's UI. Publication cannot grant authority over an unfinished speech source or bypass its existing approval requirements.

The client limits response bytes, concurrent transports, pagination, detail work, retained artifacts and retries. Transport ownership survives a logical deadline until actual send/stream/cancel/client cleanup settles. Late reads cannot replace another connection's data. Native task-submit journal ordering fences automatic display: a late older task cannot replace a newer view, user-pinned/edited views remain in place, and unknown historical ordering requires manual selection.

## Presentation lifecycle

One stable portrait, transcript and composer element tree survives normal and artifact layouts. The portrait animates from its measured global bounds into the lower-right frame and returns on ordinary close. The transcript can collapse without losing its owner or history. A connection/session/epoch change clears the prior private result immediately, independently of the decorative return animation. Keyboard focus, drafts and reachable Stop/Call/Tell controls survive layout and keyboard changes. Narrow layouts stack content, and reduced motion suppresses the transition. No new audio owner, recording, model load or call-media implementation is introduced.

## Traceability and verification

| Requirement | Implementation | Verification |
| --- | --- | --- |
| Agent-generated bounded data, immutable native provenance and patches | `services/agent/artifacts.py`, `artifact_api.py`, supervisor and seed interface | Artifact unit/integration tests: parser/limits, CAS, native evidence, authentication, persistence, recovery and final-authority races |
| Converse outcome meaning matches publication evidence | `services/converse/runtime.py` | Actual isolated plugin → publication → native outcome regression, forged/stale/origin negatives and fixed publication-only wording |
| Scoped optional display and explicit interactions | Flutter artifact models/client/state/renderer | Model/renderer, transport lifecycle and API/state tests; malformed/unknown/scope changes, ordering, uncertain actions and Ask disclosure |
| Main-stage composition with stable portrait/transcript/composer | `intelligent_converse_stage.dart`, `character_view.dart` | Widget tests: exact bounds, forward/reverse/interrupted motion, focus/draft identity, pin/close/reopen, collapse, keyboard/narrow/text scaling, reduced motion and owner changes |
| Ordinary requests travel through Decisions | Existing Converse/Decisions route and native Agent loop | Retained synthetic live evidence records actual requested/resolved models, route, task, native receipts and displayed generated data separately from deterministic tests |

The first broad Flutter run exposed three stale preference-fixture expectations, a repeated non-advancing cursor in a new test fixture, and the unrelated randomized transparent-corner regression [#166](https://github.com/rgoza1996/Kinaiya/issues/166). Fixtures now model the actual version/cursor contract; the corner test uses a deterministic unoccupied transparent point without changing production physics. The original failures remain in the verification log. Live provider tests then exposed the missing strict-wire publication field: one task exhausted its step budget and one exhausted its response budget; neither published a result. The adapter fix is verified through actual Meta adapter MockTransport → native storage → finish tests as well as new live requests. A public Ask on an empty draft also exposed an overly restrictive local-only default; empty drafts now start from the selected disclosure scope and combine the artifact scope, while existing local/private drafts remain restricted.

The [retained synthetic evidence](../../evaluation/evidence/intelligent-ui-2026-10-30.json) records native tasks, routing events, publication digests and browser checks. The first sales request completed. A subsequent database request published three valid views but exhausted its response budget. Inspection confirmed a separate history defect: large successful UI actions were clipped mid-JSON. The bounded receipt repair is exercised by long create/update requests through the actual adapter; the defective history alone does not establish why that individual provider call exhausted its budget.

After the repair, a new compact database request published four valid views and
then failed on invalid provider publication arguments. It did not establish
reliable task completion. [#169](https://github.com/rgoza1996/Kinaiya/issues/169)
tracks repeated creation and finish qualification; no automatic finish or
success-criteria change was added to hide that result.

See the [status ledger](../implementation-status.md#intelligent-ui) and the associated PR for completed checks and actual live observations. Real mobile call media, general live subscriptions, authenticated external-Agent interoperability, image/document/code viewers, arbitrary frontend plugins and automatic semantic relevance remain separate work. Laya's failures to route the new UI requests are tracked by [#168](https://github.com/rgoza1996/Kinaiya/issues/168); the live text UI test uses an explicitly configured Contributor Decisions profile. The unrelated legacy non-artifact final-outcome confirmation gap is tracked separately by [#167](https://github.com/rgoza1996/Kinaiya/issues/167). This slice does not establish human conversation quality, end-to-end voice latency or full M9/M8 completion.
