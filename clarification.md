# Kinaiya — Architectural Clarification: Autonomous Capability Expansion

**Status:** Architectural design intent  
**Scope:** Kinaiya Agent, Converse, Decisions, adaptive harnesses, executable extensions, skills, plugins, and external tools  
**Related specifications:** `Roadmap.md`, Sections 8, 10–12, 14, and 31

## 1. Fundamental Principle

**Kinaiya's capabilities are not limited to the native capabilities of its underlying large language model.**

Kinaiya is intended to be an extensible autonomous software agent, not merely an LLM with prompts, tools, or predefined workflows.

The language model provides reasoning, planning, software development, and orchestration. The actual capabilities available to the agent may be implemented by any suitable computational mechanism, including:

- Conventional executable programs and algorithms.
- Existing software packages, libraries, and command-line applications.
- Computer vision, signal processing, and other specialized computational pipelines.
- Specialized machine-learning models that are not LLMs.
- External services, APIs, MCP servers, and hardware interfaces.
- Agent-authored software, including newly generated tools, plugins, adapters, and processing pipelines.
- Combinations of these technologies.

**There is no requirement that an acquired capability be implemented by an LLM, require LLM inference during execution, or exist within the model's parameters.**

For example, an agent can learn to use a compiler without becoming a compiler. It can acquire computer-vision capabilities without requiring its language model to process every video frame.

This distinction is fundamental to Kinaiya's architecture.

## 2. Harness Optimization and Capability Expansion Are Distinct

Kinaiya pursues two related but separate forms of adaptation.

### 2.1 Harness Optimization — Improving How the Agent Works

Harness optimization concerns how an existing model performs its work.

Examples include improving context retrieval, planning algorithms, memory management, tool selection, delegation, error recovery, and model-specific execution strategies.

The objective is to improve performance, reliability, efficiency, or cost under controlled evaluation.

### 2.2 Capability Expansion — Improving What the Agent Can Do

Capability expansion concerns introducing functionality that was previously unavailable to the agent.

Examples include acquiring binary reverse-engineering capabilities, implementing a computer-vision pipeline, building a new parser, adding a simulation engine, or integrating a specialized scientific tool.

The agent may achieve this by discovering, installing, configuring, composing, modifying, or authoring executable software.

Capability expansion is **not restricted to improvements in LLM reasoning, model prompting, or harness configuration**.

An extension may be valuable because it enables a previously impossible operation, even if it produces no measurable improvement on the existing harness benchmark.

These two research objectives must be evaluated separately. A newly installed tool must not be misrepresented as a harness improvement under otherwise identical capabilities.

## 3. Autonomous Capability Acquisition

When Kinaiya encounters an authorized task requiring functionality it does not possess, it should be capable of investigating and developing an appropriate extension.

The intended workflow is:

1. **Identify the capability gap.** Determine which operations are unavailable and what functionality is required.
2. **Investigate available solutions.** Search for suitable existing libraries, tools, models, skills, plugins, and services before implementing equivalent functionality.
3. **Design an integration.** Choose an approach appropriate to the task, operating environment, resource budget, and available software.
4. **Construct the capability.** Install or integrate existing components, generate wrappers, or author new executable implementations where necessary.
5. **Validate independently.** Test functionality, correctness, security, compatibility, performance, and resource consumption against explicit acceptance criteria.
6. **Request authorization where required.** Installation, execution, new permissions, and promotion must respect the protected control plane.
7. **Register and activate.** Make the validated capability available through a versioned, discoverable, callable interface.
8. **Maintain the capability.** Preserve provenance and dependencies, monitor failures, and support updates, disablement, removal, and rollback.

Capability development may be initiated through a direct user request or through proactive identification of a missing dependency while pursuing an existing authorized objective.

Proactivity does not grant new permissions.

A failed experiment, unavailable dependency, or unvalidated implementation must be reported truthfully rather than represented as a successfully acquired capability.

## 4. Reference Examples

### 4.1 Reverse Engineering — REA and Ghidra

A reverse-engineering agent can use an orchestration layer such as REA with specialized software such as Ghidra.

Ghidra performs operations including disassembly, decompilation, and program analysis. The LLM can coordinate these operations, choose appropriate workflows, interpret findings, and request additional analysis.

The LLM does not itself need to implement a complete disassembler through language-model inference.

This demonstrates the distinction between possessing knowledge about an operation and possessing an executable capability that performs it.

### 4.2 Sign-Language Recognition

Suppose a user requests that Kinaiya acquire the ability to interpret sign language through a camera.

Kinaiya Agent could investigate and construct an extension incorporating:

- Camera capture and video processing.
- Hand, face, and body-pose tracking.
- Temporal gesture and movement analysis.
- Suitable pretrained or separately trained recognition models.
- Language-specific interpretation and uncertainty reporting.
- Interfaces that expose recognized results to other Kinaiya components.

The recognition pipeline could operate through conventional software and specialized models without continuously invoking Kinaiya's conversational LLM.

Accurate sign-language interpretation requires appropriate language-specific datasets, temporal and grammatical understanding, and representative evaluation. The existence of a working vision pipeline does not establish that it correctly understands a sign language.

Generating signed responses would constitute an additional capability requiring its own implementation and validation.

The example illustrates an architectural possibility, not an existing Kinaiya feature or a guarantee of autonomous implementation success.

## 5. Persistent, Model-Independent Capabilities

Once installed and activated, an extension should remain available independently of the specific LLM that helped construct it, provided its interfaces and dependencies remain compatible.

Replacing an LLM must not inherently require rebuilding a previously validated computer-vision system, scientific tool, or conventional algorithm.

Capabilities should expose explicit contracts, dependencies, version information, permission requirements, and resource requirements.

The model may invoke and orchestrate these capabilities, but their underlying implementations need not depend on its reasoning process.

This separation is essential to Kinaiya's provider-independent design.

## 6. Resource Efficiency

Kinaiya should prefer suitable computational methods rather than defaulting to LLM inference for every operation.

A specialized executable component may be faster, more deterministic, less expensive, or less memory-intensive than a general-purpose multimodal model.

Capabilities may use CPU execution, GPU acceleration, specialized model inference, external services, or hybrid approaches, subject to configured user permissions and resource policies.

**The 6–12 GB VRAM target concerns the intended accessible conversational experience. It does not imply that every possible extension or simultaneous workload must fit within that same memory budget.**

Heavy capabilities must declare their requirements and participate in resource scheduling, loading, unloading, or authorized offloading.

No silent migration of private data to cloud providers is permitted.

## 7. Architectural Boundaries

Autonomous capability expansion must preserve the established security architecture.

Generated or acquired software cannot independently:

- Modify the protected core, authoritative objectives, or permission policy.
- Grant itself new filesystem, network, credential, or hardware access.
- Alter protected evaluation criteria or fabricate successful results.
- Bypass sandboxing, resource limits, audit requirements, or approval mechanisms.
- Promote an extension into privileged execution without authorization.

New functionality must use the appropriate existing integration boundary: executable skill dependencies, plugins, MCP services, adapters, or adaptive harness extensions.

Not every acquired capability is an adaptive harness extension. Classification depends on whether the software changes the agent's operating strategy, introduces a new callable operation, or does both.

Converse remains responsible for personality and conversational interaction. Decisions remains responsible for asynchronous routing. Agent owns capability acquisition and task execution. The shared contracts connect them without collapsing their separate responsibilities.

## 8. Interpretation Rule for Developers and Coding Agents

When implementing Kinaiya, do not interpret "self-building," "self-extension," "learning a capability," or "improving itself" as exclusively referring to prompt optimization, LLM fine-tuning, retrieval changes, or model-specific harness tuning.

These operations are valid but represent only part of the intended architecture.

**Kinaiya must be designed to support the acquisition and construction of durable executable capabilities, including capabilities implemented entirely outside language models.**

A capability is not considered successfully acquired merely because the LLM can describe how it would work, generate source code, or call an installation command.

It must be executable, integrated, independently validated against its stated requirements, and accessible through an authorized interface.

## 9. Research Objective and Implementation Status

The broader research question is:

> Can an autonomous agent recognize its own functional limitations, acquire or construct suitable executable capabilities, integrate them into its operating environment, and reliably use those capabilities without compromising its protected core?

This is a distinct research objective from determining whether automatically generated harnesses improve performance for a fixed set of tools and models.

Neither objective should be represented as solved merely because its architecture has been specified.

The present document establishes the intended design and interpretation. Individual mechanisms must be implemented, tested, and reported according to their actual development status.

## 10. Decisions: Real-Time Converse Control and Agent Routing

**Design intent:** Decisions is a low-latency, model-selectable, asynchronously operating control and routing component between Converse and Agent. Its purpose is broader than voice activity detection, endpointing, or a simple turn-taking classifier. It determines how Converse should react to emerging speech **and separately** whether the speech should immediately affect Agent's ongoing work.

Decisions does not replace the conversational LLM, Agent's reasoning and planning, or the complete transcript. Its classifications are routing and timing instructions, not authoritative statements that work has started, stopped, or succeeded.

### 10.1 Converse-facing conversational outcomes

On each suitable streaming transcript window, Decisions may select one of four conversational behaviors:

1. **IGNORE:** Do not produce a conversational response to this speech/window.
2. **WAIT_FOR_NEXT_WINDOW:** The user may not be finished; retain relevant partial context and evaluate the next transcript window.
3. **RESPOND_INTERRUPT_USER:** Initiate a response even if doing so requires interrupting the user's current speech when appropriate.
4. **RESPOND_WITHOUT_INTERRUPTING_USER:** Prepare or deliver a response at a suitable conversational opening, without speaking over the user.

These are behavioral choices, not synonyms for fixed silence timers. The objective is to infer whether the user's utterance is actionable, incomplete, a backchannel, or an interruption **from the contents and context of successive streaming windows**, rather than mandating an artificial period of silence before every response. Timing, prosody, STT stability, and acoustic evidence may assist; no claim of perfectly eliminating ambiguity or latency is implied.

### 10.2 Agent-facing asynchronous routing outcomes

Independently of the conversational response decision, Decisions selects how the same speech should affect Agent:

1. **IGNORE_FOR_IMMEDIATE_ROUTING:** Do not immediately push this speech to Agent's task-processing path.
2. **STEER_CURRENT_TASK:** Forward a relevant instruction that changes or constrains work already in progress without automatically cancelling the task.
3. **INTERRUPT_CURRENT_TASK:** Deliver a high-priority request to pause, stop, or cancel active Agent work, using the actual executor's interruption mechanisms and acknowledgments.
4. **QUEUE_UNTIL_TASK_DONE:** Preserve the instruction and deliver it after the current task completes or reaches the appropriate queue boundary.

These outcomes are **not mutually exclusive with Converse's outcomes**. One user utterance can lead to a conversational acknowledgment while steering Agent; another can demand both immediate conversational interruption and task cancellation. Decisions may evaluate both channels concurrently or in one fast inference, but the resulting decisions must be represented separately.

### 10.3 Full transcript is preserved and accessible to Agent

**IGNORED for immediate routing does not mean deleted, lost, or inaccessible to Agent.** All timestamped transcript windows are independently appended to the complete conversation record and are synchronized to an Agent-accessible transcript/history channel or store. Agent may inspect the complete transcript whenever it deems it necessary, including windows Decisions did not select for immediate task routing.

This creates two deliberately distinct information paths:

- **Immediate attention path:** Selected speech reaches Agent now as an actionable steer, interrupt, or queued instruction.
- **Complete history path:** The full transcript remains available for later reasoning, context recovery, auditing, and follow-up, subject to the user's configured privacy and retention settings.

Decisions must not become the sole owner or gatekeeper of transcript persistence. Conversational and Agent routing errors must never silently discard the historical record.

### 10.4 Asynchronous operation and execution safeguards

- Decisions should run independently of long-running Agent inference and tool execution. Its value depends on low end-to-end latency across streaming STT, classification, dispatch, and any required executor acknowledgment, not merely fast model inference.
- Transcript windows and routing results should carry sequence identifiers and task/turn context so delayed decisions do not overwrite newer intent or attach to a retired task.
- Agent steering changes task intent through an explicit supported channel. An interruption request is not proof of cancellation; actual executor state must confirm the effect, especially for irreversible or already-dispatched actions.
- Queued instructions must retain ordering and be reconsidered against current task state when dequeued. Duplicate, superseded, or stale instructions must not be applied blindly.
- Model choice is replaceable. The intended architecture does not depend on a particular Decisions model or require the routing model to be the same model used for Agent or Converse.

### 10.5 Interpretation examples

| User speech while Korina is active | Converse decision | Agent routing decision |
| --- | --- | --- |
| 'Korina, could you... actually...' | WAIT_FOR_NEXT_WINDOW | IGNORE_FOR_IMMEDIATE_ROUTING |
| 'Mm-hmm' while Korina is explaining something | IGNORE | IGNORE_FOR_IMMEDIATE_ROUTING |
| 'Also include the documentation.' during a coding task | RESPOND_WITHOUT_INTERRUPTING_USER | STEER_CURRENT_TASK |
| 'Stop, don't delete that!' during active execution | RESPOND_INTERRUPT_USER | INTERRUPT_CURRENT_TASK |
| 'Once that finishes, check the changelog.' | RESPOND_WITHOUT_INTERRUPTING_USER | QUEUE_UNTIL_TASK_DONE |

These are illustrative outcomes; real decisions depend on context and must be tested for false interruptions, premature turn completion, missed steering, delayed cancellation, unnecessary Agent activations, and full-transcript recovery.

**Core clarification:** Converse handles the live user-facing conversation; Decisions independently governs conversation timing and selective, asynchronous task routing; Agent performs deeper reasoning and task execution while retaining access to the unfiltered conversation history. Fast Decisions inference is an enabling mechanism, not a reason to conflate these responsibilities.

---

**Core architectural statement:**

Kinaiya is not intended to be an LLM that can perform every possible task internally. It is intended to be an autonomous agent that can expand what it is capable of doing by acquiring, composing, and constructing executable software—including software that does not use an LLM at all.
## 11. Cross-Surface Task Coordination, "Steered by Converse," and Voice Traceability

**Status:** Product and architectural design intent; requirements below are not claims of completed implementation.  
**Scope:** Converse, Decisions, Agent, shared journal, Flutter Agent UI, harness inspector, audio retention.  
**Relationship to §10:** Section 10 specifies how Decisions classifies conversational and Agent-facing routing. This section specifies the **authoritative outcomes, user-visible provenance, and persistent traceability** of that routing.

### 11.1 Principle and responsibilities

Converse is the personality-focused voice surface; Agent is the independent working surface. A user may speak with Converse while watching Agent work, and may open or close either interface without inadvertently starting, stopping, or redirecting a task.

The shared journal/supervisor—not the UI or a generated harness—is the authority for what was requested, accepted, applied, rejected, or completed. Decisions proposes routing, but only Agent's actual supervisor/executor acknowledgment proves that a queue, steer, or interruption took effect. Neither classification nor conversational acknowledgment grants tool permissions.

Every consequential voice-originated task change must be traceable through: **speech transcript event → Decisions routing request → supervisor outcome → task/objective revision → selected harness and authorized execution → result**.

### 11.2 Agent interface and session highlighting

The Agent desktop UI uses **two independently collapsible left sidebars**: (1) a Harness Tree displaying protected Core, baseline and approved/eligible specialized harnesses and their versions; (2) a Workspace Tree displaying Core sessions, projects, tools, settings, and approved extension-provided entries. The central work area remains a familiar agent chat/task workspace; a separate collapsible context inspector may appear on the right.

The session tree independently distinguishes:

- **Selected**: the session currently displayed, with prominent selection highlighting;
- **Running**: a session with executing work, with an activity indicator even when not selected;
- **Converse-linked**: a session created or later affected by a voice event, with a microphone/provenance indicator;
- **Attention required**: blocked on permission, confirmation, or another user response.

These states can coexist and must not be conveyed solely by color. Switching views or collapsing panels does not cancel or migrate running tasks. Core navigation remains available even when a specialized harness contributes contextual sections.

### 11.3 User-visible provenance versus execution state

Provenance is **not** task status. A session may be “Steered by Converse · Running” or “Queued by Converse · Waiting.” Supported origin/outcome labels are:

| Label | Authoritative meaning |
| --- | --- |
| **Started via Converse** | A Converse-originated request actually created a task. |
| **Queued by Converse** | Agent accepted the request for deferred execution; it has not necessarily run. |
| **Steered by Converse** | The supervisor applied a Converse-originated revision to an existing task objective. |
| **Interrupted by Converse** | The executor acknowledged the applicable interruption; a mere request does not qualify. |
| **Direct Agent** | The instruction originated in Agent Chat rather than the Converse routing path. |

Show pending proposals as **Steering requested**, **Interruption requested**, etc., not successful past-tense outcomes. Preserve the difference between *requested*, *accepted*, *queued*, *applied*, *rejected*, *superseded*, and *failed*. Ignored transcript windows stay in history but do not create meaningless Agent sessions. The user's spoken acknowledgment and the backend's actual acknowledgment must not be conflated.

### 11.4 "View voice context" inspector

Each consequential Converse-originated event exposes an accessible **View voice context** action in the session header, routed-event chip, or task timeline. It opens the specific source passage, not an unrelated entire conversation, with an option to expand adjacent context.

The inspector displays, where available:

1. Source Converse conversation/session, timestamps, original recognized user utterance, and neighboring transcript windows.
2. Converse's conversational response or acknowledgment, clearly distinguished from Agent execution acknowledgment.
3. Decisions classification, dispatch state, supervisor outcome, target Agent session/task, and objective revision before/after.
4. The harness ID/version **actually pinned to the affected task** and the recorded execution result.
5. The audio's current availability/pruning status and any later transcript corrections.

**View voice context** answers *what was said*; **View execution trace** answers *what Agent did*; **Compare objectives** answers *what changed*. These are separate, linked actions. The trace must not expose private model chain-of-thought; show observable events, summaries, tool calls, approvals, and results instead. UI navigation never silently transfers an unfinished Converse draft to Agent Chat or vice versa.

### 11.5 Authoritative event contract and causal links

Use append-only, versioned, backend-sequenced journal events (not independent frontend state) with stable identifiers and explicit relationships. Illustrative **proposed** applied-steering event:

\`\`\`json
{
  "schema_version": 1,
  "event_id": "evt_1047_21",
  "event_type": "agent.task.steering_applied",
  "source": {
    "surface": "converse",
    "conversation_id": "conv_42",
    "transcript_event_ids": ["transcript_102"]
  },
  "target": {
    "agent_task_id": "task_89",
    "session_id": "session_12"
  },
  "routing": {
    "requested_action": "steer",
    "result": "applied"
  },
  "objective": {
    "previous_revision": 1,
    "new_revision": 2
  },
  "execution": {
    "harness_id": "development",
    "harness_version": "1.2.1"
  },
  "timestamp": "2026-10-28T17:47:00Z"
}
\`\`\`

The example is a design sketch, not a claim that this exact schema is implemented. Real event IDs, timestamps and revisions come from the authoritative backend. Bind routing requests and outcomes to the source transcript, target task, expected objective revision and relevant authorization context; reject or re-evaluate stale instructions.

Requirements: monotonic event sequence and objective revisions; idempotent delivery/replay; no duplicate task creation or reapplication on reconnection; durable request/outcome linkage; correct ordering for queued/superseded commands; safe cancellation acknowledgments; and no silently fabricated success. If a session's pinned harness changes, record an independently authorized and tested checkpoint migration instead of silently rewriting provenance.

### 11.6 Live cross-surface synchronization

Converse, Decisions and Agent can operate concurrently. The Agent UI subscribes to journal projections and shows task creation, queue admission, steering requests/outcomes, execution steps, approvals, and terminal results without a manual refresh. Converse receives concise confirmed progress when conversationally relevant, without blocking normal voice responses on long-running Agent work.

On reconnect, resume from the last acknowledged journal sequence, replay missing events idempotently, and reconcile against the supervisor's authoritative task state. Keep selection, running, origin, and blocked indicators separate.

### 11.7 Audio pruning and durable transcripts

Separate three retention classes with user-configurable, privacy-aware policies:

- **Raw audio** (for example, temporary \`.wav\` segments): useful for recognition, playback, diagnostics; may be pruned once required persistence and retention conditions are met.
- **Full text transcript**: timestamped, searchable conversation history; retain according to the user's configured policy even after audio pruning.
- **Agent task/event records**: causal references to transcript IDs, routing outcomes, approvals, objective revisions, harness versions and execution results; retain according to policy.

Store durable references to transcript events rather than copying the entire dialogue into every task. Confirm successful persistence of required transcript/event references **before automatic audio deletion**. Mark audio as pruned and remove misleading playback controls once unavailable. Retaining recognized text does not preserve tone/prosody or prove STT accuracy.

Preserve originally committed recognition text; record user corrections as later attributable revisions, not silent overwrites. For consequential low-confidence speech, require appropriate confirmation before privileged actions. Support appropriate user access, search, export, deletion, retention settings and authorization checks; do not show sensitive transcript snippets in notifications by default. Ensure deletion/retention reconciliation does not leave broken references masquerading as accessible original evidence.

### 11.8 Protected Core and harness independence

Cross-surface provenance, authoritative journal, supervisor acknowledgments, permission checks, and baseline navigation are **Kinaiya Core responsibilities**, not optional facilities controlled by a generated harness. Specialized harnesses may contribute versioned task details or registered, validated UI views, but may not forge or rewrite transcript origins, route results, objective histories, approval records, harness identities, or execution evidence.

Self-modification does not permit an extension to decide what constitutes a successful routing, promotion, or evaluation. A selected task must identify its *actually active/pinned* harness; model-generated descriptions of harness selection are not proof of activation.

### 11.9 Acceptance criteria

1. Converse-originated creation, queueing, steering, and interruption appear in Agent with accurate provenance **and** independent execution status.
2. The selected Agent session is highlighted; running, Converse-linked and attention-required indicators remain independently visible.
3. Only supervisor-confirmed applied changes receive past-tense **Steered**/**Interrupted** labels; rejected or stale requests are not shown as successes.
4. **View voice context** resolves the correct source transcript passage and nearby context; **View execution trace** and **Compare objectives** link to the same causal event.
5. The actual task-pinned harness ID/version and objective revision history remain inspectable.
6. Pruned audio leaves a truthful “audio unavailable/pruned” indicator while permitted transcript and event records remain inspectable.
7. Restart/reconnect restores authoritative state without duplicate effects or lost routing transitions.
8. Unauthorized users, models, plugins, and harness extensions cannot forge provenance or read restricted transcripts.
9. Both left sidebars and the optional right inspector collapse independently without interrupting background work.
10. Tests cover STT correction, false routing, stale steer, duplicate events, queued supersession, cancellation that fails to stop already dispatched actions, audio retention/deletion, and multi-session concurrent activity.

### 11.10 Delivery sequence

Implement in dependency order: **(A)** versioned causal event contracts and backend authority; **(B)** task/session projection and selected/active/provenance UI indicators; **(C)** voice-context, execution-trace and objective-diff inspector; **(D)** live subscriptions, reconnect replay, and correctness tests; **(E)** audio pruning and retention controls; **(F)** versioned extension UI integration and adversarial provenance tests.

**Interpretation rule:** “Steered by Converse” is a verified record of an Agent objective change caused by a particular voice interaction—not merely a model's opinion that a task was steered.

## 12. Korina Memory DAG: Optional Memory for Converse

**Status:** Architectural design intent; this section specifies intended behavior, not completed implementation.  
**Scope:** Converse's conversational memory and context preparation.

### 12.1 Purpose and ownership

The **Korina Memory DAG (directed acyclic graph) is intended for Converse only**. Its purpose is to help low-parameter conversational models make better use of a limited context window by retaining relevant information outside the active prompt and selectively retrieving or condensing that information for later turns.

For example, a model with a **32,768-token context window** should be able to maintain useful conversational continuity across a longer history, making that window *feel larger* through selective recall. Every inference request must still fit within the model's actual context budget. This is an intended improvement in effective conversational continuity, not a claim that the DAG enlarges the native context window or guarantees complete recall.

This DAG is not Agent's task memory, adaptive-harness memory system, or Decisions' routing state. Agent may use its own task-specific context and memory strategies independently.

### 12.2 Replaceable implementation and user toggle

The DAG must be an **optional, replaceable Converse memory implementation behind an explicit interface**, not a hardcoded dependency of Kinaiya Core, a particular model, or the rest of the platform.

- Provide a user-controlled **enable/disable toggle**.
- Converse must remain usable with DAG memory disabled or unavailable, using its ordinary bounded conversational context.
- When disabled, Converse must stop DAG retrieval, updates, background memory processing, and adding DAG-derived content to prompts.
- Another compatible memory implementation must be selectable without rewriting Converse's dialogue logic or changing Agent and Decisions.
- The setting governs memory use; data retention and deletion remain separate explicit user controls.

### 12.3 Shared-history boundary

The DAG supplements the authoritative shared transcript; it does not replace it. Disabling DAG memory must not disable transcript persistence, remove Agent's authorized access to retained history, or change Decisions' routing responsibilities (§10.3). Retrieved memories remain derived context subject to the user's privacy and retention settings; they cannot grant permissions or serve as authoritative execution evidence.

**Interpretation rule:** Korina Memory DAG extends Converse's practical recall through optional external memory. It must not become mandatory platform infrastructure or an implicit memory system for Agent.

## 13. Unified Providers & Models: Provider-First Configuration and Model Selection

**Status:** Product and architectural design intent, not a claim of implemented UI or integrations.  
**Scope:** Kinaiya settings, provider registry, model discovery, credential handling, and runtime role assignments.  
**Relationship to existing architecture:** Preserve independent Converse, Agent, Decisions, STT, and TTS roles, protected Core authorization, explicit cloud disclosure, resource policy, and task-pinned execution. The existing read-only model/options experience may be superseded by this explicitly authorized owner-facing configuration workflow; read-only status displays must not silently become model-controlled write access.

### 13.1 Organizing principle

**Providers own connections and credentials; models expose capabilities; runtime roles determine where models are used. Manage all three in one coherent interface.**

Do not require separate Provider and Model settings pages for the normal workflow. Use one **Providers & Models** settings page where each provider is a clickable, expandable row containing its discoverable model catalog. Supported initial provider families include Meta, OpenRouter, conventional OpenAI API, LM Studio, and Ollama; support other compatible endpoints through extensible adapters rather than hardcoding these five. An eligible ChatGPT-account sign-in flow, if officially available and authorized, is a **distinct authentication/inference integration**, not the same thing as a conventional OpenAI API key or a general promise that a ChatGPT subscription can be used as an API.

The provider is the organizational parent of models in the UI; it is not necessarily the owner of model weights or proof that a model is currently runnable. Model identifiers are scoped to their provider and connection profile. Identical names under different providers are distinct selections.

### 13.2 Provider dropdown behavior

- A configured provider row shows its name, connection/health state, model count or discovery state, and an expand/collapse chevron. Clicking the row/name or chevron opens or closes its model list; expanding **does not** automatically change the active model.
- Clicking an **unconfigured** provider initiates its setup wizard first. A successful verified connection enables model discovery and expansion. Do not show invented model choices or mark a provider connected merely because a key was entered.
- A separate settings/gear affordance on a configured provider opens a contextual **provider settings sidebar**, preventing ambiguity between expand, configure, and select actions.
- The model list supports search and, for large catalogs, filters and efficient/virtualized browsing. Users can filter by text/audio/image/video modalities, tool use, reasoning, context, or availability as metadata permits. Preserve a usable accessible keyboard and screen-reader flow; do not rely on color alone.
- Loading, failed discovery, expired credentials, offline local servers, empty catalogs, and unknown capabilities have visible and recoverable states. Manual model-ID entry may be offered if the endpoint supports inference but does not support catalog discovery, with validation and an explicit unverified-capability label.

### 13.3 Model cards and contextual model inspector

For each model, show provider-qualified identity and, when **actually known from the endpoint**, useful metadata:

- **Inputs:** text, image, audio, video, file, or other supported input modalities.
- **Outputs:** text, audio, image, structured data, or other supported output modalities.
- **Capabilities:** tool/function calling, reasoning controls, structured output/JSON, streaming, and relevant speech features.
- **Limits and performance information:** context window, maximum output tokens, model/quantization variant, local loaded/unloaded state, and pricing where the provider supplies reliable data.
- **Readiness:** available, loading, unavailable, incompatible with a particular role, or **unknown/unverified**.

Model capabilities and *adapter/endpoint* capabilities are different. A model advertised as tool-capable is not sufficient if Kinaiya's selected transport cannot correctly deliver tools or parse results. Metadata is a hint, not proof: distinguish **supported**, **unsupported**, and **unknown**, and show tested/verified compatibility separately when available. Never assume absent metadata means false. Never invent context lengths, costs, input/output modalities, or reasoning controls.

Clicking a model opens its own contextual detail/assignment inspector; it **does not silently activate** that model. Offer an explicit **Use for [role] / Assign** action after validating that the selected adapter and model support that role's necessary functions. Controls for reasoning effort, context, sampling, etc. must be offered only when genuinely supported and mapped to the underlying provider's protocol; disable unsupported controls visibly rather than silently ignoring them.

### 13.4 Contextual provider settings sidebar and connection wizard

For a configured provider, the sidebar may offer, as appropriate:

- **API key(s) and authentication profiles:** add, label, select, rotate/replace, and remove multiple credentials or accounts; show masked identifiers or labels, never raw secrets after initial entry.
- **Endpoint/base URL and connection profile:** supported local/cloud URLs, permitted network scopes, TLS requirements, health check, and model-discovery refresh.
- **Provider-specific controls:** rate limits, spending/budget caps, model preferences, provider-specific flags, local runtime resource preferences, and availability diagnostics where supported.
- **Forget provider:** an explicit, confirmed action that clears the local provider configuration and locally stored credential references, after displaying affected role assignments, sessions, and dependencies. Forgetting a provider **does not automatically revoke an external API key** at the provider's website; explain how to revoke it separately. Do not silently fail over to another cloud provider.

For an unconfigured provider, the wizard must ask only for what the integration requires: API key or approved sign-in, or local server endpoint; test connectivity and authorization; and then discover models. Never confuse credential validity, server reachability, model listing, and successful inference—they are separate states. Connection setup, testing, and discovery must not transmit private conversation content by default.

Secrets belong in protected OS/host credential storage or the existing guarded credential-reference mechanism, not source control, exported settings, frontend storage, logs, browser URLs, or publicly readable configuration. Never expose stored keys through read-only settings APIs. Multiple key profiles must not be automatically rotated to evade limits or bypass explicit user budgets/permissions. Remote/custom endpoint checks must enforce host/network allowlisting or equivalent SSRF protections, redirect controls, and the user's cloud/private-data authorization.

### 13.5 Model discovery, provider adapters, and ownership

Use a unified provider registry and normalized, versioned model-catalog contract with provider-specific adapters. Discovery can be refreshed or cached with staleness/last-checked information and should not require arbitrary inference requests to populate the UI. Different providers expose different catalogs, feature metadata, and authentication methods; adapters must preserve these differences rather than pretending all providers implement the same native operations.

Provide ordinary inference interfaces independently from optional provider capabilities such as catalog discovery, model installation/download, local load/unload, and remote account management. For example, Ollama and LM Studio may expose local model lifecycle features; cloud providers generally do not expose those same operations. A provider may support text chat without supporting native tool calling, audio, or structured outputs in Kinaiya's chosen transport.

Retain specialized providers, executable non-LLM components, and replaceable Decisions models. The UI must not imply that every Decisions or STT/TTS component is a text LLM, nor force them into an LLM-only interface. Explicit capability adapters govern compatible role options.

### 13.6 Role assignment and runtime safety

Support independent selections for **Converse dialogue, Agent execution, Decisions routing, STT, and TTS**, with capability-appropriate options for each. A model may be eligible for more than one role, but assignment is deliberate and role-specific. Decisions' model must remain independently replaceable, with Laya only a recommended option if configured as such, never a hardcoded requirement. The optional Converse memory DAG (§12) remains a separate toggle, not a side effect of choosing a provider.

A role change must be an authenticated, owner-authorized configuration mutation with preflight validation, a preview of affected components, atomic application or rollback, and truthful success/failure feedback. Explicitly handle active Converse turns, Agent tasks, in-flight streams, and local load/unload constraints. Existing Agent jobs must retain their pinned model/provider/harness identity until a permitted and safely checkpointed migration; browsing the picker or switching UI tabs must not move them. Preserve current tasks, journal provenance, authorization decisions, and transcripts. New provider selection does not silently enable cloud disclosure, grant tool permissions, or override data-scope policy.

When a selected provider fails, display the failure and permitted recovery options. Cross-provider fallback is a separate, explicit opt-in policy that must respect cost, privacy, credential boundaries, and role capability constraints; a failed call must not cause covert data export.

### 13.7 Acceptance criteria and delivery

1. The user can expand connected provider rows to inspect discoverable models without leaving the page or changing the active model.
2. Clicking an unconfigured provider opens setup; failed/partial setup is reported as failed/partial, never as connected.
3. Provider settings are accessible independently of the dropdown and expose masked credentials, profile controls, endpoint/health settings, and an explicitly confirmed Forget action.
4. Model inputs, outputs, tools, reasoning, context, and readiness are shown only when supported by verified metadata; missing facts read **unknown**, not **no** or fabricated guesses.
5. Search/filter works with large catalogs, and UI remains keyboard-accessible, responsive, and honest about loading/error states.
6. Model selection requires explicit per-role assignment and rejects incompatible model/transport combinations before changing authoritative configuration.
7. Switching never corrupts or silently rebinds in-flight Agent work, Converse sessions, Decisions routing, or retained provenance.
8. Credentials never appear in repository files, regular settings responses, logs, or exported nonsecret profiles; custom endpoint verification cannot bypass cloud authorization or network policy.
9. Removing a provider reports dependent roles and does not covertly reroute tasks, revoke external secrets by implication, or misrepresent removal success.
10. Tests cover simulated provider discovery and auth failures, stale catalogs, inconsistent metadata, active task switching, role compatibility, permission boundaries, unsafe endpoints, and reconnect behavior.

**Delivery order:** (A) registry, adapter contracts and catalog normalization; (B) authenticated connection and secrets lifecycle; (C) expandable provider list, search, and contextual sidebar; (D) validated role assignment and safe switching; (E) provider-specific lifecycle and advanced controls. Do not sacrifice existing Core safety guarantees for a faster-looking setup wizard.

**Interpretation rule:** The desired UX is one provider-first **Providers & Models** workspace—not disconnected provider and model pages, not a flat unqualified model dropdown, and not automatic model activation on expansion. This section records **target design**, not existing implemented behavior.

## 14. Optional Tool Library: Discovery, Installation, and Setup

**Status:** Product and architectural design intent, not an implemented feature or authorization to install software.
**Scope:** Optional conventional software tools, verification engines, developer utilities, media tools, and other non-model executable capabilities made available to Kinaiya Agent.
**Relationship to existing architecture:** Complements the provider/model registry (§13), autonomous capability acquisition (§3), Agent Skills, plugins, MCP, and the protected Core; does not replace those mechanisms.

### 14.1 General-purpose Tool Library, not a theorem-prover-only installer

Provide a **Tool Library** within Kinaiya where users can discover, inspect, install, enable/disable, update, and remove recommended capabilities. The library is not limited to LLMs, proof assistants, or Agent Skills. It may catalog conventional programs and toolchains (for example, Lean 4, Coq/Rocq, Isabelle/HOL, FFmpeg, Ghidra, and Playwright), plus supported wrappers that expose those programs to Agent.

Distinguish **models/providers** (§13), **tools and their executable dependencies**, **skills** (instructions or packages that may depend on tools), **plugins**, and **MCP integrations**. A catalog entry can reference a skill, plugin, or MCP adapter without implying those are interchangeable. Tools are independently useful executable capabilities; the Agent should discover suitable existing software before proposing to reimplement it.

The catalog presents the tool's purpose, capability category, recommended/optional designation, supported platforms, deployment target, estimated download/disk requirements when known, licensing and dependencies, required permissions, source/provenance, supported version(s), install/detection status, adapter availability, and documented limitations. Do not invent metadata or imply that a listed program is installed or working.

Suggested organization includes Formal Verification, Development/Automation, Media, Scientific/Analysis, and other extensible categories. Support search/filter and keyboard/screen-reader operation. Recommended indicates *curated relevance*, not automatic trust, installation, activation, or elevation.

### 14.2 Optional setup step: default off, any combination permitted

The initial Kinaiya setup wizard offers a skippable **Enhance Kinaiya's Capabilities / Optional Tools** step. All additional tools start **unselected/off**. Users may choose none, Lean only, Coq/Rocq only, Isabelle only, any pair, **all three**, or any other supported tool combination. Lean 4 may be highlighted as the **recommended starting verifier** without preselecting or downloading it.

Explain why each choice may be useful, whether it is free/open-source, its estimated installation footprint, dependencies, compatibility, and requested host permissions. Present a final explicit install confirmation with the selected tools, destination host, size, and consequences before starting downloads. Skipping setup must not impair ordinary Converse or baseline Agent operation. Users can revisit the library after installation, including to install multiple verification engines or remove an unused one.

Before offering a download, **detect compatible existing installations** and allow adoption through a verified adapter without unnecessarily installing duplicates. Show distinct states such as not installed, detected, installing, installed, adapter ready, incompatible, failed, update available, disabled, and removed. Installation success alone is not proof of an operational tool: perform a harmless capability/health test. A canceled or failed install should be recoverable and should not leave Kinaiya claiming that the capability is active.

Tool installation belongs to the **host that executes Agent or the approved external worker**, not automatically to the Flutter/browser/phone client that displays the UI. If Agent runs remotely, display the actual installation destination and require appropriate owner authorization on that host.

### 14.3 Manifest, registry, and lifecycle

Use signed or otherwise authenticated, versioned, vetted tool manifests or similarly controlled catalog records. A manifest describes identity, supported platforms/architectures, official distribution/source, allowed installation mechanism, pinned version or compatibility range, hashes/signatures where available, licenses and attribution obligations, disk/resource requirements, dependency constraints, health checks, uninstall/rollback support, adapter contracts, exposure to Agent, and required permissions.

Lifecycle: **discover -> inspect -> obtain consent -> install/detect -> validate -> register -> enable/use -> monitor -> update/disable/remove**. Installation is a privileged, separately authorized operation. Merely discovering a tool or receiving a model suggestion may not install or execute it. Downloads must use reviewed sources and verification where available, with bounded network access, least privilege, and isolated execution. Untrusted repositories, post-install scripts, build hooks, dependency installers, and tool subprocesses must not inherit protected Core permissions or unbounded access to secrets/filesystems.

Persist installation provenance and real health/status, with truthful cleanup/rollback reporting. Show conflicts and dependencies before updates or removal; do not silently remove software that predates Kinaiya or is shared with other applications. If a package manager or external service owns an installation, use an appropriate integration instead of claiming Kinaiya owns its lifecycle. Respect each distribution's licensing, attribution, and redistribution requirements.

The trusted Tool Registry exposes **typed, capability-scoped adapters** to Agent and permission-brokered operations. Installed tools do not gain autonomy or authority beyond explicitly granted scopes. User-installed tools, Agent-generated wrappers, and their outputs remain untrusted inputs for privileged decisions.

### 14.4 Agent-guided capability acquisition

When Agent identifies a capability gap, it should search the existing authorized Tool Library before writing replacement software. It may explain a tool, suggest a compatible installation, and request permission; it must not treat proactive recommendation as consent. After a successful installation and validation, the acquired capability should be durable and usable across compatible model changes (§5).

Agent may install a previously authorized dependency as part of an explicitly scoped plan only if the owner policy permits that exact operation. The protected broker—not the language model, plugin, or tool manifest itself—enforces permissions and provenance. Tools do not grant additional tool permissions, cloud-disclosure rights, privileges, or permission to edit the protected Core.

### 14.5 Tool Library acceptance criteria

1. Ordinary initial setup succeeds with every optional tool left off; users may later select none, one, several, or all available compatible verification engines.
2. Setup previews platform, target host, license, estimated downloads/storage and requested permissions before any install; nothing downloads until explicit approval.
3. Detection of existing installs is distinct from newly downloaded installs; catalog, installation, enabled, and adapter-ready states are not conflated.
4. Verified adapter health and availability—not a green download indicator—determine whether Agent may invoke the capability.
5. No tool install/update/uninstall bypasses owner permission, protected Core isolation, installer integrity checks, or appropriate cleanup/provenance.
6. Agent can discover and suggest tools without obtaining implicit installation permission or changing its own trust level.
7. Installation and tool use remain optional, with safe failure/retry paths and no hard dependency on theorem provers for the core conversational experience.

**Interpretation rule:** Tool Library is a general, opt-in capability catalog and lifecycle interface. Its contents are not assumed installed; catalog recommendations never confer permission or proof of safety.

## 15. Formal Verification: Optional Engines, Trusted Checking, and Empirical Evidence

**Status:** Research and architectural design intent, not a claim that formal proofs have been implemented or that Kinaiya's goals have already been proven.
**Scope:** Agent-produced proof artifacts, trusted checking, specified invariants, harness promotion, Decisions/Agent coordination, and research evaluation.

### 15.1 Why proof assistants are offered

Lean 4, Coq/Rocq, and Isabelle/HOL are **free/open-source proof assistants** suitable for optional local use, subject to their actual license and bundled-component obligations. Offer them individually through the Tool Library (§14), with **Lean 4 recommended as the first implementation target**, not a mandatory dependency. A user who wants all three may install all three, subject to platform support and resource limits. They are separate proof languages/toolchains and must not be advertised as interchangeable drop-in proof files.

Kinaiya's goal is **independent, machine-checkable evidence** for *specific formalized properties*, combined with empirical evaluation of actual capability acquisition, harness improvement, and conversational quality. The existence of a proof checker, a verified theorem, passing tests, or a strong benchmark alone does not establish that Kinaiya has generally achieved its research objectives.

- **Formal correctness:** a theorem is valid relative to explicitly stated definitions, axioms, and assumptions.
- **Implementation correspondence:** evidence must connect the formal model to the actual trusted controller, adapters, and deployment assumptions; a proof about an idealized state machine is not itself a proof about all Python, OS, sandbox, or network behavior.
- **Empirical effectiveness:** controlled, independently evaluated measurements are required to support claims of improved task success, latency, capability breadth, efficiency, or practical generalization.

Neither prover output nor an LLM evaluator is unconditional ground truth. Poorly chosen specifications and unmodeled runtime behavior remain sources of error.

### 15.2 How Agent uses a prover

Expose a replaceable **Verification Engine adapter** for each supported prover. Agent can detect a compatible installed engine, produce or revise proof source in a constrained workspace, invoke the corresponding compiler/checker, read structured diagnostics, and submit a candidate proof for final independent validation. Example extension interfaces include:

- detect/list engines and versions;
- check a proof file or project under explicit resource and time budgets;
- report theorem identifiers, assumptions, toolchain/dependency versions, errors, and warnings;
- submit a proof for trusted checking against a protected, pinned specification.

Typical source types include **.lean** for Lean, **.v** for Coq/Rocq, and **.thy** for Isabelle, each checked with its compatible installed toolchain. Avoid pretending a generic compiler exit code alone is a verified invariant.

A strict approval/checking path must reject or clearly flag unfinished holes, admitted theorems/axioms, unsafe trust escapes, permissive checker modes, incompatible dependencies, or other unapproved shortcuts. Check the actual proof obligations and allowed imports/axioms under a pinned toolchain; allow only the justified assumptions of the selected trusted theory. Build systems, macros/plugins, native code, and prover extensions may execute code: generated proof projects and their build hooks must run under constrained permissions, separate from the protected checking authority.

The **trusted verification service** runs independently of Agent-controlled files and reports; Agent cannot forge success, edit its authoritative specification, alter the trusted checker, manipulate proof configuration, or overwrite the resulting attestation. Preserve a signed or otherwise tamper-evident record as appropriate with specification ID/hash, proof artifact hash, toolchain version, dependencies/assumptions, checker outcome, and evaluation run ID. A failed or partial proof is reported as failed or partial; no proof outcome is silently upgraded to a guarantee about real-world deployment.

### 15.3 Initial theorem targets

Start with properties where a small formal state transition system can be specified and then meaningfully linked to Kinaiya's actual code:

1. **Protected Core integrity:** permitted extension/harness state transitions preserve specified protected state and authorization separation.
2. **Permission broker:** privileged actions occur only after the required authorized grants under the modeled policy.
3. **Harness promotion:** no candidate can be activated without the required owner authorization, validated artifacts, versioned evaluation records, and promotion policy checks.
4. **Decisions and Agent routing:** *requested* steer/interrupt events cannot become *applied* events without actual supervisor/executor acknowledgment; stale or replayed requests cannot silently change the current task revision.
5. **Converse memory DAG:** optional graph operations preserve stated acyclicity and provenance/independence invariants where formally specified; disabled DAG use does not affect the authoritative transcript.

Formulate bounded, auditable theorems rather than claiming to prove all future generated code safe. A valid example is **Promoted(h) implies Authorized(h) and Validated(h) and PassedEvaluation(h)**, provided these predicates refer to protected, independently recorded decisions. The theorem proves the *admission rule*, not the correctness of arbitrary test or benchmark definitions.

### 15.4 Proof-carrying extensions as a research direction

Explore **proof-carrying capabilities**: an Agent-generated executable extension may be accompanied by independently checkable proofs of selected interface, state, resource, or security properties. The Agent may propose a proof, but a trusted specification owner and checker decide which properties count. A candidate that writes its own convenient specification and proves it has **not** established that it meets the protected acceptance criteria.

Proof-carrying claims are only as strong as the implementation/refinement boundary that connects proofs to executable behavior. Sandbox isolation, OS capabilities, data exposure, adversarial validation, and rollback still require separate implementation and testing. Do not demand full correctness proofs for every arbitrary program: permit staged assurance levels, unverified tools with explicit labels, and fallback to conventional testing where formalization is impractical.

Formal assurance should not be confused with proving that generated harnesses outperform baselines. Any performance or autonomous capability-improvement claim still needs **held-out tests, matched models/tools/budgets, repeatable runs, uncertainty reporting, independent evaluation, and disclosure of failures/adaptation cost**, as already required by the roadmap's research methodology.

### 15.5 Delivery sequence and acceptance

**Post-hackathon research sequence:** (A) define minimal trusted checker protocol and Lean 4 adapter; (B) specify/version protected invariants; (C) prove a small admission/routing invariant; (D) tie the formal model to real protected controller transitions and integration tests; (E) evaluate proof-carrying extension experiments; (F) optionally support Coq/Rocq and Isabelle with their own adapters and proof artifacts. Adding a prover should not block the existing hackathon-critical development path or ordinary user setup.

Acceptance requires reproducible checking from pinned proof artifacts; rejection of disallowed axioms/holes; clear documentation of trust boundaries and assumptions; independently collected empirical benchmarks where improvement is claimed; correct failure/retry/revalidation; and no promotion based solely on a model's verbal assertion of proof success.

**Interpretation rule:** Kinaiya may *use* formal verification as an optional tool-assisted research capability, but mathematical proof, correct software behavior, and empirical improvement are distinct claims. Trusted independent checkers and evaluation rules—not generated Agent conclusions—establish the evidence accepted by Core.
