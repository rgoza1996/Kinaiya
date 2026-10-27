# ADR 0017: owner MCP management at task boundaries

Tracks [#44](https://github.com/rgoza1996/Kinaiya/issues/44) and fixes the
recovered-task cancellation defect [#45](https://github.com/rgoza1996/Kinaiya/issues/45).
Roadmap §31.5/§32 and M1A require usable setup/lifecycle controls independently
of Converse. ADR 0008's maintained-SDK stdio/Streamable HTTP broker remains the
transport and permission boundary; this change adds its protected control plane.

Enable the optional installed ecosystem host with `"mcp":{"enabled":true}` in
owner configuration. Existing nonempty `mcp_servers` also enables management and
seeds its first store. Subsequent managed state is authoritative; editing the old
seed list does not overwrite it. Explicit `"mcp":{"enabled":false}` disables
the host. Base Agent paths do not import the optional MCP SDK when disabled.

## Operations and authority

Owner-only `GET /v1/mcp/management` returns configuration revision, blocked state,
bounded safe server metadata and descriptor hashes. `POST /v1/mcp/control` accepts
the exported `MCPControl` 1.0 contract: operation, request identity, exact revision,
optional server ID/descriptor and explicit review flag. It supports configure,
enable, disable, remove, test, reconnect and reconcile. Configure replaces a full
definition and stores it disabled. Enable and reconciliation require owner review.
Inspection, configuration and enablement do not connect to a new definition or
execute its code. Updates retire existing transports; protocol cleanup may contact
the previous server. Test and reconnect explicitly initialize/list tools; they never
execute a tool.

Shared Agent CLI/Chat commands are `/mcp list`, `/mcp inspect <id>`,
`/mcp configure <workspace-profile.json>`, `/mcp test|enable|disable|reconnect|remove
<id>` and `/mcp reconcile`. The profile path is read with the existing no-follow
workspace broker. Raw profile contents are not put in the conversational journal.
Agent Chat also provides **MCP connections** settings with disabled profile import,
reviewed enablement and connection/lifecycle actions. These are deterministic owner
controls and remain usable when inference consent is denied. Converse gains no
command parser or MCP settings surface. Reconnecting cannot send an open profile
dialog's captured content to a different backend; late responses are discarded.

Task acceptance, recovery, model selection and MCP management share an admission
lock. Management refuses live/recoverable tasks, including paused work. Finish or
stop those tasks first; there is no untested hot migration of source, allowlists,
credentials or disclosure scopes. Slow management does not hold the command
registry's dispatch lock, so status, stop and approvals remain usable. Expanding
YOLO grants also requires idle tasks. Revocation remains immediate: `/yolo off`
withdraws the permission then retires transports through a separate trusted
controller path without changing server definitions, scopes or revision.

The broker checks its management guard before dispatch and after results. An
uncertain configuration/store state disables metadata enable flags and refuses
calls even when a transport cannot close. Managed descriptor replacement is
context-guarded to controller operations; the trusted broker handle does not grant
models or server content a configuration API. Native host retirement is a trusted
permission-revocation operation, not a descriptor update. Tool allowlists,
workspace/shell/network grants, scoped secret resolution, Contributor filtering
and independent task-effect evidence requirements remain unchanged.

## Persistence and uncertain outcomes

The controller root must be disjoint from the actual `ToolPolicy` workspace in
both directions, with no symlink ancestors. A no-follow regular owner lock holds
single-writer ownership before SQLite initialization. The database and lock reject
hard links. SQLite uses owner permissions and full synchronization. Descriptors
have source digests checked on reopening, a maximum of 16 server IDs and 256 KiB
total canonical definition bytes. Unsupported store schema versions fail closed.
Secret fields accept only `env:NAME` references. Metadata/results/errors never
return raw argument vectors, secret values or reference values; stdio endpoints
are redacted. Public tool metadata still remains untrusted content.

Every control commits intent before effects. Its digest describes the request,
independently of current server state, and lookup precedes state-dependent planning.
A duplicate remove therefore returns its original result after the server is gone;
an old enable response cannot re-enable a server disabled later. Conflicting reuse
is refused. Request identities are never evicted: there are at most 2048 records,
with one slot reserved for explicit recovery. Capacity refuses new work rather
than silently allowing an old action to replay. The non-authoritative audit tail
may discard older entries. Long-lived retention/export management remains future
hardening work.

Desired definitions/revision are committed before live application. Failures during
reservation, definition writes, application or final acknowledgment latch dispatch
closed; unacknowledged effects are not repeated. Startup detects pending intents
and exposes disabled/no-access definitions. Explicit reviewed reconciliation applies
the inspected saved definitions and can restore previously reviewed enables. It
does not replay tool requests. Native transport cleanup is shielded against repeated
cancellation; an unsuccessful probe cleanup cannot return `connected`, and uncertain
probe handles are retained until reaped/reconciled. Lifetime ownership is released
only after cleanup succeeds. Permission revocation can still retire transports.

Recovered Agent events now restore their native local counter rather than reusing
an old journal idempotency key. A verified workerless paused task can be cancelled
without replay. Its outcome explicitly retains that previous uncertain effects
were not reconciled or undone. Repeated stop requests retain the real worker handle
and cannot interrupt its cleanup with another cancellation.

## Evidence and remaining work

Deterministic tests cover native auth, reviews/revisions, admission and recovery
races, command responsiveness, descriptor guards, exact request replay, capacity,
partial store/commit faults, cleanup failure, repeated cancellation, source hashes,
root isolation and secrets. Existing real kernel/MCP tests exercise stdio and
loopback Streamable HTTP. Flutter tests cover consent-independent controls, explicit
review, unknown acknowledgments, malformed metadata and stale backend/dialog results.
`scripts/verify_mcp_management.py` is a separate real API/CLI/transport probe using
public synthetic peers and an inference fixture that must record zero calls. With
`--keep-serving`, it supports browser QA; create its owned `data-dir/stop` file to
finish and reap services. This is not an external service or OAuth benchmark.

Full M1A remains open: remote skill source acquisition, OAuth, MCP resources/prompts,
list-change notifications and upstream compatibility evidence are not completed by
these controls. There is no permission expansion through configuration content,
automatic tool-call retry, arbitrary plugin import, or claim of a full security audit.
