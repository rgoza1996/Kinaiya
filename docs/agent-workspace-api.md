# Agent harness workspace API (#157)

The two Agent sidebars inspect real protected harness versions and their durable
Agent sessions. The implemented generated extension kind is `context_selector.v1`:
a bounded workspace context selector used by the fixed Agent runtime. It is not a
general replacement Agent, model, permission profile or self-modifying core.
`seed_runtime` identifies the fixed `seed-v1` runtime when no selector registry is
configured. Display names and descriptions are literal untrusted metadata.

## Owner API used by the UI

All routes below require the existing owner bearer credential. Opening or
selecting a sidebar performs reads only; it never promotes, evaluates, loads a
model, submits work or changes disclosure consent.

`GET {agentBase}/v1/agent/harnesses?limit=20&after=<version_id>` returns:

```json
{
  "schema_version": "1.0",
  "configured": true,
  "items": [{
    "version_id": "v-0123456789abcdef01234567",
    "kind": "context_selector.v1",
    "name": "public-selector",
    "description": "Literal registered description",
    "active": true,
    "selectable": true,
    "eligibility": "active",
    "registered_at": 0.0
  }],
  "next_cursor": null
}
```

`limit` is 1–50. Cursor order is ascending immutable version ID. Eligibility is
`active`, `approved_baseline`, `owner_promoted`, `unapproved` or `unavailable`;
the first three are selectable. The immutable native baseline remains approved
after a different version becomes active. Historical promotion must still have protected, valid approval/report
evidence under the registry's current evaluation policy. Missing proof fails
closed. Registration or a model's claimed score alone never permits execution.
An unconfigured registry returns the real selectable `seed-v1` item, kind
`seed_runtime`, eligibility `active`, and `registered_at: null`.

`POST {agentBase}/v1/agent/sessions` accepts only:

```json
{"schema_version":"1.0","harness_version":"v-0123456789abcdef01234567","name":"Public synthetic work"}
```

It creates an Agent session only after native eligibility checks, returning the
ordinary Session object with its immutable `harness_version` pin. Creation does
not change the global active version. Invalid/unavailable selection is refused;
the client must reconcile an uncertain creation instead of blindly replaying it.
The request cannot supply channel, permissions, actor, eligible status or a model.
The existing deterministic `/new` command inherits its invoking Agent session's
pin and checks eligibility again. A null legacy association remains null.
`/resume` returns the target's actual association without changing it or granting
execution eligibility.

`GET {sessionBase}/v1/sessions/page?channel=agent&harness_version=<version_id>&limit=20&after=<cursor>`
returns `{"schema_version":"1.0","items":[<Session>],"next_cursor":null}`.
`limit` is 1–20. The opaque decimal cursor pages newest sessions first and must
be passed back unchanged. The filter includes empty sessions. Use literal
`harness_version=unassociated` for legacy Agent sessions whose pin is null;
omitting the filter lists all Agent sessions. A Session has:

```json
{"schema_version":"1.0","session_id":"synthetic-session","thread_id":"synthetic-thread","channel":"agent","epoch":1,"name":"Public synthetic work","harness_version":"v-0123456789abcdef01234567"}
```

Legacy sessions retain `harness_version: null`; no current configuration is
backfilled as historical fact. Existing generic owner session creation and
`GET /v1/sessions` remain compatible. A pin stored through that lower-level
owner API is metadata, never proof of execution eligibility. Converse sessions
cannot have a harness pin. Reset preserves the pin; deletion removes the session.

## Selection, history and task admission

Session selection remains local client state. Re-read the chosen identity through
`GET {sessionBase}/v1/sessions/{id}`, require channel `agent`, and fence all reads
by connection generation, selected session, thread and epoch. Restore literal
historical entries through the existing bounded `GET /v1/transcript`; never
submit a task or ask a model to reconstruct the history. Keep drafts and histories
scoped to their selected session, and preserve global pending approval controls.

Task requests retain their existing contract: callers cannot supply a harness.
Native admission revalidates the selected session pin and registry eligibility,
then records that exact version in the existing Task `harness_version` field.
It never promotes or falls back from an unavailable pin. Unassociated legacy
sessions retain their existing global-active admission behavior, while the
session's historical association remains null. Running tasks retain their
original pins across sidebar selections, session resets and global promotions.

UI collapse, filtering and task inspection grant no execution permission.
Unsupported hosts, empty catalogs, unavailable versions and unassociated legacy
sessions must remain explicit states; illustrative Penpot entries are not data.

Catalog inspection is bounded to 50 returned versions, 4,096 scanned registry
entries and 4 MB of protected approval receipts. Exhausted/unreadable proof never
silently grants historical eligibility. Session pages return at most 20 rows;
the journal client rejects pages above 256 KB or a substituted harness/channel.

## Verification

`tests/integration/test_agent_workspace.py` exercises real owner HTTP routes,
SQLite persistence/pagination/reset/delete, protected stored-report fixtures,
candidate and forged-scope refusals, source/suite/policy/profile invalidation,
bounded proof reads, concurrent creation, promotion during a source read,
legacy compatibility, task pin round trips and `/new` inheritance. The fixtures
use public synthetic metadata and ScriptedProvider; they make no paid calls or
read a real owner's journal. Historical eligibility reuses native promotion
validation, rather than accepting a score or eligible flag from the client.

The focused workspace/contracts run passes 42 cases. A broader run of session,
Agent runtime, harness, command, disclosure, source-authority and retirement
suites passes 192 cases; the three final proof-bound/policy regressions were then
included in the focused 42-case rerun. Ruff and `git diff --check` pass. UI
selection, transcript projection and the two collapsible panels are separate
client work against this contract.
