# ADR 0041: shared Converse operational role context

Tracks [#97](https://github.com/rgoza1996/Kinaiya/issues/97) and the local dialogue gate in [#49](https://github.com/rgoza1996/Kinaiya/issues/49).

Status: implementation under verification. This clarifies the dialogue contract; it does not grant tools, establish Agent availability, qualify a model or complete Stage B.

## Problem and decision

Typed and retained-window replies previously placed their operational role rules entirely inside replaceable personality text. The built-in persona says a separate Agent may work, while a tested replacement permits acknowledgment and preserves unspecified capability refusals. Neither clearly distinguishes acknowledging a benign requested goal from performing it. Corrected Gemma E2B still refused two specified file requests in a retained eight-case screen; that observation identifies a failure, not proof that this ambiguity caused it.

Both compilers will use one shared `dialogue_system_message(personality)` helper. It prepends fixed public operational context to the original personality/style text in a single system message. A single leading system message avoids relying on model-specific handling of consecutive system messages. Default personality bytes and custom-personality disclosure scope remain unchanged. History, delivery notes, notices, routing and execution ownership retain their existing paths.

The fixed context distinguishes ordinary conversation and goal acknowledgment from execution. Lack of direct dialogue tools does not establish that a requested goal is unsupported or that Agent is absent. Conversational acknowledgment promises no action, dispatch, acceptance, progress or completion. Essential missing context can be requested, while supplied information need not be requested again. Legitimate safety refusals and authoritative capability limits remain valid.

Agent availability, routing, permissions and task status remain unknown without authenticated execution context. No positive Agent-link fact is fabricated to make an acknowledgment easier. This change adds no Agent polling, tool access, permission grant, lexical task router, generated acknowledgment, output repair or fallback. Prompt instructions are advisory; trusted runtime permissions, routing and completion evidence still enforce their own boundaries.

## Verification

Deterministic capture-provider tests exercise the actual typed and retained-window compilers and preserve personality/scope, history and delivery ordering. Such tests verify composition, not model obedience or natural dialogue.

The original eight-case Gemma screen remains failed: six semantic passes, two benign file-work refusals, and one separate length refusal despite a useful partial plan. Its original persona, outputs, rubric and protocol evidence remain preserved. No criterion is changed to reinterpret that result.

An independent reviewer froze twelve new synthetic cases and grading policy outside the Agent workspace before implementation, with their prompt text withheld from the implementer and the lead. The planned bounded experiment uses the production default personality with the new shared role context, unchanged Gemma bytes/sampling/context/output limits, actual typed/window runtime and owned resource provider. It must preserve raw and published responses, strict terminal/refusal status and cleanup separately. No same-case retry or adaptive prompt/decoder tuning is part of that experiment.

Even a successful small fresh screen would not establish general dialogue quality, physical speech performance, combined 8 GB VRAM fit, a model/default promotion or the original full Stage B acceptance. Actual results and remaining failures must be recorded before review and integration.
