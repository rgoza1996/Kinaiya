# 0067: Canonical service roots at client boundaries

## Requirement and decision

[#151](https://github.com/rgoza1996/Kinaiya/issues/151) documents valid owner
authentication failing because a root URL ending in `/` produced
`//v1/sessions`. One shared `normalizeServiceRoot` now runs before request
construction in the connection profile, primary HTTP client, workspace,
Decisions, both memory services, companion and events client.

Trim outer whitespace and one optional trailing slash. Preserve the accepted
scheme/authority spelling and safe reverse-proxy path prefix. The same
canonical value identifies a connection, so equivalent slash spellings do not
discard drafts. WebSocket conversion retains the prefix when changing HTTP(S)
to WS(S).

Reject URL credentials (including an empty user-info delimiter), queries,
fragments, internal whitespace/control characters, backslashes, double path
slashes, traversal/dot segments and encoded separators or double escaping.
Require HTTP(S), a host and ports 1–65535. Port zero is rejected because Dart
can drop it while converting to WS(S), selecting a different destination from
HTTP. Errors do not echo the input. Invalid form text stays available for
explicit validation rather than silently selecting another root.

Syntax normalization grants no transport or disclosure permission. Owner setup
still requires HTTPS for remote hosts and allows HTTP on loopback. Raw transport
clients retain their existing transport-policy responsibilities. Owner tokens,
headers, first-frame event authentication, redirect refusal, payloads,
idempotency and provider/disclosure selection retain their contracts.

## Verification

`test/service_root_test.dart` covers all three primary services, bare/slash/outer
whitespace forms, safe prefixes, invalid inputs, unchanged connection draft
identity, GET/POST/multipart/SSE targets and auth, auxiliary APIs, and the real
setup dialog's authenticated checks. Invalid roots fail before any request or
connection-factory invocation. Existing memory fixtures now use a syntactically
valid synthetic unused origin rather than the literal `unused`.

`test/events_service_root_test.dart` uses an ephemeral loopback HTTP/WebSocket
server for four real upgrades with/without a prefix and slash. It checks the
exact upgrade path, authentication only in the first frame, session filtering,
cursor filtering, duplicate suppression and ordered literal replay. Sockets,
clients, subscriptions and the server are closed in `finally`; waits are
bounded. It contacts no Kinaiya service or inference provider.

See the [status ledger](../implementation-status.md#client-reliability) and the
associated PR for source-bound local checks, independent review and CI/build
results. These regressions establish URL/request consistency, not availability
or correctness of every deployment or provider.
