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

An initial Linux research implementation now includes versioned contracts, authenticated persistent transcript storage, independent Agent and Converse services, asynchronous routing, scoped tools/approvals, and real-provider integration tests. The implementation is experimental. The full roadmap, all retained voice backends, native mobile calling and production hardening remain open development gates.

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

Optional voice runs in a separate Python with the `voice` extra and preinstalled
Kokoro/Whisper weights: `python -m packages.providers.voice_worker`. Model downloads
are disabled by default; a clean-machine model installer is still a roadmap task.
Ming 3 Tiny, VibeVoice Realtime 0.5B and Supertonic 3 remain selected candidates with
unconfigured backends; they are not represented as verified integrations.

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
