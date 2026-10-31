# Typed Meta action wire (#169)

The six-case public run at `dcd3eea` retained three native failures: exhausted
output budget, trailing prose inside `ui_spec_json`, and an active
`objective_revision` alongside `ui_spec_json`. Two distinct create proposals
also occurred despite intact native receipts. These observations do not prove
why the model selected another create or establish a native replay defect.

This change introduces an explicit `action_format: "typed-v2"` Meta provider
profile. The existing `flat-v1` profile remains the compatibility default during
qualification. No automatic profile fallback, extra inference, coercion of a
rejected proposal, finish, or permission change is authorized by this format.

For an explicitly selected Meta Agent profile in `providers.agent`, set
`"action_format": "typed-v2"`. The existing launcher enables structured actions
for Agent. Direct adapter construction also requires `structured_actions=True`.
Do not apply this setting to ordinary Converse or Decisions generation. A change
to an existing host profile follows its normal owner-controlled restart/task
boundary; reading this document does not reconfigure a running host.

## Wire contract

A plain closed root object contains exactly `action`. That value selects one
closed `{tool, arguments}` object from a nested `anyOf`, with a single literal
tool name and that operation's own argument fields. Only operations from the
immutable native per-inference snapshot appear. All fourteen native operations
have a projection; optional foreign gateways retain their existing selected
schema/JSON-input and native authorization paths.

`publish_ui.arguments` is a typed create or update object. Create contains
`title`, `summary`, `components`, `layout`; update contains `artifact_id`,
`expected_revision`, `updates`. Components select the nine existing native data
shapes by their type. JSON data is generated directly rather than through a
JSON-containing string. No native task/session/evidence metadata is accepted.
All object properties are required in the strict wire. A declared optional
property uses null to request omission; only those validated object fields are
omitted on conversion. Null table cells, false, zero, empty lists and empty
strings retain their actual values. Unknown or extra fields are rejected,
including `objective_revision` on publication.

The adapter parses one bounded UTF-8 JSON object, rejecting duplicate keys,
invalid Unicode, non-finite values and trailing material, then strictly validates
the selected typed branch. A known unavailable operation retains only its name
for the existing native membership refusal; its arguments are never decoded.
Typed validation failures retain only constant diagnostics. UI byte, component,
relationship, quota, task/source, revision, permission, storage and completion
checks remain native and independently authoritative. A valid wire proposal is
not proof of a useful result or successful execution.

History conversion uses the same declared field rules. Native UI publications
continue to use the existing bounded receipt/manifest history; they are never
reconstructed as fabricated model actions. Count/generation request binding
includes the selected format and complete schema. Parallel requests must retain
their own snapshot and format.

## Implementation interface and ownership

`packages/providers/meta_action_wire.py` is pure and uses existing Pydantic:

- `TypedWireError(ValueError)` has a fixed payload-free message.
- `action_schema(tools: tuple[str, ...]) -> dict` returns a fresh schema for a
  validated native tool selection, using a plain root and nested alternatives.
- `decode_action(envelope: dict, tools: tuple[str, ...]) -> dict` strictly
  validates and returns flat `{tool, arguments}`. It removes declared nullable
  optional fields only. MCP/plugin JSON strings and their existing wire field
  names remain for the text adapter's existing gateway conversion. UI arguments
  are the native logical object, still subject to native validation.
- `encode_action(flat_wire_action: dict) -> dict` converts complete logical
  history to the wrapper and fills only declared optional nulls, validates it,
  and retains every other value. MCP/plugin inputs have already been mapped to
  their existing string wire fields by the adapter. Malformed history is never
  repaired into a successful action.

The lead owns `text.py`, the decoder boundary/diagnostics, docs and integration.
The Muse CLI worker owns only the pure module candidate in a public source
fixture. Independent Codex checks own new tests and reviews. No core/native
validation or production setting is changed by the worker.

## Qualification

[Meta's structured-output contract](https://dev.meta.ai/docs/structured-output)
permits nested `anyOf`, requires a plain root and closed/fully-required objects
in strict mode, and documents a ten-level nesting limit. Both native data-model
semantics and the emitted schema must be checked against that contract; recursive
models and unsupported schema keywords are not assumed to work. No new runtime
JSON Schema dependency is introduced.

Required verification includes all native selections, valid and invalid UI data,
null preservation, exact unknown-field/type rejection, unavailable operations,
no extra model call or finish after rejection, history and prompt-count binding,
concurrent requests, and actual native artifact publication/failure outcomes.
A bounded real-provider schema admission and separately declared task run are
required before claiming this profile works with the service. Native success,
semantic correctness, browser rendering and human voice quality are separate
claims; earlier failed cases remain retained. #169 and the broader production
acceptance gates remain open until their actual requirements are met.

The [October 31 evidence](../evaluation/evidence/meta-typed-actions-2026-10-31.json)
records successful real schema admission and a fixed six-case native task run
at `87f5307`: five native successes, one output-budget failure; three semantic
passes, two partial examples, one failure. No repeated successful create was
observed. The compatibility default and both running previews were unchanged
during qualification. This consumed development set is not a fresh holdout,
statistical improvement, browser/control test or general reliability result.
