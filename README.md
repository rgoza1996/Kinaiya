# Kinaiya

**A local-first conversational platform with an independent work agent.**

Kinaiya pairs a personable, real-time voice experience with an agent that can do useful work on its own. The design keeps conversation, routing, and task execution in separate components, then lets them share relevant context through a bounded, inspectable transcript.

## Product scope

- **Converse** owns the character, conversational behavior, voice input and output, interruptions, and the user-facing experience.
- **Agent** handles direct chat and authorized work such as repository analysis, coding, research, and document tasks. It remains useful when Converse is off.
- **Decisions** classifies and routes relevant requests between components. It does not speak as the character or grant permission.
- **Shared transcript** gives components a common record. Agent retrieves the context it needs instead of receiving the entire conversation on every turn.
- **Adaptive harness research** explores how Agent can generate and evaluate task- and model-specific executable extensions while protecting the trusted core, permissions, and evaluator.

The long-term product scope also includes resource-aware operation, provider choice, durable work, skills and MCP tools, mobile calling, and optional intelligent UI. Those are planned capabilities, not promises that every feature will ship in one release.


## Mathematical foundations

Kinaiya combines discrete-state coordination, cryptographic integrity, budget-constrained execution, streaming inference, and adaptive-harness research. The equations below distinguish **what the experimental code enforces** from **what the roadmap proposes to evaluate**. Proposed formulations illustrate possible research methods; they are **not implemented algorithms or measured performance claims**.

### 1. Decisions: risk-aware conversational routing

**Status:** A four-way asynchronous classifier and routing worker exist; calibrated probabilistic decision theory is proposed.

Decisions chooses among ignore, queue, steer, and interrupt. A possible risk-sensitive decision rule is:

$$
\mathcal{A}=\{\mathrm{ignore},\mathrm{queue},\mathrm{steer},\mathrm{interrupt}\},
\qquad
a_t^*=\arg\min_{a\in\mathcal A}\sum_{y\in\mathcal A}
P_\theta(y\mid x_t,H_t,z_t)L(a,y).
$$

Here $x_t$ is the current message, $H_t$ is relevant conversation history, $z_t$ is task state, and $L(a,y)$ penalizes choosing route $a$ when the appropriate route is $y$. Missing a cancellation may deserve a larger penalty than an unnecessary interruption. **This loss-based decision rule is a proposed formulation**, not a claim about the current classifier or calibration; model confidence is not automatically a probability. Routing never grants tool permissions.

[Decisions runtime](services/decisions/runtime.py) · [Routing design](Roadmap.md#6-asynchronous-decisions)

### 2. Shared transcript: context selection under a token budget

**Status:** The persistent journal and bounded retrieval exist; optimized snippet selection is proposed for the first self-building harness experiment.

Selecting relevant snippets can be modeled as a 0–1 knapsack problem:

$$
\begin{aligned}
\max_{\mathbf{s}}\quad &\sum_{i=1}^{n}v_i s_i\\
\text{subject to}\quad &\sum_{i=1}^{n}c_i s_i\le B,\qquad s_i\in\{0,1\}.
\end{aligned}
$$

$v_i$ is a snippet's estimated utility, $c_i$ its token cost, $B$ the token budget, and $s_i$ whether to include it. This **illustrates the planned context-selection experiment**; the existing journal enforces limits and retrieval filters but does not solve this optimization problem.

[Journal implementation](services/session/store.py) · [Harness experiment](Roadmap.md#123-first-executable-experiment)

### 3. Event consistency: ordering, revisions, and authorization

**Status:** Implemented invariants and cryptographic action binding.

The journal assigns monotonically increasing sequence numbers, consumer cursors cannot retreat, and applied task steering increments the objective revision:

$$
s_{i+1}>s_i,\qquad c_{t+1}\ge c_t,\qquad r_{\mathrm{new}}=r_{\mathrm{old}}+1.
$$

An action-specific approval is tied to the canonicalized tool name, arguments, and objective revision:

$$
h=\operatorname{SHA256}(\operatorname{CanonicalJSON}(a,\theta_a,r)).
$$

The supervisor accepts it only when its digest and revision match, it has not expired, and it has not been consumed. Hashing helps bind authorization to a particular action; **it does not by itself authorize the action or make the system infallible**.

[Journal implementation](services/session/store.py) · [Policy and approvals](core/policy/__init__.py)

### 4. API economics: conservative admission control

**Status:** Implemented in a development-only Meta API budget proxy; not live provider billing.

For reported input tokens $I$, output tokens $O$, and cached input tokens $K$, the proxy preserves an ordinary-Spark comparison and a Contributor estimate using its configured dollar-per-million-token constants:

$$
\widehat C_{\mathrm{ordinary\ comparison}}=\frac{1.25I+4.25O}{10^6},
\qquad
\widehat C_{\mathrm{contributor}}=
\frac{0.10(I-K)+0.002K+0.20O}{10^6}.
$$

Admission uses twice the verified Contributor input/output rates, ignores cache discounts, and reserves the full catalog context plus bounded output for every outstanding request. Thus $\widehat C_{\mathrm{spent}}=(0.20I+0.40O)/10^6$. This admission estimate, unsettled reservations, and the next reservation must fit the script's **$22 ceiling** within its **$25 development budget**:

$$
\widehat C_{\mathrm{spent}}+C_{\mathrm{reserved}}+C_{\mathrm{next}}\le 22.
$$

These constants reflect this repository's development proxy, **not a guarantee of current Meta prices or actual charges**.

[Budget proxy](scripts/muse_budget_proxy.py)

### 5. Streaming conversation: latency and real-time factor

**Status:** Experimental Converse streaming/chunking exists; the performance goals and broad TTS comparison are research targets.

A purely sequential speech pipeline has approximate latency

$$
T_{\mathrm{sequential}}=T_{\mathrm{STT}}+T_{\mathrm{LLM}}+T_{\mathrm{TTS}}.
$$

Overlapping pipeline stages can reduce perceived response time. The critical-path bound for an idealized dependency graph $G$ is

$$
T_{\mathrm{pipeline}}\ge
\max_{p\in\mathcal P(G)}\sum_{v\in p}t_v.
$$

For synthesis, real-time factor is

$$
\mathrm{RTF}=\frac{T_{\mathrm{synthesis}}}{T_{\mathrm{generated\ audio}}}.
$$

RTF below 1 means synthesis is faster than audio duration. The roadmap proposes warm audible TTS onset below **250 ms median / 500 ms p95**, end-of-speech to audible reply below **1 s median / 2 s p95**, and sustained **RTF < 1** (stretch **RTF < 0.3**). These are **unverified reference-profile targets**, not achieved measurements. Overlapping stages still incur buffering, contention, and scheduling overhead.

[Converse runtime](services/converse/runtime.py) · [Measurement plan](Roadmap.md#252-voice-measurements)

### 6. Local inference: GPU resource admission

**Status:** Explicit CPU voice/AMD Laya handoffs and measured headroom checks exist.
The general scheduling formulation below remains proposed.

Before loading several local workers, a resource manager could enforce a capacity constraint:

$$
M_{\mathrm{weights}}+M_{\mathrm{KV}}+M_{\mathrm{buffers}}+
M_{\mathrm{renderer}}+M_{\mathrm{headroom}}\le M_{\mathrm{VRAM}}.
$$

This is a first-order memory-accounting model, not a complete measure of feasibility: latency contention, fragmented/reserved allocations, and unloading behavior also matter. The intended policies allow queueing, deliberate worker handoff, or **user-authorized** cloud inference—not silent cloud fallback.

[Resource policy and handoff](Roadmap.md#14-inference-providers-and-resource-management)

### 7. Self-building harnesses: controlled optimization and evaluation

**Status:** The bounded seed Agent, isolated executable context selectors, protected evaluation, owner-reviewed promotion controls and rollback are implemented experimentally. The initial 12-case screening study rejected the generated candidate; no improvement was demonstrated. Paired Agent-task optimization and the formulations below remain research milestones.

With fixed target model, tasks, tools, and budgets, compare baseline harness $H_0$ and generated candidate $H_1$ on the same $n$ tasks. For independently evaluated task quality $Q_i$, a paired improvement estimate is

$$
\widehat{\Delta}=\frac{1}{n}\sum_{i=1}^{n}
\bigl[Q_i(H_1)-Q_i(H_0)\bigr],
\qquad
\operatorname{SE}(\widehat{\Delta})=\frac{s_d}{\sqrt n}.
$$

Here $s_d$ is the sample standard deviation of paired task-score differences. Repeated runs, held-out tasks, uncertainty reporting, and controlled baselines are needed before claiming an improvement. If a candidate saves $C_0-C_1>0$ per task after adaptation costing $C_{\mathrm{adapt}}$, a simple break-even estimate is

$$
N_{\mathrm{break\text{-}even}}=
\frac{C_{\mathrm{adapt}}}{C_0-C_1}.
$$

Most importantly, better scores must **never** compensate for weakened permissions, modified objectives, forged evidence, or exceeded budgets. The planned research objective is *constrained*, not an unconstrained reward maximization:

$$
\begin{aligned}
\max_{H\in\mathcal H}\quad&
\mathbb E_{x\sim\mathcal D}[Q(H,x)]\\
\text{subject to}\quad&
\operatorname{Safe}(H)=1,\quad\operatorname{Integrity}(H)=1,\\
&C_{\mathrm{adapt}}(H)\le B_{\mathrm{adapt}},\quad
C_{\mathrm{run}}(H)\le B_{\mathrm{run}}.
\end{aligned}
$$

These are **proposed evaluation formulations**, not a shipped optimization algorithm or proof of safety.

[Seed harness](harnesses/seed/__init__.py) · [Evaluation and promotion policy](Roadmap.md#12-harness-evaluation-and-promotion) · [Research measurements](Roadmap.md#254-harness-measurements)


## Hackathon target

The planned demonstration is a focused slice through the larger roadmap:

1. Real voice conversation with interruption support and an original, replaceable character.
2. A real routed request that uses shared conversation context.
3. Agent completes useful work and reports evidence of the result.
4. Direct Agent Chat remains available while Converse is off.
5. Agent generates an executable harness extension, evaluates it against a pinned baseline, and demonstrates safe rejection, promotion, or rollback.
6. At least one verified Meta integration is exercised if the event requirements, account access, and applicable terms allow it.

Meta is a target integration, not a product dependency. Model and API availability must be verified during implementation; the core stays provider-configurable. A Meta-generated character variant is a stretch demonstration, not a requirement on the voice-response path.

## Status

An initial Linux research implementation now includes versioned contracts, authenticated persistent transcript storage, independent Agent and Converse services, asynchronous routing, scoped tools/approvals, a functional Flutter web client, and real-provider integration tests. The implementation is experimental. The full roadmap, all retained voice backends, native mobile calling and production hardening remain open development gates.

Live listening now uses retained transcript windows with separate Decisions outputs
for conversation timing and Agent routing. It can hold a prepared reply for a
speaking opening, preserve speech during backchannels, steer/cancel work, and
queue follow-ups until a native task boundary. See the
[streaming-window design and verification limits](docs/adr/0019-streaming-window-control.md).

Unfinished live speech requires owner review before file writes, shell commands,
or MCP calls, including with broad tool grants or YOLO. Agent Chat shows current
approvals across conversation sessions. Silence alone does not commit speech;
see the [source-authority boundary](docs/adr/0020-streaming-source-authority.md).

An opt-in [typed Laya Decisions adapter](docs/adr/0021-laya-decisions.md) supports
local Jev inference in a separate process, with native source/task bindings and
explicit classification refusals. The optional [verified local worker](docs/laya-worker.md)
loads a private snapshot of pinned model files and checks artifact identity on
every response without inventing a Hub revision. Base-model and supervised synthetic results
remain research evidence; Laya is not yet promoted as the reference default.

See [Roadmap.md](./Roadmap.md) for architecture, milestones, acceptance criteria, risks, and implementation references.

## Project principles

- Keep the voice experience and work agent independently usable.
- Make permissions and action results explicit; model output cannot create user authorization.
- Preserve provider portability and clearly distinguish verified integrations from targets.
- Measure the adaptive-harness claim against pinned baselines and safe rollback behavior.
- Keep prototype evidence, recorded demos, fixtures, and implemented functionality clearly labeled.

## Run the host

Use Python 3.11+ in a virtual environment:

```bash
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
kinaiya init
# Configure data/config.json with your chosen providers and disclosure policy.
kinaiya --workspace workspace serve all
```

Services bind loopback: session 8760, Agent 8761, Converse 8762 and Decisions 8763.
`kinaiya serve agent` and `kinaiya serve converse` also start independently alongside
storage. `kinaiya agent "Write a small file"` connects directly to Agent. Unapproved
writes and shell calls wait for an action-specific owner approval. The worker
workspace must be separate from core, evaluation, settings and credential directories.

The [Flutter web client](apps/kinaiya_flutter/README.md) provides Character View,
voice recording/playback and independent Agent Chat. Its [design and browser evidence](docs/adr/0005-flutter-voice-client.md)
describe the tested synthetic voice-to-file flow and Agent operation while Converse
was unloaded. Native voice and the remaining M5 gates are unfinished.

Optional local skill controls are enabled with `"skills": {"enabled": true}` in
the owner configuration. `/skills list`, `/skills install <local-path>`, and
`/skills enable <qualified-id>` manage packages without inference; `/skill
<qualified-id> [request]` starts normal Agent work with selected, pinned
instructions. Packages default to local-only disclosure. The [Agent skill
boundary](docs/adr/0010-agent-skills.md) records the tested workflow and the
remaining registry, upstream compatibility and execution-adapter work.

Optional [MCP connection management](docs/adr/0017-mcp-management.md) is enabled by
`"mcp":{"enabled":true}` after installing the ecosystem extra. Agent `/mcp`
commands and its **MCP connections** settings configure disabled profiles, test
connections, enable, reconnect, disable and remove servers without inference.
Configuration changes use explicit task boundaries and durable request identities;
stop/status and permission revocation remain available. Only reviewed environment
secret references are accepted. OAuth and complete MCP/skill ecosystem conformance
remain open.

Recovered paused work supports owner [native retirement](docs/adr/0018-recovered-task-retirement.md)
after a session reset. Its durable receipt keeps the original task identity and
records unreconciled effects without copying old execution context into the new
epoch. Unacknowledged retirement stays paused until explicitly reconciled; MCP
management remains blocked while any work is nonterminal.

An optional [isolated compute-plugin host](docs/adr/0016-compute-plugins.md) can
retain ordinary Python/JSON algorithms independently of the LLM that authored
them. Owner-held acceptance suites and reviewed promotion gate activation;
`/plugins` controls and direct owner invocation require no inference. Agent can
discover and call approved versions with task pins and native result receipts.
The [public CSV acquisition evidence](evaluation/evidence/compute-plugins-2026-10-27.json)
records real source generation, isolated execution, rejected updates and rollback.
General dependency installation, hooks, foreign plugin adapters and full autonomous
capability acquisition remain open.

Optional voice runs in a separate Python with the `voice` extra and preinstalled
Kokoro/Whisper weights: `python -m packages.providers.voice_worker`. Model downloads
are disabled by default; a clean-machine model installer is still a roadmap task.
Ming 3 Tiny, VibeVoice Realtime 0.5B and Supertonic 3 remain selected candidates with
unconfigured backends; they are not represented as verified integrations.

An opt-in [CPU voice resource coordinator](docs/adr/0013-resource-handoff.md)
can own that worker, verify process release for an explicit local-work handoff,
and restore voice when a bound Agent task finishes. Optional
[managed Laya residency](docs/adr/0022-managed-laya-resources.md) adds pinned
worker ownership, AMD driver telemetry and explicit GPU headroom admission.
`/resources` controls stay in Agent Chat; approvals remain usable while Converse
is paused. General GPU scheduling, other local text-model unloading and complete
M6 reliability remain open, including the tracked legacy owner-off/restore race.

Provider keys belong in secret references/environment variables. Development Meta
Contributor profiles are for public code and synthetic inputs only. The live test
requires an explicit paid-inference flag and a budget proxy; ordinary CI is offline
with respect to inference. See [integration evidence and limits](docs/adr/0002-verified-integration-slice.md)
and the [threat model](docs/security/threat-model.md).

```bash
ruff check .
ruff format --check .
pytest -m 'not live' -q
```
