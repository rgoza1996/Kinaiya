# Kinaiya options: product and design handoff

Prepared for #126 on 2026-10-29. This catalog complements Roadmap.md and
clarification.md; it does not mark planned capabilities as implemented. The
owner-supplied Penpot design now supplies visual language and composition
([ADR0054](adr/0054-penpot-owner-interface.md)). Stable option
identities, ownership, validation and scope survive a layout change.

The persistent [Options workspace](adr/0077-options-workspace-and-harness-models.md)
embeds working editors. Models, Providers and Harness Customization are separate
sections. Native Agent defaults and per-harness overrides select configured
profiles and declared reasoning levels for new tasks; running work retains its
model pin. Converse text selection is independently owned by its native role
settings ([ADR0091](adr/0091-converse-text-model-settings.md)). Other runtime
model roles and the remaining
planned controls below still need their respective native owners.

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
| Agent model settings | Agent default; per-harness inheritance or model/reasoning override using host profiles or exact saved provider versions. | Protected `agent-models.sqlite`, revision-bound receipts; applies to new tasks. | Owner Options API and native task acceptance; accepted tasks retain model pins and fresh catalog adapters (ADR0089). |
| Auxiliary models | Separate analysis, compression, title generation, delegated reasoning and harness design choices; global defaults and per-harness overrides. Inherit primary model unless a purpose is configured. | Same native model settings revision/receipt store; accepted tasks retain available purpose pins. | Measured text-completion adapters only. Explicit `consult_model` side calls; no implicit fallback, protected-history replacement or new tool authority. See [ADR0082](adr/0082-task-pinned-auxiliary-models.md). |
| Providers catalog | Typed Meta, native LM Studio, compatible-text and Laya declarations; protected credential import/reference/rotation and tombstone removal. | Protected shared `providers.sqlite`, immutable generations and CAS/exact receipts. | Owner-only service APIs and embedded forms (ADR0086). Explicit Agent catalog selection (ADR0089) is separate from saving declarations. Other role application, readiness and fallback remain separate. |
| Converse model setup | Read-only configured conversation/STT/TTS/Decisions roles, accepted transport and reasoning. Optional Converse view starts on. | Host declarations are not load receipts; only `show_converse_view` is saved in local presentation storage. | Authenticated read-only setup API; Save merges local visibility without toggling runtime enabled or changing providers/consent. |
| Converse text model | Host selection or exact eligible LM Studio/Meta catalog generation and supported reasoning. Selected unavailable values remain visible; readiness stays unknown. | Protected `converse-model.sqlite`, revision/state CAS and exact receipts; no active admission, inference, publication or cleanup may remain at a new save. | Independent owner Converse API and embedded Models editor (ADR0091). Future typed/window admissions capture their actual provider lease/pin; STT/TTS/Decisions and capture remain separate. |
| Speech-to-text model | Existing host STT or exact eligible buffered Meta STT catalog generation. Undeclared host identity stays unknown. | Protected `converse-stt-model.sqlite`, bound original-request CAS/lookup, actual lease/cleanup and durable unresolved native admissions. | Independent Converse Audio owner/editor (ADR0099), source under review. No capture/probe on inspection or Save; TTS remains independent. Unresolved local-worker admission blocks new inference/model changes until native settlement proof exists. |
| Service connection & privacy | Session/Agent/Converse URLs, owner login, disclosure selection. Default URLs are loopback ports 8760/8761/8762; disclosure starts local-only. | Current connection; credentials and profile remain in RAM. | SetupDialog and native authentication; explicit Connect replaces the client. |
| Appearance & character | Pack import/export/reset, pack palette or explicit colors; character, captions and speech mouth movement on; reduced motion off by default. Local or device reduced motion keeps the mouth still while preserving its saved preference. Import/export is currently web-only. | App presentation only. Validated web IndexedDB record or private Android/iOS snapshot; other platforms keep session-only settings. | PresentationController; immediate appearance update with visible persistence errors. |
| Voice options | Web Dynamic VAD (on), balanced sensitivity, room recalibration; fallback-only speech confirmation (300 ms), interrupt confirmation (220 ms), clip-ending silence (1.5 s; choices 0.8/1.5/2.5). Live windows remain continuous and Decisions chooses when to wait, respond, or interrupt. | Mounted Converse view, session-only; separate from inference/provider settings and appearance export. | Current CharacterView/audio owner; explicit changes require an acknowledged bridge profile. Opening/closing Options never changes capture. See [ADR0079](adr/0079-adaptive-vad-and-top-aligned-options.md). |
| Saved Converse notes | Browse/inspect, transcript-pinned capture, immutable revised notes, explicit forget, namespace access, select 1–8 active notes, Use/Stop. New namespaces start disabled; runtime use starts off after restart. | Exact Converse session/thread/epoch. Stored namespace and process-local selected use are separate; pending content writes remain bounded RAM only. | Session memory store plus Converse recall runtime; observed revisions/runtime identity guard writes; historical exact receipts (ADR0085). |
| Agent controls | Current registered model/profile, permissions/YOLO, resources, skills and plugins commands. | Agent scope; many choices are process-local or host-configured. | Existing command registry/supervisor. The chooser fills a draft; the user still sends it. Expansion/profile switching requires settled work. |
| Agent execution settings | Native file-write/command Ask/Allow/Deny, tool network access, step limit, per-command timeout and relative shell directory. Active root, folder guards, mandatory sandbox and public environment inspection. | Protected `agent-runtime-settings.sqlite`, revision/state-bound receipts, settled service boundary. YOLO remains service-instance only. | Embedded Permissions, Workspace and Execution pages; [ADR0084](adr/0084-owner-execution-settings.md). Broader per-tool/scoped permissions, root relocation and task/concurrency/hardware limits remain incomplete. |
| MCP connections | Inspect/configure/test/enable/disable/reconnect/remove supported servers. | Agent owner configuration; native credential references and revisions. | Existing MCP management/native policy. A configured server is not a proven connection or a permission grant. |

Options navigation reads UI and authenticated host metadata. Opening, searching
or dismissing it does not connect, write settings, create a session, start/stop
capture, execute a command or call a model. Explicit controls retain their native
owners and acknowledgement rules. Agent model defaults/overrides save through
the revision-bound owner API; a UI choice cannot authorize disclosure or expand
permissions. Missing connection, registry and feature support remain visible.

## Full harness option groups

The names in this section are stable product identifiers for design/planning,
not a new serialized config schema. Do not write them into an existing native
configuration until its versioned contract supports them.

### Models and providers

| Option family | Type/default policy | Scope, persistence and authority | Status |
| --- | --- | --- | --- |
| `models.agent`, `models.converse`, `models.decisions` | Configured provider/profile/model reference for each runtime; host value, no universal model default. | Independent runtime host configuration; owner profile choice, native resource and disclosure checks. Agent profile changes apply at a settled task boundary. | Agent: App defaults/per-harness profile editor for new tasks. Converse and Decisions: Host. |
| `models.stt` | Existing host or exact buffered `meta_stt` catalog generation; configuration is not quality/readiness proof. | Independent Converse owner, settled operations, exact receipts and captured provenance (ADR0099). | Native and embedded source under review; local-worker reconciliation and wider backend selection remain work. |
| `models.tts` | Independent speech-provider/model references; supported languages, formats and hardware are capability data. | Converse voice pipeline; host restart or explicit resource handoff as supported. | Host; mutable picker remains work. |
| `providers.endpoint`, `protocol`, `credential_ref` | Validated provider-specific endpoint/protocol and protected immutable credential generation. No raw secret in URLs, exported preferences or model context. | Protected native catalog; credentials remain outside generated tools and public metadata. Existing role owners still govern application. | App catalog/credential editor (ADR0086); Agent exact-generation selection (ADR0089). Other role application remains unfinished. |
| `models.reasoning`, `sampling`, `output_limit` | Only fields advertised by the selected model: effort/budget, temperature/top-p/top-k/seed or output limit where supported. No assumed common range. | Per provider/profile, optionally task override after an explicit contract; show effective limits. | Agent: App reasoning overrides from host-declared profile levels. Other sampling/limits remain host-managed or planned. |
| `models.fallbacks`, `auxiliary_routes` | Five auxiliary text routes above are implemented. Automatic ordered failover and additional modality-specific roles remain planned. Default is no implicit new route or cloud fallback. | Native routing, disclosure and resource policy; replacement is an explicit owner action. | Auxiliary text controls implemented; broader routing remains planned. |

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
| `agent.workspace`, `working_directory` | Actual approved root, protected/read-only folders, existing relative shell directory. | Host root; owner directory edit at settled service boundary, persisted. Native containment/symlink/isolation checks. | Workspace inspection/directory editor implemented (ADR0084); root selection/worktrees remain planned. |
| `agent.permissions` | Native file-write/command Ask/Allow/Deny, network access and process-local YOLO. File-write Deny also mounts the command workspace read-only. Approval behavior and isolation are distinct. | Trusted supervisor/policy broker; revision/state-bound edits require settled work. Saved values and actual effective values are separate. | Service-level native editor implemented (ADR0084). Read/per-tool rules and task/session/workspace/default scopes remain incomplete. |
| `agent.approvals` | Pending actions, approve/deny and scope of a granted decision. Changing UI mode is not a retroactive approval. | Native owner action. Model risk assessment, if later added, is advisory. | Existing task approval controls; unified defaults editor planned. |
| `agent.sandbox`, `network`, `environment` | Mandatory Bubblewrap, read/write mount state, public fixed environment and network namespace. Executable detection is not isolation proof. | Native execution boundary; owner tool-network edit, no secrets or arbitrary mounts/environment in this slice. | Inspection/network editor implemented (ADR0084); alternate backends and broader sandbox/environment controls remain incomplete. |
| `agent.step_limit`, `deadline`, `concurrency` | Native maximum steps 1–2000 (host default 12), command cap 1–120 whole seconds. A command cap is not a whole-task deadline or task-concurrency cap. | Owner settled-service edits, persisted; accepted/unsettled work blocks changes. | Step/command editor implemented (ADR0084). Whole-task deadline, concurrency admission and hardware controls remain incomplete. |
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
| `memory.converse.capture`, `forget`, `backend` | Owner-curated transcript-pinned capture, immutable revised notes and explicit forget; future replacement backend selection. No automatic extraction or ranking implied. | Native owner API and immutable provenance; never Agent or Decisions memory. | App capture/revision/forget and exact historical receipt recovery (ADR0085); replacement backend editor planned. |
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
the earlier scene concept requests a Call/Tell-only voice bar. Current Home has
Talk with Kinaiya, Setup and separate Sleep/Wake controls after the Home/optional
Converse split; aligning that placement with the earlier intent remains pending.
Global active capture and playback stops remain visible when needed; text input is available through
Converse chat in the header, and supporting options remain accessible in chat
and Settings.
The Penpot design can supply additional panels in its own style when it lacks a
particular option. Do not invent a separate visual system for omitted settings.

The October 31 owner-confirmed `/goal` covers all twelve groups from the earlier
harness comparison, including checkpoints, scopes and development tooling, plus
ongoing harness/multimodal work. See the [delivery plan](options-delivery-plan.md)
and [#208](https://github.com/rgoza1996/Kinaiya/issues/208). A delivered increment
does not close the full inventory.

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

## Technical references

These technical sources were consulted on 2026-10-29. They are historical inputs
for configuration and compatibility work, not Kinaiya defaults, installed-binary
qualification or a feature-parity promise. Product descriptions use direct
capability names such as model selection, permissions, context, sessions and
standard agent controls.

- **Model, security and tool-server configuration references:**
  [Hermes models](https://hermes-agent.nousresearch.com/docs/user-guide/configuring-models),
  [security](https://hermes-agent.nousresearch.com/docs/user-guide/security),
  [MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp),
  [configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration).
- **Workspace, sandbox, session and automation references:**
  [OpenClaw models](https://docs.openclaw.ai/gateway/config-agents/models),
  [sandbox](https://docs.openclaw.ai/gateway/config-agents/sandbox),
  [tool policy](https://docs.openclaw.ai/gateway/config-tools/tool-policy),
  [sessions](https://docs.openclaw.ai/gateway/config-agents/sessions),
  [automation](https://docs.openclaw.ai/gateway/config-automation).
- **Application and execution configuration references:**
  [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
  [app settings](https://learn.chatgpt.com/docs/reference/settings).
- **Provider, session, usage and telemetry references:**
  [MiniMax configuration](https://agent.minimax.io/docs/cli/configuration),
  [security](https://agent.minimax.io/docs/cli/security),
  [features](https://agent.minimax.io/docs/cli/features),
  [telemetry source](https://github.com/MiniMax-AI/minimax-code/blob/main/docs/telemetry.md).
- **CLI permission, extension and context references:**
  [Muse Code configuration](https://dev.meta.ai/docs/muse-code/configuration),
  [permissions](https://dev.meta.ai/docs/muse-code/permissions),
  [extensions](https://dev.meta.ai/docs/muse-code/extending),
  [interactive controls](https://dev.meta.ai/docs/muse-code/interactive).

## Intelligent views

Converse's saved **Interactive views** switch enables native rich artifacts or a readable summary/data fallback. It changes presentation only. Each result offers Pin, Close and a Results chooser to reopen retained publications; transcript collapse retains history and its owner. Table/diagram/choice/number controls persist validated view state. Ask buttons prefill the normal composer without submitting it. Provider selection, cloud disclosure, permission approval, Agent availability and ordinary Call/Tell/Stop controls keep their separate owners. See [ADR0068](adr/0068-intelligent-converse-artifacts.md).
