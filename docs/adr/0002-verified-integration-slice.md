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
