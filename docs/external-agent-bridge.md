# External Agent bridge v1

The bridge contract, Session attachment grants, and the Pi SDK peer support
managed attachment, scoped transcript reads, Decisions delivery from Converse
typed turns and Rolling Windows, and durable replay of observed outcomes.
Queued delivery, targeted controls, and Adaptive Live remain in scope. [External Agent
bridge](https://github.com/rgoza1996/Kinaiya/issues/547)

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
tool's read. A managed attachment applies the selected runtime and network
scope when it starts the peer.

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
An invalid frame or lost process leaves in-flight effects unconfirmed.
The managed service persists validated acknowledgments and events before it
exposes them to callers.

## Managed Converse-only profile

Select the Converse + Decisions subsystem graph and configure the independently
installed Pi SDK in the host configuration. A dedicated Pi configuration
directory holds the selected backend's settings, extensions, and credentials.
The workspace is the location the owner grants to this peer.

```json
{
  "external_agent": {
    "profile_id": "pi-workspace",
    "node": "/path/to/node",
    "sdk_root": "/path/to/pi-sdk-prefix",
    "agent_dir": "/path/to/pi-configuration",
    "workspace": "/path/to/agent-workspace",
    "allow_network": false
  }
}
```

The owner authorizes an attachment in Session and then sends its ID to
`POST /v1/decisions/external-agent/attach` with operation `attach`.
Decisions rechecks the current grant and profile, launches Pi with pinned
mounts in a Bubblewrap namespace, and binds the backend identity observed
from the SDK. Network-sharing profiles currently require a public-synthetic
grant. The separate writable mounts contain the selected Pi profile,
workspace and per-attachment Pi session storage. The peer gets a cleared
environment and Session credentials stay in Decisions.

The `status`, `events`, and `disconnect` routes under the same prefix
require owner credentials and the exact bound target. A detach keeps the
owned peer available to reconnect. A shutdown receipt reports a stopped
peer once its process exit is observed. The SDK persists a session header
at attach time, so reopening an empty session retains its original identity.
Bubblewrap holds the per-attachment lock for the sandbox's lifetime. Decisions
keeps its copy through process exit and the final observation drain.

The finalized and Rolling Windows Decisions workers route eligible fresh
sources to the active attachment in the same session, thread and epoch. An
attachment takes a native journal cutoff at activation. Entries that predate
the activation stay outside automatic delivery. The worker sends the original
recorded decision ID, the native source and revision, and the exact routed
text. Windows retain their original conversation timing labels.

Authenticated Decisions code can also deliver a recorded claim through
`POST /v1/decisions/external-agent/submit`. The managed peer checks observed
occupancy before reserving a send. When busy, Decisions asks Session
`POST /v1/external-agent/claim/retained` to recover an exact original claim.
Session returns an empty response when the claim is absent. A different
command remains unapplied while the peer is busy. Queued delivery and task
controls remain open.

The journal receipts identify Pi as the executor and carry observed external
task identity separately from native Agent tasks. The managed service stores
observations in an owner-only database outside the Pi filesystem mounts.
After Decisions or Pi restarts, the owner can read retained task observations
and output using the same attachment and a currently valid Session grant.
The cursor continues across peer generations. Replay retains up to 512 events
and 16 MiB of encoded event data per attachment. Older cursors return
`replay_expired`. An incomplete run becomes `uncertain` if the prior
generation ends without a terminal observation.

## Original submission

Decisions uses the native `POST /v1/external-agent/claim` route to reserve one
send for a finalized Decisions queue action or a rolling-window queue action.
Session verifies the recorded decision, source identity, current revision,
instruction and disclosure scope. A corrected finalized turn keeps the
original source ID and names the exact correction as the decision's cause.
A recorded prior dispatch or another grant for the same source blocks a new
send. For Rolling Windows, Session also checks the current utterance for
changed text and an earlier claim for the same spoken instruction. These
checks and the claim write share one native transaction.

The first committed claim assigns an admission ID. The adapter uses it as the
Pi run identity and sends the instruction only when the reply says
`original`. A duplicate claim returns `retained` with the same admission
ID, including after Session restarts. The adapter can recover an originally
observed acknowledgment from the managed store after reopening the peer.
An original with a missing acknowledgment remains uncertain. The receipt
describes command acceptance and completed work remains an observation
from the backend.

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
paging. A real Pi replay rehearsal sends a recorded Decisions instruction
once, refuses another send after a lost native acknowledgment, and reopens
the same Pi session without starting the original again. The Converse-only
launcher rehearsal attaches and reopens a real managed peer and recovers an
accepted submission while the same Pi task is running. A separate loopback
rehearsal sends a typed turn and a synthetic audio window through the Converse
HTTP endpoints, Decisions workers and Session to the real Pi SDK. Both
paths verify the original instruction, assistant output and observed settled
state. The audio replay preserves the original transcription and receipt.
The restart rehearsals kill the managed Pi peer and the Decisions service
process in separate cases. They verify retained output and cursors,
unchanged acceptance identity, and uncertainty for a run whose terminal
event was never observed. Sandbox custody is checked with a competing lock
acquisition while the real Pi peer is live.
