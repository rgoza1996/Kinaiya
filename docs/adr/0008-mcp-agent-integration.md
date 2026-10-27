# ADR 0008: Agent MCP broker, disclosure scope and completion receipts

Status: reviewed experimental integration for #3; resolves the targeted lifecycle/security defects in #23 and #25. Full M1A and complete MCP protocol support remain open.

Agent optionally receives an MCP host from owner-authored `mcp_servers` configuration. Without it, capabilities report no MCP tools. Agent-only `mcp_list` returns valid bounded JSON pages; selecting a named tool loads its schema. Oversize schemas require an adapter instead of entering context through silent truncation. Catalogs and schemas load on demand rather than entering every prompt. `mcp_call` requires an enabled exact owner allowlist entry, the transport grant and a digest/revision/expiry-bound one-use approval. YOLO skips the per-action approval and preserves the other gates. The broker recomputes the digest from actual dispatched arguments.

Each server declares `content_scope`: `local_only` by default, `public_synthetic`, or `private_cloud_authorized`. Model-facing discovery, schemas and results enforce the actual provider at dispatch and before returning data. Contributor accepts only public/synthetic servers without secret references. The current Meta adapter always enables Contributor mode, so aliases receive the same restriction. Other cloud providers need an explicitly permitted content scope. Owner metadata remains available separately. Literal secret redaction is defense in depth, never proof of confidentiality.

Stdio requires the policy shell grant; HTTP additionally requires network permission and remote opt-in. Reusing a session cannot preserve revoked grants. `/yolo` changes retire MCP transports before acknowledgment. Calls are not replayed after timeout, cancellation or recovery. Model-only tool approval cannot change server configuration or disclosure scope. Secrets resolve only from `env:NAME`, never arbitrary files or the Muse profile. MCP-dependent imports stay lazy in the launcher.

MCP response receipts have `Evidence.kind=external` and a broker-authored payload hash. They prove response arrival and do not verify external effects. External receipts or metadata alone cannot support a verified completion. An independent native artifact/read/command receipt is required. Converse and Flutter apply the same distinction and current-revision check. This does not solve semantic verification for arbitrary external actions; service-specific reconciliation remains necessary.

The Meta Responses adapter uses strict wire fields `server_id`, `mcp_tool` and JSON-encoded `mcp_arguments_json`, then normalizes them into the provider-independent tool action. Its instructions and assistant history use consistent wire serialization. The first real run exposed missing wire fields; a second reached discovery but failed to produce a valid finish. Neither performed the MCP write or announced success. The corrected run used four proposed actions: schema discovery, one approved MCP write, native read and finish. File `mcp-live.txt` contained exactly `public synthetic MCP proof`, SHA-256 `3f45235c6453d75d78f54f00ebeb53fbdac119603bccc84ea91cc43b2d475c59`. Both failures and success, including candidate source hashes, are retained in [the evidence record](../../evaluation/evidence/mcp-2026-10-27.json). Inputs were public/synthetic and the server received no credentials.

## Configuration example

```json
{"mcp_servers":[{"id":"public-example","transport":"stdio","command":"/usr/bin/python3","args":["/workspace/public_server.py"],"enabled":true,"allowed_tools":["lookup"],"content_scope":"public_synthetic"}]}
```

The example needs an actual owner-installed server and shell grant; it does not install or execute dependencies during inspection. Remote endpoints, credentials and private scope require separate owner configuration. `GET /v1/mcp/servers` is owner-only. Dynamic setup and `/mcp` management UI remain unfinished; configuration currently requires the private host file and service restart.

## Evidence and limits

Local checks: full offline Python suite; real stdio and loopback HTTP host tests; real Agent/MCP approval, read/hash, failure, cancellation and recovery tests; model-disclosure and pagination tests; Meta strict-wire fixtures; Ruff; schema export; Flutter analysis and model tests. Exact CI counts/results belong to the PR. The live case uses real Contributor inference, not the scripted model fixture used in deterministic tests.

The optional MCP actions and external receipt kind extend the experimental 1.0 contract; upgrade host and client together. Compatibility with older strict clients is not claimed. The released alpha.1 remains the previous verified baseline. OAuth, resources/prompts, list-change notifications, arbitrary executable plugins, full ecosystem conformance, aggregate cgroup scheduling and production hardening remain unimplemented or unverified. No foreign server claim becomes owner authority or independently verified remote completion.
