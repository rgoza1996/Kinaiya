# ADR 0001: Initial service contracts

Accepted for the initial implementation, against Roadmap.md 0.2.1. The roadmap is unchanged.

Python 3.11+ host services use `packages/contracts/models.py` (schema 1.0), generated
JSON Schema, and HTTP. SQLite has a single writer in the session service. A monotonic
sequence orders the shared journal. Service-specific bearer tokens assign provenance.
Only the owner may create sessions, reset epochs, resolve approvals or change policy.
The browser uses the owner credential in memory, never a URL or character pack.

`Journal` is the async interface in `interfaces.py`. `append(AppendRequest)` returns an
authenticated JournalEntry. `read(after, limit, session_id, thread_id, kinds, task_id,
search)` is bounded. `session(id)` returns the current epoch. `LocalJournal` wraps the
SQLite store for tests; `JournalClient` calls its authenticated API in production.

`TextProvider.complete(messages)` returns text; `stream(messages)` yields stable text
deltas. Providers expose `ProviderInfo`. Fixture providers are explicitly labeled.
Private source, conversations and credentials must never enter Contributor requests.

Agent implementation interface: `AgentRuntime(journal, provider, workspace, policy,
max_steps=12)`, async `submit(TaskRequest, actor='owner') -> Task`,
`steer(task_id, SteerRequest, actor='owner') -> Task`,
`interrupt(task_id, InterruptRequest, actor='owner') -> Task`,
`status(task_id=None) -> Task | list[Task]`, `wait(task_id, timeout=30) -> Task`,
and `close()`. `ToolPolicy` is owned by `core.policy`; it scopes filesystem writes,
shell/network grants and approvals. `create_app(runtime, auth)` exposes the API.
Only the supervisor emits lifecycle/evidence; a model's `finish` is a proposal.
Model tool actions use `ToolAction`. Tool outputs are untrusted observations.

HTTP routes begin `/v1`: `/sessions`, `/transcript/entries`, `/transcript`,
`/events` (WebSocket), `/agent/tasks`, `/agent/tasks/{id}/steer`,
`/agent/tasks/{id}/interrupt`, `/agents/capabilities`, `/approvals/{id}/resolve`,
`/converse/turns`, `/converse/interrupt`, `/converse/delivery`, `/commands`.
All control endpoints authenticate, even on loopback. Unknown capabilities are not
advertised. Origins default to none; explicitly permitted origins are configurable.

Decisions polls durable conversation entries; every disposition is journaled with its
source IDs. Direct Agent Chat bypasses this consumer. Agent-off entries are marked
without dispatch and never replayed as surprise work. Assistant text cannot authorize
tools. A generation epoch independently invalidates speech; cancelling speech does
not cancel an Agent task. Outcome notices recheck task revision before playback.

The initial seed loop uses typed JSON tool actions. A bounded provider request and
host-side verification run outside model control. This is a minimal working baseline,
not proof of general task success, production security, or research improvement.
