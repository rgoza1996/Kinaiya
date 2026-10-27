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

This repository currently publishes the product specification and roadmap. The checked-in design baseline describes planned interfaces and acceptance criteria; it does not claim that those capabilities are already implemented. The roadmap separates the minimum research prototype, hackathon slice, private alpha, public beta, and production evidence.

See [Roadmap.md](./Roadmap.md) for architecture, milestones, acceptance criteria, risks, and implementation references.

## Project principles

- Keep the voice experience and work agent independently usable.
- Make permissions and action results explicit; model output cannot create user authorization.
- Preserve provider portability and clearly distinguish verified integrations from targets.
- Measure the adaptive-harness claim against pinned baselines and safe rollback behavior.
- Keep prototype evidence, recorded demos, fixtures, and implemented functionality clearly labeled.
