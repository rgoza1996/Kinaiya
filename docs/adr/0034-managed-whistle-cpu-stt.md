# ADR 0034: opt-in managed Whistle CPU transcription

Tracks [#85](https://github.com/rgoza1996/Kinaiya/issues/85). Whistle is an
experimental buffered CPU STT option. It uses independent, existing durable
audio-window requests; it does not implement the persistent streaming-session
and revision protocol required by [#49](https://github.com/rgoza1996/Kinaiya/issues/49).
The existing combined Whisper/TTS worker remains the default. Selecting
Whistle changes only the STT transport; the retained TTS profile, endpoint,
voice and model configuration remain independent. There is no cloud fallback.

## Explicit owner configuration and HTTP boundary

The owner adds `resources.whistle` and the matching `voice.stt` selection to
their existing configuration. Paths below are placeholders for an already
prepared owner interpreter and the exact pinned local artifacts. The child
interpreter, weights and library must be outside the Agent workspace.

```json
{
  "resources_url": "http://127.0.0.1:8764",
  "resources": {
    "enabled": true,
    "voice_python": "/owner/voice-env/bin/python",
    "voice_port": 8765,
    "whistle": {
      "python": "/owner/whistle-env/bin/python",
      "weights": "/owner/whistle.cact",
      "library": "/owner/libneedle3.so",
      "cpu_affinity_count": 2,
      "memory_bytes": 1073741824,
      "host_memory_reserve_bytes": 268435456,
      "startup_timeout_s": 60,
      "request_timeout_s": 300
    }
  },
  "voice": {
    "url": "http://127.0.0.1:8765",
    "profile": "Kokoro 82M",
    "stt": {"provider": "whistle", "url": "http://127.0.0.1:8764"}
  }
}
```

The selected STT origin must match the configured resources service and its
actual serving port. Configuring artifacts without selecting Whistle is
rejected. The optional decoder dependency is `av==17.1.0`, separately pinned
with distribution hashes in `requirements-whistle.lock`; Whistle does not
require Torch, VibeVoice, the upstream Python wrapper or a request-time
installer/downloader. Asset acquisition and dependency installation are
separate owner operations, never side effects of a transcription request.

Resources exposes `POST /v1/audio/transcriptions` only to its existing owner
or Converse service credentials. Agent and resources credentials cannot call
this route. It accepts a raw `audio/wav` or `audio/x-wav` body to avoid multipart
disk spooling. The byte limit is 10,000,000; supported request slices are at
most 30 seconds of integer PCM WAV, mono/stereo, 8–192 kHz, 8/16/24/32 bits.
Compressed, extensible and float WAV are refused explicitly. The 30-second
slice is our bounded supported input, not a claim that the engine truncates
there: its batch algorithm has internal 30-second passes.

The decoder runs in memory through pinned PyAV/FFmpeg, normalizes the pinned
equal-power stereo remix to `(L + R) / 2`, resamples to 16 kHz mono floats,
rejects nonfinite values and limits resampler overshoot to the native header's
`[-1, 1]` PCM contract. A result contains `text`, `provider: whistle` and
`profile: experimental_buffered_cpu`. Text is limited to 16,000 characters;
IPC and HTTP results are limited to 128 KiB. Native diagnostics, alternate
fields and error bodies are not forwarded. The worker persists no audio or
transcript. Native durable audio-window appends retain their existing checks.

Malformed/type/size requests return bounded 400/415/413 errors; an active or
retained operation and owner/resource policy changes refuse with 409; native
unavailability is 503; the finite request deadline is 504. A disconnect
discards the result (499 where a response is still possible). The owner may
configure a 30–900 second per-request deadline; the default is 300 seconds.
It is intentionally compatible with a slow experimental CPU profile.

## Native ownership and isolation

The process-global, non-thread-safe C API lives in a separate **direct**
`WorkerManager` child. The parent owns HTTP, service credentials and native
journal access. The child gets only fixed argv and a small explicit Python
environment, with no inherited service credentials. Its short owner-private
Unix socket is created before the required Linux x86_64 seccomp fence; only
then are the native engine and optional decoder used. Missing isolation
refuses startup. The fence blocks socket/socketpair/connect, send/receive
families, io_uring and pidfd_getfd, while accepted IPC uses `os.read/os.write`.
No-new-privileges and TSYNC are required and socket-creation probes must
return EPERM. This is a network fence for this trusted pinned native child,
not a general hostile-code or filesystem sandbox.

Every connection checks `SO_PEERCRED` against the real owned PID and UID on
both ends. Hello binds the selected hashes, profile, isolation label and a
random per-process incarnation. Every request/result additionally binds a
random nonce, incarnation and exact integer PID. Client-reported PIDs alone
are never authority. Frames and JSON shapes are bounded; duplicate keys,
nonfinite JSON and malformed identities are refused. Native input paths are
absolute, regular and outside the Agent workspace. The loader copies and
hashes exact local bytes into sealed memfds before an explicit
`ctypes.CDLL('/proc/self/fd/...')`; later owner writes cannot change those
validated inputs. There is no default native resolver or loader fallback.
The child applies its zero `RLIMIT_FSIZE` only after both sealed copies are
prepared, immediately before the Whistle `CDLL` call and native constructors
or model load. Linux also applies file-size limits to memfd writes, so imposing
zero earlier prevented startup before any transcription. The network fence,
address-space/core limits and CPU affinity remain active during preparation.
Failure to impose the zero limit refuses native loading and closes both inputs;
the limit is not raised again in the resident worker.

The new frozen `WorkerIdentity` is an in-process observation of a verified
ready owned child. Only the manager's issued object selects its saved exact
Process/start binding. `is_current` additionally requires running state,
verified readiness and kernel identity. `stop_if_current` compares under the
existing lifecycle lock; it still reaps/reconciles an exited same leader's
owned group. A superseded identity sends no signal and provides no fabricated
stop evidence. Only a previously ready binding in the same **open** manager
can use the existing replacement/quiescence invariant. Closed managers are
never swapped or treated as cleanup proof.

## Cancellation, recovery and resource policy

The broker owns at most one inference or unfinished cleanup; it refuses
replacement before reading another request body. Cancellation, timeout,
disconnect, stale result and owner/resource changes discard output and stop
the captured incarnation through the manager. The request waits at most one
second for cleanup; an independent task retains process/transport ownership
after that wait. Unconfirmed cleanup stays visible and blocks replacement.
A new verified binding can release an older operation without signaling the
replacement. Repeated cleanup of the same already-stopped binding uses an
explicit `release_confirmed` label for retained reaped/empty-group evidence;
it does not invent a fresh signal, memory reading or stop event.

A new request automatically recovers a confirmed-reaped Whistle child through
a fixed coordinator method under its lifecycle lock. Recovery requires ready
resource state, enabled desired/current owner intent, no lease/handoff, and
fresh measured memory of at least the configured Whistle reserve and resource
minimum. It rechecks policy/revision after validation and startup. Existing
resident voice/Laya allocations are not credited again. Already resident
Whistle calls do not trigger a new allocation check. Off/handoff requests
publish a Whistle-only preemption barrier before waiting for the lifecycle
lock; idempotent replay cannot preempt current legitimate work. Concurrent
off/handoff wins, and a revoked newly ready child is stopped before inference.
An uncertain inference is never replayed. Resource restore/handoff includes
this direct worker while standalone Agent controls remain independent.

The handoff barrier first requires a nonmutating eligibility preview. An
already-refused conversation-priority request, active lease, malformed or
out-of-bounds headroom declaration, unsupported/impossible/unmeasured GPU
request, unmanaged text provider or inactive task cannot revoke otherwise
valid Whistle recovery. Preview uncertainty or a changed resource revision
across its awaits declines the barrier. Every authoritative check still runs
under the existing lifecycle lock; the preview grants no admission and does
not mutate cached locality or durable state. Default coordinators without
Whistle do not perform this additional health/task/GPU preview. Current RAM
shortage is not a prediction of post-release headroom: actual RSS, group
release and free-memory checks remain in the original locked transition.
The idempotency ledger is checked again after the awaited preview, immediately
before publishing a barrier. A same-key action can record a rejection without
changing the resource revision; its matching replay or conflicting input must
not publish cancellation. Digest comparison and cached/uncertain result
handling remain exclusively in the authoritative locked action.

An already-reaped Whistle worker contributes zero current residency during
handoff only with retained verified release and subsequent manager
reconciliation. Missing RSS for other workers remains a refusal. Neither a
failed stop nor terminal close is a release receipt. CPU affinity is bounded
to 1–4 CPUs and address space is capped; affinity does not prove the native
thread count. There is **no cumulative lifetime CPU limit** that would kill
a healthy resident child after repeated requests.

Both existing Converse STT awaits now observe actual browser ASGI disconnects
after their body has been fully parsed. One receive watcher cancels/awaits
upstream HTTP cleanup, prioritizes simultaneous disconnect, and retains an
unfinished close in a bounded registry (32 active or retained owners). This
does not establish that canceling the legacy Whisper HTTP path aborts its
native thread. Whistle's separate broker owns native process termination.

## Public provenance and verification limits

`whistle_manifest.json` records the exact model revision/hash, wheel/member
hashes and native header. The minimal ctypes binding is a documented
Apache-2.0 derivative of `needle/agent/whistle.py` at
`b4fc11b04f1852888d375c8c6dfbaca44572d014`. Its source declares 3.2.1 and
intentionally selects the separate 3.3.3 engine. The retained Apache license
and derivative notice ship as package data. The wheel has no LICENSE/NOTICE
member; opaque binary pins do not establish full source/build/ABI equivalence
or independently resolve binary redistribution terms. No model or binary is
redistributed here.

Focused tests separate deterministic parsers and mocks, actual owned synthetic
children/IPC/seccomp/HTTP lifecycle, and actual optional PyAV decoding. Raw
failures, corrections, interpreter/import hashes, exact commands, audit and
results are retained in
`evaluation/evidence/managed-whistle-cpu-2026-10-28.json`. Existing resource,
speech, owner, typed cleanup and window regressions remain part of validation.
The existing package-data assertion is extended with the three exact new
files while retaining exact equality and all prior entries.

The [lead integration smoke](../../evaluation/evidence/managed-whistle-integration-2026-10-28.json)
used the actual pinned engine through production HTTP, IPC, ownership and
resource management. The same child transcribed the exact 4.7-second public
phrase cold in 27.35 seconds and warm in 27.20 seconds on two CPUs. Its highest
observed VmHWM was 77,164,544 bytes (73.59 MiB), not a guaranteed final peak.
The third request was canceled while pending with measured process CPU
advancement; exact native entry was not instrumented. Its process/group was
confirmed released before a new silence request recovered a different owned
child. Owner-off, restore-while-off, on, handoff and restore also passed; all
four observed children were reaped with empty groups. Converse control and
provider locality were synthetic; this was not a full voice-to-Agent test.

The first managed smoke failed before readiness with no transcription
requests. The separate startup diagnostic identified the file-size cap
blocking sealed memfd copies; the correction and original failure are
retained. The earlier preflight's warm cumulative-CPU-cap failure also remains
evidence. Independent lifecycle/kernel reviews and an actual
[Muse Contributor review](../../evaluation/evidence/managed-whistle-muse-review-2026-10-28.json)
record their source identities and limits. Exact-head CI remains a merge
requirement. Each speech request took about 5.8 times the input audio duration,
which is too slow for real-time voice; this screen establishes no general
quality, sustained-load, p95, physical-audio, default-promotion or full M2
acceptance claim.
