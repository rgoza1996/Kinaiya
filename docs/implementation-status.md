# Kinaiya implementation and acceptance ledger

**As of:** October 30, 2026, America/Los_Angeles.<br>
**Specification:** [Roadmap 0.3.1](../Roadmap.md) and [clarification.md](../clarification.md).<br>
**Runtime source cutoff:** [`f92f6d10aa136a5d27a6d4b41af4389ece3ac828`](https://github.com/rgoza1996/Kinaiya/commit/f92f6d10aa136a5d27a6d4b41af4389ece3ac828), tree `1d6b27a87aa80dc992da7fa4c4fa2ac53bf95f41`.<br>
**Reconciliation:** [#161](https://github.com/rgoza1996/Kinaiya/issues/161), documentation only; subsequent bounded client fixes [#151](https://github.com/rgoza1996/Kinaiya/issues/151) and [#163](https://github.com/rgoza1996/Kinaiya/issues/163) are recorded below. The subsequent bounded artifact slice is tracked by [#165](https://github.com/rgoza1996/Kinaiya/issues/165) and ADR0068.

The owner requested reconciliation before further feature work. Inputs were the implementation discussion and **Set up local Penpot for Kinaiya** design discussion through October 30, the merged source/ADRs, retained synthetic research evidence, and GitHub tracking. This records product decisions without copying private conversation contents, credentials or live user data. A design-chat assertion or assistant completion message is not independently sufficient implementation evidence.

**Implemented** means the cited bounded behavior exists and has the stated verification. **Experimental** retains research, environment or reliability limits. **Unverified** identifies an acceptance gate without sufficient evidence. **Planned** identifies work without a qualified implementation. A milestone can contain all four. Open parent issues are not closed by this ledger; stale “queued” descriptions do not erase merged work. No percentage-complete or production-readiness claim is made.

## October 31 Options increment

[ADR0092](adr/0092-approval-expiry-renewal.md) adds visible paused approval
expiry, exact owner renewal into a separately reviewed permit and bounded
broker history. It preserves final one-use enforcement and unknown-receipt
handling. Static checks and PR-specific CI remain distinct; broader scoped
read/per-tool permission rules are still unfinished.

The owner-confirmed [full Options goal](options-delivery-plan.md) is tracked by
[#208](https://github.com/rgoza1996/Kinaiya/issues/208), alongside ongoing #196
and multimodal #204. [ADR0084](adr/0084-owner-execution-settings.md) adds native
service-level file-write/command Ask/Allow/Deny, tool network/YOLO inspection,
persisted step/command bounds and relative shell directory, plus embedded
Permissions, Workspace and Execution pages. Deny precedes approvals/YOLO and
mounts the native command workspace read-only. Saved receipts and actual active
state remain separate. See its PR for checks actually run; this addendum does
not change the historical source cutoff or claim completion of the twelve
Options groups, broader per-tool/scoped grants, root relocation, whole-task
deadlines/concurrency, provider editing or multimodal acceptance gates.

The following in-progress increment adds [owner note creation/revision/forget
and exact historical receipt recovery](adr/0085-converse-note-editor.md), plus a
[protected provider and credential catalog](adr/0086-owner-provider-catalog.md).
The catalog explicitly does not apply a provider to a role or establish
readiness. Native LM Studio now has a measured Agent/auxiliary complete-request
interface; no fresh model qualification follows. Current evidence is static
analysis/review; consult the associated PR for later exact-head CI/browser
evidence. Automatic memory extraction/retention, catalog role application,
ordered fallback, direct multimodal/camera and the remaining full Options groups
stay open. This addendum does not change the historical source cutoff.

[ADR0089](adr/0089-agent-catalog-models.md) subsequently adds explicit Agent
catalog selection for defaults, exact harnesses and five auxiliary text purposes,
fresh bounded task-owned adapters, exact version pins, retired-generation refusal
and retained cleanup ownership. This supersedes the catalog-application gap for
Agent only. Catalog role application for Converse/Decisions/STT/TTS, ordered
fallback and broader model qualification remain unfinished. Static review and
analysis do not establish fresh inference quality or full settings coverage.

[ADR0091](adr/0091-converse-text-model-settings.md) adds Converse text selection
through its own protected store and native operation leases, with a separate
embedded editor and exact unknown-save recovery. This supersedes the application
gap for that text role only. It is under source/static review; later PR-specific
CI and runtime evidence remain separate. Decisions/STT/TTS editing, fallback,
direct multimodal capture and the other full Options acceptance groups stay open.

## Milestone status

The [native operating-mode prerequisite](adr/0105-agent-operating-modes.md) for
[#217](https://github.com/rgoza1996/Kinaiya/issues/217) adds protected Off/on-demand,
native initial-task and steering acceptance ordering, full-request historical
proof and a compact Agent Chat owner control. Off refuses new admissions while
the explicit finish-already-accepted policy preserves authorized active work;
Stop, approvals and inspection remain independent of Converse. The combined
source passed 52 independent/combined native checks, additional corruption and
restart probes, 958 Flutter tests and actual Dart/HTTP/SQLite file-work and
restart/replacement checks. [Evidence](../evaluation/evidence/agent-operating-modes-2026-10-31.json)
retains failed baselines and verification limits; exact-final-head CI/review are
separate merge gates recorded on the issue/PR. Autonomous remains unavailable.
Standing responsibilities, scheduling, inbox/notifications and the broader M7
and full Options acceptance groups remain incomplete.

The strict integration retry also exposed an existing plugin subprocess cleanup
defect, tracked by [#226](https://github.com/rgoza1996/Kinaiya/issues/226).
Its narrow repair closes actual output pipes after reaping and covers early
cancellation/setup failures. Nine new actual-process cases failed before the
repair and pass afterward; 44 existing plugin cases and all 36 affected
intelligent-UI cases pass with ownership warnings treated as errors. Independent
probes and the original failed broad run are retained in the same evidence.

The subsequent [bound model-save repair](adr/0088-bound-model-settings-writes.md)
addresses [#191](https://github.com/rgoza1996/Kinaiya/issues/191): stable native
backend identity, fenced writer generations, exact historical receipt recovery
and privately scoped Flutter pending writes with bounded actual transport
ownership. Local evidence includes all 909 Flutter tests, native/command fault
and race regressions, and real Dart-to-native HTTP checks across process restart
and replacement at the same URL. [PR #214](https://github.com/rgoza1996/Kinaiya/pull/214)
merged after [all five CI jobs](https://github.com/rgoza1996/Kinaiya/actions/runs/41266352885)
passed: 7,628 Python tests on each version, 909 Flutter tests, 38 bridge tests,
and owner/companion web, Android and iOS simulator builds. Actual merge
`62b613f837937d865ca3c94261b2bb27a2ff7858` has the tested tree. This does not close
full Options #208; execution recovery is tracked separately by
[#213](https://github.com/rgoza1996/Kinaiya/issues/213).
The historical source cutoff and original roadmap acceptance gates are unchanged.

The [execution-save repair](adr/0090-bound-execution-settings-writes.md) adds its
own protected database identity and writer generation, preserves legacy runtime
1.0 first writes/replay, and scopes pending Flutter operations, drafts and notices
to their private owner/backend. Model/runtime requests share a bounded pool of
32 actual transport owners. Verification includes 153 native author checks,
41 unchanged independent native regressions, 929 full Flutter tests and 42
independent focused client cases. These checks and the three-phase HTTP check
passed again after integrating catalog-model main `4ecb6d4`; all 309 existing
main test/support paths remained byte-exact. Three additional catalog-cleanup
integration cases and 98 relevant regressions passed. Real Dart/native HTTP checks cover restart,
historical YOLO, and replacement database conflicts without inference; all owned
fixtures exited cleanly. Independent source review found no actionable finding
within this scope. [Evidence](../evaluation/evidence/execution-settings-authority-2026-10-31.json)
distinguishes retained failures, source boundaries and checks. [PR #216](https://github.com/rgoza1996/Kinaiya/pull/216)
merged after all five [CI313 jobs](https://github.com/rgoza1996/Kinaiya/actions/runs/41271962754)
passed: 7,754 Python tests on each version, 929 Flutter tests, 38 bridge tests and
owner/companion web, Android and iOS simulator builds. Actual merge
`59a3f6b65df3c35cd4ee0adbe9b5d029c6bdb572` has the tested tree. Issues #213
and #218 are closed; full Options and the original milestone gates remain open.

The first final-head run, [CI308](https://github.com/rgoza1996/Kinaiya/actions/runs/41270490307),
passed four jobs but exposed a pre-existing voice sender/disconnect completion
race on Python 3.11. [#218](https://github.com/rgoza1996/Kinaiya/issues/218) adds
a narrow exception-identity repair with the cleanup block unchanged. Two new
ASGI/native reproduction cases failed before the fix; the focused 189-case suite
then passed. An unchanged independent response probe went from eight passes and
one failure to nine passes; independent execution of the ten new cases plus the
original failing case also passed. [Evidence](../evaluation/evidence/voice-disconnect-completion-2026-10-31.json)
retains the failed run and qualification boundaries. Updated exact-head CI313
and independent review passed before PR #216 merged. Synthetic audio does not
qualify human voice or hardware.

The original [milestone exit gates](../Roadmap.md#24-milestone-roadmap) and AT01–AT48 remain unchanged. Additional owner-refinement scenarios AT49–AT61 supplement them; AT62–AT63 record the October 31 distinction between model choice, profile recommendations and architectural comparisons. These additions claim no new runtime behavior or passed acceptance. Links to tests describe existing coverage, not a new execution of those tests during documentation editing.

| Milestone / tracking | Current evidence-backed scope | Remaining acceptance and limits |
|---|---|---|
| **M0 — foundation** [#1](https://github.com/rgoza1996/Kinaiya/issues/1) | Implemented reference-Linux research foundation: contracts, authenticated journal, ordered/cursor recovery, independent services, CI and threat model. [ADR0001](adr/0001-integration-contracts.md), [Session integration](../tests/integration/test_session.py), [CI](../.github/workflows/ci.yml), [threat model](security/threat-model.md). | This foundation does not establish production, clean-machine distribution or equivalent sandbox/hardware support on every platform. |
| **M1 — standalone Agent** [#2](https://github.com/rgoza1996/Kinaiya/issues/2) | Verified experimental baseline: seed tools, approvals/YOLO, revisions, cancellation and outcomes, with prior [CLI/steering/failure evidence](adr/0002-verified-integration-slice.md), [Stage B evidence](../evaluation/evidence/stage-b-2026-10-26.json) and [Agent Chat with Converse absent](../evaluation/evidence/browser-2026-10-27.json). The fresh [Mica workflow](agent-history-trial.md) adds actual complete inspect/modify/test work and independent result verification. [PR159](https://github.com/rgoza1996/Kinaiya/pull/159) supplies harness/session workspace. | These combined records support the stated baseline exit gate, not general task reliability or production readiness. Mica constructed no Converse runtime; physical process-off evidence comes from the earlier CLI/browser work. Glimmer/local-provider qualification remains open in [#186](https://github.com/rgoza1996/Kinaiya/issues/186). |
| **M1A — skills/MCP/commands** [#3](https://github.com/rgoza1996/Kinaiya/issues/3) | Partial: pinned local skills, progressive loading, invocation, native commands, local/loopback HTTP MCP and owner management. [ADR0010](adr/0010-agent-skills.md), [ADR0017](adr/0017-mcp-management.md), [MCP evidence](../evaluation/evidence/mcp-2026-10-27.json). | Genuine upstream OpenClaw/Hermes conformance, full ClawHub/GitHub/private-source acquisition, broader remote/OAuth/resources/prompts/list-change support and lifecycle matrix remain. Local synthetic fixtures do not establish every foreign skill/plugin works unchanged. |
| **M2 — Converse/voice** [#4](https://github.com/rgoza1996/Kinaiya/issues/4) | Substantial experimental implementation: independent typed/live-window conversation, playback generations, local speech/text profiles, explicit cloud adapters and context-capacity budgeting. [ADR0019](adr/0019-streaming-window-control.md), [ADR0056](adr/0056-explicit-gpu-converse.md), [ADR0057](adr/0057-converse-context-window-budget.md), [ADR0061](adr/0061-explicit-meta-converse.md), [ADR0062](adr/0062-explicit-openrouter-tts.md). | Ming 3 Tiny [#72](https://github.com/rgoza1996/Kinaiya/issues/72), the full retained-provider comparison, local dialogue quality [#97](https://github.com/rgoza1996/Kinaiya/issues/97), physical microphone/speaker/echo, natural sustained conversation, audible latency distributions, concurrent load and aggregate 8 GB GPU qualification remain open. |
| **M3 — routing/coordination** [#5](https://github.com/rgoza1996/Kinaiya/issues/5) | Substantial experimental implementation: persistent routing, separate live timing/Agent outputs, source/revision/epoch checks, durable backlog and native handoff receipts. [ADRs0019](adr/0019-streaming-window-control.md)–[0021](adr/0021-laya-decisions.md), [streaming evidence](../evaluation/evidence/streaming-windows-2026-10-27.json), [handoff receipts](agent-handoff-receipts.md). Exact inspection added by [PR160](https://github.com/rgoza1996/Kinaiya/pull/160). | Long natural voice/task conversations, fresh independent route/target accuracy and efficiency, broader/default Laya qualification and complete repeated failure/interruption gates remain. A published synthetic Laya split missed steering and is consumed evidence, not a reusable unseen holdout. [#49](https://github.com/rgoza1996/Kinaiya/issues/49), [#53](https://github.com/rgoza1996/Kinaiya/issues/53) stay broader than individual merged repairs. |
| **M4 — executable harness research** [#6](https://github.com/rgoza1996/Kinaiya/issues/6) | Protected isolated selector SDK/execution, immutable registry, independent evaluation, owner-reviewed activation and rollback are implemented experimentally. A real generated selector was evaluated and rejected. [ADR0004](adr/0004-executable-harness.md), [retained report](../evaluation/evidence/harness-2026-10-26/report.json), [provenance](../evaluation/evidence/harness-2026-10-26/provenance.json). | The 12-case screening result had baseline/candidate recall 0.167 and failed its gate; no improvement or promoted winner. `model_profile` is null and exact per-candidate API dollar cost is unavailable. Model/task-paired Agent benchmarks and complete cost reporting remain. An honest negative result is allowed by the original gate; do not retroactively require positive improvement or claim this narrow study proves general self-building. |
| **M4A — plugins/composition** [#7](https://github.com/rgoza1996/Kinaiya/issues/7) | Narrow implemented capability-acquisition experiment: Agent authored executable CSV code, isolated execution passed four frozen owner cases, later Agent work used the approved capability, and rejected update/rollback were recorded. [ADR0016](adr/0016-compute-plugins.md), [evidence](../evaluation/evidence/compute-plugins-2026-10-27.json). | General hooks, plugin commands/aliases, dependency installation/locks, foreign adapters, comprehensive update/recovery and equal-tool harness-composition experiments remain. One acquired program is not general autonomous software acquisition. |
| **M5 — Flutter/character** [#8](https://github.com/rgoza1996/Kinaiya/issues/8) | Extensive experimental UI: validated pack import/export/palettes/variants, retained panes/drafts, Home/optional Converse, Options, motion preferences, eight interactive setup bubbles, playback mouth and two Agent sidebars plus Decisions inspection. [Pack behavior](character-packs.md), [ADR0059](adr/0059-home-and-optional-converse.md), [ADR0063](adr/0063-floating-setup-bubbles.md), [ADR0064](adr/0064-speech-mouth-playback.md), [ADR0065](adr/0065-agent-sidebars.md), [ADR0066](adr/0066-decisions-owner-window.md). | Full provider/setup editors, direct multimodal input, specialized expression/work artwork, newer identity-reference integration, broader device/voice/accessibility acceptance and a product Meta asset-generation workflow remain. Existing pack/native presentation persistence is real; older ADR limitations are superseded where later work explicitly implements it. |
| **M6 — resources/reliability** [#9](https://github.com/rgoza1996/Kinaiya/issues/9) | Experimental owned-worker handoff, verified release/restoration, source-bound stop, admission and durable owner-off intent. Actual CPU voice and managed GPU Laya observations exist. [ADR0013](adr/0013-resource-handoff.md), [ADR0022](adr/0022-managed-laya-resources.md), [owner-intent evidence](../evaluation/evidence/converse-owner-intent-2026-10-27.json). | General GPU/Agent scheduling, allocator enforcement, constrained 8 GB/concurrent load, unmanaged model handoff, device loss and broader crash/uncertain-effect recovery remain. Larger development-GPU observations do not qualify the target; preserve unexplained ownership/readiness failures instead of weakening guards. |
| **M7 — proactive work/notifications** [#10](https://github.com/rgoza1996/Kinaiya/issues/10) | The [native operating-mode prerequisite](adr/0105-agent-operating-modes.md) implements Off/on-demand admission and owner controls with local independent/runtime evidence; its final CI/review are tracked by #217. Task outcome notices and polling remain narrower mechanisms. [Options boundaries](options.md). | Standing responsibilities, source/event adapters, scheduling, attention policy, durable inbox, deduplication, quiet hours, missed-run handling and permitted notification/call escalation require implementation and verification. Autonomous remains unavailable until a real authorized standing-work producer exists. |
| **M8 — mobile calls** [#11](https://github.com/rgoza1996/Kinaiya/issues/11) | Experimental foundations: scoped pairing/revocation, foreground invitations/literal notes, companion client, Android/iOS build targets and private presentation storage. [ADR0045](adr/0045-scoped-companion-control.md), [ADR0046](adr/0046-foreground-companion-client.md), [ADR0048](adr/0048-native-app-foundation.md), [ADR0049](adr/0049-native-presentation-storage.md). | Owner controls [#125](https://github.com/rgoza1996/Kinaiya/issues/125), real duplex media, Telecom/CallKit/push, background/locked-screen delivery, device reconnect/audio routing and in-call interactive artifacts remain. `accepted_waiting_media` explicitly is not connected audio. Compiling Android/iOS does not pass a real-device call gate. |
| **M9 — intelligent UI/external Agent** [#12](https://github.com/rgoza1996/Kinaiya/issues/12) | Bounded experimental native artifacts: Agent publishes nine declarative component types, authenticated persistent patches/actions and publication-only evidence; Converse displays the main-stage result with corner portrait/transcript, pin/close/reopen, optional fallback and unchanged audio ownership. [#165](https://github.com/rgoza1996/Kinaiya/issues/165), [ADR0068](adr/0068-intelligent-converse-artifacts.md), [contract](intelligent-ui-contract.md). | General live bindings, image/document/code viewers, automatic semantic relevance, real call/mobile media and a genuine external-Agent adapter remain. Ordinary controls and simulated design examples do not establish those capabilities. No OpenUI or LiveKit dependency is selected. |
| **M10 — distribution/hardening** [#13](https://github.com/rgoza1996/Kinaiya/issues/13) | Selected hardening is implemented: authentication/disclosure, isolation and ownership fences, bounded transports/storage, native builds and extensive deterministic CI. [Threat model](security/threat-model.md), [CI](../.github/workflows/ci.yml). | Clean-machine installation/model setup, supported hardware/provider/platform matrix, production secret/update/recovery/migration/retention flows, licensing inventory and complete accessibility/security/performance release evidence remain. The application is experimental. |

## Voice and provider profiles

Local-first remains the product preference. The concrete smallest profile is **8 GB total GPU VRAM for STT + dialogue LLM + TTS**, accounting for concurrent Decisions, caches, buffers, renderer and headroom. CPU orchestration/tokenization does not violate this requirement; CPU model inference does not qualify this GPU profile. Older CPU backends remain explicit research/alternative profiles. [GPU policy](adr/0056-explicit-gpu-converse.md) and [options](options.md) retain the separation.

Whistle, Qwen3 0.6B, Qwen3.5 2B, Kokoro and Laya are local candidates or implemented adapter choices, not a qualified winning full stack. Model identity, quantization, actual residency, context settings and complete-stack measurements are required before comparing 0.6B versus 2B memory. Suggested Liquid/LFM decision/omni and quantized Glimmer profiles remain unqualified research leads. No new model acquisition or recommendation is made by this ledger.

The owner explicitly selected cloud Converse testing: Muse Spark 1.3 **Contributor** at catalog-verified `minimal` reasoning, separate Muse Voice Transcribe STT and OpenRouter `hexgrad/kokoro-82m` TTS. [PR145](https://github.com/rgoza1996/Kinaiya/pull/145), [PR147](https://github.com/rgoza1996/Kinaiya/pull/147) and ADR0061–0062 establish opt-in adapters and bounded transport/accounting contracts. Buffered transcription/synthesis must not be called native bidirectional realtime streaming. These choices do not set Agent's model, change the local default, guarantee provider availability/price or authorize private Contributor input.

Converse's prompt uses actual measured model capacity rather than the last eight replies. [ADR0057](adr/0057-converse-context-window-budget.md), [ADR0058](adr/0058-native-leading-system-sections.md) and [context evidence](../evaluation/evidence/converse-context-window-2026-10-30.json) record prompt/system/history fixes and limits. Whole-entry history selection, output reserve, exact provider identity and optional recall have their own guards. A provider error or unknown capacity must stay visible rather than being hidden by a shorter arbitrary prompt.

Task backchannels and provider-independent TTS cleanup are implemented in [handoff/motion behavior](adr/0060-converse-handoffs-and-motion.md), [TTS normalization](../services/converse/tts_text.py) and [integration regressions](../tests/integration/test_converse_tts_text.py). The tiny local model's earlier task denials remain negative dialogue-quality evidence; working routing and a later cloud reply do not retroactively fix that evaluation. Literal dialogue remains in the transcript while only synthesis strips decorative emoji/smileys.

Retained provider evidence includes [Supertonic](../evaluation/evidence/supertonic3-cpu-2026-10-27.json) and [VibeVoice](../evaluation/evidence/vibevoice-transformers5-port-2026-10-28.json) CPU screens, with limited samples and disclosed misses. Decoded or first non-silent PCM, physical audible onset and subjective quality are different boundaries. No stable p95, human “feel,” universal GPU placement or all-retained-provider qualification follows from these screens. The owner deferred human testing while other independent product work proceeded.

## Decisions inspection

[PR160](https://github.com/rgoza1996/Kinaiya/pull/160) merged the private native capture/retrieval API and actual Flutter owner window. It preserves exact provider-bound request bodies, raw responses and validated output separately; historical source/session/epoch/revision and available provider/model/artifact identity; ignored, refused, reused, native override and no-inference dispositions; explicit missing/expired capture; literal text and timezone-aware bounded filters/pagination. Reuse resolves the original call. Direct Agent observations do not cause new Decisions inference.

[Capture requirements](decisions-traceability.md), [API contract](decisions-trace-api.md), [ADR0066](adr/0066-decisions-owner-window.md), [native tests](../tests/integration/test_decisions_inspection.py) and [Flutter tests](../apps/kinaiya_flutter/test/decisions_inspection_test.dart) define the limits. Reads are owner-only and separate from generic transcript readers. Provider-bound capture is prospective; earlier missing scripts/identities are not reconstructed. No rationale/summary or confidence is invented for a discriminative model. This is implemented traceability, not fresh model-quality or overall M3 acceptance.

## UI and design reconciliation

The owner-supplied concepts, then the consolidated Penpot design, govern visual composition. Penpot omits some full-harness settings; [options.md](options.md) remains the capability/status inventory. Current setup displays host declarations and saves local optional-view visibility; it does not implement arbitrary model hot-swap, camera/video/direct-audio transport, a provider editor or credential provisioning.

Home is the water scene; its current action panel has Talk with Kinaiya and Setup plus separate Sleep/Wake controls. The earlier concept's Call/Tell-only voice-bar intent is not fully reflected in that Home panel; Tell lives in other conversational controls. Their placement needs reconciliation with the later Home/optional-Converse split. Optional Converse is the close-up interaction presentation sharing Home's owner. Mascot interaction opens setup, replacing the original character→Agent transition. A saved animation switch/reduced-motion controls fades and decorative motion. The interaction view's waveform-like bars currently indicate activity, not measured amplitude. Only the mouth is driven by actual playback-amplitude data.

Eight setup bubbles have randomized valid positions, slow bounded collisions, independent hover/focus holds, dragging/flinging, morphing modals and floating Setup/View/Close. Empty space exits setup after any nested panel closes; scrolling, drag and voice Stop have separate hit/gesture ownership. [PR150](https://github.com/rgoza1996/Kinaiya/pull/150), [PR153](https://github.com/rgoza1996/Kinaiya/pull/153), [PR156](https://github.com/rgoza1996/Kinaiya/pull/156), [ADR0063](adr/0063-floating-setup-bubbles.md).

Agent's two sidebars, real catalog/session pagination and native immutable session/task pins are implemented by [PR159](https://github.com/rgoza1996/Kinaiya/pull/159). Selection is not promotion. Unassociated history remains unassociated and a host with no configured selector registry exposes native Seed Agent; a configured empty or failed catalog does not invent that fallback. The current extension kind is `context_selector.v1`, not an entire self-generated Agent runtime. Project/media libraries and arbitrary generated navigation remain broader design work. [Workspace API](agent-workspace-api.md) and [ADR0065](adr/0065-agent-sidebars.md) define uncertainty and ownership.

The default idle atlas sits on water under the moon; the bundled flat backgrounds use an anchored water contact and responsive desktop/phone artwork. [ADR0055](adr/0055-seated-idle-and-live-timing.md). A fresh confirmed owner-off shows sleeping Home artwork. General unavailable/sleep visual mapping remains to reconcile with explicit failure/unknown/intent labels; the running implementation must not be described as already displaying sleeping art for every unavailable state.

Playback-synchronized close-up mouth motion and restrained generated mouth poses are merged in [PR158](https://github.com/rgoza1996/Kinaiya/pull/158). Actual decoded PCM/playback timing drives amplitude states; silence, Stop, stale generations and motion preferences close the mouth. Timed viseme input is optional; current providers supply no qualified phoneme timing. The Penpot chat's newer removal of hair ornaments, tiny chest mole and moonlight-highlight refinements are reference artwork only at the cutoff. No assertion is made that they are in the released pack.

The latest Decisions desktop mockup is complete as a design; the design chat reports condensed mobile scripts and visual-only prototype filters/copy controls. Production has its own native-backed literal inspection and responsive tests. Those are separate artifacts: neither the prototype's examples nor production code prove every Penpot frame is finished or synchronized.

## Intelligent UI

[#165](https://github.com/rgoza1996/Kinaiya/issues/165) adds the bounded native v1 slice described by [ADR0068](adr/0068-intelligent-converse-artifacts.md). Agent creates the content; **Converse displays it**. A generic component renderer is used rather than scenario-specific demo pages. Native storage pins session/thread/epoch/task/objective, content digest/revision and separately persisted view state. Publication evidence proves publication only. User interactions change presentation state or prefill an ordinary scoped message; they grant no tool permission and start no model call.

The main-stage transition keeps the same portrait, transcript, composer, drafts and keyboard focus; a collapsible transcript appears above the corner portrait, Call/Tell/Stop remain reachable, and narrow/reduced-motion layouts are tested. Pin, close, chooser reopen, active editing, late older-task ordering and connection/session/epoch changes have explicit ownership. Rich rendering has a saved optional fallback. This is a text-capable runtime slice; mobile calls, streaming subscriptions and automatic topic-based relevance are not implemented by it. AT61 and the parent M9 gate stay open.

Verification and synthetic live results are recorded in the [retained evidence](../evaluation/evidence/intelligent-ui-2026-10-30.json) and associated PR. The live text test keeps Converse and its explicitly selected Decisions provider at Muse Spark 1.3 Contributor `minimal`, and Agent independently at `xhigh`, through the unchanged development budget guard. The sales task completed with native publication evidence; a later database task published usable native views but failed on its provider response budget. Long publication history corruption exposed by that run is repaired with bounded native receipts. Laya's new-task routing misses remain open in [#168](https://github.com/rgoza1996/Kinaiya/issues/168). Browser checks cover persisted selectors, schema details, close/reopen, Ask drafts, readable fallback and narrow layout. Only fresh fictional public-synthetic inputs are used. Model quality and voice latency require separate human/media tests.

The post-repair database test still repeated publication and failed on invalid
model arguments; [#169](https://github.com/rgoza1996/Kinaiya/issues/169) retains
that qualification gap. Native publication/display works independently of the
truthfully failed task. General Agent completion reliability is not established
by this slice.

The bounded [decoding diagnostic repair](ui-decode-diagnostics.md) distinguishes
constant JSON/wrapper rejection reasons in authenticated failed outcomes while
preserving error text, parser guards, terminal failure and prior usable
artifacts. It retains no raw model/parser content and performs no retry or
automatic finish. Synthetic adapter/SQLite checks verify that distinction;
the historical invalid value and repeated creation behavior remain unqualified
under #169. See [ADR0071](adr/0071-constant-ui-decode-diagnostics.md).

## Typed Meta action experiment

An experimental explicit `typed-v2` Meta Agent profile is available for #169. It
projects all native operations and the nine existing UI data shapes into closed
per-action branches, preserving native permissions and completion authority.
The compatibility default and running previews remain unchanged. See the
[wire contract](meta-typed-actions.md) and [ADR0072](adr/0072-typed-meta-action-wire.md).
At source `87f5307`, 524 targeted checks passed. Meta admitted the full protocol
schema; the same six public tasks then produced five native successes and one
output-budget failure. Independent artifact grading found three passes, two
partial normalization examples with unexplained added data, and one failure
without a deliverable. No repeated successful creates occurred in that run.
The [retained evidence](../evaluation/evidence/meta-typed-actions-2026-10-31.json)
records all 27 task calls, the separate admission, costs, failures and limits.
These consumed development examples do not establish broad improvement or
completion reliability; #169 stays open. Exact-head CI is tracked in the PR.

## Native tool advertisement

The bounded [#171](https://github.com/rgoza1996/Kinaiya/issues/171) advertisement
repair shares one immutable per-inference tool snapshot across the seed prompt,
Meta wire schema, native membership checks and capability inspection. Actual
task harness pins qualify selector availability; existing action approvals,
disclosure and selected-target brokers remain authoritative. The
[contract](native-tool-advertisement.md) and [ADR0070](adr/0070-task-qualified-tool-advertisement.md)
describe the bounded native identity projection, concurrent request scope and
negative native refusal receipts. MockTransport/native integration checks are
in [the advertising suite](../tests/integration/test_agent_tool_advertising.py).
That identity repair left #171's selected schema/permission facts and bounded
cross-catalog discovery open. The subsequent [PR178](https://github.com/rgoza1996/Kinaiya/pull/178)
implements that remainder: one read-only `tool_catalog` and authenticated API
share native logical schemas, disclosure-filtered skills, passive MCP metadata
and approved plugin metadata. Source/version and actual task-pin facts remain
distinct. Inspection neither invokes nor installs a capability, and its receipts
cannot ground task completion. Retained native guards stop dependent work when
disclosure is withdrawn, including after prompt-history eviction.

[ADR0074](adr/0074-progressive-tool-discovery.md), the
[catalog contract](progressive-tool-catalog.md) and the
[retained evidence](../evaluation/evidence/progressive-tool-catalog-2026-10-31.json)
record 1,036 focused checks on the corrected functional source and two bounded
real Contributor xhigh cases, one per wire format, that inspect a native schema,
read a public synthetic file and finish with actual read evidence. Both earlier
failed cases remain recorded; this is not general model-reliability or live
foreign-target qualification. Exact final-head CI is recorded on the PR.
Clarification §§14–15's optional Tool Library, installation lifecycle and formal
verification remain design work. Gateway configuration is not proof that a
foreign target is authorized, installed or working.

## Complete Agent context

[#179](https://github.com/rgoza1996/Kinaiya/issues/179) repairs two observed
Agent context losses: clipping complete file results to 2,000 characters and
retaining only eight history messages. Successful native reads now return a
whole stable bounded UTF-8 file or explicitly refuse it. Exact complete-request
counts select chronological whole action/result exchanges while protecting
instructions, the current objective/revision and the newest exchange. Native
evidence, approvals, source/catalog disclosure floors and completion checks
remain independent of the selected model history.

[ADR0075](adr/0075-complete-agent-context.md) records the contract and native
source admission before each count/generation. The immutable failed M1
repository case remains a failure: 18 successful provider responses produced no
edit or command; its [audit](../evaluation/evidence/agent-history-baseline-2026-10-31.json)
is retained unchanged. Data loss is proven; its repair does not establish the sole
cause of repeated reads or qualify general repository work. Source-bound
[regressions](../tests/integration/test_agent_context_admission.py) exercise real
file/journal/provider code through synthetic HTTP. Real provider counting,
future repository cases and exact-head release checks remain separate evidence.
Previously unmeasured Agent adapters now refuse explicitly until they implement
complete-count admission; model names and approximate token estimates supply no
capacity. Converse's separate budgeting interface is unchanged.

The separately frozen [Mica workflow](agent-history-trial.md) subsequently passed
one actual inspect/modify/test attempt: three complete pre-write file observations,
a two-line source correction, the exact 14-test public command and eight separate
host-graded cases. Six count/generation pairs were byte-identical. Independent
artifact review checked 208 consistency predicates and the general candidate fix.
The trial constructed no Converse or Decisions runtime and preserved unrelated
host services. This is new bounded execution evidence; the earlier failure is
unchanged, and the different cases are not a paired causal comparison.

Final upload fences also revalidate retained skill/plugin pins and native
transcript provenance, including causal ancestors of retrieved assistant replies.
These remain disclosure dependencies after prompt eviction and grant no task
authority. Scope withdrawals in count/generation hooks and after generation are
covered by [native regressions](../tests/integration/test_agent_retained_transcript.py)
and [broker regressions](../tests/integration/test_agent_retained_disclosure.py).
Missing or over-bound provenance refuses the request rather than dropping a
dependency. This does not qualify general repository work or a new model default.

## Initial Agent submission durability

The bounded [#199](https://github.com/rgoza1996/Kinaiya/issues/199) implementation
uses atomic native submission slots, strict original-request provenance and owned
client cleanup. Unconfirmed acceptance remains paused with a fixed HTTP503;
negative observations cannot release an uncertain key for another admission.
Only the original direct creation receipt can permit one local launch after fresh
authority checks. Retained and recovered receipts preserve native identity and
model pins without execution, including authentic pre-scope local-only records.
Task ID collisions refuse rather than substitute another session or overwrite a
retained task. See [ADR0087](adr/0087-agent-submit-admission.md).

The [verification record](../evaluation/evidence/agent-submit-admission-2026-10-31.json)
binds source/tests and the lead's 396 passing integrated cases, additional
overlapping review groups, initial failures and corrections. Existing assertions
remain intact while test journals explicitly implement the new native protocol.
The interrupted earlier full local run is not a pass; applicable exact-head CI is
required in the associated PR before merge. This is an M1/M6 reliability slice,
not general task reliability, a durable outbox, distributed execution lease or
exactly-once third-party effects. Upgrade requires quiescing old Agent and Session
writers; no running preview was changed during this verification.

## Agent completion durability

The bounded [#167](https://github.com/rgoza1996/Kinaiya/issues/167) repair uses
one frozen native success append and positive receipt/read-back confirmation.
Live state remains verifying until confirmed; uncertainty is paused with no
success summary, fallback terminal write or replay of work. A finalization fence
also arbitrates steering, cancellation and shutdown. See the
[contract](agent-completion-confirmation.md), [ADR0069](adr/0069-confirmed-agent-completion.md)
and deterministic [receipt](../tests/unit/test_agent_completion_confirmation.py),
[recovery](../tests/integration/test_agent_completion_recovery.py) and
[race](../tests/integration/test_agent_completion_races.py) tests.
This fixes final-outcome confirmation for legacy and UI tasks; it does not
qualify general model task reliability ([#169](https://github.com/rgoza1996/Kinaiya/issues/169)),
online owner reconciliation, arbitrary external-effect transactions or the full
M1/M6 production gates. Exact execution evidence is recorded with the PR/CI.
The [bounded verification record](../evaluation/evidence/agent-completion-2026-10-31.json)
preserves the failing prior baseline and independent checks.

## Worker ownership diagnostics

[#176](https://github.com/rgoza1996/Kinaiya/issues/176) adds constant observations
to the existing pre-grant worker verifier after CI231 refused ownership in the
memory lifecycle test. It preserves every ownership predicate, read/close order,
grant boundary and legacy outcome. No retry or timing relaxation is introduced.
The [contract](worker-bootstrap-diagnostics.md),
[ADR0073](adr/0073-worker-bootstrap-refusal-diagnostics.md) and
[bounded evidence](../evaluation/evidence/worker-bootstrap-diagnostics-2026-10-31.json)
retain the failure and verification scope. The cause was not reproduced locally;
this diagnostic slice does not fix general startup reliability or close
[#9](https://github.com/rgoza1996/Kinaiya/issues/9) or the M6 gate.

## Client reliability

The owner prioritized the intermittent bubble CI failure and trailing-slash connection failure before further features. [#163](https://github.com/rgoza1996/Kinaiya/issues/163) replaces a randomized, net-displacement test with fixed initial positions/velocities, a measured forward release gesture and frame-by-frame stationary-pointer re-entry/escape checks. Production physics and fresh random layout remain unchanged. A deliberate sticky-hover mutation fails the replacement test. The original failing CI runs and the pinned Flutter velocity diagnosis remain documented in [ADR0063](adr/0063-floating-setup-bubbles.md#deterministic-release-regression-163); the exact random layout responsible for either CI failure was not retained.

[#151](https://github.com/rgoza1996/Kinaiya/issues/151) adds one shared service-root normalizer at profile and client boundaries. Bare, trailing-slash and outer-whitespace spellings use identical Session/Agent/Converse paths; accepted reverse-proxy prefixes also survive WebSocket conversion. Invalid or ambiguous paths, URL credentials/query/fragment and port zero are rejected with bounded errors. Owner setup retains its remote-HTTPS requirement; authentication, disclosure and provider choices retain their existing contracts. [ADR0067](adr/0067-canonical-service-roots.md) maps client, setup, auxiliary APIs and real loopback WebSocket verification.

Local evidence uses pinned Flutter 3.47.8: `flutter test --no-pub` passed 771 tests after the production fixes, before the final WebSocket test was added; `flutter test --no-pub test/events_service_root_test.dart test/service_root_test.dart` then passed all nine tests at the cutoff. `flutter analyze --no-pub` and 33 `node --test web/audio_bridge.test.js` tests passed. The loopback test runs actual HTTP upgrades and first-frame auth with synthetic data, without live Kinaiya services or paid inference. Full builds and exact-head CI are recorded in the associated PR rather than inferred from local test counts. This work does not change the running preview, production provider settings or milestone acceptance gates.

## Optional Converse memory

The Korina DAG began as a prototype specification, not an existing implementation to port. Current opt-in [memory core](adr/0047-converse-memory-core.md), [selected recall](adr/0051-converse-selected-recall.md) and [owner UI](adr/0052-converse-memory-owner-ui.md) implement scoped owner-curated source-pinned notes, immutable hard dependencies, correction/refutation reconciliation, bounded complete closure, forgetting and explicit selected use. Saved namespace access and runtime selected use are separate; runtime use starts off after restart. Another backend remains an explicit interface/compatibility responsibility, not a claim that a second backend is already delivered.

This memory is Converse-only, local-only in the current slice, optional and supplementary to the authoritative transcript. Disabled/unavailable memory leaves ordinary dialogue and Agent/Decisions responsibilities intact. No automatic transcript extraction, semantic relevance ranking, note-creation/editor UI or measured continuity improvement is established. Count the entire recall-augmented prompt and fence stale derived history before publishing or sending it.

## Research and release interpretation

- **Modular voice:** Transport, cancellation and inference observations are real but bounded. Human naturalness, physical audible/echo behavior, robust long conversations, comparable full-stack latency and the 8 GB GPU profile remain unverified release gates.
- **Fixed-capability harness optimization:** Retain the failed selector study and incomplete exact-cost/model-profile fields. A valid negative result is evidence; improved model/task performance is not demonstrated. Do not retune against consumed held-out answers or substitute stronger models/tools without reporting the changed experiment.
- **Capability acquisition:** The CSV algorithm demonstrates one real generated, isolated, registered and reused capability. It does not prove general acquisition, arbitrary dependency installation or the sign-language/reverse-engineering examples in clarification.md.
- **Mobile and intelligent UI:** Scoped foreground signaling/native builds and simulated layout examples alone do not establish generated artifacts or call media. The native artifact slice has its own bounded runtime evidence; two-way media, system/background calls and external-Agent portability remain unverified.
- **CI/release:** [PR159 CI](https://github.com/rgoza1996/Kinaiya/actions/runs/41154288948) and [PR160 CI](https://github.com/rgoza1996/Kinaiya/actions/runs/41154304593) passed Python, Flutter/web/companion, Android and iOS checks at the integrated content. Browser/native synthetic evidence is described in their PRs. This does not qualify every physical device, arbitrary provider, private data flow or complete roadmap gate. A local development preview is not production distribution.

## Remaining order and maintenance

Follow [the reconciled implementation order](../Roadmap.md#immediate-implementation-order): finish documentation reconciliation; stabilize current source/connection/provider/task truth; proceed with bounded independent UI/settings/memory/mobile foundations; qualify voice with the owner; complete fresh budgeted research; deliver proactive/media/artifact capabilities by their dependencies; then qualify distribution. Human tests remain pending until actually performed; independent work can proceed without pretending those gates passed.

Each consequential change should update its owner decision/requirement, ADR or contract, acceptance mapping and this status/evidence ledger in the same PR. Record exact source, tested environment, actual versus deterministic evidence, failures and remaining gates. Update the corresponding tracking issue when appropriate, but do not close a parent milestone merely because one slice merged. Keep original acceptance obligations unless the owner explicitly changes them; identify that decision and migration consequences. Do not copy live user evidence into public artifacts or reset development spending uncertainty to obtain a pass.
