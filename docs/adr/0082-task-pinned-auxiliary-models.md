# ADR 0082: Task-pinned auxiliary text models

Status: Implemented; part of [#196](https://github.com/rgoza1996/Kinaiya/issues/196).

## Requirement and design

The owner requested purpose-specific models with overrides for every custom
harness, following Hermes's auxiliary model configuration. Extend the existing
native Agent model settings rather than introducing a second settings authority.
Five text purposes are implemented: analysis, compression, title generation,
delegated reasoning and harness design. Modality-specific vision, STT, TTS and
media generation routes remain separate work; these five labels do not imply
their availability.

Each purpose resolves from harness override, global purpose default, harness
primary choice, then Agent default. Explicit removal restores inheritance.
Settings, revisions and exact idempotent receipts share the protected SQLite
store. Old settings and legacy receipt digests remain compatible. Owner-only
API checks use the same native harness eligibility as primary model changes.
Flutter embeds the controls in Models and the individual harness editor.

At task acceptance, capture available providers and their model/provider,
locality, reasoning and settings revision. Journal and Task output retain these
pins. A settings change cannot rebind an accepted task. Recovery preserves the
historical pins but pauses work; it does not substitute current defaults.
Missing auxiliary capability does not prevent independently valid primary work.

## Execution and authority

`consult_model` accepts only a known purpose and a prompt of 1–8000 characters,
at most 16000 UTF-8 bytes. It uses the task's captured provider and returns
bounded text with its native pin. The output is untrusted external evidence,
never proof that a file, tool or remote effect happened. It cannot execute
tools, delegate permissions, start a child Agent or edit protected history.
Compression is an explicit text result, not automatic history replacement.

Adapters must implement exact measured `prepare_complete`; the existing Meta
adapter and explicit deterministic fixtures support this interface. A generic
compatible or LM Studio primary profile is not advertised as an auxiliary
profile merely because it can generate text. Optional construction failures
are cached and owned adapters are closed once. No unavailable or forbidden
route silently falls back. Additional measured backend adapters remain work.

The native supervisor rechecks task/session/epoch/revision, cancellation,
provider identity, policy, retained source/catalog/skill/plugin dependencies,
disclosure, measured prompt admission and resource policy before and after
awaits. The entire side call has a 120-second deadline, with no automatic retry.
The complete task source closure is conservatively applied to the auxiliary
prompt. Foreign tool/skill/plugin text currently requires a local auxiliary
route: disclosure to one cloud route does not authorize another. Native-only
catalog inspection does not impose this foreign-context restriction.

## Verification and limits

`test_auxiliary_models.py` covers settings inheritance/restart, old receipt
replay, owner authentication and harness eligibility, exact retries, stale
revisions, malformed input/output, missing pins, unavailable adapter cleanup,
native versus foreign catalog handling, and late results after cancel, steer,
epoch, objective or provider change. A synthetic normal Agent task consults,
writes and finishes with separate external and file evidence while a later
settings change leaves its captured model unchanged.

Flutter checks cover legacy parsing, invalid data, inheritance, explicit saving
of one selected purpose, and a narrow enlarged-text editor. Schema checks cover
the 16 native actions and all 128 optional gateway combinations. These are
deterministic checks, not a claim of real-model quality or provider readiness.
