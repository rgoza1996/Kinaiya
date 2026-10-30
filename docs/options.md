# Kinaiya options: product and design handoff

Prepared for #126 on 2026-10-29. This catalog complements Roadmap.md and
clarification.md; it does not mark planned capabilities as implemented. The
owner-supplied Penpot design now supplies visual language and composition
([ADR0054](adr/0054-penpot-owner-interface.md)). Stable option
identities, ownership, validation and scope survive a layout change.

The responsive Options hub groups working controls and forwards to their existing
owners. The larger catalog below is preparation for the full harness, not a set
of simulated switches. Backend capabilities determine which editors can exist.

## Reading the catalog

- **App**: a working owner UI control. Its current owner still performs validation.
- **Host**: implemented native configuration/commands/API, without a general app editor.
- **Planned**: needs a backend/capability contract and acceptance tests before an editor.
- **Session** means the exact session/thread/epoch, not every conversation.
- Defaults are current only when explicitly identified as such. “Host value”
  means the effective backend value, not a newly proposed Kinaiya default.

Every implemented editor should expose its effective value, source/default or
override, applicable scope, persistence and change boundary. Useful change
labels are **This app**, **This conversation**, **New tasks**, and **Host restart**.
Unknown or unavailable values stay unknown; a failed read is not an empty value.
Pending writes, conflicts and unconfirmed acknowledgments retain their actual
state and cannot trigger silent retries or success notices.

## Working settings destinations

| Destination | Current options/defaults | Scope and storage | Owner and change boundary |
| --- | --- | --- | --- |
| Models & conversation | Read-only configured conversation/STT/TTS/Decisions roles, accepted transport and reasoning. Optional Converse view starts on. | Host declarations are not load receipts; only `show_converse_view` is saved in local presentation storage. | Authenticated read-only setup API; Save merges local visibility without toggling runtime enabled or changing providers/consent. |
| Service connection & privacy | Session/Agent/Converse URLs, owner login, disclosure selection. Default URLs are loopback ports 8760/8761/8762; disclosure starts local-only. | Current connection; credentials and profile remain in RAM. | SetupDialog and native authentication; explicit Connect replaces the client. |
| Appearance & character | Pack import/export/reset, pack palette or explicit colors; character and captions on; reduced motion off by default. Import/export is currently web-only. | App presentation only. Validated web IndexedDB record or private Android/iOS snapshot; other platforms keep session-only settings. | PresentationController; immediate appearance update with visible persistence errors. |
| Voice options | Fallback clip-ending silence: 0.8/1.5/2.5 seconds, initially 1.5, used only when streaming windows are unavailable. Live window mode streams audio continuously and Decisions chooses when to wait, respond, or interrupt; no silence timer ends the turn. | Mounted Converse view; not an inference/provider setting or a persisted global audio profile. | Current CharacterView/audio owner; only explicit changes configure fallback VAD. |
| Saved Converse notes | Browse/inspect existing notes, namespace access, select 1–8 active notes, Use/Stop. New namespaces start disabled; runtime use starts off after restart. | Exact Converse session/thread/epoch. Stored namespace and process-local selected use are separate. | Session memory store plus Converse recall runtime; observed revisions/runtime identity guard writes. |
| Agent controls | Current registered model/profile, permissions/YOLO, resources, skills and plugins commands. | Agent scope; many choices are process-local or host-configured. | Existing command registry/supervisor. The chooser fills a draft; the user still sends it. Expansion/profile switching requires settled work. |
| MCP connections | Inspect/configure/test/enable/disable/reconnect/remove supported servers. | Agent owner configuration; native credential references and revisions. | Existing MCP management/native policy. A configured server is not a proven connection or a permission grant. |

The hub itself reads only UI metadata and returns a destination. Opening,
searching or dismissing it does not connect, write settings, create a session,
start/stop capture, execute an Agent command or call a model. Existing shortcuts
remain available. Missing connection/registry/feature support is shown honestly.

## Full harness option groups

The names in this section are stable product identifiers for design/planning,
not a new serialized config schema. Do not write them into an existing native
configuration until its versioned contract supports them.

### Models and providers

| Option family | Type/default policy | Scope, persistence and authority | Status |
| --- | --- | --- | --- |
| `models.agent`, `models.converse`, `models.decisions` | Configured provider/profile/model reference for each runtime; host value, no universal model default. | Independent runtime host configuration; owner profile choice, native resource and disclosure checks. Agent profile changes apply at a settled task boundary. | Host; Agent has command selection, no universal app model editor. |
| `models.stt`, `models.tts` | Independent speech-provider/model references; supported languages, formats and hardware are capability data. | Converse voice pipeline; host restart or explicit resource handoff as supported. | Host; app shows configured declarations, mutable picker planned. |
| `providers.endpoint`, `protocol`, `credential_ref` | Validated provider-specific endpoint/protocol and protected credential reference. No raw secret in URLs, exported preferences or model context. | Host/provider; credentials remain outside generated tools and public metadata. | Host; app provisioning/editor planned. |
| `models.reasoning`, `sampling`, `output_limit` | Only fields advertised by the selected model: effort/budget, temperature/top-p/top-k/seed or output limit where supported. No assumed common range. | Per provider/profile, optionally task override after an explicit contract; show effective limits. | Some host limits exist; generic editor/overrides planned. |
| `models.fallbacks`, `auxiliary_routes` | Ordered compatible choices and role-specific routes. Default is no implicit new route or cloud fallback. | Native routing, disclosure and resource policy; replacement is an explicit owner action. | Planned as a unified surface. |

The proposed tiny local Converse profile targets **8 GB for STT + dialogue + TTS
together**, as requested by the owner. This is a design target, not a measured
fit or a selected winning model. Model changes and human latency/feel testing
remain separate from preparing these options.

### Voice, personality and conversation

| Option family | Type/default policy | Scope, persistence and authority | Status |
| --- | --- | --- | --- |
| `voice.capture_mode`, `end_silence` | Explicit Call/live listening or Tell/one message; no automatic microphone arming. Fallback clip silence choices above; live window timing is Decisions-controlled, not a silence timer. | Current Converse/audio owner; retain drafts and actual capture/playback state. | App. |
| `voice.input_device`, `output_device`, `volume`, `language` | Device/model capability choices with an explicit default-device option; unavailable devices do not appear connected. | App/platform audio permission and current stream ownership. | Unified editors planned. |
| `voice.tts_voice`, `rate`, `prosody` | Supported voice/style controls only; provider-specific ranges and preview cost disclosed before a preview. | Converse TTS profile; no automatic test synthesis on open. | Host/provider-specific configuration; app controls planned. |
| `voice.interruption`, `vad`, `window_limits` | Supported interruption/VAD and retained-window bounds; preserve trusted cancellation and delivery evidence. | Native Converse/speech pipeline. A stop request is not proof playback/capture ended. | Existing runtime behavior; advanced app editor planned. |
| `converse.personality`, `instructions` | Character/personality and owner instructions, separate from task execution policy. | Converse only; editable text cannot change protected authorization or completion criteria. | Personality exists in Converse; unified editor planned. |
| `conversation.followups`, `routing_preferences` | Explicit queue/steer/interrupt preferences where native contracts support them. | Decisions routing plus trusted Agent acceptance; no permission or success authority. | Runtime routing exists; preference editor planned. |

### Agent execution, workspaces and permissions

| Option family | Type/default policy | Scope, persistence and authority | Status |
| --- | --- | --- | --- |
| `agent.workspace`, `working_directory` | Approved workspace/path reference; show actual effective root, not a guessed directory. | Host/task; native path, symlink, protected-root and OS-isolation checks. | Host; workspace picker planned. |
| `agent.permissions` | Inspect precise write/shell/network/tool grants and current YOLO mode. Approval behavior and isolation are distinct controls. | Trusted supervisor/policy broker; expanding authority waits for settled work. | Host/Agent commands; richer editor planned. |
| `agent.approvals` | Pending actions, approve/deny and scope of a granted decision. Changing UI mode is not a retroactive approval. | Native owner action. Model risk assessment, if later added, is advisory. | Existing task approval controls; unified defaults editor planned. |
| `agent.sandbox`, `network`, `environment` | Execution backend, read/write roots, egress, mounts and allowed environment references. No import of another harness's sandbox-off default. | Native execution boundary; user/model text cannot broaden it. | Existing native policy/isolation; detailed app editor planned. |
| `agent.step_limit`, `deadline`, `concurrency` | Typed finite execution bounds with native validation. Current capabilities report max_steps, initially 12; this is not a live UI setter. A per-tool timeout is not a whole-task deadline or a global task-concurrency cap. | Host/new tasks; active task changes require a versioned native operation. | Existing step/per-tool bounds. Whole-task deadline, task-concurrency admission and generic editors planned. |
| `agent.subagents` | Allowed model/provider/tools, concurrency and inherited budget/grants; no greater authority than the parent permits. | A future native delegation contract and policy; keep development Muse/Codex workers distinct from product Agent workers. | Planned. Current bounded worker/tool execution is infrastructure, not a product subagent facility. |
| `agent.instructions`, `harness_versions` | Owner/project instructions, installed extension/version, evaluation/promotion/rollback status. Protected core, goals and evaluator remain immutable to generated code. | Trusted configuration and native harness manager; no free-form setting may disable evidence requirements. | Existing native mechanisms; unified editor planned. |

### Tools, integrations and external access

| Option family | Type/default policy | Scope, persistence and authority | Status |
| --- | --- | --- | --- |
| `tools.enabled`, `allowlist`, `timeouts` | Effective capabilities and per-tool restrictions; denied/unavailable tools remain distinguished. | Agent/tool broker; availability is not authorization to execute. | Host/registry; unified editor planned. |
| `skills.sources`, `installed`, `enabled` | Source/version/trust/disclosure, installed status and explicit activation. | Owner installation plus native evaluation/policy; no silent remote install. | Native skills/Agent commands; dedicated editor planned. |
| `mcp.servers`, `allowed_tools`, `credentials` | Supported transports, tool filters, protected credential references and actual connection state. | Existing owner MCP controller; retain revision/uncertainty rules. | App plus host. |
| `plugins.installed`, `promotion`, `rollback` | Installed versions, evaluation evidence and approved active version. | Native protected plugin/harness boundary. | Native mechanisms; dedicated editor planned. |
| `integrations.accounts`, `browser`, `computer_access` | Named connection/host/device grants with explicit recipient and scope. | Connected service plus native/platform authorization; keep separate from model authentication. | Capability-specific; general app management planned. |

### Memory, transcript and privacy

| Option family | Type/default policy | Scope, persistence and authority | Status |
| --- | --- | --- | --- |
| `memory.converse.access`, `use`, `selection` | Separate stored access, process-local use and selected active notes. Default off behavior above. | Converse-only optional replaceable backend; native source/currentness/context-budget checks. | App for existing notes. |
| `memory.converse.capture`, `forget`, `backend` | Owner-curated capture/forget and future replacement backend selection; no automatic extraction or ranking implied. | Native owner API and immutable provenance; never Agent or Decisions memory. | Capture/forget API; creation/deletion/replacement UI planned. |
| `transcript.retention`, `archive`, `export`, `delete` | Explicit retention/export/deletion policy; show storage and effect before destructive actions. No borrowed 90-day default. | Authoritative Session journal; distinct from forgetting a DAG node or resetting appearance. | Journal exists; general retention/export UI planned. |
| `audio.retention`, `diagnostic_retention` | Independent raw audio, transcript and diagnostic policies; no assumed shared lifetime. | Voice/journal/diagnostic owners and explicit privacy policy. | Unified controls planned. |
| `privacy.disclosure` | Local-only, public/synthetic cloud, or explicitly authorized private cloud. Meta Contributor excludes private input. | Per-input captured scope plus native provider checks; profile change cannot silently relabel an existing draft. | App. |
| `privacy.telemetry`, `diagnostics`, `secret_storage` | Separate consent for usage, performance and error data if implemented. Secure credential references; redacted exports. | Native/app collection and storage policy. This settings feature adds no telemetry. | General controls planned. |

### Resources, context and cost

| Option family | Type/default policy | Scope, persistence and authority | Status |
| --- | --- | --- | --- |
| `resources.device`, `resident_models`, `priority`, `handoff` | Effective residency and explicit Converse/Agent resource handoff, with stop/permissions always reachable. | Native resource broker; requested unload is not confirmed release. | Native resource controls; richer app panel planned. |
| `resources.ram_vram_budget`, `workers` | Requested limits and measured/admitted usage shown separately. No claim that quantization or a token limit enforces physical memory. | Native admission/worker ownership; local 8 GB target remains unmeasured. | Some native bounds; unified hardware profile planned. |
| `context.input_limit`, `output_reserve`, `compaction` | Provider-compatible tokenizer/counting and explicit complete-prompt/output bounds; distinguish retained journal from lossy prompt projection. | Native provider/runtime; memory closure cannot be silently truncated to fit. | Native limits/counting paths; general editor planned. |
| `cost.hard_cap`, `estimates`, `usage`, `reset` | Monetary cap, token/step budgets, estimates, pending reservations and actual billing are different values. Unknown billing remains unknown. | Trusted admission/accounting across workers/providers; no UI-only “hard cap.” | Development Meta guard exists; product-wide accounting/editor planned. |

### Automation, notifications, mobile and app behavior

| Option family | Type/default policy | Scope, persistence and authority | Status |
| --- | --- | --- | --- |
| `automation.schedule`, `timezone`, `triggers`, `pause` | Explicit jobs/conditions, current timezone, enable/pause state and failure policy. | Trusted scheduler plus native task permissions; a schedule is not consent for every external action. | M7/unified app controls unfinished. |
| `notifications.events`, `destination`, `quiet_hours` | Completion/failure/attention preferences and explicit recipient/device. Unconfigured external destinations do not receive data. | App/platform permission plus native notification authority. | Unified controls planned. |
| `mobile.devices`, `pairing`, `revoke` | 120-second one-use pairing code, separate 24-hour device grant and current/expired/revoked/stale metadata. | Session companion service; owner token never goes to the phone. | Backend/device client; owner panel tracked in #125. |
| `mobile.calls`, `notes`, `audio` | Foreground invitation/end and literal retained note; answer means waiting for media. Media/device capabilities must be reported separately. | Scoped companion protocol and future call-media bridge. | Signaling/client partial; duplex audio/system calls/background delivery unfinished. |
| `app.appearance`, `accessibility` | Working palette/pack/captions/reduced motion above; future theme/density/font/contrast choices remain capability-backed preferences. | Presentation only; no secrets or inference settings in appearance storage. | App for current fields; expanded preferences planned. |
| `app.keyboard`, `send_behavior`, `language` | Searchable shortcuts, Enter/newline behavior and localization; use platform conventions and preserve drafts. | App preferences, with validated conflicts and accessible alternatives. | Unified editors planned. |
| `app.about`, `updates`, `diagnostics` | Actual build label/revision/platform/capability state. Update checks must not install or restart silently. | Build/host metadata; redacted diagnostics and explicit export. | Build information in hub; broader update/diagnostics UI planned. |

## Design and interaction rules

Keep everyday choices near their task and detailed controls in searchable
sections. Advanced controls should explain units, scope and unsupported values;
the main Converse bottom bar contains only Call and Tell. Global active capture
and playback stops remain visible when needed; text input is available through
Converse chat in the header, and supporting options remain accessible in chat
and Settings.
The Penpot design can supply additional panels in its own style when it lacks a
particular option. Do not invent a separate visual system for omitted settings.

Show saved versus effective settings and restart/next-task effects. Global,
workspace, session and task overrides need a documented precedence rule before
they can coexist. Reset restores the selected settings scope only; it does not
delete sessions, grants, notes or files. Export/import must be versioned,
bounded and redacted, with a reviewable diff before applying sensitive changes.
Never hide credentials in downloadable appearance/config backups.

Availability, identity and revision must be rechecked before writes. Preserve
the real owner state when a dialog closes, reconnects or loses a response. Keep
Stop/revoke/deny separate from inference readiness. Search and shortcuts cannot
bypass disabled/native-protected actions. Raw instructions, notes and external
metadata remain literal text rather than executable UI bindings.

## Primary-source comparison inputs

These are current rolling documentation, checked 2026-10-29, not installed-binary
qualification. Their patterns inform this catalog; their defaults are not
Kinaiya defaults.

- Hermes separates model routing, approval/security, tool-server policy and
  session/memory configuration. Its host execution, automatic writes and
  model-assisted approval behavior are not substitutes for Kinaiya's native
  grants. [Models](https://hermes-agent.nousresearch.com/docs/user-guide/configuring-models),
  [security](https://hermes-agent.nousresearch.com/docs/user-guide/security),
  [MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp),
  [configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration).
- OpenClaw separates per-agent models/workspaces, tool policies, sandboxing,
  sessions and automation. Its sandbox defaults and shared integration identity
  need independent isolation decisions here.
  [Models](https://docs.openclaw.ai/gateway/config-agents/models),
  [sandbox](https://docs.openclaw.ai/gateway/config-agents/sandbox),
  [tool policy](https://docs.openclaw.ai/gateway/config-tools/tool-policy),
  [sessions](https://docs.openclaw.ai/gateway/config-agents/sessions),
  [automation](https://docs.openclaw.ai/gateway/config-automation).
- Codex documents distinct model/reasoning, approval/sandbox, workspace,
  tool-server and concurrency configuration, while everyday app preferences
  include keyboard, notifications, appearance and personal instructions.
  [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference),
  [app settings](https://learn.chatgpt.com/docs/reference/settings).
- MiniMax Code separates provider/model variants, access/plan modes, context,
  session/usage and status presentation. Telemetry consent is separate from
  normal provider requests, account operations and submitted feedback.
  [Configuration](https://agent.minimax.io/docs/cli/configuration),
  [security](https://agent.minimax.io/docs/cli/security),
  [features](https://agent.minimax.io/docs/cli/features),
  [telemetry source](https://github.com/MiniMax-AI/minimax-code/blob/main/docs/telemetry.md).
- Muse Code CLI separates model/effort, approvals, filesystem/network/workspace
  trust, extensions, session retention and context controls. Rewinding a chat
  does not undo workspace files; execution limits are not a monetary cap.
  [Configuration](https://dev.meta.ai/docs/muse-code/configuration),
  [permissions](https://dev.meta.ai/docs/muse-code/permissions),
  [extensions](https://dev.meta.ai/docs/muse-code/extending),
  [interactive controls](https://dev.meta.ai/docs/muse-code/interactive).
