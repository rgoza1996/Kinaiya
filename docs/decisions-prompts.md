# Editable Decisions interpretation preferences

This implements the default-policy and configuration work in
[#558](https://github.com/rgoza1996/Kinaiya/issues/558), within the
[framework/model responsibility boundary](framework-model-responsibility.md).
The default prefers giving genuine user questions and commands Agent attention.
Priority does not require interruption: a question can wait at a native task
boundary while Converse keeps talking and listening. Corrections, cancellation,
social conversation, rhetorical questions and unfinished speech remain choices
for the selected model to interpret.

A status question is distinguished from a request for new work. The default
asks the model not to restart or duplicate a task merely to report its status.
This instruction does **not** repair the separate informational-task selection
and projection defect in [#485](https://github.com/rgoza1996/Kinaiya/issues/485),
or guarantee that a particular model classifies status questions correctly.

## Host configuration

The optional root `decisions_prompt` object in the host JSON config replaces
both interpretation preferences. For example:

```json
{
  "decisions_prompt": {
    "contract_version": 1,
    "finalized": "Prefer queuing genuine questions and commands for Agent. Distinguish status questions from requests to repeat work. Keep ordinary social chat with Converse.",
    "window": "Keep companion timing independent from Agent routing. Prefer queuing complete questions for Agent without interrupting ongoing work. Use the supplied task identity for corrections.",
    "content_scope": "public_synthetic"
  }
}
```

Omitting the object, or setting it to `null`, selects the published default.
When the object is present, `finalized` and `window` are both required; an explicit
empty string is an empty preference, not a request to restore the default.
Neither the services nor typed adapters append the default to an override.
The two strings each allow at most 8192 UTF-8 bytes, with no NUL or invalid UTF-8.
The strict immutable v1 document rejects unknown fields and invalid versions.
Malformed configuration fails with a constant redacted error before provider
or catalog construction. Instructions are selected at service startup; this
does not add a live prompt editor or silently restart a running service.

Custom instructions default to `local_only`. `public_synthetic` is appropriate
only for deliberately public text; `private_cloud_authorized` permits its use
with an otherwise authorized cloud provider. The provider's own public-only
ceiling still applies. A forbidden prompt refuses before inference or a
prompt-bearing cache digest. Native inspection/currentness awaits retain and
recheck this instruction floor before actual provider disclosure. Native owner
inspection may retain the exact request, including configured instructions, in
its existing owner-only inspection store.

## Fixed contracts and backend propagation

The editable text is separate from the fixed output/action contract:

- Finalized turns retain `ignore`, `queue`, `steer`, and `interrupt`.
- Streaming windows retain their independent companion-timing and Agent-route
  labels. An Agent route cannot itself mute or speak as Converse.
- Task targets/revisions come from supplied native observations. Model text
  grants no tool permission and establishes no acceptance, effect or success.
- Parsing, source/scope/session/epoch checks, native attempt admission,
  dispatch, retained transcript access and malformed-output refusal remain in
  trusted code. Editing a preference cannot override them.

`ModelClassifier` and `WindowClassifier` put the selected preference after the
fixed contract in the actual system message. The compatible text path sends
that message directly. Laya/Jev, native d1 and OpenRouter Decisions include the
complete selected system message in their bounded SystemOne state. Their fixed
choice criteria describe action semantics, without silently reinstalling a
social/question routing policy. Over-bound or invalid system text refuses
before transport. Model-selection leases preserve the same immutable prompt.

Streaming reuse includes a digest of the effective window instructions and
their disclosure scope, alongside the existing native request identity. A
changed preference requires a new classification; a byte-equivalent permitted
selection can reuse its original result. This is RAM reuse, not permission to
replay a provider call after an uncertain native attempt.

## Reproducible illustrative cases

These examples describe the suggested preference, not required model outputs:

| Public fixture | Interpretation to consider |
| --- | --- |
| Prepare a museum opening checklist | Queue Agent attention; keep Converse available |
| Change the opening time to ten | Steer the identified task if this is a correction |
| What is the status of that checklist? | Consult native observations; avoid duplicating work |
| I like that painting | Ordinary companion conversation may leave Agent work unchanged |
| Who doesn't enjoy a sunny day? | Context may make this rhetorical or ambiguous; ignore/wait is available |

`test_decisions_prompt_config.py` sends all five fixtures through both actual
classifier request paths. The controlled provider deliberately returns ignore
for Agent routing in every case, proving that code does not replace its valid
selection using keywords. This is not an accuracy measurement. The same file
checks strict configuration/UTF-8 bounds, blank overrides, actual launcher and
model-lease propagation, cloud disclosure, and source/audit awaits.

The three typed-adapter test files inspect actual MockTransport requests for
blank/custom/max-byte instructions and invalid-text refusal. The streaming
cache test preserves a completed call across a native append outage and checks
reuse with unchanged instructions versus a second classification after a
change. The new schema has its own exact byte pin; every legacy schema remains
in its existing independent aggregate. Existing routing, native-attempt,
scope/currentness, recovery and two-channel tests remain required.

No live model evaluation, paid request, prompt deployment, universal confidence
threshold or model-specific logit adjustment is part of this change. Existing
explicit adapter refusal settings remain distinct from a shared probability
calibration. Natural speech and routing accuracy require separate evidence.
