# External Agent bridge v1

The bridge contract, Session attachment grants, and the Pi SDK peer implement
the connection and transcript boundaries for
[issue #547](https://github.com/rgoza1996/Kinaiya/issues/547). The application
integration still requires managed peer startup, Decisions task delivery,
durable command recovery, and an end-to-end Converse rehearsal.

## Commands and observations

The exported request and response schemas use contract version 1. A connect
request names an owner-selected backend profile and a Kinaiya session, thread,
and epoch. Its status response binds an attachment to the backend's observed
session ID and advertises support for each control and read operation.

| Operation | Contract |
| --- | --- |
| Connect and status | Name the owned profile, allowed content scopes and current Converse identity. Observe the same backend session and bounded task state. An optional expected session ID fences reconnect. |
| Submit | Carry the original source event references, revision, disclosure scope, backend-owned execution policy and idempotency key. Queueing is explicit when the selected capability supports it. |
| Steer | Target a task and its expected current revision. A backend without a supported task-bound path can refuse the operation. |
| Interrupt | Acknowledgment records the request. A later backend observation establishes whether the attached run stopped. |
| Events | Continue from the last observed cursor. Discontinuous replay returns an explicit refusal. Assistant text and tool activity have separate typed events. |
| Read transcript | Read the authorized conversation scope on demand. The first page captures an upper sequence. Subsequent pages continue through it, including large entries via UTF-8 byte offsets. |
| Disconnect | Detach a client or request shutdown of a peer the bridge owns. Observe process exit before reporting a confirmed shutdown. |

Decisions selects actionable source events. The application attachment owner
rechecks native source provenance and current scope before effects. Pi owns its
model, tools and execution policy.

## Session grants and transcript reads

Under `/v1/external-agent`, the owner authorizes a grant through
`POST /authorize` and can later revoke it through `POST /revoke`. Session
retains the exact authorize request in its journal database. A replay of that
request observes the same grant, and a different request under the original ID
conflicts. A revocation is durable across restarts.

Decisions uses `POST /bind` to report the observed peer session ID. Session
accepts a single original backend identity and preserves it after revocation.
That identity remains tied to its original Converse scope. The current session,
thread and epoch fence later bind and read requests. Resetting or deleting the
session invalidates its earlier scope.

Decisions calls `POST /transcript` on demand using the bound target. The read
snapshot contains eligible user turns, speech windows, corrections, and
Converse replies in journal order. Session checks each entry's native origin
and folds the disclosure scopes of its recorded causes and revisions. It
returns text only for scopes allowed by the owner grant. A change in that scope
fences a partially read entry. Pages carry raw text and UTF-8 offsets so the
consumer can recover long entries and resume through the original snapshot.

The opt-in Pi tool `kinaiya_read_transcript` asks for a page through the
peer's private pipe during the current run. The Python reader checks the
original run and backend session before returning the page. Session
credentials stay with the Decisions client. A regular instruction contains
the routed text and the peer sends transcript text only in response to the
tool's read. The application still needs to match its actual backend runtime
and network scope to the owner grant before attaching.

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
backend failure. It also verifies that the real Pi SDK reads every authorized
page across the authenticated Session routes and receives a refusal after
revocation. The ordinary turn makes zero transcript reads. Focused Session
tests cover concurrent binding, restart, reset, revisions and byte-accurate
paging. Full Converse routing and the production peer launcher remain open
in #547.
