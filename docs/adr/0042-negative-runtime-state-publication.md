# ADR 0042: bounded negative runtime-state publication guard

Tracks [#97](https://github.com/rgoza1996/Kinaiya/issues/97).

Status: defense in depth under verification. This is not model qualification,
a complete truthfulness guarantee, or evidence that Stage B or the combined
8 GB STT/LLM/TTS target passes.

## Problem and authority

Local model screens emitted unsupported negative facts such as an Agent lacking
authorization or no action having occurred. Empty history, absent routing and
Converse's lack of direct tools cannot establish those facts. They also cannot
prove an Agent is absent, unavailable, or unsuccessful. The existing generated
text guards cover selected positive completion claims and remain unchanged.

Converse does not gain an Agent status API, tool permission, task ledger or
polling loop through this change. Authenticated native outcome notices retain
their separate evidence path. Decisions still routes asynchronously without
authorizing actions or establishing task outcomes.

## Publication rule

The shared pure predicate `negative_runtime_state_claim(text)` recognizes finite
English grammatical families: negative Agent presence/availability/authorization,
perfect-tense Agent task-effect denials, and global task/action/effect denials.
It reads at most the first 4,000 original input characters before quote masking
or regex matching. It has no I/O, provider, history, route, permission or runtime
evidence input.

Only the existing model-generated typed and retained-window publication gates
call it, after their existing positive completion guard. A positive substitution
is never reprocessed by the new predicate. A matched chunk becomes exactly:

> I can't verify that Agent or task status.

Typed turns reuse the existing `converse.safety` append, source/index idempotency
key, current-generation fence and publication order, with distinct reason
`ungrounded_runtime_state_claim`. Window replies add optional metadata
`guard_reason=ungrounded_runtime_state_claim` to the existing owned reply append;
they gain no separate journal await or publication owner. Source identity,
revision, epoch, content scope, retry behavior and provider cleanup stay intact.
Native outcome notices, controls and provider errors do not enter this guard.

## Preserved dialogue and limits

Questions, double-quoted spans, explicit user attribution, conditionals,
uncertainty, normative planning, direct-tool capability limits and ordinary
safety refusals have regression controls. Attribution/conditional governors
survive comma continuations within an independent span. Semicolons, colons,
dashes and contrastive `but`/`however` delimit spans. Other exclusions apply
through a candidate assertion, so unrelated later planning language cannot
exempt an earlier direct denial. Simple postfix uncertainty is also preserved.
Generic `unable to` or `cannot run` capability statements alone do not establish
runtime status and are outside the detector.

This is bounded matching, not a general grammar or semantic parser. Novel
synonyms, non-English replies, anaphora, quoted claims, claims split across
chunks and tails beyond the character bound can escape it. Complex narrative,
reported speech or qualifier scope can be misclassified. The fallback discards
the entire matched chunk, including any useful planning in it. These limits
require continued raw-model and delivered-dialogue evaluation; adding an output
substitution cannot turn an earlier failed raw-model screen into a pass.

## Verification

`test_converse_runtime_state_guard.py` exercises detection, preserved dialogue,
mixed clauses and the original-input bound. `test_converse_state_publication.py`
executes both actual publication paths with synthetic fragmented streams,
empty/nonempty history, provenance/scope checks, positive-first behavior and
observed stream closure. A late provider refusal remains an error after an
earlier guarded chunk. Existing coordination, history, disclosure, speech-window,
owner-control, cancellation and native lifecycle regressions remain required.

Independent frozen publication tests exposed a repair regression for passive
effect denials; independent boundary probes also exposed lost comma governors
and overbroad capability/modal rules. Their failed original results are retained
alongside corrected results. No model, personality, prompt, routing default or
acceptance criterion changes as part of this implementation.
