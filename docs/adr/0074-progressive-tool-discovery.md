# ADR 0074: Read-only progressive tool discovery

Status: accepted for the bounded remainder of #171.

PR173 repaired native advertisement drift. It did not give Agent complete
selected logical schemas or a common bounded view of skills, MCP targets and
compute plugins. Roadmap §§8, 31–32 and clarification §§3, 5, 7, 13 require
capability discovery without confusing installation, availability or authority.

Add a separately advertised native `tool_catalog` operation and authenticated
inspection API over the same projection. A pure registry describes canonical
native arguments; existing dispatchers remain authoritative, including documented
legacy coercions. Keep native, skill, MCP and plugin identities distinct. Bound
list rows/pages and selected schemas by canonical JSON bytes. Complete schemas
are available on selection; oversized or unobserved definitions are explicit,
never clipped. See the [contract](../progressive-tool-catalog.md).

Use metadata-only skill/plugin reads rather than load/pin/execute operations.
MCP reads configured metadata and prior observed definitions without opening a
transport. Digests identify schemas/configurations, not remote implementations.
Existing disclosure, invocation restrictions, exact allowlists, approvals,
sandbox and pinned execution remain authoritative.

Inspection receipts have a distinct additive contract kind, `inspection`, and
cannot support completion by themselves. Agent, Converse and the bundled client
exclude them from work grounding. Durable receipts omit foreign metadata; complete
responses remain only in bounded model history. Native guards recheck after
awaits, at actual built-in inference exports and before later actions/completion.
Retain guards beyond prompt-history eviction because subsequent text can quote
earlier metadata. Revocation stops the dependent task and narrows disclosure to
local-only; deleting just the original catalog entry would leave derived text.
Same-content task pin creation is a valid transition, not revocation.

The snapshot remains protocol1.0 with an additive operation. Existing snapshot
constructors default discovery off; unbound Meta calls preserve their previous
selection. Both flat-v1 and opt-in typed-v2 carry the new operation when the
runtime advertises it. Strict evidence consumers must accept the additive kind;
the bundled Flutter client and generated schemas change with this implementation.

Deterministic verification covers query/JSON bounds, actual adapter/API paths,
disclosure and pin transitions, passive MCP traps, failed-effect taint, catalog-only
completion refusal, derived-history revocation and provider export awaits. One
real Muse Contributor xhigh call produced independently reviewed pure helpers;
its frozen candidate, corrections, process receipt and cost estimate are retained.
No model-quality, live foreign-tool, Tool Library installation or proof-engine
qualification follows from these tests.
