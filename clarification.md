# Kinaiya — Architectural Clarification: Autonomous Capability Expansion

**Status:** Architectural design intent  
**Scope:** Kinaiya Agent, adaptive harnesses, executable extensions, skills, plugins, and external tools  
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

---

**Core architectural statement:**

Kinaiya is not intended to be an LLM that can perform every possible task internally. It is intended to be an autonomous agent that can expand what it is capable of doing by acquiring, composing, and constructing executable software—including software that does not use an LLM at all.
