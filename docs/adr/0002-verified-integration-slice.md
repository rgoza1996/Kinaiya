# ADR 0002: Real-provider integration gate

The specification remains Roadmap.md 0.2.1, including the owner's Kinaiya identity
rename. This implementation does not reduce its release gates.

The host runs independent session, Agent, Converse and Decisions processes. Converse
never calls Decisions/Agent to generate an ordinary reply. Every conversation entry
gets a durable disposition. Assistant entries are source-checked as non-authorizing
context without spending an inference call on each streamed sentence. They remain
in the rolling context for the next user decision. Direct Agent work bypasses routing.

Meta Responses is a verified initial development provider, explicitly selected for
public/synthetic inputs. Laya remains the intended default and needs its own setup
and calibration. Agent requests use native JSON Schema output when supported; the
local compatible adapter has a bounded textual-JSON loop, not a blanket compatibility
claim. Transcript IDs are provenance, never filesystem paths. Agent can request up
to 20 scoped journal entries, bounded to 12,000 text characters, on demand.

The first real voice attempt wrote a file but exhausted its step limit because model
narration broke the JSON parser and source IDs were mistaken for files. No success
notice was published. Native structured actions and explicit source retrieval fixed
that loop. A later steering attempt exhausted a 2,048-output-token allocation and
failed honestly. The runtime Agent development profile now uses high effort and an
8,192-token output cap. Converse uses low effort; Decisions uses minimal effort.
These application profiles are separate from Muse implementation workers, which use
Contributor xhigh. No Wonder escalation has occurred.

Two real voice-to-task runs passed with Kokoro synthesis, faster-whisper tiny.en,
Meta inference, real filesystem effects, hash verification, authoritative outcomes,
grounded spoken-result synthesis, stale speech rejection and transport deduplication.
Real-model steering reached revision 2. An individually approved `false` command
failed without being reported as success. The real Agent CLI wrote an independently
verified file while Converse was disabled. Synthetic audio was used, not a person
speaking into a microphone. No audible latency measurement is claimed.

The first two routed voice samples took 9.85 and 8.72 seconds to first text; their
result notices took 1.08 and 1.06 seconds to first decoded Kokoro audio. Both ordinary
replies arrived while Agent was running. These are screening samples, not percentile
claims or evidence that the roadmap's latency targets have been met. A shorter warm
standalone sample reached decoded Kokoro audio in 0.25 seconds; cold setup took 18.5
seconds. Tiny STT transcribed spoken “dot text” as `.text`; the reproducible fixture
uses the unambiguous filename `proof`. ASR filename fidelity remains a limitation.

The ordinary source tree and evaluator/credential directories are excluded from
worker workspaces. Linux bubblewrap provides the current shell boundary; unsupported
namespace layouts fail closed. Owner-controlled core updates are outside the worker.
This initial single-owner Linux deployment is experimental, not production hardening.
Cloud disclosure, raw-audio retention and unsupported provider profiles stay explicit.

Reproduce with `scripts/live_slice.py` against configured running services and an
empty dedicated synthetic workspace. It requires `--allow-paid-inference` and forces
Meta traffic through the run-local admission proxy. Default CI never spends inference.

## Prototype conversation behavior retained by the rewrite

The lead compared Kinaiya-Converse's live controller, adaptive VAD, partial-window queue, recorder, interruption handling, playback scheduler, history and response service. These are behavior references, rather than a wholesale port of its shared frontend state. Useful behavior includes user-armed continuous listening, adaptive speech/silence thresholds, minimum speech duration, endpoint silence, independently decodable partial recordings, draining or superseding partial work, sustained barge-in detection, stopping every scheduled audio source, and allowing a reply to an interruption to be interrupted again. Old asynchronous cleanup must never reset a newer turn's capture or speech state.

The rewrite uses session-scoped journal provenance and playback generations. Generated text, scheduled audio, estimated playback and verified task completion remain distinct. Playback notes inform subsequent conversational context without inventing exact word alignment. Sentence chunking waits for a disambiguating boundary so a decimal split across provider deltas is preserved. The Flutter workstream must verify actual local playback completion and live capture independently; the backend voice probe alone does not prove a physical microphone, audible delivery or full prototype parity.

## Direct Agent controls

`packages.commands.Registry` supplies one deterministic owner-only registry to Agent HTTP and CLI. Help, factual status, stop, configured model selection, permissions, temporary YOLO grants, sessions, history and client-view clearing do not invoke Converse, Decisions or a model. Dispatch requires a current Agent session, journals provenance, and persists transport idempotency in the protected data directory. An interrupted dispatch remains uncertain and is never silently replayed. Model changes require no active tasks and can select only owner-configured profiles; YOLO preserves workspace/core boundaries. Built-in command parsing is absent from Converse.

This is the initial command foundation. Dynamic skills, MCP lifecycle, plugin commands, user aliases and the interactive CLI session picker are still separate M1A/M4A work; the registry does not advertise them as installed capabilities.
