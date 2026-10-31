# Task-qualified native tool advertisement (#171)

The known `ToolAction` protocol and a task's available operations are distinct.
One immutable native snapshot now drives the seed prompt, Meta's strict response
enum and optional argument fields, the runtime's model-action membership fence,
and capability inspection. A configured gateway is not proof that a foreign
target is enabled, healthy or authorized.

## Native contract and request scope

`packages/contracts/agent_tools.py` defines the version 1.0 snapshot, in stable
protocol order, bounded to fifteen known actions including the separately
advertised read-only `tool_catalog`. Each descriptor has its
native name, `native:<name>` qualified identity, protocol version and `native`
kind. Snapshots reject unknown/duplicate/unordered actions, missing base actions,
partial optional gateway pairs, malformed versions and non-boolean flags.

Read, search, write, shell, transcript and finish operations remain discoverable
when action approval is required. An absent grant is not a missing implementation.
Selector availability additionally follows the task's pinned harness: a task
pinned to `seed-v1` cannot use a subsequently selected executable selector.
MCP, skills, compute plugins and UI publication follow their actual configured
gateways. Their existing selected-target discovery, pins, validation, disclosure
and permission brokers remain authoritative.

Agent binds the snapshot with a `ContextVar` around each provider inference.
Nested scopes and cancellation reset correctly; concurrent requests do not
change shared provider tool state. Prompt counting hashes the resulting complete
request, so a measurement for another snapshot cannot authorize generation.
Generic adapter calls outside Agent retain their previous fourteen-operation
selection. The runtime explicitly enables catalog discovery; that is not a
configured foreign-target readiness declaration.

The seed projects its trusted static hints, not instructions supplied by a model
or external catalog. The Meta adapter projects its enum, required nullable fields
and wire instructions from the same native context. Unsupported optional fields
are absent. No prompt text is parsed to establish runtime authority.

## Native refusal and changed availability

A known but unadvertised model action cannot execute. Meta validates a bounded,
closed JSON rejection envelope and preserves only that operation's name for the
native membership refusal; its unavailable arguments are never decoded or run.
Admitted actions retain strict argument decoding. Unknown/malformed responses
remain invalid. This is not a conversion into another allowed action.

The runtime records the refusal without a tool effect or evidence. Failed work,
publication and plugin taint cannot be cleared by an unrelated successful read.
The ordinary step budget still bounds replanning; no extra automatic inference
or completion is introduced. Dispatch also rechecks current gateway availability
after native authority reads. A gateway appearing during inference cannot widen
the original advertisement; a disappearing gateway cannot execute merely because
it was advertised earlier. Existing target permission checks still run.

## Capability inspection

Authenticated `GET /v1/agents/capabilities` retains its existing `tools` list and
adds a bounded `tool_catalog` projection:

```json
{
  "schema_version": "1.0",
  "scope": "native_gateways",
  "task_id": null,
  "task_state": null,
  "items": [{"name":"read_file","qualified_id":"native:read_file",
             "protocol_version":"1.0","kind":"native"}]
}
```

The example omits other available rows. At most fifteen descriptors are returned,
so native inspection needs no unbounded listing or schema loading. Optional
`task_id` is 1–128 characters and selects that actual task's harness pin and state;
unknown IDs return the ordinary 404. Without it, the projection uses the host's
active harness. Reads never start inference, execute tools or change tasks.
The catalog does not authorize work on a terminal task.

This identity projection remains intentionally small. The shared
[progressive catalog](progressive-tool-catalog.md) now provides selected logical
schemas, permission facts and disclosure-filtered native/skill/MCP/plugin metadata
through `tool_catalog` and authenticated `GET /v1/agents/tool-catalog`.
Existing execution brokers remain authoritative. Owner commands, installers,
credential management and promotion are not model operations in this registry.
The optional Tool Library and verification engines in clarification §§14–15
remain separate work. See ADR0074 for #171's discovery/schema extension and its
inspection-only evidence and revocation boundaries.

## Verification

Deterministic tests use synthetic configuration and actual Meta adapter requests
through MockTransport, real SQLite journals, native runtime/API calls and an
execution trap. They cover optional-service combinations, immutable identities,
concurrent scopes, prompt-count binding, seed-pinned task inspection, withheld
gateways, failed-effect taint and changes during inference. Existing authorization,
MCP, skill, plugin, artifact and Agent regression suites remain required.

See `tests/unit/test_agent_tools.py`, `test_seed_tool_snapshot.py`,
`test_meta_tool_snapshot.py` and `tests/integration/test_agent_tool_advertising.py`.
No paid model or live foreign-target qualification follows from these fixtures.
