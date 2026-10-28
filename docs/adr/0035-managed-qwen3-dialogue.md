# ADR 0035: opt-in managed Qwen3 dialogue

Tracks [#91](https://github.com/rgoza1996/Kinaiya/issues/91). Status: shared
implementation contract; the managed provider is not yet implemented or
verified. Full M2 and persistent STT #49 remain open.

Converse may select an explicit `qwen3` text kind alongside independently
selected STT and TTS. Agent/Decisions cannot select this kind. Optional native
imports stay in a direct resource-owned child. No model/download/default,
personality, disclosure, transcript or permission change is implied.

## Fixed worker contract

The worker id is `qwen3`. Fixed argv calls
`python -m packages.providers.qwen3_worker --socket-dir DIR --model-dir MODEL
--workspace AGENT --cpu-count 2 --memory-bytes BYTES --max-input-tokens N
--max-new-tokens M`. Initial configuration defaults: 16 GiB address space,
8 GiB host restore reserve, two-CPU affinity, 2,048 input and 128 output tokens,
120-second startup and request deadlines. Input configuration supports 64–4096
tokens and output 16–512, but actual supported capacity must be measured before
claiming it. No cumulative lifetime CPU limit applies to this resident child.

`qwen3_manifest.json` pins seven public model/card/license files and six
installed runtime core files. Runtime versions are Python 3.12, CPU-only
Torch 2.14.1+cpu and Transformers 5.21.0; the six-file check is not full build
equivalence. No assets or runtime binaries are redistributed.

Create the private listener before the required `deny_network()` fence. The
fence precedes optional runtime imports. In the child, copy and hash all seven
regular no-follow input files into sealed memfds; retain those FDs for the
model's lifetime. A private snapshot directory contains only the fixed model
filenames, symlinked to this child's sealed `/proc/self/fd/N` inputs. Native
loading therefore cannot consume later owner writes. Prepare those copies and
links before applying `RLIMIT_FSIZE(0,0)`, then verify runtime identity/import
and load the model under the file/core/address-space/network limits. The
owned parent removes only this fixed snapshot after confirmed group release.

`sealed_model(model_dir, workspace, snapshot_dir)` in `qwen3_assets` is a context
manager yielding that immutable snapshot path. `validate_runtime()` validates
the six installed file pins without importing Torch/Transformers and returns
the exact manifest runtime dictionary. `Qwen3Engine(snapshot_dir,
max_input_tokens=2048, max_new_tokens=128)` imports optional runtime lazily,
sets intra/inter-op threads to 2/1 and loads CPU float32/eager safetensors with
local-only, no remote code. Render with the tokenizer's template,
`enable_thinking=False`, and tokenize without extra special tokens. Refuse
oversized rendered prompts before generation; do not truncate context.

`engine.generate(messages)` yields dictionaries with `kind=delta,text=...`
and one `kind=done,finish_reason=eos|length,input_tokens,output_tokens` only
after the actual producer has joined. Use actual decoded Unicode text, not
token-callback timing as a text delta. A bounded queue (eight items), finite
token/byte limits and a process-owned failure path bound producer backpressure.
Input/output-limit exceptions map to fixed `input_limit`/`output_limit` codes;
other generation failures use `generation_failed`, with no raw error or prompt
forwarding. Closing a generator is not proof a native thread stopped; a
transport failure with a live producer terminates the dedicated child.

## IPC and parent integration

The implemented `qwen3_ipc` module defines exact version-1 shapes, manifest
identity, strict integer/UTF-8/role bounds and shared low-level framing/peer
checks. Every call passes an explicit byte limit to the framing helpers.
The listener sends the exact hello required by `parse_hello`: PID/incarnation,
model/revision/manifest, runtime dictionary, token settings,
`isolation=seccomp-network-v1` and `input_mode=sealed-safetensors-v1`.
Probe and generate requests follow `request_message`/`parse_request`.
Probe returns `event_message('ready',...)`; generation frames follow
`parse_event` with nonce and monotonically increasing sequence starting at 0.
Delta, done and fixed-code error shapes are distinct. EOF is never completion.
Both child and parent enforce total 32 KiB text, 8192-byte decoded deltas and
at most 4096 stream events including the terminal. The 64 KiB encoded-frame
bound accommodates JSON escaping of every valid decoded delta. Every event
requires an explicit valid expected nonce, including a probe response.
No queue, frame or deadline is unlimited.

The parent factory `qwen3_worker(settings, root, workspace)` returns fixed
WorkerSpec and endpoint. `Qwen3Endpoint(directory, max_input_tokens=...,
max_new_tokens=...)` exposes `probe`, `stream(messages, identity)`, `forget`
and `close`, retaining the same exact ready PID/incarnation/socket-inode
semantics as Whistle. No service credential enters the child environment.
The broker holds one active request or retained cleanup and streams a bounded
HTTP JSON-lines protocol at `POST /v1/converse/completions`, authenticated only
for owner/Converse. HTTP body is exactly `{"messages":[...]}`. Public HTTP
frames contain only `delta/text`, `done/finish_reason/input_tokens/output_tokens`
or fixed `error/code`; native identity stays internal. Length exhaustion is
explicitly surfaced by the provider rather than silently claimed as EOS.

`ManagedQwen3Provider(base_url, token, *, max_input_tokens=2048,
max_new_tokens=128, request_timeout_s=120, transport=None)` implements the
existing TextProvider protocol via that fixed loopback origin. Its info is
`provider=managed-qwen3,model=Qwen/Qwen3-0.6B,local=True,streaming=True`.
`complete()` collects the same stream. HTTP cleanup uses the existing owned
transport mechanism and retains uncertain cleanup rather than retrying.

`Qwen3Broker(coordinator, endpoint, request_timeout_s=...)` exposes `status`,
`stream(request)` and `close`. Before every admission and output boundary it
rechecks `coordinator.qwen3_policy()` against the captured resource/owner
revision. `await coordinator.admit_qwen3(expected_policy)` returns a real
WorkerIdentity or raises `Qwen3AdmissionError(cleanup_confirmed=...)`.
Cancel/disconnect/error/stale policy stops only that captured binding and
retains ownership until real direct-child and group release. Never replay
uncertain generation. A distinct new request can recover after proven release
and measured headroom; off/handoff wins awaited readiness.

Coordinator integration binds the selected owner-configured Qwen profile,
resources origin and manifest to protected Converse health metadata. Exact
configured bindings are required before treating local text as managed;
unknown or changed local models still refuse handoff. Startup configuration
identity is separate from a ready child; actual release/residency uses the
manager's verified identity and retained cleanup evidence. Existing Whistle
barrier/replay protections and default behavior remain acceptance constraints.

## Verification gates

The three frozen preflight generations only screen short synchronous CPU
requests. They do not prove this protocol, decoded streaming, cancellation,
snapshot compatibility or representative prompt capacity. Retain their
4,884,344 KiB whole-child peak and greeting brevity failure. Test malformed
identity/sequence/terminal data, limits, actual sealed files and process
cleanup, disconnect/backpressure and owner/handoff races. Then perform bounded
actual cold/warm streaming, cancellation/recovery and representative Converse
prompt admission with exact source/assets/runtime measurements and full CI.
No realtime, quality, p95, physical voice or default promotion is established
until its applicable broader acceptance evidence exists.
