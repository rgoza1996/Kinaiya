# Agent Options API 1.0

Implements [#188](https://github.com/rgoza1996/Kinaiya/issues/188) and
[ADR0077](adr/0077-options-workspace-and-harness-models.md).
Every route requires authenticated owner access. Metadata is local inspection,
not inference, endpoint readiness, permission expansion or harness promotion.

## Inspect

`GET /v1/agent/options` returns `schema_version: "1.0"`, current default provider
metadata, effective permission flags, maximum task steps, native tool names and
`models`. A host without the model-settings owner returns `models: null`.
The model snapshot contains:

```json
{
  "schema_version": "1.0",
  "revision": 0,
  "change_boundary": "new_tasks",
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

`GET /v1/agent/options/model-selection?harness_version=seed-v1` resolves current
native eligibility and returns the effective provider and model pin for that
harness. Omit the query to use native active-harness selection for a legacy
unassociated session. An unavailable/unapproved selection returns 409, not a
replacement provider. This read creates no session, task or override.

## Change

`POST /v1/agent/options/models` accepts only:

```json
{
  "schema_version": "1.0",
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

Success returns an exact persisted receipt:

```json
{
  "schema_version": "1.0",
  "idempotency_key": "unique-owner-request-id",
  "revision": 1,
  "harness_version": "seed-v1",
  "binding": {"profile_id": "host-default", "reasoning": null},
  "change_boundary": "new_tasks"
}
```

An inherited binding returns `binding: null`. The revision advances once. Repeat
the same key and body to recover the same receipt, including after a lost
response or restart. A key/body conflict or stale revision returns 409. Invalid
profile/eligibility/limits return 400; malformed wire input returns 422;
unconfigured/unavailable management returns 503. Authentication uses the usual
401/403 rules. Writes never retry automatically or rewrite existing task pins.

`GET /v1/agent/options/model-changes/{idempotency_key}` retrieves a committed
receipt. A 404 means no committed receipt is currently visible; it does not
prove an earlier request cannot still commit. Exact-key retry is safe, and a
different change requires reconciliation first.

The additive Task `model_pin` field is native output, never a TaskRequest field.
It contains schema version, profile ID, provider, model, locality, reasoning and
settings revision. Old tasks may have null/missing pins. Future settings or
profile names must not be projected backward as historical identities.
