# External Agent bridge v1

The transport-neutral contract and the Pi SDK peer are the first implementation
for [issue #547](https://github.com/rgoza1996/Kinaiya/issues/547). The application
integration still requires an attachment owner, authenticated Decisions
delivery, on-demand transcript access, and an end-to-end Converse rehearsal.

## Commands and observations

The exported request and response schemas use contract version 1. A connect
request names an owner-selected backend profile and a Kinaiya session, thread,
and epoch. Its status response binds an attachment to the backend's observed
session ID and advertises support for each control and read operation.

| Operation | Contract |
| --- | --- |
| Connect and status | Observe the same backend session and bounded task state. An optional expected session ID fences reconnect. |
| Submit | Carry the original source event references, revision, disclosure scope, backend-owned execution policy and idempotency key. Queueing is explicit when the selected capability supports it. |
| Steer | Target a task and its expected current revision. A backend without a supported task-bound path can refuse the operation. |
| Interrupt | Acknowledgment records the request. A later backend observation establishes whether the attached run stopped. |
| Events | Continue from the last observed cursor. Discontinuous replay returns an explicit refusal. Assistant text and tool activity have separate typed events. |
| Read transcript | Read the authorized conversation scope on demand. The first page captures an upper sequence. Subsequent pages continue through it, including large entries via UTF-8 byte offsets. |
| Disconnect | Detach a client or request shutdown of a peer the bridge owns. Observe process exit before reporting a confirmed shutdown. |

Decisions selects actionable source events. The attachment owner must recheck
native source provenance and current scope before effects. Pi owns its model,
tools and execution policy. The transcript capability must bind reads to the
attachment's current session, thread, epoch and authorized disclosure scope.
The bridge passes the task instruction and lets Pi request authorized transcript
pages as needed.

## Pi SDK peer

The adapter uses the public SDK from an independently installed Pi Coding Agent
1.5.2 package. Direct Pi RPC prompts can dispatch slash commands and expand
templates. The SDK supports forwarding literal text through
`AgentSession.prompt()` with `expandPromptTemplates: false`, while Pi's
configured input handlers still run. [Pi SDK](https://github.com/earendil-works/pi/blob/v1.5.2/packages/coding-agent/docs/sdk.md) ·
[Pi RPC commands](https://github.com/earendil-works/pi/blob/v1.5.2/packages/coding-agent/docs/rpc-commands.md)

The private JSONL peer uses an explicit installation, workspace, Pi configuration
and session directory. It observes the exact saved session or owner model
selection after Pi loads its extensions. If that selection is unavailable, it
refuses startup. The owner settings retain the saved selection. [Extension
registration startup evidence](https://github.com/rgoza1996/Kinaiya/issues/721)

The peer publishes preflight acceptance and observes state, output, tool
activity, and Pi's `agent_settled` event. That event reports that the SDK run
settled. An interrupt is tied to the active request's run identity, and
`agent_settled.aborted` provides the stop observation. [Pi event contract](https://github.com/earendil-works/pi/blob/v1.5.2/packages/coding-agent/docs/json.md)

The Python reader retains original request identities across caller timeouts.
An exact request lookup can recover a late acknowledgment without another write.
An invalid frame or lost process leaves in-flight effects unconfirmed. Requests
are retained within the lifetime of the original peer and replay has a bounded
window. The application owner needs durable attachment records and original
request receipts to reconcile after an owner restart.

## Verification

The isolated integration uses Node 22.22.1 and a private bubblewrap filesystem
and network namespace, with a Pi extension that supplies deterministic model
responses. The tests use the real SDK and its session files. The SDK prefix can
be prepared from the official release's install package and lockfile with
`npm ci --ignore-scripts --no-audit --no-fund`. The release publishes SHA-256
digests for both files. [Pi 1.5.2 release](https://github.com/earendil-works/pi/releases/tag/v1.5.2)

```sh
KINAIYA_PI_TEST_SDK_ROOT=/path/to/pi-sdk-prefix \
  python -m pytest -q tests/integration/test_pi_sdk_peer.py
```

The suite checks verbatim slash-prefixed text, duplicate delivery, persisted
session reopen, exact extension model selection, targeted interruption, and
backend failure. The transport tests cover lost acknowledgment, identity
mismatch and an unbound event. The full Converse and transcript integration
are still open in #547.
