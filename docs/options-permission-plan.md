# Precise permission rollout plan

Status: **proposed next increment; not implemented by this document**.
Tracking: [#208](https://github.com/rgoza1996/Kinaiya/issues/208), within the
[full Options delivery goal](options-delivery-plan.md). This plan does not close
the precise-permissions group or replace any other group in that goal.

## Required outcome and current foundation

The owner requires Allow/Ask/Deny for reads, writes, commands, network and
individual native, MCP and plugin tools; task/session/workspace/default scopes;
effective/source labels; active grants and pending approvals. Approval behavior
must remain separate from actual filesystem, process, sandbox and transport
access. Native enforcement and truthful lifecycle state are required before
presenting an editor as working.

`core/policy/__init__.py` owns protected/workspace/read-only boundaries and
one-use approvals bound to tool, exact arguments and objective revision.
`services/agent/runtime.py` owns pending approvals, source/task/epoch checks and
final tool dispatch. ADR0084 exposes service-level write/command modes, network,
process-local YOLO and actual execution restrictions. Its protected owner store,
CAS, receipts and settled-worker checks are useful foundations; scoped read and
per-tool rules are still missing.

## Smallest native extension

Use bounded native rules rather than a model-authored policy language. Each rule
selects one exact scope and target, a mode, and native rule ID/revision/source:

- Scope: service default, approved workspace, exact Agent session/thread/epoch,
  or native task. Validate scope ownership and current objective revision when
  saving a task rule. Specify whether rules survive steering before shipping;
  a one-use action approval never survives a changed objective by implication.
- Target: protected native operation identity; exact MCP server/configuration
  digest and tool name; or immutable plugin version/source identity. Avoid
  display-name parsing, wildcard grants and discovery-derived authority.
- Precedence: task, session, workspace, default, then existing host behavior.
  Protected roots, read-only mounts, hard native Deny, sandbox/transport grants,
  source authority, disclosure and extension eligibility remain independent
  ceilings. A scoped Allow cannot remove them.

Implement `read_file` first with the existing action-approval broker, then
individual registered native tools, MCP tools and qualified plugins. Preserve
all scopes in the goal even if delivery is incremental. Network and command
policy must keep their native enforcement; a per-tool rule alone does not grant
network, shell, unrestricted filesystem access or plugin promotion.

For an owner rule that allows an action otherwise requiring Ask, native code may
mint a short-lived one-use permit through the existing exact-digest/revision
broker. Record provenance as an owner rule with its rule ID/revision/scope;
do not fabricate a user-click approval event or temporarily mutate global
write/shell/YOLO flags. Any independent uncommitted-speech authorization remains
required. Deny must be checked before asking and again at final admission.

`read_file` control authorizes that tool only. Search, shell and context
selection can read through separate native owners. Label the editor accordingly;
do not describe it as a complete filesystem-read boundary.

## Admission, lifecycle and revocation

The existing all-tasks-settled settings gate cannot simply be reused for live
task/session rules: it would make useful Task scope uneditable. Introduce a
narrow native action-admission boundary while preserving worker ownership:

- Recheck the effective rule and exact target/source after every relevant await,
  including MCP discovery/transport work and plugin resolution/queue admission.
- A saved Deny prevents subsequent admissions. Already admitted, queued or
  running work remains visible; saving Deny is not proof of cancellation,
  rollback or absence of remote effects.
- Rules govern future operation admission. Previously obtained task context
  remains available under the existing disclosure policy; changing a tool rule
  does not erase retained context or recall bytes already disclosed.
- Include effective permission revision/fingerprint in inference, retained
  context, auxiliary dependency and tool-catalog guards. A stale model snapshot
  must not export context or act using revoked authority.
- Existing MCP host allowlists, disclosure and transport checks remain final
  ceilings. Plugin permits bind the immutable resolved version/source and must
  not authorize replacement code selected after an approval was requested.
- Journal bounded permission provenance at actual admission. Keep private
  credentials and unnecessary absolute workspace paths out of model context.

### Approval-expiry prerequisite

The inspected baseline's `pending_approvals` hid expired approvals, while `_await_approval`
waits for its resume event without an expiry wake/reissue path. The policy
broker also retains used/expired records without pruning. A task can therefore
remain Awaiting Approval with no visible pending row; adding more Ask actions
would amplify retained-state growth.

Before expanding Ask, provide bounded native expiry handling and explicit
reissue or retirement, with truthful pending/expired/used/cancelled state.
Pruning must preserve references needed by live tasks and in-flight dispatch.
An expired row disappearing is not evidence that a task finished or stopped.
Renewal must not automatically rerun inference or side effects.

[ADR0092](adr/0092-approval-expiry-renewal.md) implements this bounded lifecycle
prerequisite: visible paused expiry, exact owner renewal into a separately
reviewed permit, explicit retirement and bounded broker history. See its PR for
verification. Precise scoped rules remain unfinished.

## Owner storage, API and Options

Keep the implementation with the current native policy/runtime owners. Reuse
the protected settings-store pattern for a bounded rules table, permission
revision and exact-operation receipts. Do not silently merge unrelated settings
stores or introduce a general replacement policy framework.

- Upsert/remove one exact scoped rule with expected revision/state and an
  idempotency key. Validate native scope and target identity at save time.
- Reset removes only the chosen rule/scope, restoring inheritance. It must not
  delete tasks, sessions, workspace data or unrelated preferences.
- Do not silently evict active or unresolved scope records to meet a cap. Refuse
  a new record clearly and retain reconciliation information.
- Owner reads return effective mode and source, inheritance/ceiling information,
  exact target version/source and stale/removed target state. Scope selection
  uses native task/session identity, not model-supplied authority.
- Separate historical exact save confirmation from current effective state.
  Lost acknowledgements remain unknown until reconciled; receipt lookup must
  not reapply a stale rule or imply cancellation of delayed writes.
- Show saved/effective values, scope/source, actual sandbox/network restrictions,
  active grants and approval lifecycle. Add controls only for native-backed
  behavior, within the traditional Options workspace.

## Implementation locations and acceptance evidence

Primary owners: `core/policy/__init__.py`, `services/agent/runtime.py`,
`runtime_settings.py`, `tool_catalog.py`, `mcp_tools.py`, `plugin_service.py` and
`auxiliary.py`. MCP's configured host in `packages/ecosystem/mcp_host.py` remains
authoritative for external-tool grants. Versioned contracts and the owner
Options API/UI must describe the implemented subset and actual lifecycle.

For each increment, record static review and the checks actually authorized/run
for rule precedence and inheritance; hard-denial/sandbox ceilings; exact
argument/objective/source binding; steering/epoch changes; expiry and bounded
cleanup; concurrent admission/revocation; target version changes; CAS and unknown
write recovery; and saved/effective UI state. Do not call the full permission
scope delivered until all required scopes/tools and their acceptance evidence
are present. This document adds no runtime enforcement or verification result.
