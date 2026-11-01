# 0089: Task-owned Agent catalog model selection

October 31, 2026. Next increment of
[#208](https://github.com/rgoza1996/Kinaiya/issues/208), following ADRs 0077,
0082 and the protected catalog in ADR0086. This extends actual Agent defaults,
exact-harness overrides and all five auxiliary text purposes. It does not apply
the catalog to Converse, Decisions, STT or TTS and does not add ordered fallback.

## Explicit saved references

A model change may select an existing host profile or an exact typed
`catalog_binding` containing `id`, `generation` and `configuration_sha256`.
These are mutually exclusive sources. Display labels and encoded picker keys
are never native authority. Legacy host requests omit the additive field and
retain their original request digests/receipts. Inheritance remains an explicit
empty choice; a catalog selection is not inheritance just because
`profile_id` is null.

The model snapshot retains the bounded host `profiles` list and adds a separate
bounded `catalog_profiles` list (at most thirty-two each). Catalog entries show
their exact saved reference and measured-interface eligibility, independently
of model qualification/readiness. Removed, rotated or replaced generations are
unavailable for new selection. A saved reference is retained as an unavailable
placeholder rather than silently following a newer generation with the same ID.
New serialized settings are capped at 512 KiB; an existing larger legacy state
may still be repaired by strictly shrinking owner changes. This bounds catalog
reference growth independently of the per-harness count limits.

Only native LM Studio text completion (`none` reasoning) and Meta text with an
explicit context budget have the required measured complete interface here.
OpenAI-compatible and Laya declarations remain in the catalog but cannot become
Agent complete-request choices by declaration alone. Actual endpoint/model
support remains subject to native admission and provider refusal; no model name
proves multimodal or production readiness.

## Capture and task ownership

Catalog settings inspection and model-selection reads validate declarations
without constructing a catalog adapter or reading a key. A genuinely new task
captures the current exact profile and credential generation in a coherent
native catalog transaction. Existing disclosure scopes remain authoritative.
An exact retry of already accepted work returns its original receipt before
constructing any model or consulting current catalog eligibility.

Each task receives fresh catalog adapters: one primary and separate adapters for
available analysis, compression, title generation, delegated reasoning and
harness design. At most sixty-four task model leases can remain owned, with at
most six adapters each. Host-cached providers keep their original independent
runtime ownership. No auxiliary failure silently chooses another model or
grants tool authority.

Native `AgentModelPin` adds the exact catalog binding and captured catalog
revision beside model-settings revision, provider/model/locality/reasoning.
Pins enter the existing journal/task receipts and owner Task details. They never
contain raw keys. A configured alias identifies configuration, not verified
weights. Task-owned LM Studio adapters pin the first native model/configuration
snapshot before formatting a prompt; subsequent alias replacement is refused
before another prompt upload. Existing exact count/generation identity and
disclosure admission still apply.

Model changes affect new tasks. Accepted tasks retain their adapter/key/config
copies and pins through later catalog edits or rotation. Recovered paused tasks
do not reconstruct providers from a current profile with a matching label.
Permission, source and content-scope revocation remain separate active guards.

## Cleanup and uncertain acceptance

Task leases close only after the actual worker/provider operations and native
task resources settle. A logical timeout or terminal outcome is not release
evidence. Failed initial acceptance preserves its unknown journal outcome,
starts no work and independently retains unused-adapter cleanup ownership.
An adapter close failure retains capacity and is exposed as cleanup pending or
failed; it does not rewrite already published task completion evidence.

The Agent's generic Resources completion notification follows successful
skill/plugin release and the exact task model lease's positively returned close.
Pending or cancelled close and earlier uncertain request cleanup suppress that
notification, even if a later adapter close succeeds. The actual lease retains
its own cleanup operation through waiter cancellation. Tasks without a lease
retain their existing notification behavior when cleanup is otherwise confirmed;
closing/nonterminal tasks still suppress it. This ordering does not certify
physical GPU release or change the authoritative work outcome.

The existing runtime busy boundary includes model cleanup, so an execution
settings change cannot treat terminal publication alone as idle. Shutdown waits
for owned cleanup; it does not manufacture successful close from cancellation.
The native owner retains failed cleanup rather than rebinding an active task.
An unavailable host declaration or credential is represented as unavailable,
without invented provider identity/locality. It cannot become selectable or
perform inference, while owner settings and a valid explicit catalog default
remain independently operable. Historical recovery retains evidence and source
floors without borrowing that current default's adapter or disclosure identity.

## UI and remaining work

### Integration with confirmed native admission

The combined increment preserves [ADR0087](0087-agent-submit-admission.md).
Fresh primary/auxiliary catalog pins are part of the original frozen proposal;
an initial task can launch only from its validated native `created` admission
after fresh authority checks. An exact pending retry retains that proposal,
without constructing a newer catalog generation. A timeout or retained receipt
cannot grant a new launch opportunity. Suppressed or losing proposals must
release only their own unused adapters, with real cleanup ownership retained.
The admission validator accepts the strict additive catalog binding/revision
pair while preserving historical missing/null pin representations.

Flutter retains an uncertain submission's exact request and original key for
reconciliation. A later Send cannot silently assign that same submission a new
identity after a timeout or HTTP503. These integration requirements need fresh
combined-head review/CI; the prior independent green runs do not establish it.

Models and Harness Customization use the same source-aware picker for primary
and auxiliary choices. Saved provider identity is inspectable; exact reference,
reasoning and CAS receipt matching preserve uncertain writes across closing the
Options page. Task details show historical versions independently of the current
picker. Saving provider connection settings still does not itself select a model.

The full [Options plan](../options-delivery-plan.md) remains open, including
ordered private/cloud fallback, other runtime role editors, direct multimodal
input/qualification and [precise scoped permissions](../options-permission-plan.md).
No new paid/model call or automatic credential probe is introduced by inspection.

## Evidence boundary

Implementation uses static review, formatting, Ruff/Python compilation and Dart
analysis. No new automated tests or inference calls were performed for this
increment. Exact-head CI and any browser/runtime observations must be recorded
on its PR with their actual revision. The earlier synthetic model failures and
cumulative $10 paid-test budget/reservations remain unchanged.
