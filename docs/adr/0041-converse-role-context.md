# ADR 0041: shared Converse operational role context

Tracks [#97](https://github.com/rgoza1996/Kinaiya/issues/97) and the local dialogue gate in [#49](https://github.com/rgoza1996/Kinaiya/issues/49).

**Status: historical, unmerged experiment.** The role-context compiler in
[PR #103](https://github.com/rgoza1996/Kinaiya/pull/103) was evaluated but was
not adopted on main. Its three model screens failed qualification. This document
preserves the proposal and its limitations; it does not describe a shipped role
policy, grant tools, establish Agent availability, qualify a model or complete
Stage B. The current built-in acknowledgment guidance is in
[ADR 0060](0060-converse-handoffs-and-motion.md).

## Problem and experimental proposal

Typed and retained-window replies previously placed their operational role rules entirely inside replaceable personality text. The built-in persona says a separate Agent may work, while a tested replacement permits acknowledgment and preserves unspecified capability refusals. Neither clearly distinguishes acknowledging a benign requested goal from performing it. Corrected Gemma E2B still refused two specified file requests in a retained eight-case screen; that observation identifies a failure, not proof that this ambiguity caused it.

The experimental branch used one shared `dialogue_system_message(personality)` helper in both compilers. It prepended fixed public operational context to the original personality/style text in a single system message. A single leading system message avoids relying on model-specific handling of consecutive system messages. The experiment preserved default personality bytes and custom-personality disclosure scope. History, delivery notes, notices, routing and execution ownership retained their existing paths. This helper is not on main.

The proposed fixed context distinguished ordinary conversation and goal acknowledgment from execution. Lack of direct dialogue tools does not establish that a requested goal is unsupported or that Agent is absent. Conversational acknowledgment promises no action, dispatch, acceptance, progress or completion. Essential missing context can be requested, while supplied information need not be requested again. Legitimate safety refusals and authoritative capability limits remain valid.

Agent availability, routing, permissions and task status remain unknown without authenticated execution context. No positive Agent-link fact is fabricated to make an acknowledgment easier. This change adds no Agent polling, tool access, permission grant, lexical task router, generated acknowledgment, output repair or fallback. Prompt instructions are advisory; trusted runtime permissions, routing and completion evidence still enforce their own boundaries.

## Verification

Deterministic capture-provider tests exercise the actual typed and retained-window compilers and preserve personality/scope, history and delivery ordering. Such tests verify composition, not model obedience or natural dialogue.

The original eight-case Gemma screen remains failed: six semantic passes, two benign file-work refusals, and one separate length refusal despite a useful partial plan. Its original persona, outputs, rubric and protocol evidence remain preserved. No criterion is changed to reinterpret that result.

An independent reviewer froze twelve new synthetic cases and grading policy outside the Agent workspace before implementation, with their prompt text withheld from the implementer and the lead. The bounded experiment used the then-current default personality with the new shared role context, unchanged Gemma bytes/sampling/context/output limits, actual typed/window runtime and owned resource provider. It preserved raw and published responses, strict terminal/refusal status and cleanup separately. No same-case retry or adaptive prompt/decoder tuning was part of that experiment.

Even a successful small fresh screen would not establish general dialogue quality, physical speech performance, combined 8 GB VRAM fit, a model/default promotion or the original full Stage B acceptance. Actual results and remaining failures must be recorded before review and integration.

## Collected results and next evaluation

Both fresh twelve-case screens at commit `3771f5c` failed qualification. Corrected
Gemma E2B raw output received nine passes, two partials and one failure, plus one
separate length refusal. The independent delivered-output grade differed only
on the truncated draft case (eight passes, three partials, one failure); both
interpretations remain recorded. Qwen3-VL-4B Q4 raw and delivered output each
received nine passes, one partial and two failures, plus four length refusals.
Its unsupported negative authorization and action-state assertions motivated
the separate bounded publication guard in ADR 0042, not a regrade of that run.

A later Qwen3-8B Q4_K_M screen also failed. Its root raw grade was nine passes,
two partials and one failure; the independent raw grade was nine passes, one
partial and two failures. The independent delivered grade was eight passes,
two partials and two failures. Two of twelve replies ended at the output limit.
The reviewers' differences and original results remain in
[PR #103](https://github.com/rgoza1996/Kinaiya/pull/103); no regrading is
implied by this summary.

Those prompts, source, model bytes, sampling, outputs, grades and failed results
remain fixed. Updating this branch with the independently verified guard changes
the source for future experiments; it does not alter either earlier result.
The owner confirmed **8 GB total VRAM for STT, conversational LLM and TTS
together**. Further candidate evaluation must retain raw output separately from
guarded delivery, use new frozen cases, and measure runtime and memory separately
from dialogue quality. No model/default promotion follows from this context
compiler change.

## Remaining work

Issue [#97](https://github.com/rgoza1996/Kinaiya/issues/97) remains open. A
future candidate needs fresh, independently frozen public synthetic work,
social, ambiguous and safety cases and a fixed rubric before evaluation. Record
raw and delivered output separately, terminal/refusal and cleanup outcomes, the
exact source, model, configuration and resource measurements. Do not reuse the
consumed cases to claim qualification or treat scripted-provider regressions as
evidence of model obedience.

Any new prompt composition must also be checked against the current typed and
retained-window paths, including complete-request token admission, custom
personality disclosure, independent Agent operation and native state guards.
Actual combined STT, dialogue and TTS residency and physical voice latency need
separate measurements; neither follows from these CPU dialogue screens.
