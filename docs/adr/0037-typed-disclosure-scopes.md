# ADR 0037: bind disclosure to native input and its derivatives

Tracks [#95](https://github.com/rgoza1996/Kinaiya/issues/95), found while
preparing the local Converse integration rehearsal for #49. Provider selection
and a global cloud setting did not establish permission for each typed input.
Finalized typed sources also lacked the independent checks already applied to
speech windows. Local Decisions could therefore route restricted text to a
Contributor Agent. No private-data export was observed; the defects were
reproduced with synthetic recording providers.

## Native input grants

`ConverseRequest`, `TaskRequest`, `Task`, `SteerRequest` and `CommandRequest`
now carry `content_scope`, using the existing three speech-window scopes:

| Scope | Local provider | Ordinary cloud | Meta/Contributor |
| --- | --- | --- | --- |
| `local_only` | Allowed | Refused | Refused |
| `private_cloud_authorized` | Allowed | Allowed | Refused |
| `public_synthetic` | Allowed | Allowed | Allowed |

Missing input and historical scope defaults to `local_only`. Invalid scope is
never a grant. Wire schemas retain version 1.0 with a defaulted field; existing
local callers continue working. Legacy unscoped cloud calls now require a
positive input grant. Historical records are not rewritten as public. This
intentionally supersedes ADR 0033's unscoped typed-history consent path.

The scope is supplied by trusted owner input/configuration, never inferred by a
model, Decisions label or tool output. It is a declaration about supplied input
and the task's authorized workspace, not a classifier that discovers private
files, redacts arbitrary tool output or establishes universal DLP.

## Provenance and continuation

Native typed source validation joins original, every intervening correction,
and resolved scope. Missing corrections impose a local floor; later public
labels cannot undo it. A chain must use consecutive strict integer revisions
in 1..9,007,199,254,740,991 and matching native identity/origin. The shared source
helper reads at most one 200-row revision metadata page per source; exhausting
that page stays local. This bounds returned metadata, not SQLite execution
work. Unavailable provenance cannot authorize inference.

Decisions applies those floors before remote classification and propagates
the input scope to native task/steer requests. Refused classification is
persisted before the consumer advances, so a private source cannot starve
later public work. The independent Agent checks authenticated sources and
the actual configured provider before admission and continuation. Persisted
task scope can only narrow across steering, transcript reads, recovery,
inference, approval and effect boundaries. Direct retrieval of a revision row
must also honor its target's original and intermediate floors, both for
explicit event IDs and bounded scans. Deterministic status/cancel/profile
controls remain available without starting inference. Existing native task
creation through skills carries its owner's scope; this adds no new Agent
subagent facility.

Converse filters typed/window history using the existing bounded native
snapshot and returns the floors of the rows actually consumed. Generated
reply scope joins those floors, current input and persona. The built-in persona
is public code; custom names/personas default local unless trusted constructor
configuration declares a scope. The actual provider object is pinned for
projection and inference after awaited admission; the compiled context is
checked immediately before its stream starts. Grounded deterministic outcome
notices join persisted task scope and native source-chain floors. Evidence and
completion checks are unchanged.

Window identity, index/revision ordering, deduplication before disclosure,
delivery qualifications, stop ownership and cleanup bounds are preserved.
Stale lower-index windows do not replace or taint a different selected native
window. Output inherits the history actually offered to inference.

## Client and CLI

Flutter defaults to local input. The existing public cloud choices retain
their public/synthetic meaning. An explicit private-cloud choice authorizes
private off-device input to ordinary cloud providers and excludes Contributor.
Drafts, steering text and audio captures retain their initial scope across
edits, provider selection, async transcription and reconnects; changes can
narrow that scope. Clearing and entering new input establishes a new grant.
Unknown restored drafts stay local. JSON turns/tasks/commands/steers, speech
windows and multipart transcription carry scope explicitly. Local STT echoes
the owner's scope after provider results; a transcription response cannot
upgrade capture consent. Role/provider verification remains independent.

The CLI defaults local and accepts an explicit global argument, for example
`kinaiya --content-scope public_synthetic agent "Read the synthetic fixture"`.
Converse's model and speech backends remain independently selected and local
by default; the development Muse CLI is separate from that dialogue path.

## Verification and limits

Six independent baseline probes reproduced the typed/direct/history defects
on `661e347`; all six pass unchanged after repair. A second independent native
probe retained eight failed window/output/source/target-switch cases and two
passing controls from the original Muse implementation; all ten pass after
integration repairs. Independent Agent review retained four revision-export
failures and two public controls; all six pass unchanged after repair.
Native revision-bound probes also preserve their original two failures.
Equivalent native assertions are included in deterministic CI, along with
actual HTTP current-input scope combinations and draft/capture wire tests.

These are synthetic in-process journals, HTTP transports and recording
providers. They prove the inputs offered to those providers, not general
private-file detection or behavior of an external model. Real multi-model
local Converse rehearsal, full #49/M2, realtime/physical audio acceptance and
production hardening remain separate open gates. Automated independent
review is not human approval. Exact results, retained failure provenance and
the genuine Contributor development run are recorded in the evidence file
for this change and its PR/CI record.
