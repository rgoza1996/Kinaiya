# Agent Options API

Native root selection and owner read-only folders use the independently bound
workspace owner in [ADR0097](adr/0097-workspace-owner-selection.md):
`GET/POST /v1/agent/options/workspace-settings`, exact receipt inspection at
`GET /v1/agent/options/workspace-changes/{key}`, and full-request reconciliation
at `POST /v1/agent/options/workspace-changes/lookup`. Saved settings and active
concrete-root identity are separate. Older execution metadata below remains
inspection of its own owner; its `workspace_mutable: false` does not grant root
selection through an execution-preference write.

## Approval expiry and renewal

The existing owner approval list retains expired actions with additive
`status:pending|expired`. Expiry pauses the same action; approving an expired
request is refused. Owners may deny, stop, or explicitly renew through
`POST /v1/approvals/{old_id}/reissue` with strict `ApprovalReissue` fields
`schema_version`, `action_digest` and `objective_revision`. This creates a fresh
unapproved request. Typed `ApprovalReissueReceipt` retains both IDs, exact
digest/revision, replacement expiry and current `replacement_status`.
The same old-ID/body replays its receipt without allocating another replacement.
HTTP410 means bounded history is unavailable, not failure proof. See
[ADR0092](adr/0092-approval-expiry-renewal.md) for native ownership and UI recovery.

Implements [#188](https://github.com/rgoza1996/Kinaiya/issues/188) and
[ADR0077](adr/0077-options-workspace-and-harness-models.md).
Bound model mutations and recovery are specified by
[#191](https://github.com/rgoza1996/Kinaiya/issues/191) and
[ADR0088](adr/0088-bound-model-settings-writes.md). The outer inspection schema
remains 1.0; new model mutations use 1.1.
Every route requires authenticated owner access. Metadata is local inspection,
not inference, endpoint readiness, permission expansion or harness promotion.

## Inspect

### Scoped native operation rules (ADR0090, implementation under review)

`GET /v1/agent/options/tool-permissions?scope=default|workspace|session|task`
resolves the scope on the native host. Session and Task choices also require
`session_id` or `task_id` respectively. An exact `scope_id` instead inspects a
saved scope, including one whose task revision or session epoch has retired.
The snapshot returns the instance identity, global revision/state fingerprint,
canonical scope, liveness and three rows for `read_file`, `write_file`, `shell`.
Each row distinguishes its saved override, effective mode/source and host
hard-denial ceiling. These controls do not cover every filesystem or network
channel.

`POST /v1/agent/options/tool-permissions` accepts strict
`ToolPermissionChange {schema_version, expected_revision, expected_state,
idempotency_key, scope, tool, mode}`. A null mode resets only that exact rule;
new grants require a live native scope. Saved retired scopes remain resettable.
The change boundary is `next_action_admission`, independent of the service-wide
execution editor's settled-worker gate. Task rules bind objective revision;
session rules bind thread and epoch. Ask remains human review under YOLO;
Allow cannot remove hard denial, sandbox, disclosure or speech-consent checks.

`GET /v1/agent/options/tool-permission-changes/{key}` returns the historical
`permission_saved` receipt with the original CAS state/revision, exact scope,
tool, mode, key and committed revision. A missing receipt is not failure proof.
`GET /v1/agent/options/tool-permission-scopes?limit=20&after=...` pages saved
scope identities for inspection/reset. Storage caps are 1,024 rules and 10,000
receipts. All routes require the authenticated owner; reads apply no settings.
See [ADR0090](adr/0090-scoped-native-tool-permissions.md) for native admission,
retained-context semantics and the same-instance revision fence that can allow
explicit release of stale RAM tracking without deciding its historical outcome.

### Unconfirmed Agent submissions and task commands

Owner clients retain a submission's original session/thread/epoch, exact body
and idempotency key after a dispatched timeout or refusal. A non-success status
alone is not proof that native admission failed. Rechecking sends that same
strict task body to owner-only `POST /v1/agent/submissions/reconcile`, returning
HTTP200 for a confirmed task or HTTP503 while unconfirmed. It cannot create a
proposal, launch inference, publish another transcript entry or assign a new key.
Positive replies retain historical model pins, independently of current choices.
Earlier-scope unknowns remain inspectable without blocking a new session.

Task-producing slash commands freeze the derived `TaskRequest`, immutable skill
target and result metadata before submission. Native command replies may return
`status:unconfirmed`; clients retain their original command key. A reserved
command retry only reconciles its original task and cannot create a fresh
proposal or worker. Unobserved native state stays unknown. Legacy interrupted
commands without frozen evidence cannot be redispatched by retry.

New task-command validation refusals before submission explicitly return
`status:error`, the canonical command and `task_rejected_before_submit:true`,
with a nonempty diagnostic capped at 512 Unicode characters. This proof permits
retiring that pending UI attempt while preserving its draft. Legacy cached
errors never gain the marker; an ordinary error status is not failure proof.

At most 128 unresolved task-command intents are retained, with each stored cell
capped at 69,632 UTF-8 bytes. The exact eventual journal request is preflighted
against its 65,536-byte native limit before submission. The first positive task
reply and full command journal body are frozen together; concurrent confirmers
and lost-ACK retries reuse that exact body. Task-result proof is distinct from
command-journal confirmation. Generic journal transport keeps its existing
parsing/cleanup behavior; this increment validates its returned native DTO
projection without claiming strict original-wire parsing for that older route.

`GET /v1/agent/options` returns `schema_version: "1.0"`, current default provider
metadata, effective permission flags, maximum task steps, native tool names and
`models`, `runtime_settings`, and `execution`. Legacy permission flags remain
unchanged for compatibility; precise native modes/effective flags are in the
new runtime snapshot. A host without the model-settings owner returns `models: null`.
The model snapshot contains:

```json
{
  "schema_version": "1.0",
  "revision": 0,
  "change_boundary": "new_tasks",
  "write_authority": {
    "mutation_version": "1.1",
    "backend_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "instance_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "writable": true
  },
  "default": {"profile_id": "host-default", "reasoning": null},
  "harnesses": {},
  "profiles": [{
    "id": "host-default",
    "selectable": true,
    "provider": {"schema_version": "1.0", "provider": "example-provider",
                 "model": "example-model", "local": true, "streaming": true},
    "reasoning": null,
    "reasoning_options": []
  }]
}
```

`harnesses` is a map keyed by exact native harness version. An absent key inherits
the Agent default. A profile whose adapter could not be constructed has
`selectable: false`, `provider: null`. No endpoint or secret reference is exposed.
`backend_id` identifies the protected model-settings database across ordinary
service restarts. `instance_id` identifies its current registered writer. Use the
actual observed identifiers, not these example values. A retired controller
reports `writable: false`. Missing or unsupported authority permits inspection
but is read-only for the new client.

`GET /v1/agent/options/model-selection?harness_version=seed-v1` resolves current
native eligibility and returns the effective provider and model pin for that
harness. Omit the query to use native active-harness selection for a legacy
unassociated session. An unavailable/unapproved selection returns 409, not a
replacement provider. This read creates no session, task or override.

## Change

`POST /v1/agent/options/models` accepts this bound request for a new change:

```json
{
  "schema_version": "1.1",
  "expected_backend": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "expected_instance": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "expected_revision": 0,
  "idempotency_key": "unique-owner-request-id",
  "harness_version": "seed-v1",
  "profile_id": "host-default",
  "reasoning": null
}
```

`harness_version: null` changes the Agent default. A nonnull harness must be
currently eligible for new work, using the same native gate as task acceptance.
`profile_id: null, reasoning: null` removes a harness override and restores
inheritance; the Agent default itself cannot inherit. A nonnull reasoning level
must occur in that profile's host-declared options. Null means the profile's
configured effort. Models and provider addresses cannot be supplied inline.
Unknown fields, invalid types/IDs and unsupported labels refuse.
Both authority identifiers are required 32-character lowercase hexadecimal
strings. A request belongs to the observed database and writer generation;
matching revision/profile values on an unrelated backend cannot authorize it.

### Exact provider catalog selections (ADR0089)

The additive `catalog_binding` selects a saved provider generation instead of a
host profile. For example, an explicit Agent default change uses:

```json
{
  "schema_version": "1.0",
  "expected_revision": 4,
  "idempotency_key": "example-owner-catalog-selection",
  "harness_version": null,
  "profile_id": null,
  "reasoning": "none",
  "catalog_binding": {
    "id": "example-local-provider",
    "generation": 2,
    "configuration_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}
```

The illustrative digest must be replaced with the exact observed native
configuration digest. A nonnull catalog binding and nonnull host `profile_id`
are mutually exclusive. A catalog choice with `profile_id: null` is an explicit
selection, not inheritance. With neither source, the existing inheritance rules
still apply. Add `auxiliary_task` to select one of the five auxiliary purposes;
all default/exact-harness precedence stays unchanged. Legacy host requests omit
the new field and retain their original idempotency digest shape.

Saved and effective catalog bindings contain `catalog_binding` and `reasoning`,
instead of `profile_id` and `reasoning`. The existing exact-operation receipt
returns that same binding. New state is bounded at 512 KiB; an already larger
legacy state can still be reduced by a strictly shrinking owner change.

The model snapshot adds `catalog_profiles` (at most 32) independently of host
`profiles` (at most 32), catalog availability errors, and task-model cleanup
ownership counts. Entries include their exact reference, declaration metadata,
selectability/reason, reasoning options and unknown readiness/qualification.
Meta requires an explicit measured context; LM Studio requires `none`; the
compatible and Laya catalog kinds cannot provide measured Agent completion.
An unavailable catalog does not hide independently configured host choices.

Settings/model-selection inspection constructs no catalog adapter or key. New
task acceptance captures a fresh primary and available auxiliary adapters with
exact profile/credential generations. A later edit cannot retarget an accepted
task. Retired versions remain unavailable until the owner explicitly selects a
current generation. No automatic fallback occurs. The task model pin adds
`catalog_binding` and `catalog_revision`; historical Task details expose these
beside the existing model-settings revision without reconstructing a provider.
See [ADR0089](adr/0089-agent-catalog-models.md) for admission and actual cleanup.

Success returns an exact persisted receipt:

```json
{
  "schema_version": "1.1",
  "backend_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "service_instance": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "idempotency_key": "unique-owner-request-id",
  "revision": 1,
  "harness_version": "seed-v1",
  "binding": {"profile_id": "host-default", "reasoning": null},
  "change_boundary": "new_tasks"
}
```

An inherited binding returns `binding: null`. The revision advances once. Repeat
the same key and body to recover the same receipt, including after a lost
response or restart. A recorded receipt retains its original backend and service
instance. Exact replay precedes current writer/revision, harness, profile and
default-provider checks and does not assign the runtime provider or apply the
old preference again. A genuinely new write must pass all current checks.
A key/body conflict, foreign backend, retired writer or stale revision returns
409. Invalid
profile/eligibility/limits return 400; malformed wire input returns 422;
unconfigured/unavailable or unconfirmed storage returns 503. Authentication uses the usual
401/403 rules. Writes never retry automatically or rewrite existing task pins.

Legacy 1.0 bodies are accepted only for exact persisted receipt recovery. Their
original normalized digest and receipt remain unchanged, including omission of
null `auxiliary_task`; they never gain invented authority fields. New unbound
1.0 mutations refuse with 409. Upgrade requires stopping older writers that do
not enforce generation fencing.

`GET /v1/agent/options/model-changes/{idempotency_key}` retrieves a committed
receipt. A 404 means no committed receipt is currently visible; it does not
prove an earlier request cannot still commit.

`POST /v1/agent/options/model-changes/lookup` accepts the complete original flat
1.1 change body, with the original authority, key, revision and choices. It
returns:

```json
{
  "schema_version": "1.1",
  "backend_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "request": {
    "schema_version": "1.1",
    "expected_backend": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "expected_instance": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "expected_revision": 0,
    "idempotency_key": "unique-owner-request-id",
    "harness_version": "seed-v1",
    "profile_id": "host-default",
    "reasoning": null
  },
  "status": "unobserved",
  "receipt": null,
  "mutation_fenced": false
}
```

The echoed request is the complete normalized original body. A recorded result
has `status: "recorded"`, the exact strict receipt and `mutation_fenced: true`.
For an unobserved result, fencing is true only when the original writer has been
permanently retired or the persisted revision is greater than the request's
expected revision. A future expected revision is not fenced merely because it
differs. The native observation serializes receipt, digest, database identity,
revision and writer generation in one transaction without selecting a provider
or harness. A foreign backend refuses 409; corrupt/unreadable state refuses 503.
Neither may issue authoritative absence for the original backend.

Only an exact validated recorded receipt, or a same-backend unobserved result
with this permanent native fence, settles an uncertain save. A bare 404, changed
snapshot instance, matching values or subsequent refusal cannot erase an older
uncertain attempt. A different change requires reconciliation first. These
claims assume protected, unforked and non-rolled-back database state; they are
not authentication of an arbitrary replacement server.

The owner client retains immutable pending changes within their private
authentication scope, canonical Agent endpoint and stable backend identity.
Another owner/backend at the same URL cannot view, retry or settle them.
Same-authority close/reopen/reconnect preserves the original request; navigation
never retries it. Dedicated model requests permit no redirects or automatic
retry, bound request/response bodies to 4096/16384 bytes, and expose a total
10-second result deadline with at most one second spent awaiting cleanup. The
8-second UI waiting deadline does not establish transport settlement. Actual
late owners remain counted globally (32), with 16 logical pending operations,
at most two concurrent mutation owners and one lookup owner per operation.
These are concurrent bounds, not lifetime retry counts; explicit manual retry
can reuse capacity only after actual settlement.

Agent `/model` uses the same observed bound authority for new native changes.
Recovery of its exact inner model receipt returns `model_settings_receipt` and
`recovered: true` without current profile/work/provider checks; that historical
receipt is not a claim about the currently active provider.

## Existing task receipts

The additive Task `model_pin` field is native output, never a TaskRequest field.
It contains schema version, profile ID, provider, model, locality, reasoning and
settings revision. Old tasks may have null/missing pins. Future settings or
profile names must not be projected backward as historical identities.

For [#192](https://github.com/rgoza1996/Kinaiya/issues/192), an exact retry of an
accepted `POST /v1/agent/tasks` returns the retained task and its original pin,
including after restart. Unlike the Options routes, task submission permits
the authenticated owner or Decisions actor; Decisions still requires native
source references. Every retry rechecks the current session/thread/epoch,
authenticated finalized sources, revisions, speech-window supersession and
interruption, and literal Decisions objective equality. The request digest
still binds the objective, source IDs/revision and requested content scope;
the existing pre-scope legacy exception permits only `local_only`.

An exact receipt read does not select or construct a provider, resolve current
harness eligibility, change model settings, append journal entries or restart
work. A recovered paused task stays paused. Original explicit-skill identity
remains part of the digest, owner-only and label-validated; its exact receipt
can be read after the optional skill service is removed. New work still
requires that service when invoking a skill, native harness/model admission,
and both source and combined task disclosure checks. Recovery never rebinds
execution to a replacement model.

[Native submit admission](adr/0087-agent-submit-admission.md) subsequently
resolved [#199](https://github.com/rgoza1996/Kinaiya/issues/199) by atomically
recording initial acceptance and its logical slot. An unconfirmed result stays
paused; a RAM idempotency entry alone is not proof of acceptance. This does not
establish exactly-once external effects or automatic execution after recovery.

## Auxiliary tasks

[ADR0082](adr/0082-task-pinned-auxiliary-models.md) adds five text purposes:
`analysis`, `compression`, `title_generation`, `delegation`, and `harness_design`.
The snapshot includes `auxiliary_tasks` (ID/label pairs), `auxiliary` (global
purpose bindings), `harness_auxiliary` (exact harness to purpose bindings), and
`profiles[].auxiliary_selectable`. Legacy snapshots without them remain valid.
Only adapters supporting measured, bounded text completion are selectable for
these purposes. A primary Agent adapter's availability alone is insufficient.

To set a purpose, add `"auxiliary_task": "analysis"` to the same owner change
request. Null `harness_version` sets the global purpose default; a harness ID
sets that harness's purpose override. Null `profile_id` and `reasoning` remove
the selected purpose override. Resolution is: harness purpose override, global
purpose default, harness primary model, Agent default. The purpose is echoed in
the exact persisted receipt; all scopes share one revision and receipt store.
Legacy request digests and receipts stay unchanged.

At acceptance, Task `auxiliary_models` captures the available purpose pins.
Later defaults cannot rebind them. `consult_model` accepts exactly `purpose`
and bounded `prompt`; it cannot select a provider, run tools, start a child
Agent, edit protected history, or grant permission. Its returned text and
receipt remain untrusted external evidence, not proof of a work effect.
Disclosure, measured prompt admission, resource policy, cancellation and
session/revision fences apply independently to each call. Unavailable or
forbidden routes refuse without silently using another model. Retained foreign
tool/skill/plugin context currently requires a local auxiliary route rather
than reusing a disclosure granted to the primary cloud route.

## Execution settings

[ADR0084](adr/0084-owner-execution-settings.md) adds independent owner execution
settings. `GET /v1/agent/options` returns `runtime_settings: null` when its native
controller is unavailable; old hosts can omit this field. A configured snapshot
contains schema version, revision, 64-character lowercase SHA-256 `state_token`,
service `instance_id`, `source` (`host_configuration` or `owner_saved`), actual
`settings`, persisted `saved_settings`, `active_matches_saved`, `yolo`,
`yolo_scope: "service_instance"`, `effective_write`, `effective_shell`, `busy`,
`change_boundary: "settled_service"` and `execution` metadata.

The same `execution` inspection is also returned at the outer level. It contains
the actual workspace, shell directory or null and its validity, the shell's
`read_only`/`read_write` workspace mount (or `unavailable` when its starting
directory is invalid), protected/read-only folder lists (first
64 entries plus total/truncated), required Bubblewrap declaration and executable
detection, `isolation_verified: false`, configured network namespace, fixed public
environment (cleared host environment, PATH `/usr/bin:/bin`, HOME `/tmp`),
`sandbox_optional: false` and `workspace_mutable: false`. It is owner-only, not
tool/model context, does not enumerate secrets, and executes no isolation probe.

`POST /v1/agent/options/runtime` accepts only:

```json
{
  "schema_version": "1.0",
  "expected_revision": 0,
  "expected_state": "0000000000000000000000000000000000000000000000000000000000000000",
  "idempotency_key": "unique-owner-execution-request",
  "settings": {
    "file_writes": "ask",
    "shell_commands": "ask",
    "tool_network": false,
    "max_steps": 12,
    "shell_timeout_s": 120,
    "working_directory": "."
  },
  "yolo": false
}
```

Use the actual observed token, not the example zeros. Modes are `ask`, `allow`
or `deny`; network/YOLO are strict booleans. Steps are integer 1–2000; command cap
is integer 1–120 seconds. The directory is a bounded relative path within the
existing approved workspace, existing and free of traversal, controls or symlink
aliases. Dot/redundant separators normalize before digest/receipt creation.
The command owner revalidates it for every launch. Root relocation, arbitrary
environment, alternate backends and provider settings are not accepted fields.

The native owner requires settled tasks, workers, child processes and retirement
cleanup, retires MCP transports, then rechecks revision, actual state and worker
ownership. A transaction stores preferences and receipt before synchronous native
apply. File-write Deny precedes approvals/YOLO and mounts the shell workspace
read-only; Ask on file writes does not prevent an independently allowed command
from writing. Tool network access does not grant cloud model disclosure.

Success returns schema, `status: "settings_saved"`, incremented `revision`, exact
`idempotency_key`, normalized `settings`, `requested_yolo`, original
`service_instance`, and `change_boundary: "settled_service"`. Preferences persist;
YOLO does not restore from this store after restart. A historical receipt proves
that save, not current active values. Retry with the identical normalized body/key
returns the receipt without reapplying it or retiring transports again.

`GET /v1/agent/options/runtime-changes/{key}` reads that exact receipt. Key/body,
revision/state or pending-work conflicts return 409; invalid native path/bounds
return 400; malformed wire input returns 422; unavailable/unconfirmed storage or
transport retirement returns 503. A 404 lookup means no committed receipt is
currently visible, not proof an earlier request cannot still commit. Inspect
active values after confirmation, including after restart or another owner
change. Different pending execution changes require reconciliation first.
Inspection and execution-settings routes use `Cache-Control: no-store`.
