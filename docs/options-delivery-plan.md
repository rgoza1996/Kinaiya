# Options delivery goal

Owner confirmation: October 31, 2026. Tracking:
[#208](https://github.com/rgoza1996/Kinaiya/issues/208), with ongoing feature work
in [#196](https://github.com/rgoza1996/Kinaiya/issues/196) and multimodal gates in
[#204](https://github.com/rgoza1996/Kinaiya/issues/204).

The owner supplied the full earlier comparison of agent harness settings and
explicitly made all twelve groups below part of the active `/goal`. This scope
is additive to harness customization, per-harness auxiliary models, local and
cloud multimodal qualification, voice/camera/Decisions integration, slash
commands and generated libraries. Recording a missing capability in this plan
does not deliver it. The parent stays open until implementation and the
corresponding acceptance evidence are present.

## Acceptance groups

| Group | Required outcome | Current delivered foundation / next work |
| --- | --- | --- |
| Models, providers, fallback routes | Independent Agent, Converse, Decisions, STT and TTS choices; supported reasoning/input/sampling controls; endpoint/local-model and protected credentials; explicit fallback order with private/cloud disclosure approval. | Agent defaults, exact-harness and five auxiliary text-purpose overrides exist (ADR0077/0082). The provider catalog and exact Agent task adapters are merged through #212 (ADR0086/0089). Converse text selection (#220, ADR0091) and ordered routes for the five auxiliary text purposes (#221, ADR0093) are merged. Independent Decisions selection is merged in #223 (ADR0098); the authenticated 8808 preview exposes all three increments. Independent STT selection is merged in PR225 (ADR0099), including durable unresolved-worker fencing; all five CI jobs passed and 8808 serves this source. Positive worker reconciliation is merged in PR229 (ADR0100) after all five CI jobs passed; independent TTS is merged in PR230 (ADR0101) after all five CI jobs passed; its combined 8808 preview has passed passive API and browser review with no speech worker or budget installed. Primary/other-role fallback and broader sampling remain incomplete. Direct audio requires the actual transport and qualification. |
| Precise permissions | Allow/Ask/Deny for reads, writes, commands, network and individual tools; task/session/workspace/default scopes; active grants and approvals; approval behavior separate from actual sandbox access. | Service-wide native execution controls exist (ADR0084). Scoped read_file/write_file/shell rules and exact approval expiry/renewal are merged in #215 (ADR0096/0092); Search and exact MCP targets are merged in #221 (ADR0094). ADR0103 adds a separate redacted one-use authorization activity inventory and exact approved-unspent revocation, with positive historical Session proof; source/independent review are complete and CI/preview are pending. General read/network enforcement and remaining tools/plugins remain work. The [permission rollout](options-permission-plan.md) retains the broader admission obligations. |
| Workspace and execution | Select/inspect the active root and readable/writable/protected folders; actual isolation state/network; supported execution backend and optional isolated Git worktree. | Native containment/Bubblewrap and execution inspection/relative starting directory exist (ADR0084). Owner root selection, read-only overlays, concrete root pins and an embedded editor are merged in #222 after all five CI jobs passed (ADR0097), and present in the authenticated 8808 preview. Alternate backends and coding worktrees remain work. |
| Tools, skills, MCP, plugins | Individual/group enablement; MCP tool selection; source/version and active status. Distinguish installed, connected, enabled and authorized. | MCP management exists. ADR0102 adds protected native tool availability plus installed Skills/Plugins inspection, exact activation/evaluation/promotion/disable/rollback and embedded editors. Source and independent native/Flutter review are complete; final integration/CI/preview remain pending. Installation/update/removal, bound scope editing and uncertain multi-file activation reconciliation remain work. |
| Context and history | Actual used/available model context, response reserve, supported compaction thresholds and model/provider provenance. Distinguish retained transcript from reduced prompt context. | Measured prompt admission and explicit auxiliary compression calls exist. ADR0104 adds passive bounded Agent/Converse context observations and embedded input/capacity/reserve/headroom/selection inspection; source and independent review are complete, with CI/preview pending. Automatic compaction, general limit editors and retention remain work. |
| Memory and retention | View/edit/select/forget Converse notes; approved capture/proposals; separate transcript, raw-audio and diagnostic policies; reviewable export/deletion. | Selection/use and transcript-pinned capture, immutable revised notes, explicit forget and historical receipt recovery are embedded in Options (ADR0085). Retention, backend replacement and general history management remain work. |
| Task, resources, spending | Steps, whole-task duration, concurrency, retries/timeouts; requested/measured CPU/GPU/memory; token/cost accounting and native enforceable spending cap. | ADR0084 exposes step/per-command caps. Native resource foundations and a development Meta guard exist. Whole-task/concurrency/product-wide accounting and general controls remain work. |
| Product subagents | Explicit delegation enablement; child models/tools/concurrency/inherited budgets and separate workspace/worktree policy. | Auxiliary delegated reasoning is a bounded text call, not a child Agent. Product delegation remains work. Development Codex/Muse workers are not evidence of delivery. |
| Schedules and notifications | Timezone, pause/resume, missed-run behavior, completion/failure/attention events, quiet hours/destination and per-automation permissions/budget. | General native scheduling/notification owners and their editors remain work. No external notification is implied by opening Options. |
| Checkpoints and undo | Audit existing filesystem behavior; automatic checkpoints/retention, changes preview and reviewable workspace restore. | Harness/skill rollback does not establish undo for arbitrary task edits. Audit and native filesystem checkpoint/restore remain work. |
| Scopes and profiles | Defaults plus workspace/current-session overrides, documented precedence, effective/source labels, section reset preserving unrelated data. | Presentation, voice, model and execution settings retain distinct owners/lifetimes. General override/profile/reset model remains work; do not silently merge their stores. |
| Advanced development | Native-backed language servers, formatters, watcher exclusions and hooks, separated from everyday Converse settings. | General development configuration and hook contracts remain work. |

## Delivery rules and continuing work

- Keep the traditional embedded Options workspace. No navigation back to Home
  bubbles, duplicate category grid, fake provider login or decorative switch.
- Implement missing native owners where needed. Until then, document the gap;
  do not relabel it as delivered because a read-only card exists.
- Preserve source, exact harness/model/provider identity, capabilities, scopes,
  revisions, idempotent receipts and uncertain outcomes. Show saved and active
  values and the actual restart/new-task/service boundary.
- Preserve the cumulative **$10 paid-test budget**, including earlier uncertain
  reservations. Do not reset accounting or imply approximate estimates are bills.
- Continue LFM3 Omni, Gemma 4 12B and suitable OpenRouter multimodal qualification
  with Decisions, audio/video/camera and the applicable Muse Transcribe/Meta
  image/SAM tooling. Existing synthetic evidence and failed gates remain recorded
  in ADR0083; they do not prove physical camera or natural conversation quality.
- Keep harness customization, auxiliary overrides, Projects, Generated Documents,
  Generated Media and slash-command work in scope. Preserve already delivered
  features and link remaining gates rather than starting duplicate work.
- Keep repo contracts, inventory, implementation status, Penpot handoff and
  GitHub tracking aligned as each slice becomes reviewable. Preserve the primary
  checkout's user edits and unrelated running services.

## Current increment

ADR0084 and the owner Options API implement native execution preferences plus
the Permissions, Workspace and Execution editors. This is the first increment
of #208; none of the twelve broad acceptance groups is closed by that increment.
Check the associated PR for checks actually run and any remaining failures.

Subsequent increments add owner note editing and protected provider catalog
(ADR0085/0086), then exact Agent catalog selection (ADR0089). ADR0092 addresses
approval expiry before expanding scoped Ask rules. None closes an acceptance
group; the remaining obligations stay in the table above.

[ADR0096](adr/0096-scoped-native-tool-permissions.md) is merged in #215:
four native scopes for the exact `read_file`, `write_file` and
`shell` tools, independent action admission, protected CAS/receipt storage and
an embedded editor. It does not establish blanket filesystem-read protection,
individual MCP/plugin or scoped network rules, active-grant inventory or the
remaining full permission acceptance. See the PR for actual verification and
preview promotion; source presence alone does not close these obligations.

[ADR0091](adr/0091-converse-text-model-settings.md) adds the next independently
owned role editor: Converse text, exact eligible catalog generations/reasoning,
settled-operation CAS, captured stream leases and pins, and receipt-first UI
recovery. Decisions, STT/TTS, ordered fallback and real multimodal qualification
remain incomplete. This increment is merged in #220; all five CI jobs passed,
and the authenticated 8808 preview exposes the embedded Converse text editor.

[ADR0093](adr/0093-auxiliary-fallback-routes.md) records native ordered routes
for the five auxiliary text purposes: exact task-captured catalog/credential
generations, narrow pre-generation advancement, actual cleanup ownership, and
closed attempt provenance. Native and embedded route-editor source is merged in
#221 after all five CI jobs passed; the authenticated 8808 preview loads its
metadata and editor. Primary and
other role/modality fallbacks remain work.

[ADR0094](adr/0094-native-and-mcp-permission-targets.md) adds native Search and
exact MCP target rules while preserving the legacy three-tool protocol. Passive
inventory, stale-binding refusal, and one-use actor-queue admission are merged
in #221 after all five CI jobs passed. The target owner API/editor is implemented;
its source review and metadata inspection do not qualify actual external MCP
execution. Neither
increment closes an acceptance group, adds a task network firewall, or replaces
the outstanding obligations above.

[ADR0097](adr/0097-workspace-owner-selection.md) adds explicit native root and
read-only-folder selection, saved/active separation, immutable runtime guards,
captured root identity, actual-operation retirement and an embedded editor.
This increment is merged in #222 after all five CI jobs passed and is present
in the authenticated 8808 preview. Live isolation qualification remains separate.
Historical unpinned outputs remain readable with an
unknown root rather than being resolved through today's workspace. No actual
workspace has been changed by implementing this increment.

Independent Decisions model selection is merged in #223 (ADR0098) after all five
CI jobs passed, and its embedded editor was visually reviewed on 8808.
It retains classifier ownership and exact script/output provenance without
adding conversational reasoning or speech settings. Agent model authority
recovery is merged in #224 after all five CI jobs passed: an existing empty/truncated
database must not mint another host-default selection. The current preview is
frozen at the qualified #230 source and includes #224's recovery follow-up,
#229 worker-settlement metadata and independent STT/TTS editors. Consult their CI and preview records; neither increment closes an
acceptance group. Preview classifiers are metadata-only synthetic fixtures;
this browser review does not qualify actual Decisions inference.

[ADR0099](adr/0099-independent-stt-model-settings.md) implements an independent
STT owner, exact Meta transcription declarations, embedded Audio controls and
server-captured transcript provenance. Native and Flutter source reviews,
formatting/static analysis and schema determinism passed; all five CI jobs
passed at c49d37584684ed3fa4196b8058086fe31c512220 and PR225 is merged.
The 8808 preview serves this source with no speech worker installed; host STT
is truthfully unavailable and no transcription was called. Unresolved local-worker admissions remain
durable across restart and block inference/model changes until positive native
settlement can be established. Supported positive local Whisper reconciliation is merged in PR229/ADR0100
after all five CI jobs passed; TTS selection is ADR0101 below. Their CI/preview and real
backend/device qualification remain separate work. No paid calls were made.

[ADR0101](adr/0101-independent-tts-model-settings.md) adds independent TTS
selection through Audio options: the existing host interface or exact Kokoro
OpenRouter catalog generation with its fixed supported voice. It reuses the
existing native budget path and unchanged ceiling/reservations, captures
synthesis/frame/delivery provenance, and owns actual producer, playback and
cleanup through cancellation. Native and Flutter source review/static analysis
passed; all five CI jobs passed at 3d8357fcb8c3fea3b4d8996ed08ac00ce538d557
and PR230 is merged. The combined 8808 preview passed passive native API and
browser review with separate unavailable host STT/TTS declarations; no model,
worker, budget or device was exercised. Real model qualification remains work. Positive worker reconciliation, local voice reconfiguration
and unified product-wide spending administration remain work. It creates no
fresh allowance, performs no automatic synthesis and closes no acceptance group.

[ADR0102](adr/0102-installed-capability-administration.md) adds protected native
tool availability independently of permissions, complete installed skill/plugin
inventory and exact native lifecycle actions. Embedded Tools, Skills and Plugins
pages preserve drafts and each original uncertain operation, including responsive
Disable during evaluation. Source and independent native/Flutter review are
complete; final main integration, CI and preview remain pending. Legacy lifecycle
mutation cannot bypass the configured new owner. Installation, update, removal,
bound scope editing and positive unresolved-activation reconciliation remain work.

[ADR0104](adr/0104-context-observations.md) adds process-local, immutable metadata
from already measured native prompt preparation and exact Agent export admission.
The embedded Context page reads each role independently and opens the existing
provider-generation editor for supported response-reserve changes. It makes no
extra tokenizer/model call, does not delete omitted transcript history, and
does not implement automatic compaction or retention. Source review, schema
export, static analysis and an offline web build are complete; CI and preview
qualification remain pending.

[ADR0103](adr/0103-active-authorizations-revocation.md) adds embedded Permissions
authorization activity. Broad service configuration is distinct from exact
requested/approved-unspent/used/expired/revoked permits. Owner revocation
competes with spend through native CAS and cannot cancel admitted work. Exact
positive historical Session proof can recover an original receipt without
recreating authority. Native/Flutter independent review and static checks are
complete; CI and preview qualification remain pending.
