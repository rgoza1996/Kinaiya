# ADR 0006: Native MCP host (client) for the skills/MCP/plugin milestone

Accepted as the M1A MCP foundation, against Roadmap.md 0.2.1 section 31.5.
The roadmap is unchanged; this does not claim plugin support or full
protocol coverage.

## Context

Roadmap 31.5 requires a version-negotiated MCP client with local stdio and
remote Streamable HTTP support, behind Kinaiya's permission layer. Server
descriptions, tool schemas, and results are untrusted input. This ADR
records the owned surface: `packages/ecosystem/mcp_host.py`, its tests,
and this note. `__init__.py` lead exports, `skills.py`, shared core,
services, and CI are untouched.

## Decision

`MCPConfig` (pydantic, `extra="forbid"`) carries `id`, `transport`
(`stdio`|`http`), `command`/`args` or `url`, `enabled=False`,
`allowed_tools=[]`, `env_secret_refs`/`header_secret_refs` (name to
resolver ref, never values), `allow_remote=False`, `timeout_seconds=10`
(1..120), and `max_result_chars=16000`. `MCPHost(configs, policy,
secret_resolver=None)` exposes `list_servers()` (metadata, no secrets),
`async discover(id)` (bounded metadata; grants nothing),
`async call(id, tool, args)` (exact-allowlist gated), `async close()`,
and owner-only `replace_configs(actor="owner", ...)`.

Authority: disabled servers and non-allowlisted tools raise `PolicyDenied`
before any connection or byte is sent. Tool names, descriptions, and
`readOnly` annotations never grant anything. Argument schemas are
validated host-side with `jsonschema` (already a dependency). Results are
plain JSON dicts marked `origin="mcp"`, `untrusted=true`; server `isError`
raises instead of reading as success.

Isolation: stdio needs the policy shell grant and runs through the
existing `services.agent.tools.build_bwrap_args(policy,
shlex.join([command, *args]), policy.network)` tuple (no fallback;
missing bwrap fails closed via `ShellUnavailable`). Typed argv is
preserved through `shlex.join`; the workspace appears at `/workspace`,
ambient env is cleared (`HOME=/tmp`), and explicit secret refs arrive only
as `--setenv` pairs for that server. Network isolation stays on unless
the policy network grant is set. HTTP needs the network grant plus
per-server `allow_remote`, HTTPS outside loopback, no userinfo, no
secret-bearing query keys or fragments, and `httpx` default
no-redirect behavior; headers come only from secret refs.

Robustness: per-server locks serialize sessions; timeouts and crashes
raise `MCPTransportError` without crashing the host; a broken session is
dropped and only read-only listing reconnects once; calls never
auto-replay side effects. Metadata, schemas, arguments, and results are
size-bounded; oversize results fail clearly rather than truncating into a
false success. Errors and metadata are scrubbed of secret values, query
strings, and userinfo.

## Verification and limits

MCP SDK 1.30.0, PyYAML 6.0.3 (root `.venv`). Transport verified: stdio,
via a stdlib-only synthetic JSON-RPC server over real bubblewrap.
Transport not run live: Streamable HTTP (no external servers; validation,
grant, redaction, and refused-connection paths tested only). No paid
inference, no external network. `ruff check`, `ruff format --check`,
and `pytest tests/integration/test_ecosystem_mcp.py -q` are the gates.

Not claimed: OAuth flows, sampling/elicitation/roots, resources/prompts,
list-change handling, cancellation acknowledgement, full plugin
compatibility, or production hardening. Those remain M4A/future work.
