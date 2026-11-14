# ADR 0110: Explicit provider disclosure policy

Status: accepted (prerequisite for provider-agnostic harness work).
Specification: Roadmap §§7 and 14. This is a prerequisite for configurable
endpoint adapters; it does not enable a new runtime protocol.

## Context

Conversation, command, skill, MCP and plugin checks repeat a Meta/Contributor
predicate. A generic Responses adapter must not lose the public-only
restriction when its provider label changes. Model engines, loading and
inference remain user-managed; no engine work belongs here.

## Decision

- Add optional `ProviderInfo.disclosure_policy` (`public_synthetic_only`,
  `owner_authorized`, null; default null). It is a remote content ceiling only;
  it grants no consent, execution, credential or tool permission.
- Null omits only the new field in `model_dump`/JSON via the existing
  Pydantic wrap-serializer pattern; old dicts, receipts and digests are
  unchanged. Explicit non-null survives typed round trips.
- Add shared `packages.providers.disclosure.public_only_provider(info)`: accepts
  attribute or mapping metadata, validates exact provider/model strings and
  exact policy literal/null, raises constant `ValueError` without reflecting
  data. True when `provider == "meta"`, `contributor` in model (case-insensitive),
  or policy is `public_synthetic_only`. `owner_authorized` never relaxes.
- `scope_allowed` keeps three closed scopes, denies invalid/missing scope and
  invalid policy metadata, preserves local behavior, restricts public-only
  remotes to `public_synthetic`, and keeps ordinary/owner-authorized remotes at
  `public_synthetic` or `private_cloud_authorized`.
- Apply the shared predicate in commands skill visibility, skill, MCP and plugin
  boundaries with existing typed refusals, pending-write fences,
  credentialed-MCP exclusion and local behavior. Invalid policy is
  unavailable/refused, never an unhandled reflecting error.
- `standing_export._complete` permits one exact raw-dict legacy shape for
  `AgentExportProviderInfo` with the five original keys, normalizing
  `disclosure_policy` to None internally. All other completeness checks stay.

## Consequences

Unfamiliar `public_synthetic_only` providers get the same public-only limits as
Meta/Contributor across export and model-facing tools. Ordinary and
`owner_authorized` behavior is unchanged apart from the ceiling. No stored
profiles, defaults, routes, execution or tuning change. A later generic adapter
must bind policy/configuration generations before runtime activation.

Helper and component refusals contain no rejected metadata. Typed contract
validation retains ordinary Pydantic diagnostics; callers must handle them
at the component boundary. Independent synthetic tests verify legacy wire
bytes/digests, nested export round trips and model-facing disclosure controls.
