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
| Models, providers, fallback routes | Independent Agent, Converse, Decisions, STT and TTS choices; supported reasoning/input/sampling controls; endpoint/local-model and protected credentials; explicit fallback order with private/cloud disclosure approval. | Agent defaults, exact-harness and five auxiliary text-purpose overrides exist (ADR0077/0082). ADR0086 adds provider/credential-generation catalog forms; ADR0089 applies exact catalog versions through fresh Agent task adapters. Other role editors and ordered failover remain incomplete. Direct audio requires the actual transport and qualification. |
| Precise permissions | Allow/Ask/Deny for reads, writes, commands, network and individual tools; task/session/workspace/default scopes; active grants and approvals; approval behavior separate from actual sandbox access. | Native scoped action approvals exist. ADR0084 adds service-level native file-write/command modes, network and process-local YOLO inspection/editing. Read/per-tool rules and additional scopes remain work; the [proposed permission rollout](options-permission-plan.md) records the native admission, scope and approval-expiry follow-on without claiming implementation. |
| Workspace and execution | Select/inspect the active root and readable/writable/protected folders; actual isolation state/network; supported execution backend and optional isolated Git worktree. | Native containment/Bubblewrap exist. ADR0084 adds root/restriction/environment inspection and a bounded relative starting directory. Root relocation, alternate backends and coding worktrees remain work. |
| Tools, skills, MCP, plugins | Individual/group enablement; MCP tool selection; source/version and active status. Distinguish installed, connected, enabled and authorized. | MCP management exists. General native tool selection and a unified skills/plugin inspection/editor remain work. |
| Context and history | Actual used/available model context, response reserve, supported compaction thresholds and model/provider provenance. Distinguish retained transcript from reduced prompt context. | Measured prompt admission and explicit auxiliary compression calls exist. Automatic compaction, usage and general limit editors remain work. |
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

[ADR0090](adr/0090-scoped-native-tool-permissions.md) is the next implementation
under review: four native scopes for the exact `read_file`, `write_file` and
`shell` tools, independent action admission, protected CAS/receipt storage and
an embedded editor. It does not establish blanket filesystem-read protection,
individual MCP/plugin or scoped network rules, active-grant inventory or the
remaining full permission acceptance. See the PR for actual verification and
preview promotion; source presence alone does not close these obligations.
