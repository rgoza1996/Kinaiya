# Progressive Agent tool discovery (#171)

Status: implemented bounded contract; not broad third-party qualification.
This extends the task-qualified advertisement in
[ADR0070](adr/0070-task-qualified-tool-advertisement.md). Roadmap §§8, 31–32 and
clarification §§3, 5, 7 and 13 remain authoritative.

## Observable behavior and authority

Agent receives a read-only native `tool_catalog` operation and an authenticated
inspection API using the same query/projection. Bounded summaries precede one
selected logical argument schema. Native operations, skills, configured MCP
metadata and approved compute plugins keep distinct qualified identities.
Discovery never installs, evaluates, activates, executes, approves or creates a
task/version pin. Existing executors and disclosure/permission brokers remain
authoritative. Its `inspection` evidence never counts as work evidence for
Agent completion or Converse/client success grounding. It cannot clear failed
work/publication/plugin taint or authorize a terminal task.

The new operation is separately advertised by the trusted runtime. Existing
snapshot constructors and provider calls without that feature retain their
previous operation selection. Optional unavailable operations are explicitly
labeled unavailable, without an invocation binding, and remain absent from the
callable snapshot/wire enum. Native write/shell operations requiring approval
remain implemented operations; missing grants are not missing implementations.

## Query and response contract

Logical arguments are `kind`, `name`, `server_id`, `query` and `after`, all
optional. `kind` is exactly `native`, `mcp`, `skill` or `plugin`. Omitted values
mean list all permitted kinds. `name` requests exact selected inspection and
requires `kind`; an MCP selection also requires `server_id`. `server_id` is
accepted only with `kind=mcp`. A selected query cannot also search or paginate.
An empty search is equivalent to no search. No arguments select provider,
disclosure scope, owner authority, invocation mode, permissions or pin creation.

Names are strict, nonempty, NUL-free UTF-8 strings of at most 512 characters;
server IDs have the same checks with a 128-character bound. Search is strict
UTF-8 text of at most 80 characters. `after` is a strict integer in 0..1,000,000;
booleans/coercion, unknown fields and malformed combinations fail with constant
errors. No input is stripped, interpreted as code or used as a filesystem path.

Disclosure filtering precedes matching, sorting, pagination and output. Search
uses case-folded literal substrings of identity/name/description, never regex or
full skill instructions. Per-request stable order is kind (`native`, `mcp`,
`skill`, `plugin`), server ID, then local name. Pages contain at most four rows,
each at most 2 KiB of canonical JSON; the complete page is at most 16 KiB. Each
page reflects its own current metadata; an offset is not a durable snapshot or
an execution pin. No unbounded catalog/schema injection occurs in prompts.
`next_after` advances by returned rows when more matches remain, and is null
at exhaustion (including an offset beyond the last match).

Items carry separate `kind`, `name` and optional `server_id` fields. Native
qualified identities retain `native:<name>`; skill/plugin identities prefix
their exact local ID; MCP identities use `mcp:` followed by canonical JSON of
`[server_id,name]`. Execution uses the explicit fields, never delimiter parsing
of display identities. Descriptions remain bounded untrusted metadata.

Selected inspection returns a complete bounded schema or an explicit unavailable
schema state. It never truncates/repairs JSON. Logical schemas describe canonical
valid calls, not provider wire aliases/JSON strings or proof that contextual
checks will accept an action. Existing legacy coercion/clamping is not silently
changed by documentation. Dynamic requirements stay explicit: workspace and
protected-root checks, current revision, exact allowlists, action approvals,
transport grants, sandbox and disclosure are rechecked at dispatch.

Explicit native file-write/command Deny is projected as `approval: "denied"`,
with no requestable speech approval. It is distinct from both `per_action` and
`broad_grant`. Denied operations remain known protocol operations and refuse
before consuming an approval; no-approval-needed does not imply authorization.

The complete selected response and retained model-history unit are bounded to
32 KiB. Serialization accepts only finite, UTF-8-valid JSON primitives, bounded
depth/nodes and string keys. No arbitrary Python objects, exception text,
credentials, endpoints, secret-reference names or local installation paths are
copied. Catalog failures use constant reasons; cancellation still propagates.

`GET /v1/agents/tool-catalog` requires the existing owner/Decisions actor
authentication and accepts the same query fields, plus an optional actual
`task_id`. It uses the configured Agent provider's disclosure view; callers
cannot choose a different provider or scope. GET cursors use bounded ASCII
decimal text. Duplicate/unknown fields and invalid combinations return constant
400 errors; an unknown task returns 404. Host metadata failure returns 503 or
an explicit per-catalog `metadata_unavailable` state. The existing capability
endpoint continues to expose the smaller task-qualified native identity list.

Native-only guards recheck metadata after broker and journal awaits and at
built-in provider export boundaries. Once a catalog is delivered to Agent,
its guards remain for that task even after the original entry leaves the
eight-entry Agent seed history window. Later actions may quote earlier metadata:
withdrawal therefore stops the dependent task and sets a sticky local-only
disclosure floor, rather than deleting one entry and exporting derived history.
This is separate from Converse's context-window conversation history.
Same-content skill/plugin pin creation remains valid; historical active-version
references remain subject to disclosure. MCP cache refresh is a new observation,
not an implementation pin. Configuration/transport/scope withdrawal still
invalidates retained MCP metadata. Already dispatched effects are not recalled.

Durable inspection evidence stores a digest and an explicit metadata-omitted
receipt. Complete schemas stay only in the bounded, guarded model-history unit;
they are not copied into task outcomes for other roles. Catalog inspection uses
no new inference call. Tests use synthetic model transports; live capability or
model-quality qualification is separate.

Model history records the executed canonical logical action (at most 8 KiB) and
its complete result as separate guarded units. Results identify the completed
inspection explicitly. A real typed trial repeated discovery while the action
trace was omitted; that observation alone does not establish a general cause.
Flat serialization still rejects non-null fields
outside the five catalog arguments, including `objective_revision`; its wire
instructions make that restriction explicit. No malformed output is repaired.

## Source-specific facts

- Native inspection uses the actual task-qualified snapshot and trusted logical
  definitions. Availability, contextual permission requirements and execution
  readiness are separate facts; no health probe runs merely to inspect.
- MCP inventory uses only configured, disclosure-filtered metadata and any
  already observed tool definitions. It does not initialize/connect/spawn or
  fan out `discover()` to fill a page. Missing definitions say `not_discovered`;
  the existing explicit `mcp_list` remains the controlled discovery operation.
  Configuration/definition digests are not remote implementation hashes or
  task pins. Missing server version stays unknown.
- Skill inspection is a new metadata-only projection, separate from `load()`.
  Preserve package identity, version/digest, invocation restrictions and current
  disclosure rules. Do not return full instructions/resources or dependent-task
  identities. Selected inspection creates no package pin.
- Plugin inspection verifies the selected approved version without evaluating
  or running it. Distinguish active and task-pinned versions; never describe the
  active schema as the schema of an older task pin. Disablement and disclosure
  revocation remain effective. Unknown readiness stays unknown.

## Pure helper interface for independent implementation

`packages/contracts/tool_catalog.py` supplies only the following pure helpers;
it imports no runtime, broker, provider, filesystem or network code:

- Constants `CATALOG_KINDS`, `PAGE_SIZE=4`, `MAX_ITEM_BYTES=2048`,
  `MAX_PAGE_BYTES=16384`, `MAX_CATALOG_BYTES=32768`, `MAX_CATALOG_ITEMS=8192`.
- `CatalogError(ValueError)` with one closed `.code`: `invalid_query`,
  `invalid_metadata`, `metadata_too_large` or `catalog_too_large`. Messages never
  interpolate input or exception payloads.
- Frozen `CatalogQuery(kind=None, name=None, server_id=None, query=None, after=0)`
  validates direct construction, and exposes boolean `.selected`.
  `parse_query(arguments)` accepts an exact dict with only those five keys.
- `qualify(kind, name, server_id=None)` validates identity and returns the
  canonical display identity above without interpreting it as an executable ID.
- `encode_catalog(payload, *, max_bytes=MAX_CATALOG_BYTES)` returns canonical
  ASCII JSON (`sort_keys`, compact separators, `allow_nan=False`); maximum is a
  strict integer in 1..32768. Reject objects/subclasses outside exact JSON types,
  invalid UTF-8, nesting beyond 24 and more than 16384 visited values/keys. The
  helper must not call user-defined string/repr/iteration methods.
- `catalog_page(items, query, *, catalogs=())` requires an exact list/tuple of at
  most 8192 exact dict items, validates identities and row byte bounds, and
  returns fresh JSON data: `{schema_version:"1.0",items:[...],next_after:...,
  catalogs:[...]}`. It accepts list queries only. Schema-bearing list rows are
  invalid. Only the parent supplies already disclosure-filtered/normalized
  metadata; this helper cannot infer authorization. Descriptive schema metadata
  is allowed, but actual payload keys `schema`, `input_schema`, `output_schema`,
  `argument_schema` and `arguments_schema` are forbidden in list rows, including
  nested values. Each item requires kind,
  name and matching qualified_id; only MCP items have a server_id. Reject
  duplicate identities. `catalogs` has at most one exact
  `{kind,state}` dict per kind, with state `configured`, `not_configured` or
  `metadata_unavailable`. Sort those states in the same kind order. Pagination
  advances through the filtered rows; no row/schema is clipped or repaired.

## Verification requirements

Cover literal search, advancing byte-bounded pages, name collisions, hostile
JSON/metadata, selected logical schemas, enabled/disabled native combinations,
provider wire agreement and unavailable-action execution traps. Native API and
model dispatch must use the same source/projection. Test disclosure before
search/count/page, scope revocation across awaits, MCP initialization/call traps,
skill pin/load traps, pinned-versus-active plugin schemas, oversized schemas,
terminal/stale task behavior and failed-effect taint. Existing permission,
MCP/skill/plugin, completion and provider suites remain required. No live-service
or general model-reliability claim follows from deterministic fixtures.

Contract review note: the initial independent-helper prompt used a 256-character
name limit. Existing valid skill IDs can reach 264/265 characters. This contract
therefore requires 512, with regression cases for those existing identities;
the frozen original prompt and any resulting correction remain in the development
evidence. A universal bound must not silently hide a valid installed package.
