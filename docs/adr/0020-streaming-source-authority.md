# ADR 0020 — Unfinished speech and owner action approval

Status: accepted for the experimental runtime. Requirement: Roadmap AT11;
[issue #51](https://github.com/rgoza1996/Kinaiya/issues/51).

## Defect and boundary

A retained, authenticated speech window proves what the recognizer reported,
not that the user finished giving the instruction. The released window runtime
checked source revision and epoch before execution, but a file write could
finish before a later recognition correction existed. A deterministic native
reproduction overwrote an existing file with `allow_write=true` and requested
zero approvals. Broad permissions alone therefore did not satisfy AT11.

Source authority is now checked separately from tool permissions. A task
carrying any authenticated `conversation.window` with `capture_ended=false`
may plan, read, retrieve context, and use isolated computation, but `write_file`,
`shell`, and every `mcp_call` require the existing owner action approval. MCP
calls are conservatively treated as effectful because third-party tool metadata
cannot establish that a call has no side effects. This check also applies with
broad write/shell grants and YOLO enabled.

The native supervisor re-resolves the task's speech sources before requesting
approval and before dispatch. It fails closed if source authority is unavailable.
The authority read checks the bound source revision too: a correction to a
completed capture between two asynchronous reads cannot upgrade the old task.
The action remains bound to its exact tool, arguments, objective revision, expiry,
and one-use approval. With broad file/shell grants, the supervisor consumes that
same approval because the ordinary broker would skip it; otherwise the existing
broker consumes it. MCP consumes a supplied approval even in YOLO. No new grant
or derived execution permit is minted. Workspace restrictions, protected roots,
OS isolation, transport grants, MCP enablement/allowlists and disclosure checks
still apply.

A steer accepted while asynchronous source validation is in flight discards
the old action and replans at the safe boundary. It cannot be erased by clearing
an approval-wait signal. Approval resolution rejects stale source revisions or
session epochs before recording intent. A recognition correction without a
steer does not authorize a revised action; a fresh authenticated instruction is
required. Once a broker has admitted a side effect, later steering cannot undo
it; uncertain outcomes retain the existing no-blind-replay rules.

## What constitutes complete input

`user_speaking=false` is only an acoustic opening. Silence, classifier confidence,
and a model's interpretation do not commit an instruction. A native-authenticated
window explicitly marked `capture_ended=true` is a completed capture; finalized
user messages and direct owner tasks keep normal tool-grant behavior.

Current live listening emits uncommitted windows and therefore requires owner
review for these effects. A later completed window does not automatically upgrade
an existing task's original partial source. This conservative behavior closes the
demonstrated AT11 effect without claiming autonomous semantic commitment is solved.
It does not delay ordinary Converse replies or require a model to approve tools.

## Owner visibility and compatibility

`GET /v1/approvals` is owner-only and returns schema-versioned native pending
requests across all task sessions, including Converse sessions. Pages contain
at most 100 requests and use an exclusive `after` task-ID cursor. This reads
current supervisor state rather than the most recent 100 transcript events.
Resolved/used/expired requests, terminal tasks, pending steering, stale sources,
and stale session epochs are omitted. Restarted uncertain tasks remain paused
and do not restore actionable approval permits or replay effects.

Agent Chat loads these pages immediately and on its normal task poll. It checks
task revision, session/thread/epoch, expiry and connection generation, displays
why live speech requires review, and sends the exact digest/revision resolution.
Approvals scroll with the task list. A 404 alone enables the old journal-based
compatibility path; authorization or transport failures do not use that fallback.
The response is advisory: native checks remain authoritative at execution.

## Verification and limits

The Muse Contributor test worker produced eight behavioral regressions against
the real journal, Agent runtime, policy and temporary files: five failed and
three passed on the released base. Its CLI runs reached their step caps, so
there is no completed worker review verdict. The lead reviewed and integrated
the file, tightened its reason/exception assertions, and made its steering test
wait for the documented applied revision.

A subsequent bounded Muse Contributor review reported no actionable findings
in the native diff it inspected. It did not independently re-read the existing
brokers or run tests; the lead checked those broker implementations directly.
After that review, a lead-authored asynchronous correction test reproduced a
remaining write-before-revision-check race. The lead added the source-revision
check described above and retained the failing-then-passing regression. Neither
automated review is represented as independent human approval.

Additional lead integration tests cover one-use exact-action approvals with and
without broad grants/YOLO, denial, expiry, real same-slot correction, epoch reset,
cancellation, steering during asynchronous validation, actual isolated shell and
stdio MCP execution, and an owner-only paged view of 103 pending tasks across
Agent/Converse sessions. Flutter tests cover cross-session visibility without an
Agent session, pagination and filtering, exact approval resolution, reconnect
isolation, schema/origin checks, and 404-only legacy fallback. Inference and
speech text in these regressions are explicitly synthetic; they are not an
acoustic STT benchmark or a new real-provider voice run.

The broader voice performance and human microphone/echo gates remain open in
[issue #49](https://github.com/rgoza1996/Kinaiya/issues/49). This change does not
establish completion of the full roadmap.
