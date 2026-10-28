# ADR 0013: Explicit CPU voice resource handoff

Status: experimental implementation for #9 and Roadmap 14.2–14.4. This is a
manual CPU voice adapter, with measured release and task-bound restoration.
M6 remains open for general inference scheduling, GPU/device failures,
constrained-memory workloads and production recovery.

## Authority and ownership

An opt-in fifth HTTP service, `resources` (loopback 8764), owns a first-party
voice subprocess. Agent, Converse HTTP, Decisions, the session journal and
Flutter remain separate. It never adopts an already-running voice endpoint.
The voice health PID must match the verified subprocess PID. Its empty
`loaded` list means API ready; STT/TTS weights load only on demand.

The process host in ADR 0011 requires Linux pidfds and a sealed memfd
configuration. A trusted bootstrap cannot execute the configured worker
before ownership verification and a one-use pipe grant. Sealing fails closed.
`PR_SET_PDEATHSIG(SIGKILL)` plus parent checks prevents the direct worker
outliving the controller. This does not contain hostile forked daemons; no
cgroup or GPU ownership guarantee is made. Signals use retained pidfds only.
Resource status omits argv, environment and raw process logs.

The owner alone changes policy through Agent's `/resources` commands or
`POST /v1/resources/actions`. HTTP cannot supply process commands or PIDs.
The `resources` service credential can disable/enable Converse and publish
`resource.changed`, but cannot grant Agent tool approvals. Existing generated
credential files have included this role since foundation commit `38f709c`.

## Configuration and commands

Install the optional voice environment and cache the selected model weights
separately. Configure the owner data file, outside the Agent workspace:

```json
{
  "converse_url": "http://127.0.0.1:8762",
  "resources_url": "http://127.0.0.1:8764",
  "voice": {"url": "http://127.0.0.1:8765", "profile": "Kokoro 82M"},
  "resources": {
    "enabled": true,
    "voice_python": "/absolute/path/to/voice-venv/bin/python",
    "voice_port": 8765,
    "stt_model": "tiny.en",
    "profile": "conversation_priority",
    "minimum_free_bytes": 134217728,
    "voice_memory_reserve_bytes": 2147483648
  }
}
```

`serve all` includes the resource service only when explicitly enabled.
Owner controls are available in independent Agent Chat and the direct CLI:

```text
/resources status
/resources profile local_work_priority
/resources handoff 268435456 <active-task-id>
/resources restore
/resources converse off
/resources converse on
```

Omit the task ID for a manual lease requiring explicit restoration. A task ID
binds automatic restoration to that actual Agent task's terminal callback.
Inactive tasks are refused before disrupting voice. `GET /v1/resources` on
Agent proxies the controller status for the client. The version 1.0 status
includes controller identity, durable policy revision, measured memory,
worker ownership and release evidence. Action bodies are strict, bounded,
operation-specific and require an idempotency key.

## Transitions and disclosure

`conversation_priority` refuses a manual handoff before disabling voice; it
does not yet implement automatic heavy-job queueing. `local_work_priority`
permits explicit handoff. `local_only` blocks configured cloud text providers
at the trusted provider boundary before each new request. `configured_hybrid`
uses the already-authorized provider configuration. None selects a fallback
provider, grants disclosure or changes a tool permission. Already-transmitted
inference cannot be recalled by changing policy.

Handoff persists `quiescing`, blocks new Converse inference, disables Converse
and invalidates active speech generations, records `unloading`, stops the
owned voice process and verifies reaping/group release. Unknown RSS or kernel
`MemAvailable` refuses admission. Only then does sufficient declared headroom
permit `local_work`. Local Converse text LLMs are explicitly unmanaged by this
adapter; handoff is refused if one is configured. The checked headroom is not
a hard reservation or enforcement of the Agent task's eventual memory use.

Restoration checks available memory against the configured reserve and the
maximum previously observed resident footprint plus margin, starts a new
verified voice process, enables Converse, then records `ready`. Retaining the
observed high-water mark is deliberately conservative: a cold empty worker
is not evidence that future loaded weights need less memory. It can refuse
restoration under pressure. An explicit owner-off choice remains off through the
[shared durable intent gate](0023-converse-owner-intent.md), including the legacy
Converse owner endpoint and restoration races.

Flutter polls current controller status, not historic resource events, and
checks controller/revision identity. Pause or loss of a known controller's
status invalidates pending turns and stops local capture/playback. A stale
status is displayed as unconfirmed availability. Restoration never rearms
the microphone. Polling and browser callbacks do not prove physical audio
has stopped; server status keeps `physical_playback_stopped: unconfirmed`.
The shared history remains intact, and old speech is rejected after handoff.

## Recovery and limits

Actions persist intent and result in an owner-only SQLite ledger. Matching
replays return the stored result; conflicting keys fail. An interrupted
intent remains uncertain and is never automatically re-executed. Agent's
terminal callback retries transient transport failures at most three times
with the same durable key. A recorded uncertain result is not retried under
a new key. Agent shutdown waits boundedly for terminal cleanup before closing
the resource client. If release remains unconfirmed, native task evidence
stays unchanged; the resource transition requires inspection/restoration.

A controller restart during a lease marks resource ownership failed for
inspection rather than blindly restoring or redoing work. The direct worker
dies with its controller; generic descendant containment, a reconciliation
daemon and crash-transparent lease recovery remain unfinished. Owner data
directories and executables are trusted, outside the Agent workspace. This
is not isolation against the OS account owner or administrator.

## Verification

The [public synthetic live record](../../evaluation/evidence/resource-handoff-2026-10-27.json)
captures source hashes, actual process IDs and release measurements. Cached
Whisper tiny.en and Kokoro weights were loaded in a real CPU subprocess;
handoff verified its release, Agent commands and one exact file approval
worked with Converse off, and native file/read hashes proved the task result.
The bound task restored a new voice PID, old speech returned HTTP 409, and
fresh STT/TTS loaded and worked again. The initial absent-cache model attempt
is retained as a failed setup case; downloads remained disabled.

Reproduce offline regressions with:

```bash
PYTHONPATH=. pytest -q tests/integration/test_resource_processes.py \
  tests/integration/test_resource_boundaries.py \
  tests/integration/test_resource_parent.py \
  tests/integration/test_resource_handoff.py
cd apps/kinaiya_flutter
flutter analyze
flutter test
node --test web/audio_bridge.test.js
```

Process tests exercise real synthetic workers and injected rare boundary
failures; coordinator tests use deterministic memory-admission inputs. Live
proof is a small public synthetic Agent file task, not demanding local model
execution, a memory-pressure benchmark or physical microphone/hearing proof.
That first study did not measure GPU residency; [ADR 0022](0022-managed-laya-resources.md)
adds optional managed Laya with AMD driver measurements. Latency targets, full M6, private beta
readiness and complete roadmap implementation are not claimed.
