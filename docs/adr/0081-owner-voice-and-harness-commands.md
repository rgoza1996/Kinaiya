# ADR 0081: Owner Voice navigation and harness creation commands

Status: Accepted implementation; part of #196.

Agent's native command registry already implements `/yolo on|off`. Retain its
existing owner authentication, process lifetime and workspace permission limits.
Do not infer a permission grant from a transcript, model response or command
discovery. Slash commands still require explicit owner submission in Agent.

`/voice`, `/voice vad`, `/voice tts` and `/voice stt` are deterministic controls.
They return a fixed `open_options: voice|audio`, focus hint and
`capture_changed: false`. The Flutter client maps only those two destinations to
its persistent Options workspace. No model call, model swap, session creation,
microphone or speaker operation occurs. TTS/STT navigation opens the currently
implemented Audio options; it does not claim a mutable provider editor exists.

`/create-harness <request>` has execution class `task_invocation`, so both the
client and native task admission require inference disclosure. A stale registry
cannot relabel it as a deterministic control. The native handler constructs an
ordinary, session-bound Agent task with the original content scope and a stable
command-derived idempotency key. The request asks for source, manifest and usage
notes inside the granted workspace. It explicitly identifies the currently
supported `context_selector.v1` contract and asks Agent to report unsupported
requirements and remaining owner steps honestly.

The result is an accepted task identity, not a claim that a harness was built,
registered, evaluated or activated. Ordinary tool permissions, evidence and
completion rules remain authoritative. Candidate promotion and protected-suite
changes remain outside model authority. Command replay returns its original
receipt without creating a second task. Invalid syntax starts no inference.

Native command checks cover deterministic Voice destinations, syntax rejection,
task/disclosure identity, unchanged permissions and replay. Existing Flutter
command/consent checks continue to apply; native admission and actual generated
harness quality are separate checks.
