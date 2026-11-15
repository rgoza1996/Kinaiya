# Converse output fidelity (#486)

Kinaiya owns the architecture and harness. Models own their generated answers.
Converse therefore preserves admitted model text in typed, retained-window and
native-media publication. English positive/negative work-claim patterns no
longer replace it with a fixed sentence. A model's generated claim cannot
suppress its matching original native speech or trigger a TTS fallback.

This intentionally supersedes the semantic substitution in ADR 0042. The old
pure English predicates remain available for historical diagnostics; production
publication does not call them. Existing normal chunk framing and modality
formatting retain their own contracts; this change concerns semantic rewriting.

The deterministic native boundaries remain separate:

- Authenticated [task observations](converse-task-context.md) provide bounded
  state, evidence-kind counts, actual program-receipt observations, publication
  pending state and explicit `semantic_verification: not_assessed`. Missing or
  unavailable observations stay unknown; a task state is not semantic proof.
- `outcome_notice` still requires an authenticated Agent-supervisor outcome,
  evidenced success, current objective revision and its existing native proof
  rules. Dialogue cannot invoke or replace that path.
- Model output grants no task/action/tool permission and cannot fabricate an
  authenticated outcome or receipt. Decisions routing remains independent.
- Source/session/thread/epoch/revision, owner intent, model leases, disclosure,
  stale-result rejection, bounded input/output and uncertain cleanup still
  control publication. Refused providers remain errors.
- Native audio retains matching text/PCM identity, hashes and its original
  reply custody. Generation remains `delivery: generated`; real delivery needs
  the existing player acknowledgment. No speech-device or acoustic quality
  claim follows from a retained PCM hash.

The original unsupported accomplishment in #486 remains a model-behavior
observation. This change neither endorses it nor promises to make a selected
model truthful. No persona tuning, confidence adjustment, reroll, model/default
change or semantic success classifier is added.

## Verification

Before the change, 22 controlled output-fidelity cases produce **14 failures and
8 passes**, with 75 other cases deselected. The first log and frozen test source
are retained under local `output/issue-486-output-fidelity/`. The unchanged
fidelity oracles plus the full native-audio and publication modules then pass
**103 tests in 14.47 seconds**. Public fixtures include ordinary praise, positive
and negative work claims, conditional planning and non-English dialogue. These
are input/output and custody checks, not model-quality scores.

The native speech tests use the real Store, controller, authenticated ASGI
speech route and controlled native provider. They verify exact text, original
PCM bytes/hash, native event binding, one model call, no fallback and no invented
outcome/delivery. The broader native task-context, outcome-evidence, disclosure,
ownership and cleanup suites qualify the retained architecture separately.

The broader 37-file run retains **1,385 passed / 1 failed** in 589.73 seconds;
the sole failure is an old speech-window expectation requiring the retired
semantic substitution. A separate coordination/playback/MCP run retains
**139 passed / 1 failed** in 41.27 seconds, with its sole failure the analogous
old typed expectation. Both are migrated to faithful text plus the existing
no-native-outcome assertion under the owner's changed requirement. The complete
affected modules and expanded fidelity cases then pass **140 tests in 20.25
seconds**, including model text imitating a native outcome envelope. These counts
describe separate runs; the earlier failures remain retained.

The [source review](evidence/converse-output-fidelity-2026-11-15/architecture-review.json)
checks unchanged native outcome, currentness, speech-validation and delivery
methods, plus unchanged task-context and native-audio owner modules. No native
permission, proof, source, disclosure or cleanup boundary is replaced by a
semantic claim filter.
