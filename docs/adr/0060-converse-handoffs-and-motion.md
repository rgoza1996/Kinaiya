# 0060: Conversational acknowledgments, native handoffs and interface motion

Issue #141 follows the owner’s report of missing Agent handoffs and the latest
Penpot master. Routing depends on intent and observed Agent availability;
Converse running locally does not imply that Agent must run locally. Native
Agent admission still enforces authenticated source identity, tool grants and
the selected provider’s disclosure policy. A routing label is not acceptance.

The [native handoff contract](../agent-handoff-receipts.md) distinguishes queued,
accepted/applied, rejected, unknown and not-applied states. The frontend binds
these observations to current authenticated sources and decisions. It never
uses companion text as an execution receipt. Existing uncertain journal writes
rely on the Agent’s native idempotency key; no distributed transaction is claimed.

The built-in shared Converse persona acknowledges work requests briefly, for
example “Give me some time on it” or “Let me think about that.” These are
conversational acknowledgments, not claims of queueing, execution or completion.
Typed, rolling-window and measured-context prompts use the same persona. Custom
personalities retain their existing explicit ownership. Native state-claim and
completion guards remain unchanged. Starting rolling Call reuses a pinned typed
session; it no longer selects another named session behind the visible history.
Existing disconnect/session-acquisition semantics remain separate.

The unchanged October 30 **Kinaiya — Master design** native export has SHA256
`62ba30b5a01b9056d4cbc490108e8bcb33f4840739d5049eb4d57a491ef37673`,
12 pages and 125 actual top-level boards (82 on the updated first page). Its
Model, Reasoning, Inputs, Speech and Decisions bubbles project current host
declarations through read-only popovers. They do not implement hot-swap,
direct-audio dialogue or camera input. Current microphone/playback Stop controls
stay reachable outside the fading panels. The existing mounted conversation
and Agent owners retain their sessions, drafts and operation lifetimes.

Interface transitions use 220 ms ease-in/out fades. The local
`animate_transitions` preference defaults to true; strict released six- and
seven-field records migrate to the new eight-field shape. Unknown or incomplete
records remain invalid. Interface transitions can be disabled separately from
character animation. Owner reduced motion and the OS animation setting always
override the fade. Outgoing content cannot receive pointer/focus input or be
announced as current. Turning motion off does not replace the current child’s
state, arm a microphone, delay an operation or grant permission.

Speech cleanup runs after native speech validation and immediately before the
upstream synthesizer. It removes pictographic emoji, their modifiers/joiners,
flags/keycaps, standalone ASCII smileys and decorative bullets. Useful words,
Unicode letters/accents, currency, numbers, temperatures and mathematical
operators remain. Limited unambiguous formatting markers are removed; this is
not a complete Markdown or phonetic renderer. The original text stays in the
journal and visible transcript. A cleaned-empty chunk completes without opening
TTS or claiming audible delivery; stale generations still cannot synthesize.
Literal words such as “smiley face” are ordinary text and remain.

Verification includes authenticated ASGI/AgentClient rejection and uncertain
ACK cases, native task/source binding, strict preference migrations, responsive
and keyboard tests, retained audio owners and actual speech-route synthesis
capture with unchanged journal text. Scripted inference proves these boundaries,
not human voice quality, classification accuracy or audible latency. Live and
CI evidence is recorded on the linked pull request; full M3/M5 remain open.
