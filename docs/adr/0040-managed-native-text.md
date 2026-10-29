# ADR 0040: managed native text transport and release

Tracks [#101](https://github.com/rgoza1996/Kinaiya/issues/101), supporting the
local Converse work in [#97](https://github.com/rgoza1996/Kinaiya/issues/97)
and [#49](https://github.com/rgoza1996/Kinaiya/issues/49).

Status: experimental implementation under verification. The initial worker is
CPU-only. No new model, default, GPU grant, dialogue quality, realtime latency
or complete voice profile is qualified by this change. The owner's smallest
profile target is **8 GB total VRAM for STT, Converse LLM and TTS together**.
GGUF size and successful transport tests cannot establish that fit.

## Selected worker and authority

One protected resource configuration selects `qwen3` or `gguf`. Both use one
`ManagedTextBroker` implementation and one selected text allocation; Agent and
Decisions remain independent. Existing Qwen native IPC and public NDJSON retain
their formats. GGUF uses native compatible SSE, including actual optional usage
metadata. Generic `stop` is never converted to Qwen `eos`, and absent token
counts remain unavailable.

The GGUF provider is opt-in through `providers.converse.kind=gguf`, with only
the matching resources origin in that provider profile. `resources.gguf` holds
the Python path, small manifest path, independently pinned manifest SHA256,
context/output limits, startup/request deadlines and host memory reserve.
Converse receives a scoped service credential; the child receives none.
Agent tools cannot select native argv, mounts, model paths or worker identities.
Ordinary compatible endpoints remain unmanaged and cannot borrow this worker's
release authority through their self-reported health.

The manifest identifies owner-selected bytes, not verified publisher/build
provenance. Version 1 requires `profile`, `model`, `alias`, `assets`, `sampling`
and `version`. Every asset has absolute `path`, `guest`, positive integer
`bytes` and lowercase `sha256`; only `/model.gguf`, `/runtime/<basename>` and
`/vendor/<basename>` are supported guests. Exactly one model and
`/runtime/llama-server` are required. The manifest is at most 64 KiB, with at
most 64 files and a 64 MiB executable verification bound. Sampling is fixed
owner configuration, with non-thinking template mode; requests cannot override
it. No downloads, automatic discovery, arbitrary command arguments or fallback
provider run on this path.

Parents read the small manifest and input metadata. The granted child alone
copies/hashes model and runtime files into sealed memfds, then exposes only
those inherited descriptors through exact guest symlinks to `/proc/self/fd/N`.
The asset bytes remain sealed; no mutable copy replaces them. Bubblewrap's
FD-bind source canonicalization cannot resolve anonymous memfds, which the
first actual startup attempt demonstrated before native/model loading. Unused
inherited descriptors survive into the payload, including its dynamic loader;
the trusted native process owns those descriptors and namespace-local links.
Other opened inputs/control descriptors are closed or close-on-exec. This
composition preserves the base sandbox's private
PID/network/user/UTS/IPC/cgroup namespaces, fresh `/proc` and `/dev`, and private
IPC workspace. It mounts no broad runtime/vendor directory or host GPU/sysfs
tree. The CPU worker retains its explicit address-space, file/core and
two-CPU limits; it has no cumulative lifetime CPU limit. These are not a hard
RSS or GPU-memory reservation.

## Streaming and identity

`OwnedUnixTransport` uses public HTTPX/httpcore interfaces with a Unix-only
network backend, no proxy, redirects, TLS, retries or connection reuse. Its
awaited socket verifier runs before the connection pool can send HTTP headers
or a body. Socket closure has a retained asynchronous owner, including repeated
cancellation and close failure; buffering a complete reply in a thread is not
used as streaming evidence.

The trusted endpoint verifies private directory/socket inodes, SO_PEERCRED,
UID, exact Manager outer identity, bounded stable ancestry, isolated native/init
PID and network namespaces, and the native executable's sealed bytes/inode.
It retains observer pidfds independently of Manager. After hashing an opened
executable, it checks the current `/proc/PID/exe` inode again: `exec` can preserve
PID, start time and namespaces. A ready lease binds the exact opaque
WorkerIdentity object. Matching numerical fields do not authorize replacement.

GGUF public requests are bounded JSON at `POST /v1/chat/completions`, authorized
only for owner/Converse, with exact `model`, `messages`, `max_tokens` and
`stream=true`. The configured native alias and output limit must match. Context
is a total native context capacity, not an independently measured input-token
count. Context exhaustion, length finishes and malformed/unsupported native
results are refusals; context is not silently truncated by trusted code.

The existing strict compatible parser now also exposes validated SSE events
with their original assembled data string. Its text-only client projection
retains its validation and limits. The native endpoint forwards those actual
data events without synthesizing text, finish or usage evidence. It withholds
native DONE until parser EOF and actual HTTP/client/socket close. The broker's
queue holds public bytes and a private end marker; no protocol's terminal
dictionary is used as the common lifecycle contract. Failure after headers
emits a trusted-host error that compatible clients refuse.

The resource-policy provider wrapper also awaits the selected iterator's
optional close in `finally`. Closing that wrapper early must retain actual
HTTP cleanup and propagate an unconfirmed close; asynchronous-generator
finalization is not a replacement for that owner. Plain async iterators
without a close method remain supported.

## Off, handoff and recovery

The action/replay ledger remains serialized under the action lock. A separate
shared allocation lock serializes Whistle/text admission and restore headroom
accounting. Admission never needs the action lock, so owner-off can await its
retained cleanup without a lock cycle. Restore fences new work before taking
the allocation lock and releases it before an off/drain operation. Exact
resident credit, lost-credit checks, durable owner revisions and replay rules
remain required.

Off/handoff/shutdown fence policy, abort the existing broker, stop owned workers
to unblock I/O, drain retained admission/HTTP work, join allocations and perform
a final stop/release reconciliation. A delayed admission cannot escape an early
stop. Full release requires Manager's exact child/group proof, joined HTTP work
and extinction of the originally observed native/init pidfds. Unknown release
retains the slot and prevents successful off/handoff/replacement. Shutdown
still attempts Manager.close after an endpoint/broker failure.

The trusted child publishes a bounded startup phase outside its mounted IPC
directory. Initial creation is exclusive; only that preparing PID can publish
one terminal transition. `exec_pending` is published before exec; an uncertain
publication can never be downgraded to `pre_exec_failed`. A pre-exec failure can
support cleanup only alongside exact successful Manager proof and extinction
of any retained observers. A partial startup without sufficient native identity
remains uncertain. A model response cannot write or assert this phase receipt.

## Verification limits

Synthetic tests exercise strict manifest/seal/mount boundaries, actual UDS
streaming and pre-request refusal, identity races, parser/EOF/close ordering,
retained cancellation, startup quarantine and coordinator allocation/stop
races. They use no model inference. Original Muse delivery and independent
failing regressions are retained separately from repaired code. Existing Qwen,
Whistle and resource regression expectations remain unchanged.

A real no-model sandbox regression compiles a tiny ELF/DSO fixture and runs
the production worker. It checks the original sealed executable inode, all
declared guest inputs and their seals, refused writes, dynamic-library loading,
no unexpected inherited descriptors and owned child/group extinction. The
original FD-bind startup failure is retained separately from its repair.

Before promotion, collect actual owned native startup, streaming,
cancel/recovery, off/handoff and unload evidence at the tested tree. A separate
narrow GPU policy, runtime compatibility, full voice-stack VRAM measurements,
dialogue quality and original Stage B acceptance remain required. None is
replaced by passing these deterministic tests.
