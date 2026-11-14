# Native task media enrollment

`services/agent/task_media_factory.py` is optional owner wiring for one new
voice task. It does not start providers or implement continuous video intake.
With no `AgentRuntime.task_preworker_hook` and no native harness factory,
submission behavior and tool availability retain their existing defaults.

## Owner setup and order

1. Import the explicitly authorized MP4 into the scoped Agent workspace and
   retain its exact SHA256 and byte count. Do not relabel a private source as
   public synthetic content.
2. In the isolated owner driver, intercept the actual native audio-clip POST
   before forwarding it. Write an owner plan with the actual session, thread,
   epoch, utterance ID, and SHA256 of the complete WAV decoded from the
   request's `audio_base64`. This is neither the JSON body digest nor the raw
   PCM digest. The synthetic intent window is `public_synthetic`.
3. Atomically replace the plan file as a regular mode `0600` file in an owner
   mode `0700` directory **outside** the Agent workspace. No symlinks or hard
   link aliases are admitted. Generated code cannot write this plan.
4. Inject `TaskMediaFactory(plan_path=..., provider=native_adapter)` as
   `runtime.task_harness_broker_factory` and assign its `enroll` bound method
   to `runtime.task_preworker_hook`. The adapter receives only prepared exact
   source grants and retains credential and physical transport ownership.
5. Forward the clip through ordinary Converse/Decisions. After native submit
   confirmation, the retained preworker hook validates the immutable voice
   window, matching native Decisions queue route, exact submit receipt, fresh
   session, exact owner-selected primary Meta model/reasoning pin, and actual workspace video.
   It binds only one task; it does not accept model-provided permissions.

The owner file DTO is `TaskMediaOwnerPlan`, schema `task_media_owner.v1`:

| Fields | Binding |
| --- | --- |
| `plan_id` | Owner grant identity |
| `session_id`, `thread_id`, `epoch` | Exact designated fresh session |
| `utterance_id`, `audio_sha256` | Exact completed native voice clip |
| `source_path`, `source_sha256`, `source_bytes` | Exact relative workspace MP4 |
| `content_scope` | Source disclosure scope |
| `targets` | Unique native operation/provider/model/reasoning/trusted prompt |
| `primary_result_target` | Exact provider/model/local for derived result export |
| `required_operations` | Native required subset of target operations |

ASL owner configuration includes both `sam_hands` (`meta`, `sam-3.1`, no
reasoning, exact prompt `hands`) and `transcribe_video` (`meta`,
`muse-spark-1.3-contributor`, `xhigh`, owner transcription prompt), with both
operations required. The primary result target is exact Meta Contributor,
`local: false`. Owner text supplies the transcription prompt; the generated
harness cannot supply or extend it.

### Optional Wonder Standard target

The default remains Contributor xhigh. A separately authored owner plan may
select `muse-wonder-1.0`, `max` for both the primary result target and video
transcription target. The factory requires that exact primary pin; a plan for
Contributor cannot authorize Wonder exports, and a mixed primary/transcription
model pair is refused. `muse-wonder-full-1.0`, aliases, and Contributor `max` are
not supported by this option. Existing source/task/revision/publication checks
and the private disclosure floor remain in force.

The optional budget gateway flag is `--enable-wonder-standard`. It adds no
allowance: the retained ledger, combined ceiling, unresolved reservations, and
default **$1.50 media sublimit** remain unchanged. Its exact-video manifest must
contain `sam-3.1` and `muse-wonder-1.0` prompt digests for the exact MP4 SHA256,
byte count and independently measured frame count. Loading a Wonder manifest
without the flag refuses. Standard requests omit the Contributor-mode header.

The gateway admits only stateless, buffered Wonder `max` requests with local
function tools, at most **32,768 input tokens and 32,768 output tokens**. A native
primary profile can declare `context_tokens: 65536`, `max_tokens: 32768`; output
includes reasoning. This is an explicit run bound. It does not claim that the
model's full context is affordable. The existing native history selection and
measured-request identity checks still apply; no suffix clipping or unchecked
tool-schema reduction is introduced.

Before each generation, the gateway requires a completed exact-model token
count for the same canonical complete body and authorization. This bounded,
process-local receipt expires after 60 seconds and is consumed once before
budget admission. A changed body, authorization, unknown count, over-bound
count, restart, or used receipt refuses generation. Native Wonder video counts
its exact trusted prompt plus entire granted MP4 and closes that original
stream before opening the generation stream. Counts never generate output.

[Meta's pricing](https://dev.meta.ai/docs/pricing-rate-limits) lists Wonder
Standard at $4 input, $0.20 cached input, and $20 output per million tokens.
There is no API Contributor variant. The gateway reserves twice the uncached
input/output bounds: a 32,768/32,768 request reserves **$1.572864**, while the old
1,007,997-input/32,768-output bounds would require **$9.374696**. A verified,
settled response can use the separately reported cache count; future admission
never assumes a cache hit. Model identity, finite bounded usage, EOF and the
original upstream close must all succeed before settlement or delivery. An
uncertain response retains the full reservation and never retries.

The [official model catalog](https://dev.meta.ai/docs/models) identifies
`muse-wonder-1.0` as Standard API; Wonder Full is subscription-only in Muse Code.
The [reasoning guide](https://dev.meta.ai/docs/reasoning) supports `max` for
Wonder Standard and states that reasoning consumes the output budget. The
[counting guide](https://dev.meta.ai/docs/token-counting) requires recounting
when changing models; Contributor usage is not a Wonder token-count receipt.
Enabling this source option does not prove a provider connection, successful
transcription, completion, playback, or available budget for an entire task.

The plan contains no task ID or expected transcription of the voice intent.
Enrollment derives the objective digest and native task identity from
authenticated journal receipts. Enrollment refusal retains the already
confirmed task identity, fails it before worker launch, and cannot create a
replacement through an idempotent retry. Cancellation suppresses the sole
launch opportunity while retaining the original enrollment operation until it
settles. A late result cannot restart the task.

## Generated program and provider boundary

After enrollment, `factory.describe(runtime, task)` returns only the source
path, SHA256, byte count, allowed operation names, and required operation
names. It supplies the native seed's read-only source reference before first
inference. No grant ID, trusted prompts, private video contents, credentials,
or private outputs enter that descriptor.

`ConsultMediaRequest` admits exactly `operation`, `source_path`, and
`source_sha256`. `WorkspaceMediaBroker` snapshots a stable, no-follow regular
file and rechecks its complete digest before each original operation. It
retains one original call per operation, including uncertain or failed calls;
an observer timeout cannot authorize a paid retry. Candidate execution has no
provider object or credential access.

The native adapter must call `current()` immediately before actual dispatch
and must settle its actual response/body/client before returning or raising.
Broker `close()` proves only its original coroutine join, not independent
physical cleanup. `close() == false` leaves original custody retained and
blocks replacement work.

`MediaConsultationResult` binds task, objective revision, source, scope,
logical request digest, actual provider request/response digests, and exact
result digest. The broker retains immutable completed bytes.
`check_primary_export(provider_info, result)` authorizes only those actual
completed results at the owner's exact primary target and rechecks current
native authority, including after broker close. This does not expand global
Contributor disclosure policy or authorize unrelated private history.

## Native completion obligation (#469)

Successful enrollment installs a trusted completion check on the native Task
record. Removing or replacing the advertised factory cannot remove that check.
Before ordinary Task success, the owner requires a settled current-revision
`task_orchestrator.v1` execution with matching native source-write and extension
evidence. The host retains immutable canonical observations separately from the
candidate's returned summary. Required operations come from the enrolled grant;
each must match its actual original broker result, source, target and scope.

The final persisted Intelligent UI must include the required operation outputs
in one same-task, same-revision artifact at the full native disclosure scope.
For ASL this means the actual returned SAM asset and complete observed transcript
in text components. Whitespace normalization permits headings and split text
components, but a replacement summary is insufficient. Native storage verifies
the asset receipt and both retained video variants; this does not prove playback
or transcription accuracy. Earlier public placeholders remain independently
verifiable and cannot substitute for the private result.

The synchronous completion check makes no provider calls or new awaits. It runs
after the final awaited verification and before success reservation, rechecking
source bytes, native owner authority and exact disclosure. Cancellation/steering
retain precedence. Missing, failed, stale or unsettled work cannot report success
through a generic read or text-only publication. Other owner plans follow their
declared required subset; non-enrolled Tasks retain their existing completion
behavior. An admitted live runtime is not retroactively changed by a source fix.

### Optional executable completion

A fresh owner plan may explicitly select `completion_mode: "executable-v1"`.
The legacy `media-v1` mode remains the default. The new mode retains its declared
native media consultations and requires actual SAM media, then additionally
requires an Agent-authored program executed through the separate
[`task_program.v1` route](task-program.md). It publishes the actual complete
program output rather than treating a video model's descriptive text as an ASL
translation. The native floor and exact-primary result-export check are installed
at enrollment before ordinary research/build shell can emit source-derived
stdout. An enrolled plan cannot silently acquire this mode by being rewritten.

The focused local qualification passed 1,496 synthetic/contract/integration
tests with stable Python input hashes; the separate sandbox runner suite passed
43 tests after preserving and repairing its initial anonymous-file mount
failures. Those results include optional Wonder source paths with fake
transports only. They authorize no Wonder activation, extra allowance, provider
call or live-task success claim. The complete execution limits and retained
failure interpretation are documented with the task-program route.

Synthetic broker tests are in `tests/unit/test_agent_media_tools.py`; native
SQLite enrollment, protected-file, revocation, refusal, and cancellation
regressions are in `tests/integration/test_agent_task_media_factory.py`.
These checks do not qualify a real decoder, provider call, transcription
accuracy, or physical transport cleanup.
