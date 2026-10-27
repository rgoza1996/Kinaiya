# Kinaiya — Product Specification, Technical Plan, and Roadmap

**Specification version:** 0.2.1  
**Decision baseline:** October 26, 2026  
**Status:** Pre-implementation design baseline. Requirements and acceptance criteria below are not claims of implemented functionality.  
**Repository:** `rgoza1996/Kinaiya`  
**Voice prototype:** `rgoza1996/Kinaiya-Converse`

> A personality-first conversational system, paired with an optional, independently usable agent that builds and improves its own executable harness.

Kinaiya builds on an existing local-first voice prototype and expands it into a modular platform for real-time conversation, independent agent work, and adaptive-harness research. The architecture below keeps those product surfaces separately operable and ties them together through explicit contracts.

This document consolidates the product decisions, specifies the boundaries between components, and defines an implementation sequence with measurable release gates. Explicit owner decisions take precedence over earlier exploratory suggestions. Where a choice was not settled, this document labels its proposed implementation default rather than presenting it as an agreed requirement.

## Contents

1. [Vision and research objectives](#1-vision-and-research-objectives)
2. [Settled product and architecture decisions](#2-settled-product-and-architecture-decisions)
3. [Scope and non-goals](#3-scope-and-non-goals)
4. [System architecture](#4-system-architecture)
5. [Shared transcript as common state](#5-shared-transcript-as-common-state)
6. [Asynchronous Decisions](#6-asynchronous-decisions)
7. [Converse and real-time voice](#7-converse-and-real-time-voice)
8. [Agent independence and adapter protocol](#8-agent-independence-and-adapter-protocol)
9. [Action truth and completion reporting](#9-action-truth-and-completion-reporting)
10. [Self-building executable harness](#10-self-building-executable-harness)
11. [Immutable core and security boundary](#11-immutable-core-and-security-boundary)
12. [Harness evaluation and promotion](#12-harness-evaluation-and-promotion)
13. [Permissions, autonomy, and YOLO mode](#13-permissions-autonomy-and-yolo-mode)
14. [Inference providers and resource management](#14-inference-providers-and-resource-management)
15. [User experience and navigation](#15-user-experience-and-navigation)
16. [Character packs and animation](#16-character-packs-and-animation)
17. [Image generation and asset expansion](#17-image-generation-and-asset-expansion)
18. [Intelligent UI](#18-intelligent-ui)
19. [Proactivity, notifications, and calls](#19-proactivity-notifications-and-calls)
20. [Persistence, privacy, and operations](#20-persistence-privacy-and-operations)
21. [Protocol and data examples](#21-protocol-and-data-examples)
22. [Proposed repository layout](#22-proposed-repository-layout)
23. [Implementation workstreams](#23-implementation-workstreams)
24. [Milestone roadmap](#24-milestone-roadmap)
25. [Research and performance evaluation](#25-research-and-performance-evaluation)
26. [Acceptance-test matrix](#26-acceptance-test-matrix)
27. [Hackathon demonstration and release gates](#27-hackathon-demonstration-and-release-gates)
28. [Risks and unresolved implementation choices](#28-risks-and-unresolved-implementation-choices)
29. [Prototype migration and documentation rules](#29-prototype-migration-and-documentation-rules)
30. [Implementation references](#30-implementation-references)
31. [Skills ecosystem, MCP, and plugins](#31-skills-ecosystem-mcp-and-plugins)
32. [Slash commands and direct Agent controls](#32-slash-commands-and-direct-agent-controls)

## 1. Vision and research objectives

Kinaiya combines two independently testable research efforts.

### 1.1 Modular real-time conversation

Produce responsive, expressive, interruption-aware conversation from off-the-shelf speech recognition, text language models, and speech synthesis. Native speech-to-speech models must not be a prerequisite.

The difficult part is not simply connecting STT to an LLM to TTS. It is managing incremental output, turn-taking, interruptions, personality, audible delivery, asynchronous work, and hardware contention without forcing the user to wait for the worker agent.

**Research question:** Can a modular voice pipeline deliver convincing conversation while remaining hardware-accessible, model-interchangeable, and correctly coordinated with an independent agent?

### 1.2 Self-building agent harness

Kinaiya Agent starts with a minimal protected core and a usable seed harness. It can generate actual executable extensions, test them, and adopt better harnesses for particular models and task families.

This is not restricted to changing a system prompt, selecting a predefined workflow, or generating JSON configuration. The agent must eventually be able to implement new context strategies, tool wrappers, planning behavior, memory retrieval, delegation policies, and execution helpers in code.

**Research question:** Under equal and explicitly recorded budgets, can model- and task-specific generated harnesses outperform a fixed baseline without changing the objective, corrupting evaluation, or bypassing permissions?

Improvement is a hypothesis to test, not a promised outcome of every experiment. Rejected candidates and negative results are valid research output.

### 1.3 Product expression

Personality lives exclusively in Converse. Character embodiment, expressive voice, proactive interactions, and useful tools make the research understandable to users.

Kinaiya must remain capable of serious work: coding, repository analysis, email management, research, document drafting, and other authorized activities. A charming character must not replace working tools, inspectable evidence, or reliable execution.

The platform and initial default character identity are both Kinaiya. Users can replace the character identity without changing the underlying agent implementation.

## 2. Settled product and architecture decisions

| ID | Decision | Consequence |
|---|---|---|
| D01 | Kinaiya grows from an existing voice prototype into a broader, modular product. | Preserve useful conversation lessons while adding independently operable Agent, Decisions, shared state, and adaptive-harness research. |
| D02 | Converse and Agent are separate runtimes. | Either can operate without the other; communicate through versioned contracts. |
| D03 | Personality belongs exclusively to Converse. | Agent and Decisions do not become competing character/personality systems. |
| D04 | The shared transcript is the common conversational state. | Persist it once and make it available on demand; do not continually forward full histories. |
| D05 | Decisions reads every conversational prompt asynchronously, with rolling context. | Converse does not wait for Decisions or Agent before ordinary responses. |
| D06 | Decisions routes `ignore`, `queue`, `steer`, or `interrupt`. | Routing changes Agent attention, not the meaning of user permission. |
| D07 | Agent is optional and user-switchable. | Conversation, character identity, and existing history survive Agent being off. |
| D08 | Agent works independently when Converse is off or unloaded. | Direct Agent Chat, tools, approvals, and execution must not require a voice model. |
| D09 | Agent Chat connects directly to Kinaiya Agent. | Direct messages bypass Converse and its Decisions router. |
| D10 | Converse should eventually work with other agent runtimes. | Design adapters for Pi, Hermes Agent, OpenClaw, and other compatible workers. |
| D11 | Kinaiya Agent builds its own model/task-specific harness. | Executable extension generation and controlled evaluation are core product work. |
| D12 | The trusted core is immutable to Agent and extensions. | Self-building must not rewrite permission enforcement, goals, or evaluation rules. |
| D13 | Converse prefers local GPU inference; Agent may use local or cloud models. | Resource management and provider adapters are first-class components. |
| D14 | A constrained machine may unload Converse to make room for local work. | The application and standalone Agent remain usable during the handoff. |
| D15 | Permissions are user-configurable, including YOLO mode. | Users can grant broad action autonomy without silently surrendering the protected core. |
| D16 | Flutter is the application framework. | Desktop, web, Android, and iOS share UI/protocol code; native integration is still required where applicable. |
| D17 | Spritesheets and JSON manifests are the character format. | Skeletal and layered animation are not required for this roadmap. |
| D18 | Animation communicates real state; Converse directs emotional expression. | Motion is presentation, not a substitute for task evidence. |
| D19 | Character style is unrestricted by an anime aesthetic. | Painterly, impressionist, pixel-art, abstract, and other styles must work. |
| D20 | Agent controls intelligent UI. | The frontend validates and renders declarative components; expensive UI is optional. |
| D21 | Important events notify; critical events may initiate internet calls. | Explicit user requests can also schedule or condition a call. |
| D22 | Intelligent UI remains usable during a call. | Audio and interactive artifacts share a session without sharing a rendering lifecycle. |
| D23 | Retain Ming 3 Tiny, VibeVoice Realtime 0.5B, Kokoro 82M, and Supertonic 3. | Build a provider interface; do not replace the shortlist with new recommendations by default. |
| D24 | Default brand colors are `#141091` and `#FFFFFF`. | Chat inherits character colors unless the user overrides the theme. |
| D25 | Kinaiya Agent must consume OpenClaw and Hermes-compatible skills. | Target portable Agent Skills packages, preserve supporting files, and explicitly handle runtime-specific requirements. |
| D26 | Users can point Agent at ClawHub or a GitHub repository to install skills. | Support discovery, inspection, pinned installation, activation, updates, removal, and rollback. |
| D27 | MCP is a first-class Agent integration surface. | Connect local and remote MCP servers through the permission/tool broker, independently of Converse. |
| D28 | Agent supports plugins and conventional extension management. | Provide a versioned executable plugin interface and explicit compatibility reporting for foreign runtimes. |
| D29 | Hermes-style slash commands belong exclusively to Agent-facing interfaces. | Share a registry across Agent Chat and the Agent CLI; never expose its parser, autocomplete, or command palette in Converse. |
| D30 | Converse is designed for non-developers. | Use ordinary voice/text conversation, clearly labeled buttons, and approachable settings; technical commands are neither required nor presented as its interaction model. |

**Proposed engineering defaults, not additional owner requirements:** Python for the initial host services, SQLite behind a single local storage service, JSON Schema for contracts, authenticated HTTP/WebSocket control transport, and a WebRTC-based mobile media layer. Specific libraries, versions, and endpoint names remain replaceable implementation choices.

## 3. Scope and non-goals

### 3.1 Initial product scope

The first credible release must include a standalone Agent, a separate modular Converse runtime, asynchronous Decisions, transcript-on-demand coordination, a minimal spritesheet character, direct Agent Chat, and at least one real executable-harness experiment.

Production scope additionally includes configurable permissions, resource-constrained operation, provider setup, durable work, truthful status, character customization, notifications, mobile calling, and optional intelligent UI.

A Meta-backed Agent profile and Meta image-generation integration are desired for the hackathon. Neither the core protocol nor the product must depend on a model name that can only be accessed through a vendor's unrelated subscription application.

Skill loading, MCP client support, plugin interfaces, and slash commands are baseline Agent capabilities, not features dependent on character/mobile work. Sections 31–32 and milestones M1A/M4A define their phased delivery; complete foreign-plugin compatibility is not implied by basic skill support.

### 3.2 Explicit non-goals

- No requirement for native speech-to-speech inference, a giant local multimodal model, or continuous frontier-model inference.
- No constant full-transcript forwarding, periodic LLM-written state reports, or two replicated conversational brains that must synchronize every turn.
- No requirement for a pet representing every subagent. Work animations represent meaningful activity, not a second simulated agent society.
- No skeletal animation, Live2D-style rigging, or combinatorial layered-outfit system in the initial implementation.
- No arbitrary downloaded executable frontend code under the label of intelligent UI.
- No PSTN telephone integration requirement. Internet calls to the companion app suffice.
- No dependence on Converse to approve actions, operate tools, or keep Agent alive.
- No unrestricted self-modification of the trusted core, user goals, benchmark definitions, or permission policy.
- No requirement for every planned feature to ship in the hackathon build.

## 4. System architecture

### 4.1 Logical topology

```mermaid
flowchart TD
    UI[Flutter application: Character View and Agent Chat]
    CV[Converse: personality, dialogue, STT, TTS]
    DS[Decisions: asynchronous rolling-context router]
    SH[Shared transcript and session journal]
    AD[Versioned Agent adapter]
    KA[Kinaiya Agent: protected core and adaptive harness]
    EX[External agent adapter: Pi, Hermes, OpenClaw]
    RM[Resource and provider manager]
    WK[Tools, isolated extensions, and subagents]

    UI <-->|voice/text/character presentation| CV
    UI <-->|direct tasks/results/approvals| AD
    CV -->|append conversation entries| SH
    SH -->|new conversation entries| DS
    DS -->|routed references, not full histories| AD
    AD <-->|retrieve context and append operational facts| SH
    AD <--> KA
    AD <--> EX
    KA <--> WK
    SH -->|relevant outcome references| CV
    RM -.-> CV
    RM -.-> DS
    RM -.-> KA
```

### 4.2 Runtime boundaries

**Converse service** owns personality, conversational wording, voice selection, performance direction, and character-expression intent. Its inference workers may be unloaded without deleting its durable identity or session.

**Agent service** owns work, tools, subagents, proactive assessment, intelligent UI specifications, and harness-building. It exposes a direct work interface. Its neutral work responses must remain useful when Converse is absent.

**Decisions service** consumes conversation entries and emits routing decisions. It is not a second assistant, a permissions engine, or an authority on task success.

**Session/storage service** exposes the shared transcript and journal through a lightweight authenticated API. It assigns ordering, persists acknowledgments, and supports bounded retrieval. This is infrastructure, not another model.

**Resource manager** coordinates memory and inference admission. It can operate without any conversational model loaded.

**Flutter shell** renders character motion, chats, tools, approvals, notifications, and optional artifacts. It must remain responsive through model loading, unloading, failure, and provider changes.

Separate runtimes do not require a fleet of containers. A single launcher may supervise several local processes. Native dependencies and model execution can use additional workers where isolation or memory reclamation requires it.

### 4.3 Independence requirements

A clean Agent-only installation must not import or initialize STT, TTS, character rendering, or Converse inference. A Converse installation with an external agent must not need Kinaiya's adaptive-harness implementation.

Disabling either service must produce a clear capability change, not a broken session. A frontend reconnection retrieves the existing session and outstanding actions rather than starting duplicate work.

## 5. Shared transcript as common state

### 5.1 The central coordination rule

**The shared transcript is the common conversational state. Models access it selectively; they do not continuously send their entire contexts to each other.**

Every user/Converse exchange is persisted once. Decisions observes new entries with a bounded rolling context. Agent receives an actionable reference only when routing requires it and may request more history when useful.

`ignore` means no Agent disturbance. It never means deleting the exchange from the shared record.

Agent's private execution stack, model KV cache, tool handles, and harness scratch space do not need to be serialized into conversation turns. Important factual outcomes can be appended as typed journal entries alongside the transcript. This is minimal execution bookkeeping in the same session infrastructure, not a replacement shared-state system or a recurring LLM summarization loop.

### 5.2 Storage model

**Proposed default:** a local SQLite database owned by the storage service, with append-oriented entries, transactional writes, indexes, bounded queries, and an outbox for durable delivery. Remote clients use the service API rather than sharing the database file over a network.

Use one ordered journal per workspace with session/thread identifiers. Visible conversation is a filtered view of that journal. Source annotations preserve who said what, which device delivered it, whether it was final, and how much generated speech actually played.

Minimum entry kinds:

| Kind | Meaning |
|---|---|
| `conversation.user` | User text or finalized speech transcription. |
| `conversation.converse` | Converse-generated dialogue with delivery metadata. |
| `conversation.revision` | Correction to an earlier transcription or message. |
| `delivery.updated` | Audio/text delivery status, including interruption. |
| `routing.decided` | Decisions result linked to its source entries. |
| `agent.command` | Accepted direct/routed command and its provenance. |
| `agent.outcome` | Meaningful result, failure, blocked state, or completion evidence. |
| `approval.requested` / `approval.resolved` | Action-specific permission lifecycle. |
| `artifact.updated` | Versioned artifact/UI reference, not large binary payloads. |
| `resource.changed` | Relevant model-availability or resource-handoff event. |

Large audio, image, document, and diagnostic artifacts live in a separate local content store and are referenced by ID/checksum. Raw audio recording is off by default beyond the temporary processing buffer; retaining it requires an explicit setting.

### 5.3 Retrieval, privacy, and corrections

Agent retrieval supports recent entries, ID ranges, a cursor, thread filters, task-linked entries, and search. Every query has a maximum entry/token budget. The full retained transcript is available on demand within the granted scope; it is not the default payload.

Converse can retain its own efficient generation cache, but the shared transcript remains recoverable history. Derived summaries are disposable, provenance-linked accelerators, not a competing authority.

Append revisions rather than silently overwriting already-routed text. Resolve references against the current revision before side effects. Distinguish model-generated speech from speech the user actually heard.

Retention/export/deletion are user-controlled. Append-only during normal operation does not mean the user can never delete personal data. Privacy deletion must also remove relevant indexes, derived caches, and retained artifacts according to the selected policy.

### 5.4 Ordering and delivery

Each committed entry has a stable ID, monotonic journal sequence, origin, session, and causal references. Wall-clock timestamps are useful metadata, not the ordering authority.

Use at-least-once delivery with idempotent handlers and durable acknowledgments. Do not claim end-to-end exactly-once execution for arbitrary third-party tools. Deduplicate commands and reconcile uncertain external results before retrying irreversible actions.

Connection loss does not erase an entry. A consumer resumes from its cursor. Session reset creates a new epoch rather than reusing IDs that an old worker might still publish against.

## 6. Asynchronous Decisions

### 6.1 Responsibility

Decisions reads every new conversational prompt/message from the user and Converse asynchronously. It may use recent exchanges, referenced tasks, and compact factual metadata to resolve context such as “actually, cancel that.”

Converse never waits for Agent or for Decisions to finish classifying routine dialogue. This does not eliminate STT, inference, or endpoint-detection latency; it eliminates an unnecessary serialized dependency and any artificial waiting period added solely for Agent coordination.

Laya is the intended initial default, with a provider abstraction rather than a hardcoded dependency. Its documentation describes typed decisions and routing; Kinaiya still needs its own schema, calibration, regression set, and hardware verification. [S1]

Setup should install/configure the selected provider with minimal manual work, after displaying download size and license information. Alternative classifiers remain supported.

### 6.2 Routing semantics

| Route | Meaning | Required behavior |
|---|---|---|
| `ignore` | No current Agent intervention needed. | Preserve transcript; deliver nothing to Agent's work inbox. |
| `queue` | Actionable work/context can wait for a suitable boundary. | Persist a bounded envelope with source references; dispatch immediately when the worker is idle and policy permits. |
| `steer` | Update an existing task's direction. | Target a task/revision; apply at the next supported safe boundary; acknowledge whether it was applied. |
| `interrupt` | Urgent cancellation, pause, or conflicting instruction. | Signal the targeted work immediately, block further conflicting side effects, and report whether stopping actually succeeded. |

The four routes are attention decisions, not a complete action plan. A queued request can start an idle Agent without inventing a fifth conversational classification. A route may carry a minimal faithful instruction summary plus references; it must not invent user intent.

### 6.3 Context and backpressure

The rolling context is configurable by turns, tokens, and/or elapsed time. Pin unresolved references needed to understand an active correction; do not simply retain all history. Do not confuse this text context with the separate audio/video capture buffer.

Every committed conversational message receives a routing disposition. Streaming token updates may be coalesced into stable sentence/message units; partial STT revisions should not cause repeated irreversible commands. Superseded entries remain traceable.

When the classifier falls behind, preserve pending messages, coalesce superseded revisions, and show routing lag. Do not silently skip finalized user messages or wake Agent on every backlog item. A direct UI stop button and direct Agent Chat remain available without classifier inference.

### 6.4 Authority and feedback-loop control

A suggestion spoken by Converse is not proof the user authorized it. Source type and permission scope travel with every route. Model output, quoted text, webpages, and emails cannot manufacture user authorization.

Outcomes spoken by Converse must carry their originating event IDs so Decisions does not turn a completion announcement into a new task or endless interruption loop.

A confidence score is a model signal, not calibrated certainty by default. Ambiguous high-impact changes remain pending clarification or safe suspension. The router records the source, chosen route, target, confidence where available, and an inexpensive reason label for evaluation.

### 6.5 Decisions when components are unavailable

If Agent is off, record the conversation without dispatching or stockpiling tasks for surprise execution later. Decisions may be suspended or run in non-dispatch evaluation mode. Re-enabling Agent does not automatically replay all old conversation as instructions.

If Converse is unloaded, direct Agent Chat bypasses Decisions. If Decisions is unavailable, ordinary conversation remains possible and direct work controls remain available; display that conversational delegation is unavailable rather than pretending it succeeded.

Optional future use of a lightweight classifier for semantic turn-ending is separate from Agent routing and must not turn the latter into a mandatory blocking step.

## 7. Converse and real-time voice

### 7.1 Exclusive ownership of personality

Converse owns the character profile, conversational habits, humor, tone, social continuity, emotional expression, vocal performance directions, and expression/motion requests. The renderer executes those requests locally.

Agent produces facts, task results, plain work responses, and UI intent. It does not adopt Converse's character or maintain a second personality prompt. Decisions classifies; it does not converse.

A character may be playful, reserved, painterly, mechanical, or entirely user-defined. Appearance, voice, and personality can change without resetting unrelated task history. Private memories and credentials are not bundled into shareable character packs.

### 7.2 Pipeline

```text
microphone -> echo/noise handling -> VAD/endpointing -> streaming STT
                                                        |
                                   shared transcript <- finalized/revised text
                                                        |
                                   Converse LLM -> dialogue + delivery intent
                                                        |
                                    text chunker -> TTS -> audio playback
                                                        |
                                         delivered-span acknowledgments

new conversation entries -> Decisions -> selected Agent attention
```

STT, conversational generation, and TTS should overlap where their backends permit it. Do not wait for the entire response when usable, stable text chunks can already be synthesized. Chunking must preserve prosody and avoid splitting control tags, numbers, or incomplete phrases unnecessarily.

Retain a text-only mode. Keep the STT, response LLM, and TTS provider interfaces independently replaceable. Local models are preferred for the immediate personality loop; explicitly configured cloud alternatives must be labeled as transmitting data off-device.

### 7.3 Interruptions and playback truth

The Kinaiya Converse prototype explored interruption-aware playback; Kinaiya must preserve and test that behavior as the architecture evolves.

Each conversational turn has a generation epoch. Each utterance/chunk has an ID. Barge-in cancels obsolete generation and queued audio, stops playback promptly, and prevents late chunks from resuming old speech.

Persist what was generated separately from what played. If fine-grained audio/text alignment is unavailable, record a conservative delivered-span estimate or `delivery_unknown`; do not claim exact word alignment.

Stopping speech is not automatically stopping a background task. “Stop talking” and “stop editing those files” have different effects. A user-facing stop control should expose that distinction.

Model-ready, text-ready, first-decoded-audio, audible-playback, and playback-complete are distinct events. Finishing text generation does not mean the user has heard the response.

### 7.4 Emotional performance

Converse emits dialogue and performance direction in the same inference pass when possible. Keep voice identity stable while varying delivery. A provider adapter maps supported directions; unsupported directions degrade gracefully rather than being spoken literally.

The same expressive intent should inform voice delivery and the character's expression. It must not fabricate work results or override urgent factual content. No random emotion oscillator or extra personality model is required.

### 7.5 Retained TTS providers

| Provider candidate | Initial integration role | Required validation |
|---|---|---|
| Ming 3 Tiny | Retained user-selected synthesis option; may need a non-realtime profile. | Exact checkpoint, runtime, licensing, output quality, and warm/cold latency. |
| VibeVoice Realtime 0.5B | Retained streaming candidate. | Audible onset, uninterrupted streaming, voice consistency, and concurrent-load behavior. |
| Kokoro 82M | Proposed implementation baseline for the first voice slice. | Streaming/chunking behavior, backend portability, and expressive limitations. |
| Supertonic 3 | Retained CPU-oriented option. | Actual supported runtime, CPU load, quality, and interaction latency. |

These are selected candidates, not claims that all meet the same realtime target. Previous screening showed substantial implementation-dependent differences. Preserve measured results with hardware/runtime metadata; do not substitute marketing TTFA or a full-output timing for audible streaming latency.

Do not silently remove a selected model because it is slower. Offer truthful profiles such as realtime, quality/buffered, and CPU fallback. Other TTS providers can be added later without changing the protocol.

## 8. Agent independence and adapter protocol

### 8.1 Standalone Agent

Agent must support a normal work loop: receive an objective, inspect context, plan, invoke authorized tools, observe results, revise its approach, and produce an evidenced outcome. It must be usable through Agent Chat and a minimal CLI/API without Converse.

Initial tool surface: scoped filesystem access, repository search, command execution, patching, test execution, and artifact creation. Email, browser research, document workflows, and other connectors expand this surface later. Credentials and permissions stay outside model prompts wherever possible.

The seed harness must be functional before it attempts to improve itself. Self-building is not permission to leave the base agent unusable until an experiment succeeds.

### 8.2 Adapter contract

The common protocol should support:

| Operation | Contract |
|---|---|
| `capabilities` | Declare supported commands, steering, cancellation, approvals, artifacts, and event protocol version. |
| `submit` | Accept an objective with an idempotency key, scope, source references, and execution policy. |
| `steer` | Accept a task-targeted revision or explicitly reject/defer it. |
| `interrupt` | Request pause/cancel; distinguish request received from operation stopped. |
| `status` | Return bounded factual operational status without invoking an LLM merely to summarize it. |
| `events` | Stream/replay outcomes, tool activity, approvals, and artifact references. |
| `resolve_approval` | Resolve one authenticated, unexpired action request. |
| `read_transcript` | Retrieve authorized shared context on demand. |
| `artifacts` | Fetch result metadata/content under access controls. |
| `close` | Detach, checkpoint, or shut down according to explicit lifecycle semantics. |

The protocol must not assume every external runtime supports live steering, resumable cancellation, or Kinaiya-style self-building. Capability negotiation exposes limitations instead of simulating success. An adapter's buffering behavior must be visible.

Implement Kinaiya's adapter first and prove portability with one real external adapter before claiming broad compatibility. Pi, Hermes Agent, and OpenClaw are intended integration targets, not already integrated dependencies.

### 8.3 Subagents

Subagents have task IDs, budgets, inherited/restricted capabilities, isolated working context, and explicit result channels. They may use different models and harness versions. Their completion is evidence for the parent, not automatic completion of the user's whole objective.

Agent coordinates subagents without requiring a character per worker. Parent acceptance criteria and dependency checks decide when a composite task is finished.

### 8.4 Skills, MCP, plugins, and commands

Connecting Converse to an external agent is different from Kinaiya Agent using skills originally written for that agent. Both are required directions of interoperability. The skill manager, MCP host, plugin host, and command registry belong to Agent infrastructure and remain usable with Converse absent. Their contracts are specified in Sections 31–32.

## 9. Action truth and completion reporting

The shared transcript records speech, but a spoken claim cannot create an execution fact. Converse saying “done” must not mark a task complete; an Agent model declaring success must not bypass required evidence.

### 9.1 Minimal operational lifecycle

Use a task-local lifecycle such as:

```text
accepted -> queued -> running -> verifying -> succeeded
                          |          |
                          +-> blocked/awaiting_approval
                          +-> failed
                          +-> cancel_requested -> cancelled
                          +-> paused -> running
```

A meaningful transition is appended to the shared journal with the task ID, objective revision, evidence references, and writer identity. UI projections are derived from these facts. There is no recurring model-to-model state-reporting conversation.

Only the runtime can commit execution transitions. A provider request ending or an agent reporting worker becoming idle is not a task-success signal.

### 9.2 Grounded reporting

A completion notice must reference an accepted terminal outcome for the current objective revision. Partial results must be labeled partial. “I will stop it,” “cancellation requested,” and “stopped” are not interchangeable.

Before playback of an action-status utterance, check whether its referenced result/revision is still valid. Drop or regenerate stale notices after new steering, cancellation, or session changes. Do not block unrelated social conversation on task execution.

Use typed, grounded action notices and safe fallback wording for high-risk status claims. Free-form LLM prose can still imply unsupported success; prompting alone cannot prove this impossible. Regression tests must exercise that failure explicitly, and unsupported claims must be logged and corrected rather than described as solved by a schema.

### 9.3 Simultaneous work and conversation

Converse can continue unrelated dialogue while Agent works. Ordinary progress can remain ambient. A verified completion becomes a short, relevant update at a suitable speech boundary; urgent requests use the established interruption policy.

Direct Agent Chat displays the worker's own results and approvals immediately without routing through Converse. A shared task reference lets Converse discuss the work later without copying the entire Agent Chat into its prompt every turn.

## 10. Self-building executable harness

### 10.1 Core versus harness

The immutable core supplies trusted supervision and constrained interfaces. The harness determines how a model works within those interfaces.

Allowed adaptation areas include prompt construction, task decomposition, context selection/compaction, retrieval, tool wrappers/parsers, verification workflows, retry strategies, planning, provider integration, and subagent coordination.

The harness may propose scheduler helpers, but cannot silently rewrite the protected schedule or objective of an existing user responsibility. New schedules and altered responsibilities must flow through the ordinary authorization/control interface.

### 10.2 What counts as executable self-building

A candidate must be able to contain executable source code, dependency declarations, a typed entry point, a manifest, tests, and compatibility metadata. Python is the proposed first extension language; the interface should not permanently assume Python-only workers.

Examples: an extension that selects relevant repository context, parses a flaky tool's output, implements a model-specific structured-call recovery strategy, or coordinates a specialized verification workflow.

Changing only a temperature field or selecting a prewritten template can be useful configuration, but does not demonstrate the executable self-building research objective.

### 10.3 Model/task specificity

A harness selection key includes model identity/revision, provider, relevant capability profile, inference/runtime settings, task family, extension dependencies, and protocol version. Quantization and local runtime differences may matter and should be recorded when known.

Do not assume one harness per model is sufficient. One model may use distinct coding, research, or email harnesses; subagents may use their own variants. Unknown model fingerprints fall back to a tested seed harness rather than inheriting unverified claims.

### 10.4 Lifecycle

```text
observe -> propose -> build isolated candidate -> test
        -> compare against pinned baseline -> reject or promote
        -> monitor -> retain or roll back
```

A candidate never edits the active harness in place. Every version is content-addressed or otherwise immutable after registration. Promotion changes an active pointer through the supervisor; running tasks remain pinned unless a tested checkpoint migration explicitly permits switching.

The Agent can autonomously create experiments within user budgets. Automatic promotion is configurable; initially, use review-required promotion until the evaluation boundary and rollback are proven.

## 11. Immutable core and security boundary

### 11.1 Protected responsibilities

| Protected surface | Requirement |
|---|---|
| User objectives and acceptance criteria | Revisioned, authenticated changes only; extensions cannot redefine success. |
| Permission policy and credentials | Enforced by the supervisor/tool broker, not by prompts inside the extension. |
| Task identity and lifecycle | Only authorized runtime transitions; extensions submit observations. |
| Extension activation and provenance | Validate manifests, hashes, capabilities, compatibility, and promotion decisions. |
| Evaluation definitions and scores | Candidate cannot alter hidden tests, expected outputs, or the authoritative scoring process. |
| Event provenance and audit | Extensions cannot impersonate the user or erase evidence of their own actions. |
| Resource and spending limits | Enforced outside candidate code. |
| Cancellation and recovery | Remain available when candidate code hangs or crashes. |

Keep the trusted computing base small. Rich planners, builders, and evaluators can be separate components, but their protected configuration and authority must not be writable by the candidate they supervise.

### 11.2 Immutability is a privilege boundary

A “do not edit core” prompt, a Git branch, a checksum checked by the same untrusted process, or an ordinary subprocess with unrestricted host permissions is not an adequate immutability boundary.

Run generated extensions under a constrained execution identity/sandbox. Expose scoped workspaces, brokered tools, bounded network access, and short-lived capabilities. Protect core code, owner configuration, evaluator assets, credential stores, supervisor IPC, and update mechanisms from extension writes.

Prevent indirect paths such as a tool shell modifying core files, installing startup hooks into the host, changing protected cron entries, accessing container-control sockets, or retrieving supervisor credentials. Canonicalize paths and test symlink/path traversal and dependency-install escape cases.

The exact sandbox mechanism is an implementation decision validated per platform. If equivalent isolation is unavailable, executable extension execution must be disabled or explicitly labeled experimental; do not claim a protected production mode without it.

### 11.3 User control and threat-model limits

The owner can deliberately install a signed/verified core update or change policy through a separately authorized control plane. “Immutable” means immutable to the runtime agent and generated extensions, not impossible for the developer or device administrator to update.

YOLO mode can grant broad action autonomy within that boundary. It does not silently grant the running model the updater's authority or the ability to change evaluation outcomes.

A device owner with administrator access can deliberately dismantle local protection. Production guarantees must state that limitation rather than claiming resistance to the host administrator.

## 12. Harness evaluation and promotion

### 12.1 Independent evaluation

The candidate may author its own unit tests, but those tests alone cannot qualify it for promotion. Use protected regression tests, held-out tasks, environment checks, and independently collected measurements.

Record baseline and candidate versions; model/provider settings; input fixtures; allowed tools; token, time, cost, and resource budgets; tool traces; outcome artifacts; and the scoring-policy version.

Keep development examples separate from held-out evaluation. Restrict candidate network/secret access during evaluation and avoid exposing expected answers through logs or mounted files. Repeated adaptive access can overfit even a fixed holdout; rotate protected test sets and disclose the selection process in research reports.

### 12.2 Promotion policy

Required gates:

1. Contract, dependency, integrity, and sandbox checks pass.
2. No permission, objective-integrity, or evidence-provenance regression is observed in the required suite.
3. Task quality meets the predefined acceptance threshold.
4. The candidate meets a predeclared improvement or non-inferiority criterion under comparable budgets.
5. Resource use, cost, and failure behavior stay within configured limits.
6. Rollback and previous-version recovery are verified.

Thresholds belong to owner/developer policy, not candidate code. Do not select the success metric after seeing which number improved. Include failed candidate-generation attempts in total adaptation cost.

A stronger model writing a harness for a weaker model is allowed, but its creation/evaluation cost must be reported separately and amortization must not be hidden.

### 12.3 First executable experiment

**Proposed first experiment:** let Agent create a repository-context selection extension that chooses relevant files/snippets under a token budget. Compare the generated extension against a pinned baseline on unseen repository tasks using the same target model and tools.

This is intentionally narrow enough to inspect and roll back, while still requiring actual code generation and execution. A valid demonstration includes a rejected candidate or a rollback test, not just a polished successful run.

Later experiments can adapt tool recovery, planning, or subagent verification. No initial promise is made that any particular model benefits from a particular strategy.

## 13. Permissions, autonomy, and YOLO mode

### 13.1 Operating modes

| Mode | Behavior |
|---|---|
| Agent off | Converse works; no Agent work dispatch or proactive monitoring. |
| On-demand Agent | Executes explicit user requests; unsolicited work is disabled. |
| Autonomous Agent | Pursues authorized standing responsibilities and can initiate approved alerts/work. |

Permission presets are separate from these modes. A user may choose restrictive permissions or broad YOLO permissions for either on-demand or autonomous work.

Turning Agent off requires an explicit active-work policy: finish accepted work, pause at supported boundaries, or request immediate cancellation. Show the actual outcome. Completed external effects are not undone merely because cancellation was requested.

### 13.2 Permission model

Permissions cover filesystem roots, shell execution, network destinations, connector/account scopes, sending/publishing, destructive changes, spending, model/provider data disclosure, image generation, notifications/calls, and extension execution/promotion.

Default to least necessary scope with visible confirmation for consequential actions. Let the user change those defaults, including enabling YOLO for selected workspaces or tools. Explain exactly what the selected mode grants.

Approval requests bind to the action parameters, task/objective revision, expiry, and request ID. Changing the action invalidates a previous narrow approval. A reconnect or replay cannot spend the same approval twice unintentionally.

### 13.3 User choice without confused authority

Decisions decides relevance, Agent proposes/executes work, and the permission layer decides authorization. External content and Converse suggestions do not inherit user authority.

Provide an authenticated emergency stop and a way to revoke future tool access. Do not rely on the Decisions model or on a loaded Converse model for those controls.

Document-drafting workflows should distinguish drafts from filed/sent documents and support source inspection and human review. Financial or legal examples in demonstrations must use synthetic data and must not silently perform consequential actions.

## 14. Inference providers and resource management

### 14.1 Provider configuration

Provider adapters separate STT, Converse LLM, TTS, Decisions, Agent LLM, and image generation. Support local inference and configured cloud APIs without assuming all vendors implement identical streaming, tools, cancellation, or accounting.

Intended Agent provider targets include Meta, OpenRouter, Groq, and compatible local servers. Muse Spark 1.3 Contributor is the user's desired development profile, subject to verifying the account's actual available model ID and applicable terms. Do not hardcode unverified pricing, training-data policies, or access claims into Kinaiya.

Provider setup must show endpoint, model/revision where available, capabilities, credentials source, estimated download/runtime requirements, privacy implications, and a connection test. Store secrets in an appropriate local secret store, not character packs or tracked configuration.

### 14.2 Resource policies

Offer user-selectable profiles:

- **Conversation priority:** keep Converse responsive; queue heavy local work or use an explicitly allowed cloud Agent.
- **Local work priority:** allow resource handoff and temporary unloading of Converse.
- **Local-only:** never silently send work to a cloud provider to resolve pressure.
- **Configured hybrid:** use only providers and data-disclosure scopes the user has authorized.

These profiles govern behavior; they do not imply hardware-enforced GPU priority exists on every backend. Admission control and actual measurement are required.

### 14.3 Safe handoff

Before admitting a resource-heavy job, account for loaded weights, KV caches, runtime buffers, model warmup, graphical rendering, and observed headroom. Detect latency contention as well as memory exhaustion.

When unloading Converse is necessary:

1. Show the reason and selected policy; acknowledge through Converse if it is currently available.
2. Finish/cancel playback at a safe boundary and persist delivery/session metadata.
3. Stop new conversational inference, unload the required workers, and verify memory release.
4. Admit local Agent work while keeping the shell, journal, controls, and direct Agent Chat alive.
5. On completion, checkpoint, or preemption, release the worker's resources and restore the user's configured conversation profile.
6. Resume from shared history without replaying stale spoken responses.

Model loaders may retain allocations; merely dropping an object is not proof memory has been freed. Use supervised worker restart when required by the backend.

An urgent incoming/outgoing call may require pausing local work to restore voice. If the policy does not allow that or loading fails, send a truthful notification/text fallback rather than connecting a silent call.

### 14.4 No deadlock on approvals

Agent must ask for and receive clarification through the shell/Agent Chat without loading Converse. Tool approval, cancellation, and job inspection cannot depend on GPU availability.

Converse being off by user choice must not be overridden just because Agent wants to speak. Respect the selected delivery fallback.

## 15. User experience and navigation

### 15.1 Primary views

**Character View** is Converse's personality-focused surface: voice, optional Converse text entry/transcript, an animated character, and minimal surrounding controls.

**Agent Chat** is the direct productivity surface connected to Kinaiya Agent, or the selected external agent through its adapter. It provides instructions, tool activity, subagent inspection, artifacts, approvals, and cancellation.

The later decision that Agent Chat connects directly to Agent takes precedence over the earlier idea that every chat view is merely the same Converse conversation. A separate top-level Converse Chat screen is optional, not a prerequisite for the initial UI.

Use explicit channel labels. Changing views must not accidentally redirect an unfinished draft to a different recipient. Linked task references connect the experiences; do not merge the histories or silently replay one thread into the other.

### 15.2 Character-to-chat transition

Clicking/tapping the character can make it approach, open a book, unroll a scroll, project a panel, or perform another pack-defined transition that opens Agent Chat. The transition is skippable and never delays urgent interaction.

A sidebar control returns to Character View. Speech and background tasks continue across navigation where their runtimes are enabled.

In Agent Chat, a compact mascot can occupy the send-button position. Its accessible label and behavior must remain unambiguous: submit nonempty input; expose stop/cancel separately while work runs. Do not turn an empty send action into an undocumented navigation surprise.

### 15.3 Branding and themes

Ship one original, replaceable mascot. Default colors are deep blue `#141091` and white `#FFFFFF`; do not substitute Meta's brighter palette. Meta integration is technical, not a requirement to copy Meta's product branding.

Theme precedence: explicit user override, character-pack palette, Kinaiya default. Derive legible light/dark surface variants and check contrast. Do not require image regeneration to change UI colors.

An abstract or painterly character must receive the same tools and layouts as an anime or pixel-art character. Character aesthetics are not capability tiers.

### 15.4 Accessibility and presentation controls

Support reduced motion, animation skipping, captions, keyboard navigation, readable focus states, screen-reader labels, scalable text, and a non-character fallback. Flutter provides accessibility-testing and platform-integration facilities, but they still require deliberate implementation and testing. [S2] [S3]

An optional minimized floating companion is a later desktop surface, not a replacement runtime. Hiding the main window does not stop authorized work. Platform support must be tested rather than assumed from Flutter compilation alone.

### 15.5 Command palette and extension management

Agent Chat provides `/` autocomplete, searchable command help, argument hints, and an Agent-only non-text command palette backed by the same registry as the interactive Agent CLI. A skills/MCP/plugins management surface supports discovery, install/configure, enable/disable, health, update, and removal. These ordinary application controls do not depend on intelligent UI generation or on Converse being loaded. Sections 31–32 define behavior and permission boundaries.

### 15.6 Converse is command-free and non-developer-friendly

Converse has no slash-command parser, command palette, slash autocomplete, or requirement to learn technical command names. This applies to Character View, any Converse text interface, the floating companion, and Converse's mobile/call surfaces. Plugin installation must not add command UI to those surfaces.

Users interact through ordinary speech or text and clearly labeled controls. For example, they can say “stop working on that,” ask “what are you doing?”, change a voice through settings, or approve an explained action through a button. Natural-language task requests still follow Decisions and the existing permission rules; the interface must not ask users to retype them as slash commands.

Keep essential controls, account setup, and permission explanations approachable. Advanced Agent tooling is available when the user deliberately opens Agent Chat or the Agent CLI, not imposed on the conversational experience. Both experiences can access authorized capabilities without forcing non-developers to operate the technical command interface.

## 16. Character packs and animation

### 16.1 Pack format

A pack contains a versioned JSON manifest and spritesheets. It may include reference artwork, static backgrounds, portraits, and metadata. A ZIP is a packaging convenience, not permission to execute bundled code.

Required metadata: pack/version IDs, schema version, display name, author/provenance, asset rights/license information, file checksums, frame dimensions/rectangles, anchors, scale limits, animation timing, semantic mappings, fallbacks, theme colors, and voice/profile references.

Keep secrets and private memories outside the pack. Personality/profile data belongs to Converse configuration and may be exported only by explicit choice. Importing artwork must not automatically overwrite personality or permissions.

### 16.2 Semantic activity mapping

| Semantic activity | Example presentation |
|---|---|
| `idle` | Looking out a window, reading, resting, or wandering. |
| `listening` | Turning toward the user or leaning forward. |
| `warming` | Walking toward the foreground. |
| `speaking` | Expression and mouth/body motion synchronized to played audio. |
| `working.inspect_repo` | Detective attire and a magnifying glass. |
| `working.write_code` | Working at a typewriter. |
| `working.reason` | A thinker pose on a large rock. |
| `working.read_email` | Reviewing letters or sorting papers. |
| `working.draft_document` | Writing at a desk. |
| `blocked` | Waiting with a visible request for input. |
| `alert.important` / `alert.critical` | Pack-defined attention gestures consistent with urgency. |

These are illustrations, not required costumes. Every pack maps common semantics to its own visual style. Support multiple variants with weights, cooldowns, and optional conditions. Avoid rapid costume switching during short tool calls.

Reasoning animation indicates an observable work phase, not access to a model's private thoughts. Missing backend activity detail falls back to generic work rather than fabricated specific actions.

### 16.3 Warmup choreography

The character can start far away and approach while models warm. If inference becomes ready before it arrives, a hop/plop/short transition can bring it forward. Do not wait for a long walking animation before starting available speech.

If the animation finishes before inference is ready, display an honest loading/waiting state. Animation does not remove actual model latency or justify hiding a loading failure.

### 16.4 Renderer requirements

Use local deterministic animation playback, not per-frame LLM calls. Keep speech, activity, emotion, and position as presentation inputs so combinations do not require a giant semantic state machine.

Bake costumes/props into sheets for the initial implementation. Layered compositing and skeletal rendering are explicitly deferred. Approximate mouth-open/closed animation based on playback amplitude is acceptable where phoneme timing is unavailable; do not claim accurate lip synchronization.

Validate atlas bounds, image dimensions, frame counts, transparency, anchor alignment, loop continuity, and maximum texture/memory budgets. Bad packs fall back to a known-good idle asset rather than crashing the app.

## 17. Image generation and asset expansion

Use a provider interface with Meta image generation as the intended hackathon integration. Resolve the actual model/API availability and asset-use terms during integration; keep model IDs configurable.

Workflow: user approves an identity/style reference, generate core poses/expressions, normalize and validate frames, preview animation, and activate a versioned pack. Generate an entire sheet or individual poses according to measured consistency; no one generation format is assumed reliable without testing.

Converse may request an expressive asset because it fits the character. Agent executes authorized generation work and handles tool use. When Agent is off, existing packs remain usable and the user can still manage/import packs; automatic expansion is unavailable unless separately implemented as an explicit settings operation.

The agent may expand an approved library with new work animations, outfits, actions, or emotions within user-defined spending and appearance-change policy. Use an existing fallback while generation runs. Never put fresh image generation on the normal speech-response path.

Keep the approved identity reference, style direction, generation parameters where available, and asset lineage. Provide preview/accept/revert and atomic pack activation. No new asset may modify the Agent core or request extra permissions simply by being imported.

## 18. Intelligent UI

### 18.1 Ownership and resource policy

Agent decides which information/actions deserve a UI, constructs the specification, and handles meaningful interactions. Converse describes it in character. Flutter renders it.

The user's concern about weak hardware is a requirement: intelligent UI must be optional, bounded, and capable of degrading to text/cards. Basic task controls and approvals must remain available even when intelligent UI is disabled.

Separate inference cost from rendering cost. A weak phone may render a small cloud/desktop-generated dashboard, while a strong desktop may generate it locally. This does not authorize arbitrary heavy graphics or continuous refresh on weak devices.

### 18.2 Declarative contract

Initial components: text, status, table, chart, source link, image preview, document preview, code/diff preview, form, and approval action. Use a versioned schema with explicit data and action bindings.

No arbitrary JavaScript/Dart from the model, unrestricted embedded webpages, or client-side secrets. Unknown/invalid components fail safely to a readable representation. Extensions may implement backend computations, but new frontend widget types require the separate reviewed application/plugin mechanism.

Use stable artifact IDs and revisions. Patch component data rather than regenerating the whole interface for every data point. Bind live sources through authorized backend adapters with sampling/refresh limits and freshness labels.

### 18.3 During a call

Audio and UI operate concurrently in one authenticated session. A user can select a range, inspect a source, or approve a specific action while speaking. Interactions are journaled and delivered to the responsible Agent task.

For the illustrative market scenario, display source, observation time, units, and delayed/stale status. Never present simulated data as live data, or an alert as authorization to trade.

Artifacts persist after the call. Active subscriptions have their own lifetime and budget; end/pause refresh when no longer needed unless the user authorized continued monitoring.

Character View should yield space to work. Move the character aside rather than obscuring a document, dashboard, approval, or accessibility control.

## 19. Proactivity, notifications, and calls

### 19.1 Attention policy

| Situation | Default presentation |
|---|---|
| Routine or low relevance | Silent journal/activity update. |
| Important, not critical | Notification with user-configurable sound and vibration, like a message notification. |
| Critical, time-sensitive, and permitted | Incoming internet call. |
| Explicit “call when X happens” instruction | Call on verified condition, subject to the user's call policy. |
| Explicit “call at time T” instruction | Scheduled call with timezone and missed-run policy. |

Agent owns attention assessment. Decisions' `interrupt` classification addresses worker routing, not the user's notification urgency.

Inactivity alone is not proof an event is critical. Deduplicate related evidence, account for uncertainty, and record why the chosen escalation is justified. An unanswered call is not permission for unlimited retries.

A credible missed-payment warning is an example, not a rule that every email containing mortgage-related words must trigger a call. Separate “message claims X” from “X verified through an authorized source.” Untrusted messages cannot instruct the agent to call an arbitrary number or execute financial actions.

### 19.2 Watches and scheduling

Persist standing responsibilities, source configuration, condition, delivery preference, timezone, last evaluation, dedupe key, retry policy, and ownership. Lightweight event sources/polling wake Agent only when needed; do not run an expensive model continuously to discover nothing changed.

Handle daylight-saving changes, host downtime, expired opportunities, and reconnects. “As soon as” means the defined source-detection latency, not a guarantee of instantaneous knowledge. Show the actual polling/subscription behavior.

Turning Agent off suspends its monitoring. On restart, evaluate whether missed events remain relevant; do not call repeatedly for stale backlog. Any separate background-scheduler-only mode would require an explicit later setting, not a hidden exception to Agent off.

### 19.3 Internet-call architecture

The agent host normally remains on the user's machine. The phone is a thin Flutter companion with microphone/playback, character/UI rendering, controls, and authenticated session access.

Use a signaling/push path plus WebRTC media. LiveKit is a candidate transport, not a required agent framework; its documented Flutter and media/data capabilities can support the transport without replacing Converse or the shared-journal protocol. [S4] [S5]

Plan for NAT traversal/relay, device pairing, expiring call tokens, device revocation, decline/missed-call handling, and network reconnection. A minimal relay can handle reachability without becoming the owner of Agent memory or credentials. Document which traffic the relay can observe; transport encryption is not automatically equivalent to end-to-end encryption through every server topology.

Native incoming-call handling is a separate integration task: iOS uses the relevant PushKit/CallKit path for VoIP; Android requires its calling/audio/background integration. A working Flutter web call does not validate locked-screen mobile incoming calls. [S6] [S7]

Respect platform notification permissions and Do Not Disturb. Do not misuse VoIP pushes as a generic wake mechanism or promise delivery through every force-stop/battery/network condition. Validate supported states on actual target devices.

### 19.4 Voice unavailable

Agent remains functional and can send a standard notification or show an approval in Agent Chat. Where policy permits, it can request a resource handoff to restore Converse for a call. It cannot create a second personality implementation inside Agent as a shortcut.

The always-on host must be awake, connected, and running the required services for host-backed watches/calls. Optional cloud-hosted runtime fallback is future work, not an implicit availability guarantee.

## 20. Persistence, privacy, and operations

### 20.1 Durable state

Persist sessions, transcript/journal entries, routing cursors, command acknowledgments, task/objective revisions, approvals, watch definitions, extension versions/evidence, model profiles, character metadata, artifact references, and user preferences.

Keep working directories and generated artifacts separate from core files. Save recovery metadata before switching models or promoting extensions. Schema migration must be versioned, backed up, and tested against prior fixtures.

### 20.2 Privacy

Local-first is a deployment preference, not a claim that configured cloud providers receive no data. Disclose which audio, transcript ranges, documents, source code, or image references will leave the device.

The entire transcript should not be uploaded to a cloud Agent merely because it exists. Retrieval is scoped and budgeted; explicit full-history retrieval remains possible under the user's policy.

Use redacted operational logs and opt-in diagnostic recording. Do not commit real emails, financial documents, legal materials, private transcripts, provider keys, or voice references to the public repository.

### 20.3 Recovery and observability

Track routing lag, queue size, actual model residency, inference timing, audio underflow, cancellations, pending approvals, tool failures, call delivery, extension crashes, and spend. Logs reference IDs and timestamps; avoid copying sensitive payloads unnecessarily.

A model timeout, classifier crash, or invalid UI spec must not crash the shell or erase work. Show degraded capability, retry with bounded policy, and never silently substitute an unauthorized cloud provider.

### 20.4 Setup and updates

The setup wizard selects hardware profile, providers, default voice/character, Agent mode, permission preset, transcript retention, and mobile pairing. Downloads require explicit size/license information and integrity checks.

Core updates, model downloads, character imports, and generated-harness promotions are different update paths. They have different trust requirements and rollback procedures.

## 21. Protocol and data examples

These examples define intended semantics. They are not a claim that the endpoints or schemas already exist. Final schemas must be generated/tested for both Python and Dart clients.

### 21.1 Shared journal entry

```json
{
  "schema_version": "1.0",
  "event_id": "evt-00481",
  "sequence": 481,
  "workspace_id": "workspace-main",
  "session_id": "session-12",
  "thread_id": "converse-12",
  "epoch": 3,
  "kind": "conversation.user",
  "origin": "authenticated-user",
  "created_at": "2026-10-26T18:41:00-07:00",
  "payload": {
    "text": "Actually, use Python instead.",
    "final": true,
    "revision": 1,
    "input_mode": "voice"
  },
  "causes": [],
  "task_refs": ["task-42"]
}
```

The storage service assigns sequence/provenance after authentication; clients cannot gain authority by supplying an `origin` string.

### 21.2 Decisions envelope

```json
{
  "schema_version": "1.0",
  "decision_id": "decision-00481",
  "source_event_ids": ["evt-00481"],
  "source_revision": 1,
  "route": "steer",
  "target_task_id": "task-42",
  "expected_objective_revision": 2,
  "instruction": "Use Python instead of the previously selected language.",
  "reason_code": "user_changes_implementation_constraint",
  "confidence": 0.91,
  "idempotency_key": "session-12:evt-00481:r1:steer"
}
```

The numerical confidence is illustrative. The receiver validates current task/revision and permissions before accepting the instruction. A later cancellation must prevent an older queued instruction from reviving work.

### 21.3 Grounded outcome

```json
{
  "schema_version": "1.0",
  "kind": "agent.outcome",
  "task_id": "task-42",
  "objective_revision": 3,
  "state": "succeeded",
  "summary": "Implemented the requested change and passed the required test suite.",
  "evidence_refs": ["artifact-diff-42", "artifact-tests-42"],
  "remaining_required_work": [],
  "harness_version": "harness-coding-0007",
  "emitted_by": "agent-supervisor"
}
```

The supervisor authenticates the writer and evaluates the required evidence. JSON text supplied by a model is not sufficient to establish this provenance.

### 21.4 Character manifest excerpt

```json
{
  "schema_version": "1.0",
  "pack_id": "kinaiya-default",
  "version": "0.1.0",
  "display_name": "Kinaiya",
  "renderer": "spritesheet",
  "theme": {
    "primary": "#141091",
    "on_primary": "#FFFFFF"
  },
  "atlases": {
    "work": {
      "path": "spritesheets/work.png",
      "frame_width": 256,
      "frame_height": 256,
      "columns": 8,
      "rows": 4
    }
  },
  "animations": {
    "detective": {"atlas": "work", "frames": [0, 1, 2, 3], "fps": 8, "loop": true},
    "typewriter": {"atlas": "work", "frames": [8, 9, 10, 11], "fps": 8, "loop": true},
    "thinker": {"atlas": "work", "frames": [16, 17, 18, 19], "fps": 4, "loop": true},
    "generic_work": {"atlas": "work", "frames": [24, 25], "fps": 4, "loop": true}
  },
  "activities": {
    "working.inspect_repo": ["detective"],
    "working.write_code": ["typewriter"],
    "working.reason": ["thinker"],
    "working.generic": ["generic_work"]
  },
  "fallbacks": {
    "working.inspect_repo": "working.generic",
    "working.write_code": "working.generic",
    "working.reason": "working.generic"
  }
}
```

This is a work-animation excerpt, not a complete distributable pack. The complete schema also requires idle/listening/speaking fallbacks, bounds/checksums, transition metadata, and provenance. Manifest inputs cannot reference arbitrary host paths.

### 21.5 Executable extension manifest

```json
{
  "schema_version": "1.0",
  "extension_id": "repo-context-selector",
  "version": "0.1.0",
  "entrypoint": "selector:select_context",
  "runtime": "python-worker",
  "contract": "context_selector.v1",
  "requested_capabilities": ["workspace.read", "repository.search"],
  "network_access": false,
  "filesystem_write": false,
  "limits": {"wall_seconds": 10, "memory_mib": 256},
  "evaluation_suite": "context-selection-heldout-v1",
  "activation": "supervisor_only"
}
```

Limits are example values, not universal defaults. The registered package must additionally contain verified artifact hashes, dependency locks, model/task compatibility, lineage, and evaluation references. Requested capabilities are not self-granted capabilities.

### 21.6 Initial service surface

| Surface | Purpose |
|---|---|
| `POST /v1/sessions` | Create a session/thread with explicit channel and policy. |
| `POST /v1/transcript/entries` | Append authenticated entries/revisions. |
| `GET /v1/transcript` | Bounded retrieval by cursor/range/thread/search. |
| `WS /v1/events` | Subscribe/replay relevant events with cursors and filters. |
| `POST /v1/converse/turns` | Submit text to Converse; audio transport is separate. |
| `GET /v1/agents/capabilities` | Discover the configured adapter's actual capabilities. |
| `POST /v1/agent/tasks` | Direct or Decisions-routed submission. |
| `POST /v1/agent/tasks/{id}/steer` | Apply a revision-aware steering request. |
| `POST /v1/agent/tasks/{id}/interrupt` | Request pause/cancel independently of speech. |
| `POST /v1/approvals/{id}/resolve` | Resolve one action-specific authorization. |
| `POST /v1/resources/handoffs` | Request a policy-checked resource transition. |
| `POST /v1/harness/experiments` | Create a budgeted isolated experiment. |
| `POST /v1/harness/promotions` | Supervisor-controlled activation. |
| `POST /v1/artifacts/{id}/actions` | Validate a UI interaction against an artifact revision. |
| `POST /v1/calls` | Create an authorized internet-call invitation. |
| `GET /v1/commands` | Discover authorized commands, argument schemas, origins, and supported surfaces. |
| `POST /v1/commands/execute` | Dispatch an authenticated, scoped command without conversational routing. |
| `GET /v1/integrations` | List skills, MCP connections, plugins, versions, and health. |
| `POST /v1/integrations/plan` | Resolve and inspect an install/update/configuration request without activating code. |
| `POST /v1/integrations/apply` | Apply a revision-bound plan under the selected permission policy. |

Bind locally by default, authenticate even local control endpoints, and restrict browser origins. Do not expose an unrestricted tool-execution server to the network for mobile convenience. Names may change through an explicit schema/version decision.

## 22. Proposed repository layout

```text
Kinaiya/
  Roadmap.md
  LICENSE
  apps/
    kinaiya_flutter/          # Character View, Agent Chat, mobile companion
  services/
    session/                 # shared transcript/journal, retrieval, outbox
    converse/                # personality, dialogue, voice orchestration
    decisions/               # asynchronous routing and calibration
    agent/                   # standalone Agent lifecycle and API
    resources/               # provider admission, residency, handoffs
    relay/                   # optional pairing/signaling/push service
  core/
    policy/                  # protected authorization interfaces
    supervision/             # task lifecycle, provenance, cancellation
    extensions/              # validation/activation/rollback boundary
  packages/
    contracts/               # versioned schemas and generated bindings
    providers/               # model, STT, TTS, image adapters
    agent_adapters/          # Kinaiya, then external runtimes
    skills/                  # SKILL.md loader, source adapters, compatibility
    mcp/                     # MCP clients, transport/auth, capability discovery
    plugins/                 # versioned plugin host and extension integration
    commands/                # shared slash-command registry and dispatcher
    character_runtime/       # manifest validation and semantic playback
    ui_protocol/             # declarative artifact/component contracts
  harnesses/
    seed/                    # functional, pinned baseline harness
    sdk/                     # executable extension interfaces
    examples/                # reviewed examples, not privileged templates
  evaluation/
    fixtures/                # synthetic fixtures safe to commit
    voice/                   # timing/quality harness
    routing/                 # labeled routing scenarios
    harness/                 # baseline/candidate comparison tooling
    compatibility/           # skills, MCP, plugins, command conformance
    security/                # boundary and prompt-injection regressions
  tests/
    unit/
    contract/
    integration/
    end_to_end/
  docs/
    architecture/
    adr/
    providers/
    benchmarks/
    security/
    user-guide/
```

Actual transcripts, credentials, model weights, candidate scratch environments, live held-out answers, and private artifacts belong in ignored user-data locations, not this source tree. Repository folders alone do not establish sandbox boundaries.

The existing repository license is Apache 2.0. Model weights, generated assets, dependencies, and imported character packs require their own compatibility checks; the application license does not automatically relicense them. [S8]

## 23. Implementation workstreams

| Stream | Deliverables | Interfaces consumed | Acceptance focus |
|---|---|---|---|
| Platform/contracts | Schemas, journal service, identity, local auth, CLI, launcher, CI. | None initially. | Persistence, ordering, no duplicate dispatch, independent startup. |
| Converse/Decisions | Provider interfaces, streaming voice, personality profiles, routing, interruption tests. | Journal and capability contracts. | Latency, audible truth, bounded context, no Agent wait. |
| Agent/core | Seed loop, tools, permissions, subagents, direct chat, task evidence. | Journal, providers, protected policy. | Standalone work and truthful completion. |
| Harness research | Extension SDK, sandbox, builder, evaluator, registry, promotion/rollback. | Protected core and seed baseline. | Real executable adaptation under independent evaluation. |
| Ecosystem/commands | Skill/source managers, MCP host, plugin SDK, command registry, CLI/Flutter management. | Tool broker, policy, extension boundary, Agent API. | Portable imports, on-demand loading, useful direct controls, honest compatibility. |
| Experience | Flutter views, spritesheets, transitions, themes, settings, accessibility. | Events and artifact contracts. | No animation-induced latency or routing ambiguity. |
| Remote/proactive | Watches, notifications, pairing, calling, mobile UI. | Agent outcomes, Converse media, policy. | Reliable/authorized attention delivery and graceful degradation. |

Parallel work starts after the shared contracts are sufficient to build deterministic fakes. Use a fake Agent, deterministic Decisions fixtures, and prerecorded test audio to unblock UI and integration work, but label them as test fixtures and replace them in demonstration paths.

## 24. Milestone roadmap

Milestones are ordered by dependencies, not invented calendar dates. All are planned until implementation evidence is linked. Hackathon scope is a slice through these milestones, not a promise to finish production in a fixed number of days.

### M0 — Contracts, provenance, and reproducible skeleton

**Depends on:** none.

Deliver schema versioning, architecture decision records, repository layout, lint/test CI, local configuration/secrets rules, synthetic fixtures, and a supervisor/storage skeleton. Pin the first dependency versions only after compatibility checks.

**Exit gate:** start the shell/storage service with fake providers; append/replay entries; authenticate access; fail invalid payloads; recover a cursor after restart. Document the threat model and benchmark methodology before publishing performance claims.

### M1 — Standalone Agent baseline

**Depends on:** M0.

Implement direct Agent Chat/API/CLI, seed harness, filesystem/search/shell/test tools, permissions including scoped YOLO, explicit task/objective revisions, approvals, cancellation, and outcome evidence. Add one configured cloud provider and a compatible local-provider interface.

**Exit gate:** complete a synthetic repository task and present evidence with Converse not installed or running. Denied operations are actually denied. A crashed tool cannot turn into a success report. Approvals remain usable without a GPU.

### M1A — Skills, MCP, and command foundations

**Depends on:** M1; may proceed alongside M2.

Implement portable skill parsing and progressive loading, GitHub and ClawHub source resolution, pinned install/enable/disable/remove, missing-dependency diagnostics, local and remote MCP client paths, and the shared slash-command registry. Deliver `/help`, `/status`, `/stop`, session/model selection, `/skills`, `/skill`, `/mcp`, and eligible dynamic skill commands. Plugin metadata/namespace contracts are defined here; executable plugin activation follows M4/M4A isolation.

**Exit gate:** install and use representative OpenClaw and Hermes skill fixtures without rewriting their portable instructions; preserve supporting files; complete a real local and a real remote MCP interaction; demonstrate commands in Agent Chat and CLI without Converse. Inspection never executes downloaded hooks, and installing many skills does not inject their full contents into every prompt.

### M2 — Converse and retained voice providers

**Depends on:** M0; can proceed alongside M1.

Implement standalone Converse, character/personality profile persistence, microphone/STT, streaming text generation, TTS/playback, interruption epochs, transcript delivery metadata, and text-only mode. Integrate the first working TTS provider, then all four retained candidates behind the same interface.

**Exit gate:** sustain conversation without Agent; interrupt and resume without stale audio; distinguish first chunk from audible speech; report each model's actual hardware profile and limitations.

### M3 — Decisions and shared-transcript coordination

**Depends on:** M1 and M2.

Integrate Laya or a verified initial provider with rolling context, all four routes, source-aware permissions, durable inbox references, retrieval APIs, revision handling, and loop prevention. Add direct bypass for Agent Chat.

**Exit gate:** a long casual conversation does not repeatedly invoke Agent; a task can be delegated, steered, and cancelled without full-history forwarding or artificial Converse delay; unrelated conversation continues; no completion notice precedes its evidence in the regression suite.

### M4 — Executable self-building harness proof

**Depends on:** M1; contract tests use M0.

Implement extension SDK/manifests, isolated execution, protected evaluation assets, candidate generation, budget enforcement, a versioned registry, promotion, and rollback. Run the first narrow model/task-specific experiment against the seed harness.

**Exit gate:** Agent generates actual executable code; a candidate is evaluated on held-out work; a rejected candidate cannot activate itself; core/objective/evaluator mutation attempts fail; results include adaptation cost and a demonstrated rollback. A negative improvement result is reported honestly, not relabeled success.

### M4A — Executable plugins and ecosystem composition

**Depends on:** M1A and M4.

Implement plugin lifecycle/configuration schemas, scoped hooks, namespaced tool/command registration, source/version locks, update/rollback, and user-defined aliases/approved execution shortcuts. Let the harness compose imported skills and MCP tools, and generate tested compatibility wrappers where needed. Add tested foreign-plugin adapters incrementally rather than claiming universal plugin compatibility.

**Exit gate:** a plugin adds a usable tool and command without modifying the core; a failing plugin can be disabled and rolled back; permissions and protected command names remain enforced; source updates cannot silently widen authority. A harness test records identical external tool/skill versions in baseline and candidate runs.

### M5 — Personality-first Flutter experience

**Depends on:** M0; integrates M1–M3.

Ship an original default mascot, pack loader, minimal activity/expression set, Character View, direct Agent Chat, book/scroll-style transition hooks, exact default colors, theme overrides, reduced motion, and accessible controls. Add one Meta image-generation workflow when the verified API is ready.

**Exit gate:** replacing the pack changes appearance/theme without losing tasks or identity state; app navigation does not recreate sessions or misroute drafts; animation never holds up ready speech; invalid packs fail safely.

### M6 — Resource handoff and reliability

**Depends on:** M1–M3; exercised with M5.

Implement measured resource admission, local/cloud policy, unload/reload, cancellation during handoff, device-loss handling, and recovery after provider failures. Test constrained-memory and concurrent-inference cases.

**Exit gate:** local Agent work can deliberately unload Converse and finish; approval and direct chat still work; Converse resumes from history; no unauthorized cloud fallback; there is no resource/approval deadlock.

### M7 — Proactive work and notifications

**Depends on:** M1, M3, and M6.

Implement standing responsibilities, source adapters, event conditions, scheduled triggers, user-configurable attention levels, sound/vibration notifications, deduplication, quiet hours, missed-run policy, and audit reasons.

**Exit gate:** synthetic important events notify once; critical events produce a permitted escalation request; Agent off really suspends monitoring; stale events and injected source instructions cannot generate notification storms or new authority.

### M8 — Mobile internet calling and in-call artifacts

**Depends on:** M2, M6, and M7; artifact foundation from M5/M9 as needed.

Implement paired-device signaling/media, Android incoming-call integration, iOS integration, call lifecycle, decline/missed/reconnect behavior, and minimal in-call artifacts. Validate hardware and platform requirements before claiming support. Flutter's cross-platform code does not remove platform-specific build tooling. [S2]

**Exit gate:** an authorized host event reaches a paired phone in tested background/locked states; answer enables two-way conversation; a small interactive artifact updates during the call; hangup preserves the artifact without leaking resources; failure falls back truthfully.

### M9 — Intelligent UI and external-agent portability

**Depends on:** M1, M3, and M5; can begin before M8.

Implement declarative component validation, artifact persistence/patches, bounded live data bindings, feature-level resource settings, and one real external Agent adapter. Add remaining adapters after the contract suite passes.

**Exit gate:** weak-client text/card fallback works; invalid UI never gains execution rights; a dashboard and its state survive view/device changes; Converse works with a supported external agent without changing personality code.

### M10 — Production hardening and distribution

**Depends on:** relevant earlier milestones.

Harden multi-session isolation, sandbox support, backup/migrations, installer/update paths, provider/asset licensing inventory, secrets storage, abuse limits, accessibility, documentation, and performance across the supported hardware matrix.

**Exit gate:** a clean-machine installation can complete the documented workflows; recovery and boundary suites pass; supported/unsupported combinations are explicit; versioned benchmark evidence and known limitations accompany the release.

## 25. Research and performance evaluation

### 25.1 Required baselines

| Experiment | Baseline | Candidate comparison |
|---|---|---|
| Voice pipeline | Conventional non-overlapped STT -> LLM -> TTS on the same hardware. | Streaming/chunked pipeline with the same models where possible. |
| Coordination | Prototype-style transcript forwarding/reporting, measured in a test harness. | Shared transcript plus asynchronous selective Decisions routing. |
| Routing | Simple deterministic/rule baseline and labeled human decisions. | Selected Decisions model with/without rolling context. |
| Harness | Pinned seed harness with fixed model and tool budget. | Generated executable harness under matching task/model conditions. |
| Model specificity | One shared harness across tested models. | Model/task-specific harness versions. |
| Resource policy | Isolated model performance. | Full concurrent stack and intentional unload/reload. |
| Expressiveness | Plain dialogue synthesis. | Converse-directed delivery with the same voice/model. |

Do not confound a stronger model, different tools, more tokens, longer time limits, or a better GPU with a harness improvement. Log those variables and run ablations.

### 25.2 Voice measurements

Measure user-end-of-speech to audible response; endpointing delay; STT finalization; LLM time to first useful chunk; TTS first decoded chunk; first audible speech; full synthesis time; RTF; buffer underflow; cancellation latency; and p50/p95 distributions.

RTF is synthesis wall time divided by produced audio duration for the stated measurement boundary. First decoded waveform may contain silence. Buffered full-output latency is not streaming TTFA. Pipeline stage durations overlap, so do not sum them blindly into an end-to-end claim.

Record cold start separately from warm inference. Use short acknowledgments, normal replies, long explanations, emotion changes, names/numbers, interruptions, and background noise. Include all four retained TTS candidates and concurrent Converse/Decisions loads.

**Proposed reference-profile targets, not measured promises:** warm audible TTS onset below 250 ms median and 500 ms p95 for a realtime profile; end-of-user-speech to audible reply below 1 second median and 2 seconds p95; sustained RTF below 1 with no underflow during the defined test. RTF below 0.3 is a stretch/headroom target, not a requirement for every retained provider.

Cold starts and quality/buffered profiles are reported separately. Publish what hardware and backend meet each target. Collect enough repetitions to support percentile claims; three medium utterances are screening data, not a stable p95 characterization.

### 25.3 Coordination measurements

Measure unnecessary Agent wakeups, routing confusion matrix, missed urgent cancellation, time-to-applied steering, bytes/tokens delivered to Agent, retrieval volume, decision backlog, duplicated work, and false completion claims.

Compare against fixed test conversations with long irrelevant stretches and short consequential corrections. The primary efficiency claim is reduced Agent processing of irrelevant conversation, not zero Decisions cost.

### 25.4 Harness measurements

Report task success, independent verification, tool/schema errors, recoveries, wall time, token/API cost, total adaptation cost, resource use, regressions, objective mutation attempts, and rollback success.

Use repeated runs, disclose sample counts and uncertainty, and preserve exact baseline/candidate artifacts. Predeclare pass/improvement thresholds. Never select only the best stochastic run or hide candidates that failed.

### 25.5 Quality and personality

Evaluate naturalness, intelligibility, voice identity across emotions, appropriate pacing, interruption recovery, character/voice consistency, and whether visual waiting behavior helps or merely conceals unacceptable delay.

Use blinded or randomized listening where feasible. Do not optimize personality for dependency, flattery, notification frequency, or time spent conversing. Include disagreement, silence, and respectful boundaries in qualitative evaluation.

## 26. Acceptance-test matrix

| ID | Scenario | Required result |
|---|---|---|
| AT01 | Agent disabled; user talks for an extended session. | Converse/character work; no Agent calls or deferred surprise jobs. |
| AT02 | Converse disabled/uninstalled; user issues a coding task. | Agent completes/requests approval through direct chat/CLI. |
| AT03 | User and Converse exchange irrelevant social dialogue during work. | Entries persist; Decisions evaluates them; Agent is not repeatedly awakened. |
| AT04 | User changes a task constraint mid-execution. | Steering references the correct task/revision and is acknowledged or explicitly deferred. |
| AT05 | Cancel arrives after an older queued instruction. | Old work does not revive; external actions already completed are reported honestly. |
| AT06 | Converse generates “done” while work is running. | It cannot create success; unsupported action notice is blocked/corrected and logged. |
| AT07 | A required subagent is still working. | Parent cannot report full success unless the objective's required dependencies are satisfied. |
| AT08 | User interrupts TTS; old provider chunks arrive late. | No stale playback resumes; delivered transcript reflects interruption. |
| AT09 | Audio stops but Agent task should continue. | Speech cancellation does not implicitly cancel the task. |
| AT10 | Restart/reconnect during command dispatch. | Durable replay does not duplicate an acknowledged side effect. |
| AT11 | Partial STT incorrectly sounds destructive, then revises. | No irreversible action is authorized solely from unstable text. |
| AT12 | An email/webpage asks to change the goal or call a third party. | Untrusted content does not become user authority. |
| AT13 | Decisions crashes or is overloaded. | Conversation/direct chat continue with truthful routing status and durable backlog. |
| AT14 | Resource pressure unloads Converse. | Shell/approvals remain; Agent works; session restores without deadlock. |
| AT15 | A cloud call would violate local-only policy. | Operation waits/fails/falls back locally; no silent disclosure. |
| AT16 | Generated extension edits core, goals, scheduler ownership, or evaluator files. | Boundary blocks it; evidence is retained; candidate is rejected. |
| AT17 | Candidate forges a passing score or success event. | Supervisor ignores unauthenticated claims. |
| AT18 | Candidate improves one metric by exceeding budget. | Promotion fails under the predeclared policy. |
| AT19 | New harness regresses after activation. | Rollback restores the known-good version; running-task provenance stays intact. |
| AT20 | YOLO enabled for a workspace. | Permitted work proceeds without redundant prompts; hard core boundary and revocation remain. |
| AT21 | Invalid/oversized/path-traversing character pack is imported. | Validation rejects unsafe assets; existing character remains usable. |
| AT22 | Model becomes ready before approach animation finishes. | Ready speech is not delayed for choreography. |
| AT23 | User switches Character View and Agent Chat with a draft. | Recipient/session stay explicit; no accidental cross-channel send. |
| AT24 | Generated UI includes unknown components or executable code. | Safe fallback; no code execution or leaked secrets. |
| AT25 | Live artifact source becomes stale. | Staleness is visible; old data is not presented as current. |
| AT26 | Important and critical synthetic events arrive. | Notification/call choice follows policy and deduplication, not keywords alone. |
| AT27 | User explicitly requested a noncritical event call. | Confirmed condition uses the selected call delivery policy. |
| AT28 | Phone is offline, locked, declined, or disconnected. | Supported behavior is tested; bounded retry/missed-call state is truthful. |
| AT29 | User interacts with a dashboard during a call. | Audio continues; actions are authorized and artifact state is preserved. |
| AT30 | External adapter lacks steering/cancellation. | UI reports the actual capability; no false applied/stopped acknowledgment. |
| AT31 | User deletes conversation data or revokes a device. | Relevant retrieval/derived access is removed; revoked credentials no longer work. |
| AT32 | Session is reset while old workers are running. | Old-epoch events cannot contaminate or speak into the new session. |
| AT33 | Install OpenClaw and Hermes skill fixtures from ClawHub and GitHub. | Portable instructions and support files work without a vendor runtime; source/ref/path/hash are recorded. |
| AT34 | Skill requires an unavailable tool, binary, credential, or platform. | Status identifies the missing prerequisite; installation is not misreported as working capability. |
| AT35 | Hundreds of skills are installed. | Discovery remains bounded; only selected instructions/references enter task context. |
| AT36 | A source changes, two publishers share a name, or a skill update conflicts with local edits. | Pinned provenance and namespaces prevent substitution; update/rollback preserve reviewed versions and local forks. |
| AT37 | Inspect an archive/repository containing hooks, malicious paths, or prompt injection. | Inspection does not execute code; unsafe paths are rejected; content cannot authorize itself or alter the core. |
| AT38 | Local and remote MCP servers expose tools/resources/prompts. | Negotiated capabilities, scoped auth, bounded results, namespacing, and availability are handled truthfully. |
| AT39 | MCP server requests extra context/sampling, changes tools, or disconnects during a write. | Policy still applies; definitions are refreshed safely; uncertain side effects are not blindly retried. |
| AT40 | A plugin adds a tool, hook, and slash command, then crashes. | Only granted capabilities operate; the host survives; disable/update/rollback work without core edits. |
| AT41 | Agent Chat and CLI request help/autocomplete or invoke the same command. | Shared metadata and dispatch semantics match, with declared surface-specific differences only. |
| AT42 | A skill/plugin attempts to register `/stop`, `/help`, or another reserved command. | Built-in control wins; the extension remains reachable through its qualified name where permitted. |
| AT43 | `/stop` or `/status` is used while LLM inference hangs and Converse is absent. | Direct controls work without an LLM/Decisions call and report actual runtime state. |
| AT44 | Slash-like text appears in code, a quote, a tool result, or an unknown command. | No accidental command execution; escaped text is literal and unknown commands show suggestions. |
| AT45 | A quick-command alias loops, includes shell metacharacters, or attempts a policy change. | Cycles/invalid arguments are rejected; execution uses explicit policy and authenticated user authority. |
| AT46 | A task is resumed after plugin/skill/harness updates. | Recorded versions remain available or incompatibility is reported; no silent dependency substitution. |
| AT47 | A user enters slash-like text or discusses a command in Converse. | No command parser, command autocomplete, or direct slash dispatch runs; ordinary conversation remains available. |
| AT48 | A plugin adds commands or the user switches between Agent Chat and Converse on desktop/mobile. | Command controls remain Agent-only; Converse retains natural-language input, plain controls, and no leaked command palette or slash shortcuts. |

Contract and deterministic safety tests should run in ordinary CI without paid APIs or GPUs. Hardware/provider tests run in a separate opt-in tier. Synthetic fixtures must remain clearly labeled.

## 27. Hackathon demonstration and release gates

### 27.1 Minimum credible hackathon slice

Prioritize the two research pillars, not maximum feature count:

1. A real modular voice conversation through a retained TTS provider, with interruption support and the default character.
2. Decisions routes a real request by reference while the shared conversation remains available on demand.
3. Agent performs actual work and Converse reports an evidenced result without premature completion.
4. Direct Agent Chat works while Converse is off or resource-unloaded.
5. Agent generates a real executable harness extension, evaluates it against a pinned baseline, and demonstrates safe rejection/promotion/rollback behavior.
6. At least one verified Meta integration is exercised, subject to the actual hackathon requirements and account access.

A small character pack and honest result panel are enough for this slice. Full avatar generation, multiple external agents, all mobile platforms, and rich dashboards must not consume the entire integration budget before the two research claims can be tested.

### 27.2 High-value stretch demonstration

Use a synthetic urgent event to call a paired Android phone, hold a two-way conversation, and show a small interactive artifact during the call. Add iOS only after its native integration is genuinely validated; do not present a browser tab as proof of locked-screen iOS calling.

A Meta-generated character/style variant is another useful stretch demonstration because it connects customization to a real provider capability without putting image generation in the voice critical path.

### 27.3 Evidence package

Release evidence includes the architecture diagram, exact commit/model/backend configuration, voice timing traces, routing comparison, baseline/candidate harness results, sandbox/rollback tests, a reproducible demo script, known limitations, and a feature-status matrix.

Separate live functions, recorded demonstrations, deterministic fixtures, and proposed capabilities in both the UI and presentation. Do not claim universal superiority or production readiness from one successful run.

### 27.4 Release progression

| Release | Required evidence |
|---|---|
| Research prototype inside Kinaiya | M0–M4 interfaces exercised; clear experimental status. |
| Hackathon build | Minimum credible slice above, original/default character, honest Meta integration and measured results. |
| Private alpha | Reliable resource handoff, permissions, persistence, diagnostics, notification basics, clean-machine setup, and M1A skills/MCP/command foundations. |
| Public beta | Validated mobile calling on claimed platforms, at least one external adapter, optional intelligent UI, hardened extension isolation, and M4A plugin lifecycle. |
| Production candidate | Supported hardware/provider matrix, recovery/migration/update paths, privacy/security review, and complete acceptance evidence. |

## 28. Risks and unresolved implementation choices

### 28.1 Principal risks

| Risk | Mitigation / release response |
|---|---|
| STT/TTS latency overwhelms conversational responsiveness. | Measure the full pipeline; keep provider choices; label non-realtime profiles honestly. |
| Decisions misses or misroutes a critical instruction. | Labeled evaluation, source/revision checks, direct stop controls, and ambiguity handling. |
| Model prose invents completion. | Evidence-linked outcome channel, playback validation, explicit adversarial tests, safe fallbacks. |
| Local Agent starves Converse or graphics. | Resource admission, measured headroom, handoffs, and authorized cloud profiles. |
| Extensions escape or game their evaluation. | Independent privilege boundary, protected objectives/evaluation, budget enforcement, rejection and rollback. |
| Self-building grows complexity without benefit. | Narrow experiments, total-cost accounting, held-out comparisons, retain seed baseline. |
| Image generation produces inconsistent frames. | Anchored identity, pack validation, preview, small reusable sets, versioned fallback. |
| Native mobile calling behaves differently from browser tests. | Platform-specific workstream and real-device lifecycle tests. |
| UI generation becomes expensive or unsafe. | Allowlisted declarative components, capability limits, off/text modes. |
| Public repository leaks private development material. | Synthetic fixtures, secret scans, ignored data roots, review before publication. |
| Feature scope obscures both research claims. | Gate the hackathon slice around voice coordination and executable harness evaluation. |
| Marketplace/package content introduces malicious instructions or code. | Pinned provenance, non-executing inspection, scoped execution, explicit activation policy, and rollback. |
| Skill-format compatibility is mistaken for full runtime/plugin compatibility. | Publish tested compatibility levels and missing-capability diagnostics rather than blanket guarantees. |

### 28.2 Choices to resolve through implementation spikes

These are engineering tasks, not reasons to reopen settled product decisions:

- Exact Laya checkpoint, backend, rolling-window defaults, and calibration thresholds.
- First STT and Converse LLM profiles; measured CPU/GPU compatibility of retained TTS providers.
- Exact model IDs, API access, licensing, and data-use settings for Meta and other configured providers.
- Python/backend packaging and the supported sandbox mechanism on each host platform.
- First external-agent adapter, selected by a verified control/event interface rather than a guessed integration.
- WebRTC/signaling deployment choice, relay cost, pairing implementation, and encryption trust model.
- Initial character resolution/frame rate/asset budget and default mascot artwork.
- Promotion thresholds and benchmark fixture sets, locked before evaluating candidate improvements.
- Hardware cutoffs for intelligent-UI levels, defined by measured behavior rather than arbitrary branding tiers.
- Pinned Agent Skills/MCP revisions, OpenClaw/Hermes compatibility fixtures, source-adapter APIs, and plugin SDK contracts.

Document the result of each spike in an ADR with a small reproducible test. Avoid expanding these into an unbounded pre-build research phase.

## 29. Prototype migration and documentation rules

The Kinaiya Converse prototype already has modular backend services, a voice pipeline, and transcript/report-based Agent integration. Its `agent_service.py` generates reports and exposes worker activity; that should not be mistaken for a complete task-execution lifecycle. Those capabilities inform the redesign; they are not claims that the full Kinaiya Agent or adaptive harness already exists. [P1] [P2]

Preserve useful interruption, playback, provider, and benchmark lessons. Review code licensing and dependencies before copying components. Do not carry forward process-global/session-ambiguous state, constant transcript forwarding, or worker-idle-as-completion semantics.

Keep implementation status evidence-based. A checked milestone requires a test/demo artifact or commit reference. Record protocol/schema changes explicitly. Future coding agents must not silently rewrite this roadmap's owner decisions or acceptance criteria to match what they happened to implement.

A user-authorized change may revise the roadmap, objectives, or policies. That is distinct from an autonomous runtime changing its own target. Proposed design changes should identify the affected decision ID and migration consequences.

### Immediate implementation order

1. Establish contracts, storage/authentication, synthetic fixtures, and independent launch commands.
2. Build the standalone seed Agent and a separate minimal Converse pipeline in parallel.
3. Integrate Decisions over the shared transcript and prove truth/order/interruption behavior.
4. Implement the protected executable-extension experiment before treating self-building as a completed claim.
5. Add the minimal character/direct-chat experience and measured resource handoff.
6. Expand into proactive notifications, native mobile calls, asset generation, external adapters, and intelligent UI according to the milestone gates.

Build M1A skill/MCP/command foundations alongside this sequence; do not postpone ordinary Agent extensibility until mobile/character work is finished. M4A adds executable-plugin composition after the protected execution boundary exists.

**Architectural invariant:** one shared conversational record, selective asynchronous attention, personality only in Converse, a standalone self-building Agent, and a protected core that generated harness code cannot redefine.

## 30. Implementation references

External references below ground platform/integration details, not claims that Kinaiya already implements them. Consult the current upstream documentation and pin tested versions during implementation. Product requirements in this document come from the design discussion, not vendor marketing claims.

- **[S1] Laya documentation:** https://nandhakishorm.github.io/laya/ — typed decisions and integration entry points. Consult its own deployment/benchmark limitations before choosing a backend.
- **[S2] Flutter platform integration:** https://docs.flutter.dev/platform-integration — multi-platform development and platform-specific tooling/integration.
- **[S3] Flutter accessibility testing:** https://docs.flutter.dev/ui/accessibility/accessibility-testing — test facilities for labels, contrast, and target sizes.
- **[S4] LiveKit Flutter quickstart:** https://docs.livekit.io/transport/sdk-platforms/flutter/ — candidate Flutter media transport integration.
- **[S5] LiveKit media and data:** https://docs.livekit.io/frontends/build/media-data/ — media tracks, text/data transport, and interaction mechanisms.
- **[S6] Apple PushKit/CallKit guidance:** https://developer.apple.com/documentation/pushkit/responding-to-voip-notifications-from-pushkit — native incoming VoIP handling and system-call integration.
- **[S7] Android Core-Telecom guidance:** https://developer.android.com/develop/connectivity/telecom/voip-app/telecom — native calling lifecycle, audio routing, and foreground integration.
- **[S8] Existing Kinaiya license:** [LICENSE](./LICENSE) — inspected as Apache License 2.0; this roadmap does not modify it.
- **[S9] Agent Skills specification:** https://agentskills.io/specification — portable skill directories, frontmatter, and supporting resources.
- **[S10] OpenClaw skills:** https://docs.openclaw.ai/tools/skills — Agent Skills use and runtime-specific metadata/dispatch behavior.
- **[S11] ClawHub:** https://docs.openclaw.ai/clawhub — registry discovery, versioned skill/plugin distribution, and source tracking.
- **[S12] Hermes skills:** https://hermes-agent.nousresearch.com/docs/user-guide/features/skills — progressive loading, source installation, and conditional activation.
- **[S13] MCP transports:** https://modelcontextprotocol.io/specification/2025-11-25/basic/transports — documented stdio and Streamable HTTP baseline; negotiate and test supported revisions rather than treating this reference as a perpetual latest-version claim.
- **[S14] MCP architecture:** https://modelcontextprotocol.io/specification/2025-11-25/architecture — host/client/server responsibilities and capability boundaries.
- **[S15] MCP authorization:** https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization — remote authorization requirements for the referenced revision.
- **[S16] Hermes slash commands:** https://hermes-agent.nousresearch.com/docs/reference/slash-commands — shared registry, autocomplete, skill commands, and quick-command behavior.
- **[S17] Hermes plugins:** https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins — example of a runtime-specific plugin interface, not a universal plugin ABI.
- **[S18] OpenClaw plugins:** https://docs.openclaw.ai/plugins — runtime plugin integration and compatibility requirements.
- **[P1] Prototype architecture:** https://github.com/rgoza1996/Kinaiya-Converse/blob/master/README.md — inspected README blob `14c4f1fb9b9a1901dbb3be0ed6980644dc54e47f`.
- **[P2] Prototype Agent integration:** [Kinaiya Converse source](https://github.com/rgoza1996/Kinaiya-Converse) — the inspected `agent_service.py` implementation generates reports and exposes worker activity; this is migration context, not a production implementation claim.

No private transcripts, credentials, benchmark archives, or user documents are included in this specification.

## 31. Skills ecosystem, MCP, and plugins

### 31.1 Product requirement and distinctions

Kinaiya Agent must reuse existing agent ecosystems rather than requiring every capability to be rewritten. A user should be able to provide a ClawHub entry or a GitHub repository/path and ask Agent to install the relevant skills. The same lifecycle must be available through explicit controls without an LLM interpreting the request.

These are related but different extension types:

| Type | Purpose | Integration approach |
|---|---|---|
| Skill | Reusable instructions, references, assets, and optional scripts for performing work. | Load compatible `SKILL.md` packages and resolve their actual tool/runtime prerequisites. |
| MCP server | Externally provided tools, resources, and prompts. | Kinaiya acts as host/client and brokers authorized operations. |
| Plugin | Executable integration registering tools, hooks, commands, providers, or bundled skills. | Versioned Kinaiya plugin contract, isolated execution, and explicit foreign-runtime adapters. |
| Adaptive harness extension | Changes how a model plans, selects context, uses tools, or delegates. | Generate and evaluate under Sections 10–12; may compose the other extension types. |
| External-agent adapter | Lets Converse and the application use a different worker runtime. | Section 8 protocol; does not by itself import that runtime's skills/plugins. |

Agent Skills defines a portable `SKILL.md` directory format. OpenClaw and Hermes document support for this family of skills; their additional runtime conventions still require compatibility handling. ClawHub is the registry name used here, including for the user's reference to “clawdhub.” [S9] [S10] [S11] [S12]

These features do not move personality into Agent or require Converse. Installing a skill must not overwrite Converse's personality, import another agent's identity configuration, or enable autonomous operation without the selected policy.

### 31.2 Skill format and compatibility levels

Support YAML-frontmatter/Markdown skill files, preserving the skill directory, relative references, scripts, assets, and bundled licenses. Do not install only the top-level Markdown when the procedure depends on other files. A valid format is not proof that its scripts or external tools will run on the current machine.

Use explicit compatibility states: `ready`, `needs_setup`, `needs_adapter`, `unsupported`, and `disabled`. Parsing/install completion and successful execution are separate statuses. Preserve unrecognized metadata and explain consequential unsupported fields rather than silently treating them as implemented.

Initial compatibility profiles should cover portable Agent Skills; OpenClaw dependency/platform metadata, base-directory substitution, and invocation/dispatch controls; and Hermes tool/toolset requirements and platform/setup metadata. Pin representative upstream revisions and fixtures. Unknown requirements that affect authorization or execution must not be ignored. [S10] [S12]

Map tool aliases only through validated adapters. A skill requesting a tool named `terminal` or a browser capability is not automatically satisfied by an unrelated similarly named tool. Keep any adaptation patch separate from the original package, with lineage, tests, and an explicit compatibility report.

Portable instructions should work without requiring OpenClaw or Hermes to be installed. Skills deeply tied to their internals may require a wrapper or the external runtime; report that limitation honestly. Do not promise that all existing third-party skills/plugins work unchanged.

### 31.3 Installation from registries and repositories

Accepted source classes: ClawHub skill identifiers/URLs; public GitHub repositories and explicit subdirectory/ref selections; authorized private GitHub sources; and local directories/archives. Additional registries can implement the same source-adapter interface later. A repository containing several skills should present candidates rather than arbitrarily enabling all of them.

The installation flow is:

```text
resolve source -> discover packages -> inspect without execution
 -> assess prerequisites/permissions -> fetch pinned bytes -> validate
 -> install inactive -> configure authorized dependencies -> activate -> smoke test
```

Under an appropriately scoped YOLO/automatic-install policy, allowed steps may proceed without repeated prompts. The steps, provenance, and core protections still exist. Ordinary installation should be convenient, not a requirement to manually audit every line each time.

Record canonical publisher/source, repository path, immutable commit or registry version, content digest, license metadata, dependency/configuration requirements, granted scope, install time, and local modifications. Resolve moving tags/branches to the exact installed content. A hash detects change; it does not prove a publisher or package is trustworthy.

Do not execute repository hooks, dependency installers, plugin imports, or downloaded scripts during discovery/inspection. Package setup that executes code is a separate authorized operation in the intended environment. Validate extraction paths, symlinks, download boundaries, file/size limits, and source changes between review and activation.

Registry scan results are advisory evidence, not a guarantee of safety. A package's `allowed-tools` or similar frontmatter is a request/constraint to interpret under host policy, not authority to enlarge the user's grants.

### 31.4 Lifecycle, scope, and efficient loading

Support search/browse, inspect, install, configure, enable/disable, update, rollback, remove, and source management through Agent tools, slash commands, CLI, and Flutter settings. Distinguish profile/user-level installs from workspace-level activation. Use namespaced IDs so identical display names cannot silently substitute for one another.

Updates show changed instructions/code/dependencies/permissions and preserve the prior version. Do not overwrite local forks or files used by active tasks. Running tasks pin relevant skill/plugin versions; apply changes at a declared boundary. Removing an integration reports dependent tasks and handles them explicitly.

Use progressive disclosure: bounded discovery metadata first, full instructions when selected, supporting resources when needed. Large catalogs need retrieval/pagination rather than an unlimited metadata dump. Apply the same principle to large MCP tool catalogs. Never inject every installed skill, tool schema, and reference into every model turn. This is independent of Decisions, whose role remains conversational routing. [S9] [S12]

Skills may be selected naturally by Agent or invoked explicitly via slash commands. Respect invocation-only controls. Keep credentials in scoped secret references; do not ask users to paste API keys into the shared transcript or skill source.

### 31.5 MCP host requirements

Provide a version-negotiated MCP client implementation with local stdio and remote Streamable HTTP support. Additional/legacy transports are explicit compatibility options, not implicit fallback. Use a maintained SDK behind Kinaiya's interface and publish tested protocol revisions. The referenced MCP revision documents these transports and host responsibilities. [S13] [S14]

Required behavior includes server configuration/discovery, capability negotiation, tool listing/calls, resource discovery/read, prompt listing/retrieval, supported list-change notifications, bounded timeouts, cancellation requests, result-size limits, health/reconnect handling, and structured errors. Capabilities not implemented must not be advertised.

Namespace server tools and retain their provenance. Route calls through the same permission/audit layer as native tools. Treat server descriptions, prompts, resources, and returned text as external content; a server's claim that an action is read-only is not sufficient authorization.

Remote authentication must support the applicable OAuth flow or configured credentials according to the server and negotiated protocol. Keep tokens server-scoped, redact them from logs, support revocation, and never forward credentials indiscriminately between servers. Local stdio processes receive only explicitly granted environment/filesystem/network access. [S15]

MCP sampling, elicitation, roots, and other client-side capabilities require explicit implementations and consent/budget policies. A server cannot request the entire transcript, spend unbounded model tokens, or access unrestricted filesystem roots merely because it is connected. A root declaration is not an operating-system sandbox.

Cancellation acknowledgment does not prove remote side effects were undone. Reconcile uncertain write outcomes before retrying. Dynamic tool/schema changes must refresh cached definitions and recheck relevant permissions, not inherit broader access silently.

Expose connection setup, test, reconnect, enable/disable, and remove through `/mcp` and settings. Useful MCP operation must not require the voice or character pipeline. Exposing Kinaiya itself as an MCP server is a possible later feature, not required by this client-support milestone.

### 31.6 Plugins and hooks

Provide a versioned Kinaiya plugin manifest/SDK with entry points, compatibility ranges, dependency locks, configuration schemas, requested capabilities, tools, commands, lifecycle hooks, and optional bundled skills. A plugin may add a connector, event source, tool provider, or workflow integration without editing the immutable core.

Use the protected extension host and tool broker rather than arbitrary in-process imports into the supervisor. Hook subscriptions are bounded by time/resources and declared event scope. A hook cannot rewrite authenticated origin, user permission, objective ownership, or the evidence needed to commit completion.

Skills and MCP are interoperability targets; native plugin APIs are runtime-specific. OpenClaw and Hermes each document their own plugin systems. Compatible assets/skills within a package can be imported where supported; executable foreign plugins require a tested adapter or port. Display `needs_adapter`/`unsupported` instead of claiming generic binary/API compatibility. [S17] [S18]

Plugin registration, activation, upgrade, disable, and removal are audited and recoverable. Plugins may register namespaced tools/commands but cannot replace protected controls or install arbitrary Flutter code. Frontend widget extensions remain subject to Section 18's separate reviewed mechanism.

### 31.7 Relationship to self-building

Kinaiya should first discover an adequate existing skill, MCP tool, or plugin before rebuilding equivalent functionality. Agent may install permitted integrations, compose them into a model/task-specific harness, and generate missing wrappers or new plugins under the existing isolation/evaluation process.

Keep upstream packages pinned/read-only and record generated adaptations as separate versions. An external package cannot redefine harness acceptance criteria. Generated plugins/commands do not self-promote into privileged authority.

For research comparisons, log the complete skill/MCP/plugin dependency set and permissions. Giving a candidate a new external service or stronger tool is not the same experiment as improving a harness under identical capabilities; report such changes separately.

### 31.8 Compatibility evidence and delivery

M1A establishes skill installation, MCP, and command foundations; M4A adds executable-plugin lifecycle and harness composition. Maintain a compatibility matrix by skill metadata profile, runtime/OS, plugin API version, MCP protocol/transport, and command surface.

The first conformance set includes portable skills from both ecosystems, a script-bearing skill, a missing-prerequisite skill, a conflicting-name case, local/remote MCP fixtures, and a plugin with a command/hook. Tests must prove use, not only parsing or listing. AT33–AT48 cover the additional cross-system failure cases and the Agent-only command boundary.

## 32. Slash commands and direct Agent controls

### 32.1 Interaction model and Agent-only scope

Kinaiya Agent's work interface should feel familiar to someone using Hermes: type `/` for discovery, select a command, see argument hints, and invoke a skill by name. Hermes documents a shared registry for multiple command surfaces and dynamic commands for installed skills; this is the interaction reference, not a promise to clone every Hermes command verbatim. [S16]

**Slash commands belong exclusively to Agent-facing interfaces. Converse has no slash commands.** Character View, Converse text input, the floating companion, and Converse's mobile/call experience stay natural-language-first and friendly to non-developers. They do not register a slash parser, display command suggestions, or expose a technical command palette.

Use one versioned command registry for Agent Chat, the interactive Agent CLI, Agent help/autocomplete, and an Agent-only Flutter command palette. Later messaging/mobile surfaces may consume those definitions only when explicitly operating as an Agent interface; sharing the Flutter application or transport does not make commands globally available. No LLM inference is needed merely to discover, parse, authorize, or dispatch a deterministic Agent control.

Each definition includes canonical ID/name, aliases, description, argument schema, execution type, owner/origin, supported Agent surfaces, permission requirements, target scope, confirmation behavior, and availability. Help shows why an installed command is unavailable, such as a missing credential, disabled plugin, unloaded provider, or unsupported adapter capability. Plugin-defined commands inherit the same Agent-only restriction.

### 32.2 Dispatch classes and routing

Separate four command classes:

| Class | Example | Execution |
|---|---|---|
| Runtime control | `/status`, `/stop`, `/model` | Deterministic handler; no Converse/Decisions/model round trip. |
| Skill invocation | `/<skill-name> ...`, `/skill <qualified-id> ...` | Resolve the installed version, load its instructions on demand, and create normal Agent work. |
| Registered tool/plugin command | `/plugin <id> <command> ...` | Validated handler under tool/plugin permissions; model use only when declared. |
| User shortcut | Alias to another command or an approved execution shortcut. | Resolve safely with visible target, bounded expansion, and ordinary authorization. |

Direct Agent Chat commands bypass Decisions because the user has already selected the worker interface. They still enter the journal with command/source/task provenance, without being replayed into another task by the conversational router.

Read-only/help/stop controls must function even when Agent inference hangs or Converse is unloaded. A skill that needs Agent inference reports that it is queued/loading rather than pretending deterministic dispatch completed the work.

Control commands cannot be issued by instructions embedded in a webpage, tool result, model reply, or skill. Equivalent programmatic actions use authenticated APIs and their own granted authority, not a text string pretending to be the user.

### 32.3 Proposed initial command families

The following names are Kinaiya design targets. Their exact options are finalized through the registry contract and tests; they are not commands available today.

| Command family | Intended behavior |
|---|---|
| `/help [command]`, `/commands` | Search/list commands, syntax, aliases, origins, and availability. |
| `/new [name]`, `/sessions`, `/resume <id>` | Create, inspect, and resume Agent sessions with explicit task linkage. |
| `/history`, `/save`, `/clear` | Inspect/export history or clear the visible chat without silently deleting the shared record. |
| `/status [task-id]`, `/tasks` | Show factual worker/task state without LLM summarization. |
| `/stop [task-id]`, `/pause [task-id]` | Request cancellation/pause through the actual runtime capability; distinguish requested from completed. |
| `/model [alias]` | Inspect/select the Agent model/provider profile; preserve Converse's personality and configuration. |
| `/plan <request>` | Start an explicitly labeled planning task under its selected tool/write policy. |
| `/compress` | Refresh bounded Agent model context/derived summaries; do not erase or rewrite shared transcript truth. |
| `/skills search|inspect|install|list|enable|disable|update|remove|rollback ...` | Manage skill sources and lifecycle, including ClawHub and GitHub. |
| `/skill <qualified-id> [request]`, `/<skill-name> [request]` | Explicit on-demand use of an installed, eligible skill. |
| `/mcp list|add|inspect|test|enable|disable|remove ...` | Configure and manage MCP connections and capability access. |
| `/plugins list|inspect|install|enable|disable|update|remove|rollback ...` | Manage executable plugins and their configuration/permissions. |
| `/permissions`, `/yolo on|off` | Inspect/change the selected permission preset through authenticated owner controls. |
| `/harness status|list|evaluate|rollback ...` | Inspect versions, request budgeted experiments, and perform authorized recovery. |
| `/alias ...` | Manage user-defined aliases and explicitly approved execution shortcuts. |

New sessions do not silently cancel old background work. Clearing a view is not privacy deletion. Pause/stop with an ambiguous target presents the active task choices rather than cancelling unrelated work. `/model` cannot bypass local-only policy or silently change all users' defaults. Command-driven permission changes remain owner actions, not tools a model can use to elevate itself.

### 32.4 Dynamic commands, collisions, and customization

Eligible user-invocable skills appear as dynamic slash commands after activation. Preserve manual-only and model-invocation restrictions where the imported profile supports them. Disabled/unconfigured skills are labeled appropriately; they do not masquerade as ready capabilities.

Reserve built-in names and aliases. A skill named `stop` must not intercept `/stop`; it remains accessible through `/skill <qualified-id>` if permitted. Resolve collisions between skills/plugins through explicit namespaces and visible provenance, not install order. The Hermes reference similarly protects built-in command names. [S16]

Plugins can register additional commands through the SDK with argument/permission schemas. A generated harness can propose commands as part of a candidate extension, but registering them follows normal validation and activation; self-building cannot override help, stop, policy, or recovery controls.

Support user aliases to existing commands and optional execution shortcuts. Prefer argument arrays and typed substitution over shell-string concatenation; shell execution must be explicit and permission-controlled. Detect alias cycles, cap nesting, validate arguments after expansion, and show the resolved target for consequential actions. Long reusable workflows should normally be skills rather than opaque command strings.

### 32.5 Parsing, safety, and feedback

Recognize slash commands only from an explicit Agent command input/palette submission, with a documented leading-slash grammar. Converse input is never parsed or forwarded as a slash-command invocation; slash-like text there remains conversational content. Natural-language requests continue to use Decisions and normal authorization, not an implicit command bridge.

Within Agent interfaces, code blocks, pasted paths, quoted messages, and tool output are not commands. Provide a literal-text escape and do not send an unknown `/command` to the LLM as though it were a valid action; show help/suggestions instead. This command-validation behavior must not introduce command-error screens into Converse.

Do not treat command arguments as a shell program unless the user deliberately configured that command type. Handle quoting, Unicode, multiline arguments, and redaction consistently across CLI and Flutter. Sensitive setup fields belong in secure input controls, not saved chat text.

Every command returns an immediate structured acknowledgment or validation error. Long-running work returns a task/install-operation ID and progress, not a premature completion message. Duplicate transport submissions use idempotency keys, and clients show the real result after reconnect.

Command availability is capability-driven for external Agent adapters. Never forward a Kinaiya slash command blindly into another runtime with potentially different semantics. Translate through the adapter or label it unsupported. GUI buttons and slash commands should exercise the same underlying authorized operations so permissions and behavior cannot drift; sharing those operations does not expose command syntax or a command parser to Converse users.

### 32.6 Delivery criteria

Ship the registry, help/autocomplete, direct status/stop controls, session/model selection, and skill invocations in M1A. Add plugin-provided commands and user execution shortcuts with M4A. Add richer Agent UI/messaging presentation only after those shared semantics are tested. AT47–AT48 must also verify that Converse remains command-free after plugin activation, view changes, and mobile integration.

The required result is an Agent that remains practical to operate like a conventional agent while retaining Kinaiya's adaptive harness: users can discover capabilities, install a skill, connect an MCP server, run a workflow, inspect work, or stop it without involving Converse or paying for a model call just to manipulate application controls. Converse remains the separate, approachable conversational experience; users do not need to learn these commands to talk with their character or request authorized work.
