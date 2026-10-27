# ADR 0010: Standalone Agent skill controls and selected context

Roadmap sections 31.4 and 32; issue #28. Optional owner configuration
`"skills": {"enabled": true}` creates the controller under the protected data
folder, outside the worker workspace. Install the locked ecosystem dependencies
when enabling it. No model can configure this controller or set a disclosure
grant through a tool argument.

## Controls and invocation

`GET /v1/skills` provides owner-authenticated paged metadata.
`POST /v1/skills/control` uses a strict DTO for local install/archive install,
inspect, enable/disable, update/archive update, rollback, remove and scope.
The shared Agent registry exposes the same operations as `/skills ...` in CLI
and Agent Chat. Lifecycle operations perform no inference. `/skill <qualified-id>
[request]` and eligible unique bare skill names create normal Agent work;
the reply contains its accepted task id/state. Commands remain unavailable in
Converse and cannot originate from model output or skill text.

Built-in names/aliases are reserved. Publisher collisions require a qualified
id; install order never chooses a publisher. Dynamic discovery is limited to
64 owner records. Larger catalogs expose the explicit qualified `/skill`
control plus paged `/skills list [after]`, rather than guessing whether a name
collides outside the discovery window. Unconfigured/disabled or disclosure-
blocked commands report unavailable. Existing status, stop and approval
controls remain independent of Converse and inference.

Explicit user selection is an owner-only runtime parameter recorded with the
submit intent and included in its idempotency digest. It permits user-only
instructions and resources for that selected skill. The model-facing
`skill_load` arguments allow only `skill_id` and optional `resource`; they
cannot choose `invocation=user`, grant scope, or mutate lifecycle state.
Replaying the same command returns the same task; recovery does not execute
uncertain work again.

## Model and evidence boundary

`skills_list` returns a bounded filtered catalog. `skill_load` retrieves only
selected instructions/resources, pins their exact version and records package
source/digest, instruction/resource path and SHA-256 in supervisor evidence.
The seed history keeps at most eight messages, with at most 12000 characters
for a selected skill observation and 2000 for ordinary observations. No whole
catalog or transcript is automatically injected. The Meta strict action wire
supports the same skill fields; no new provider is required.

Skill observations are untrusted `external` receipts. They cannot authorize a
write, shell, network call or declare task completion. Receipts alone fail the
existing finish gate. Independent native file/command/test evidence for the
current objective revision is required. Scripts must be separately copied to
the granted workspace and executed through the existing broker/isolation;
loading a script never executes it or installs prerequisites.

This extends the experimental 1.0 ToolAction enum with `skills_list` and
`skill_load`. Older hosts reject those actions as unsupported. The current
Flutter client already consumes the shared command registry and unverified
receipt kind; no claim is made that arbitrary older strict clients understand
new pre-release actions.

## Evidence and remaining work

Full offline Python suite: 289 passed. Real Contributor task
`b9432bfa-9879-4ac7-b013-a6f1eeac8080` used the explicit `/proof-recipe` command,
loaded a supporting payload, obtained one exact owner write approval, wrote
`skill-proof.txt`, and verified it with a native read. The artifact hash is
`9c8304ea25542defdff2d2dc44b91debbb0901076ffeb19fbdf5b44f379977e1`.
Duplicate command delivery returned the same task. Source hashes and observed
actions are preserved in the evidence file; this used public/synthetic content
only through the budget proxy. Billing receipts were unavailable.

Live ClawHub resolution/search, direct remote installation controls, private
repository transport, upstream OpenClaw/Hermes conformance, installation diffs
and local forks, execution adapters/dependency installation and richer settings
remain open under #3. Local instructions and one synthetic successful task do
not establish autonomous executable capability acquisition. Registry/directory
crash transactions remain a hardening gate described in ADR 0009.
