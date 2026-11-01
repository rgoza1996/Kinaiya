# 0094: Native Search and exact MCP permission targets

October 31, 2026. Native, owner API, and embedded editor source is implemented
under review for [#208](https://github.com/rgoza1996/Kinaiya/issues/208).
Exact-head CI and preview verification remain pending. This extends
[ADR0096](0096-scoped-native-tool-permissions.md), preserves its legacy requests
and receipts, and does not close the full precise-permissions acceptance group.

## Inventory and editable targets

The native registry lists the 16 actual protocol operations and configured or
observed individual MCP tools. Saved removed targets remain inspectable. The
owner projection is passive: it does not connect, discover, test, construct a
model, or invoke a tool. Observed schema metadata is not a health check or grant.

| Target | Rule protocol |
| --- | --- |
| Exact `read_file`, `write_file`, `shell` | Existing schema1.0 rules/API, unchanged |
| Exact native `search` | New schema1.1 target rule |
| Exact configured MCP `server_id` and tool name | New schema1.1 target rule |
| Other native operations, including `finish` | Inventory only; no editable scoped broker |

`rule_api` distinguishes `legacy`, `target`, and null. A read-only inventory row
does not deliver a permission control. `finish` remains lifecycle control rather
than an execution grant. Skills, plugins, context selection, transcript reads,
and generic network policy are not newly scoped by this increment.

## Logical identity and exact bindings

The logical native target is `{kind:native,name}`. An MCP target is
`{kind:mcp,server_id,name}`. Its stable `target_id` hashes that logical identity,
so changing configuration cannot silently create a different target that falls
through to host Allow or YOLO.

Native binding contains `protocol_version:1.0` and `schema_sha256` of the native
argument schema. MCP binding contains `config_sha256` and `tool_sha256`. The
latter hashes the exact observed tool name and input schema, independently of
other tools or descriptions. Configuration metadata contains opaque secret
references, not resolved secret values. These hashes do not attest remote code,
executable behavior, server version, or the absence of remote side effects.

Normal scope precedence resolves the first saved rule: task objective revision,
session/thread/epoch, approved workspace, default, then host. If that winning
rule's stored binding differs from current native metadata, its effective value
is Deny until the owner explicitly rebinds or resets it. Resolution does not
continue to a broader Allow. Missing metadata is unavailable and also refuses
admission. Current and stored bindings, `binding_state`, rule source/revision,
host mode, hard-denial state, and native scope remain separate facts.

An unobserved MCP schema has `tool_sha256:null`. Deny can bind the current
configured target in that state; Allow and Ask require an enabled, allowlisted,
transport-permitted target with an observed schema. Existing explicit MCP
discovery/Test/Connect paths can populate observations. Reading Options cannot.
A removed target may have `binding:null` while its stored binding remains
available for inspection/reset. Reset removes only the selected exact rule and
restores inheritance; it is not a new grant over other targets.

## Persistence and additive contracts

`ToolPermissionTargetChange` is strict schema1.1 with exact fields:
`expected_revision`, `expected_state`, `idempotency_key`, native `scope`, logical
`target`, matching `binding`, and nullable `mode`. Non-null mode is Allow, Ask,
or Deny; null resets. Native target changes accept Search only; the legacy three
operations continue through schema1.0, and other native rows remain read-only.

The protected store adds target rows while sharing the existing global revision,
fingerprint, 1,024-rule cap, 10,000-receipt cap, and idempotency-key namespace.
Both rule tables count toward the rule cap. Historical exact receipts are read
before current target/scope eligibility or CAS and never reapply an old write.
Rule mutation, revision increment, and receipt commit together, without an await
inside the transaction. Full capacity refuses rather than evicting history.

The four-table permission store uses SQLite storage version 2. Affirmative
atomic file creation may initialize the tables and revision-zero singleton;
existing empty, missing-state, truncated or corrupt authority stays unavailable.
The exact three-table layout at version 0 or 1 is fully validated before a
transaction adds the target table and advances the storage version. Validation
checks the combined rule cap and both strict receipt versions, original request
digests, complete unique revision history, and exact agreement between each
latest operation and its retained rule, including target bindings. It consults
no current model, scope liveness or MCP server during migration.

The unversioned pre-release four-table layout is explicitly unsupported and
left in place for owner recovery. It is never interpreted as an empty store.
Version-1 services reject the newer version/table rather than ignoring target
policy. Version and closed-schema checks continue before target reads, including
missing-rule, saved-target and historical-receipt lookup paths, so those paths
cannot interpret newer or incomplete schema as host inheritance.

A schema1.1 receipt has `status:permission_target_saved`; it binds original
scope, target, binding, mode, expected revision/state, key, and resulting revision
exactly. Existing schema1.0 receipt shapes remain unchanged. A target page uses
schema1.1 with the same native scope/state/instance metadata, `targets` instead
of `tools`, and `next_after`. It contains at most 20 rows, ordered by 64-hex logical
target ID. Current inventory is bounded to 16 native plus at most 16 servers'
128 observed and 64 allowlisted names; at most 1,024 saved target identities can
also remain visible. Saved scope paging includes either rule table and supports
retired-scope inspection/reset.

The native store entry points are `select_targets`, `target_snapshot`, and
`change_target`; `receipt` serves either strict receipt version. The owner API
integration below `/v1/agent/options` uses:

| Method and route | Meaning |
| --- | --- |
| `GET /tool-permission-targets` | Native scope selection using `scope`, `session_id`, `task_id`, or saved `scope_id`; target paging uses 64-hex `after` and `limit` from 1 to 20. |
| `POST /tool-permission-targets` | Save/reset an exact schema1.1 target rule. |
| `GET /tool-permission-changes/{key}` | Existing receipt lookup, now serving either exact receipt version. |
| `GET /tool-permission-scopes` | Existing saved native scope page, including both rule tables. |

Owner authentication and no-store responses apply. CAS/scope/binding conflict
returns HTTP409; refused changes return HTTP400, unavailable or unconfirmed state
HTTP503, and schema-invalid requests can return HTTP422. The embedded editor
uses the same facts. Its API/editor source has been statically reviewed; the
endpoint contract is not a claim that it has passed runtime acceptance or
preview verification.

The editor retains up to 32 unsaved choices in owner-scoped RAM independently
of up to 16 unconfirmed saves. Each draft keeps its original native instance,
scope, CAS, logical target, current/stored binding, and override. Refresh or a
view change does not silently replace that base. A changed binding or base state
requires explicit draft discard before a new choice; an unchanged saved mode
can be explicitly rebound to the reviewed current identity. Removed targets
remain inspectable/resettable, and their retained drafts remain visible even
when absent from the loaded inventory page.

As in ADR0096, an unconfirmed save retains its unchanged body/key. Missing
receipt or HTTP404 is not negative proof. After receipt lookup, an authoritative
same-instance revision strictly above the original CAS revision can fence any
new commit of that old request, allowing explicit release of RAM tracking only.
It does not prove the historical outcome, roll back a save, cancel a request, or
release actual outstanding HTTP ownership.

A dispatched HTTP400/409 without an exact receipt also remains unknown. These
responses currently provide no typed negative mutation proof, so an unchanged
native revision may leave the retained request blocked until a real mutation
fence becomes observable. The editor does not infer failure or release local
tracking from HTTP status alone.

## Actual Search and MCP admission

Search validates its exact broker arguments, path containment, and current
scoped decision before beginning filesystem inspection. Its own rule governs
Search; internal bounded reads do not substitute a `read_file` approval. A
`read_file` Deny therefore remains distinct from a Search Deny.

MCP carries an explicit native `MCPToolAdmission` object through the actor queue.
An actor's copied `ContextVar` cannot supply another task's authority. The token
binds exact immutable call arguments, digest/revision, native task and host/policy
identity, source/disclosure state, permission fingerprint, current target
binding, effective decision, and any reviewed human approval.

The actor checks before transport preparation, after connection/discovery waits,
and after schema validation. Native source/session/speech authority is refreshed
after waits. Immediately before SDK call handoff, synchronous currentness checks
run and the one-use approval is consumed once. Ask still needs a human approval
under host Allow or YOLO. Uncommitted speech independently needs human review.
Approval expiry preserves the exact paused action for
[ADR0092](0092-approval-expiry-renewal.md) renewal, without inference or effects.

Caller timeout/cancellation retires the token synchronously before waiting for
actor cleanup. A retired/consumed token, changed task or objective, changed host,
stale rule, or mismatched arguments cannot admit another call. Already admitted
operations remain owned until actual cleanup; later permission changes cannot
recall their bytes. MCP transport failure is not automatically replayed.

Admission provenance distinguishes owner approval, owner rule, and host source,
including exact target/binding and native rule identity/revision. A failed call
after admission can be recorded with `effect_state:not_established`; admission
or a received response is never proof of a remote effect, rollback, or no effect.

## Preserved ceilings and evidence

Host hard Deny, exact configured MCP allowlist, enabled state, stdio shell grant,
HTTP network grant, remote opt-in, native disclosure, protected paths, and actual
Bubblewrap isolation remain mandatory. A target Allow cannot enable a server or
widen those ceilings. Shared long-lived MCP transports are not a task-specific
network firewall. Search rules do not retroactively erase retained task context
or create a blanket filesystem-read prohibition. Existing disclosure policy
still governs retained context; next-admission rules govern future operations.

The strict target/binding/change roots are explicitly exported by
`scripts/export_contracts.py`. JSON Schema describes structural contracts; native
validators and brokers remain authoritative for cross-field bindings and policy.
Source review, schema export, formatting, and static checks do not establish a
test result, provider readiness, or qualified remote behavior. No new automated
cases or local test execution are implied. Exact-head CI and preview evidence
belong on the PR. Remaining work stays in the
[delivery plan](../options-delivery-plan.md).
