# ADR 0065: Agent harness and session sidebars

Implements the Flutter portion of #157 using the owner
[workspace API](../agent-workspace-api.md). The supplied Agent design has two
independently collapsible sidebars between global navigation and chat: Harnesses
and the selected harness's Workspace. Narrow or enlarged-text layouts open each
sidebar in a separate sheet. Task details remain optional. Literal metadata is
rendered as text; the design's illustrative harnesses and sessions are not data.

The protected Kinaiya Core is described separately from registered
`context_selector.v1` extensions. These bounded workspace selectors do not
replace the Agent runtime, model, permissions or protected core. An unconfigured
registry's `seed_runtime` catalog entry comes from the native host, never from a
client-created fallback. Native `selectable` is the creation gate; active,
previously owner-promoted, approved-baseline, unapproved and unavailable entries
remain inspectable without promotion or inference. Legacy null session pins
appear under Unassociated historical sessions. Missing historical harnesses are
explicitly unavailable rather than assigned the current active version.

`SessionInfo.harnessVersion` and `AgentTask.harnessVersion` retain native pins.
The owner client reads bounded catalog/session pages and creates sessions only
through the native eligibility endpoint. Session creation has no automatic retry.
An unconfirmed result retains a RAM latch across same-backend reconnects; the
owner must refresh and inspect sessions before explicitly allowing another
creation. A different backend clears the previous backend's workspace state.

Catalog, session-page, identity and journal reads use independent request-owned
HTTP clients, separate from task and voice transports. Three retained admission
slots, a 1 MiB streamed response ceiling, a five-second request deadline and a
300 ms cleanup deadline bound each reader. A logical timeout closes only its
owned client and does not release its slot until the actual read and response
cancellation settle. Redirects and non-JSON/compressed responses refuse. Explicit
test/shared HTTP injection requires a separate workspace client factory; missing
injection reports unavailable rather than contacting a real host.

AppState owns the selected harness/session, page cursors and up to 100 RAM session
snapshots. Drafts retain their original disclosure scope per exact
session/thread/epoch. Selection re-reads the native Session and restores literal
Agent submit, attributable tool, and outcome records through bounded journal
pages, accepting only `agent-supervisor` provenance. Tool records require one
task reference matching their payload. Each page rechecks native scope before
publication. Consecutive records for one task and revision form a collapsible
activity group; shell summaries and output are read-only text, with bounded
display and recorded result status. The summary can contain only a shortened
command. A navigation rail targets loaded recorded user/Agent event IDs; it
does not offer markers for unloaded history. The client polls for new records
while Agent Chat is open once the current pages are loaded. Tool start/progress
and failure records are not guaranteed, so this is not a complete live trace.
Same-connection catalog refreshes share one in-flight read, installed before any
synchronous notification; connection invalidation immediately permits a new read.
Chat records authentication transitions before callbacks and skips duplicate
first-frame discovery. A completed or failed refresh still permits manual retry.
Connection, selection and request generations fence late completions. Reset
epochs do not reuse older drafts or project older transcript messages into the
current chat. Read failures remain visible and never ask a model to reconstruct
history. `/new` and `/resume` select the returned native identity through the
same verification path.
The composer waits for a selected native identity: catalog loading, an unsupported
host or a pending selection never chooses an arbitrary legacy session for a draft
or slash command. Existing selected legacy sessions retain their native null pin.
While identity is pending, the composer is disabled and queued submissions refuse;
a draft revision also prevents a late cached restore from replacing newer input.

Workspace lists actual sessions, including empty ones. Chat task cards are scoped
to the selected session/thread/epoch, while pending approvals remain globally
reachable with their existing task/revision-bound controls. Task inspection,
sidebar collapse, filtering and selection never grant execution authority.
Commands, MCP, steering, cancellation and independent Converse state retain their
existing owners. Already-sent work cannot be recalled by changing selection; its
late receipt cannot clear a different session's draft or append to its history.

Deterministic synthetic checks cover native DTO/endpoints, eligibility,
unassociated history, paging, per-session drafts, delayed selection/journal
responses, reset/connection fences, uncertain creation, responsive sidebar
controls and retained global approvals. Native service integration is verified
in the companion backend workstream. Browser/native builds establish compilation,
not model quality, physical audio or mobile-call acceptance. The sidebar widget
source was partially delivered through the actual visible Muse Contributor CLI;
that run reached its step limit without tests. Codex owns the responsive/lint
repairs, all tests, integration, state/client work and final verification.

Recorded activity projection and paging are exercised in
`apps/kinaiya_flutter/test/agent_workspace_state_test.dart`; scope-fenced polling
is exercised in `agent_workspace_integration_test.dart`; disclosure, shell text
and navigation through variable-height messages are exercised in
`ui_consistency_test.dart`. These checks use synthetic data and do not establish
that the native journal records every tool start, result, or failure.
