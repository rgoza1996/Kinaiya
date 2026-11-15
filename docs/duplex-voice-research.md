# Optional duplex Converse backends

Research for [#560](https://github.com/rgoza1996/Kinaiya/issues/560), checked
November 15, 2026 UTC (November 14 locally). Repository baseline:
`4e34f94df9845717eb5896aec61d4ea6f9d24c19`.

**Decision:** proceed with a separate continuous-audio contract and controlled
prototype; defer production adoption and paid evaluation. GPT-Live client
delegation is the first integration to evaluate because it leaves backend and
context selection with the application. Gemini Live is a second candidate.
This is an architectural inference from the documented interfaces, not a
measured quality, latency or reliability ranking. No adapter is implemented by
this research, and no model, account, credential or deployment was changed.

## Provider comparison

Prices below are published USD rates at the research date. Token prices are
per million tokens; they are not estimates for a minute of conversation.

| Candidate | Documented conversation/work behavior | Published cost | Important integration boundary |
| --- | --- | --- | --- |
| OpenAI `gpt-live-1` | Simultaneous listening/speaking; client delegation lets the app use its own agent and select context/results | $0.05 per active session minute, billed per second; backend model/tools extra | Separate Live protocol. Client delegation supplies metadata, so Kinaiya must obtain the actual request from its own journal. [Delegation](https://developers.openai.com/api/docs/guides/live-delegation), [model/rate](https://developers.openai.com/api/docs/models/gpt-live-1) |
| OpenAI `gpt-realtime-2.1` | Speech, reasoning and tool requests share a session; speech and Agent work still need separate application ownership | Audio input/output $32/$64; text input/output $4/$24; cached input $0.40 | A pending function call can coexist with conversation; this does not make its arguments native routing or completion proof. [Async behavior](https://developers.openai.com/blog/realtime-api), [rates](https://developers.openai.com/api/docs/pricing) |
| Google `gemini-3.8-live`, Gemini Developer API | Audio conversation with nonblocking function calls; blocking mode pauses generation until a response | Audio input/output $3/$12; text input/output $0.75/$4.50, including output thinking tokens | Use the documented audio response mode and transcription events. API surface and deployment matter: Developer API guidance labels Live preview, while the Enterprise platform documents GA model availability. [Capabilities](https://aistudio.google.com/docs/live-api/capabilities), [tools](https://aistudio.google.com/docs/live-api/tools), [rates](https://aistudio.google.com/docs/pricing#gemini-3.8-live), [Enterprise model](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/guides/gemini-3-8-live) |

GPT-Live bills active time even during silence, muting and backend work. WebRTC
creation bills 15 seconds during initialization, credited against subsequent
running duration; it is not an extra 15 seconds added to every completed call.
Reconnect policy therefore affects cost. Realtime input transcription can have
its own charge. [OpenAI cost accounting](https://developers.openai.com/api/docs/guides/voice-latency-cost?api=live)

Gemini's guidance describes accumulated session context being charged again
on later turns. A headline audio-minute estimate therefore does not establish
whole-session cost. Its free and paid tiers also have different published data
use terms; no free-tier disclosure authorization is inferred here.
[Gemini billing](https://aistudio.google.com/docs/live-api/best-practices#pricing-and-billing),
[tier/rate table](https://aistudio.google.com/docs/pricing#gemini-3.8-live)

OpenAI Realtime documents a 60-minute session limit. Gemini documents 15-minute
audio sessions without compression, roughly 10-minute connection lifetimes,
and resumption handles valid for two hours after termination. Provider context
compression or resumption cannot replace Kinaiya's durable transcript.
[Realtime lifecycle](https://developers.openai.com/api/docs/guides/realtime-conversations),
[Gemini session management](https://aistudio.google.com/docs/live-api/session-management)

The fetched GPT-Live session guide describes expiry but does not establish a
numeric lifetime bound. Project access, concurrency quotas, region, storage
settings and the applicable deployment's limits remain to be checked before
any authorized evaluation. No account access or availability was tested.

## Proposed Converse-only adapter

The current [buffered native adapter](adr/0112-buffered-native-audio.md) admits
whole utterances and waits for bounded, completed text/PCM output. It is
loopback-only. GPT-Live streams continuously, uses different event names and
has no equivalent final-response event. It therefore requires a distinct
contract and player ownership; changing its URL or pretending it is
`realtime_v1` would be incorrect.
[Live migration/event differences](https://developers.openai.com/api/docs/guides/live-migration)

The smallest proposed adapter owns these operations and observations:

| Boundary | Required behavior |
| --- | --- |
| Open/close | Capture the selected deployment, credential generation, scope, native session/thread/epoch and spend admission. Confirm readiness before PCM. Retain original connection/cleanup ownership on timeout. |
| Input/transcript | Admit original audio and incremental transcript revisions with native source IDs and order. Retain the complete scoped journal independently of provider history or compression. Agent may retrieve it on demand. |
| Dialogue output | Keep generated captions, received PCM, queued audio and playback receipts separate. Use the player clock for presentation; do not label a generated response heard. |
| Backend hints | Treat provider delegation/function-call IDs as correlation data. Resolve task text from authenticated journal sources and let the selected Decisions lane choose queue/steer/interrupt/ignore. A hint cannot dispatch a second Agent task. |
| Status/result | Send only bounded, current, permitted native observations and validated results. Keep partial work, publication, execution and semantic verification distinct. Unknown or unavailable remains explicit. |
| Interruption | Separate stopping speech, stopping capture, canceling a provider exchange and requesting Agent cancellation. Retain uncertain effects and outstanding task ownership after voice disconnect. |

GPT-Live's client context and commentary appends provide a way to return
selected progress/results without configuring a hosted Responses backend.
Use the original delegation ID for correlated updates; its append limit is
500 tokens. In Gemini, nonblocking functions and response scheduling offer a
similar conversation/work separation. These capabilities need native mapping
and tests, not a second provider-controlled execution loop.
[Live client updates](https://developers.openai.com/api/docs/guides/live-delegation),
[Gemini scheduling](https://aistudio.google.com/docs/live-api/tools)

## Proposed interaction sequence

| Step | Native owner and action | What voice may know |
| --- | --- | --- |
| User asks a question | Converse capture admits the source and transcript revisions into Session | Heard wording; no inferred Agent acceptance |
| Route independently | Decisions validates the current source and emits its bounded routing decision | Acknowledgment may continue; no mandatory silence |
| Accept work | The chosen native/external Agent validates authority and returns its original idempotent receipt | Exact accepted/pending/refused fact, when permitted |
| Continue talking | Converse keeps listening/speaking; corrections pass through Decisions against the original task revision | Unrelated conversation and pending steering stay distinct |
| Finish or fail | Agent publishes an authenticated evidenced outcome, or retains explicit uncertainty | Selected verified result; no success inferred from tool intent or file creation |
| Deliver result | Converse supplies bounded result context to voice; the client schedules PCM and records playback | Spoken summary and actual delivery remain different observations |

Replayed provider events, reconnects and duplicated delegation IDs must join or
reconcile the original native operation. A new voice session cannot reset a
charge, resurrect a stale source or repeat Agent effects. A provider's canceled
tool-call notification is not proof that an independently running Agent stopped.
For Gemini, drain/flush the local playback queue on interruption as well as
handling the server signal.
[Gemini interruption handling](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/troubleshooting)

## Evaluation and adoption gate

First build controlled fixtures for overlapping speech, delayed/refused backend
work, correction, duplicate delivery, disconnect/resume, stale scope, exhausted
budget and resistant cleanup. Preserve the current editable-persona policy and
native permissions/outcome gates; do not turn provider speech into authority.
Before implementing transport, freeze numeric frame/session/queue bounds and
the mapping from provider stream offsets to original native input sources.

A later separately authorized public-audio rehearsal should record admission,
first transcript, Decisions completion, Agent receipt, first useful answer,
first PCM and player start/end. Measure median/tail latency, interruption
overrun, lost/duplicated words, task truthfulness and complete billed usage.
Keep inputs, backend, instructions and playback setup fixed across candidates.
Separate fast acknowledgments from useful answers. No such measurement was run
for this research.

Retain the modular STT → text Converse → TTS path and existing local native
mode as independent choices. Proceed to a production adapter only after its
own scope, lifecycle, transcript/recovery and cost controls qualify. Paid voice
is optional; neither cloud provider replaces Decisions or grants Agent access.

The [source manifest](evidence/duplex-voice-research-2026-11-14.json) records
fetch time and response digests. Full source snapshots remain local research
inputs rather than copied documentation in the repository. Directory lookup
was unavailable because its CLI is not installed; the facts above were checked
directly against official provider documentation, without provisioning a service.
