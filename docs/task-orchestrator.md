# Executable task orchestration

`task_orchestrator.v1` is an optional generated task program, separate from
`context_selector.v1`. A selector retrieves file references. A task program
actually controls a bounded sequence of native media consultations and consumes
their observed results. Neither execution nor a model claim establishes task
success, independent quality evaluation, promotion or release qualification.

## Native configuration and authority

The default host does not advertise this operation. Trusted owner wiring may
set `runtime.task_harness_broker_factory` to an object implementing:

- `available(runtime, task) -> bool`, a strict native declaration without model
  discovery, network requests or broker construction;
- `factory(runtime, record, current) -> WorkspaceMediaBroker`, where `current()`
  synchronously raises if the captured native task authority has changed.

An enrolled factory may also expose `describe(runtime, task)`. The native owner
captures its bounded source path, SHA-256, byte count, allowed operations and
required operations once before the first inference. This passive JSON metadata
appears in the seed task context so Agent can name the exact media request without
reading binary video as text. No prompt, grant ID, credentials or provider results
are accepted in the descriptor. It cannot authorize a call; the broker still
checks the original grant. Default hosts retain their existing prompt shape.

The broker owns the exact media source grant, operation/model targets and
primary-result disclosure target. Candidate code never receives the broker,
credentials, grant constructors or permission callbacks. Missing source or
primary-result authority disables the operation. A content-scope label, filename,
model output or generated program cannot create a grant.

The grant may also declare required operations as a unique subset of its allowed
targets. The broker exposes their native frozen set; a harness result is refused
unless actual validated receipts cover every required operation. The ASL demo
grant requires both `sam_hands` and `transcribe_video`. Candidate output cannot
supply or weaken this requirement.

Media consultation uses the separate strict broker contract. The program selects
only `sam_hands` or `transcribe_video` for the exact granted workspace path and
SHA-256. Model, reasoning and consultation prompt come from trusted target
configuration. The source grant separately names the exact primary provider/model
eligible to receive source-derived results. No ordinary text, workspace, skill,
plugin or MCP disclosure policy is broadened.

## Agent invocation

The Agent must first author the Python source through its ordinary `write_file`
tool in the current objective revision. The write result supplies its actual
SHA-256. A pre-existing file or model-supplied evidence is insufficient.

```json
{
  "tool": "run_task_harness",
  "arguments": {
    "source_path": "asl_task.py",
    "source_sha256": "<actual 64-character source SHA-256>",
    "input": {
      "operation": "transcribe_video",
      "source_path": "authorized-video.mp4",
      "source_sha256": "<actual 64-character media SHA-256>"
    }
  }
}
```

Legacy `ToolAction` and its exported schema remain unchanged. The supervisor
parses the additive action only when its native snapshot advertises it. The
typed Meta wire uses `harness_input_json` to encode the logical `input` object;
the provider adapter restores that object before native validation. Existing
default snapshots and provider schemas omit the new branch.

## Generated program

A source file defines synchronous `run(payload)`. Each isolated phase receives
`contract`, native task/session/thread/epoch/revision `binding`, immutable
`source_sha256`, original `input`, previous `state` and the latest actual broker
`observation`. It returns exactly one of:

```python
{
    "op": "consult_media",
    "request": {
        "operation": "transcribe_video",
        "source_path": "authorized-video.mp4",
        "source_sha256": "<actual media SHA-256>",
    },
    "state": {"phase": 1},
}

{"op": "finish", "result": {"transcript": "<derived observed result>"}}
```

The host runs each phase through the existing pure-compute bubblewrap and
libseccomp boundary. It mounts only copied candidate source, bounded JSON and
the trusted runner. The generated process has no host workspace, protected
core/evaluator, credentials, network or supervisor IPC. Kernel syscall rules
deny opening files, sockets and processes. Missing isolation refuses execution.
Returned operations are validated untrusted input to the native broker.

Bounds are 64 KiB source, 64 KiB original input, 128 KiB total phase payload,
64 KiB state/action/final result, depth32, 16,384 JSON nodes and JSON-safe finite
numbers. There are at most16 phases and15 consultation attempts. Each isolated
compute phase retains the existing10-second wall/CPU and256MiB bounds. Provider
deadlines and accounting remain the native media adapter's responsibility.

## Receipts, cancellation and completion

The host captures immutable source bytes and requires matching native file-write
evidence. It binds every phase to task/session/thread/epoch/objective revision,
workspace, factory, permissions and disclosure state. Native checks run around
awaits and before broker dispatch. Each observed consultation receives a native
`agent.tool` entry (`phase: media_consultation`) with source, task, logical request, actual
provider request/response and result digests. Raw provider bodies and credentials
are excluded. The result returned to Agent contains actual consultation receipt
metadata and clearly marks `task_completion: not_established`.

One execution is admitted per objective revision. Repeating the identical tool
action reads its original completed result; a changed action or an original
failed/uncertain execution cannot dispatch a replacement. Source or provider
failure never triggers an automatic fallback/retry. The media broker separately
owns its original operation and prevents replay.

Cancellation signals the actual compute thread, kills/reaps its sandbox through
the existing runner and joins that original work. The service observes the sole
broker close; an unconfirmed close retains the original broker and native busy
state. Cleanup failures do not create a usable result. A failed required harness
cannot be hidden by an unrelated successful read, write or UI publication.

Source-derived results retain their exact native primary-export check. The
existing request admission fence rechecks it before later model count/generation
dispatch, including when original result history has been compressed or omitted.
Withdrawal stops later export; it does not erase historical receipts or grant a
different model access.

Private media results also impose a native publication floor. Subsequent
artifact and outcome entries, including the persisted Task snapshot, retain
`private_cloud_authorized` rather than inheriting a public label from the task's
initial context. Artifact storage and final verification use that projected
scope. Every new artifact for a harness-enabled task remembers its exact native
publication scope. An earlier public placeholder remains independently verifiable
alongside a later private artifact after the terminal Task becomes private.
Artifact verification requires the projected Task scope to match protected storage;
legacy receipts retain their existing same-scope behavior. The ordinary scope policy still refuses private entries for
Meta/Contributor retrieval. The exact primary-result grant is separate and does
not authorize arbitrary private task history. Local owner/Converse display
continues through existing local disclosure controls.

The Agent can publish the derived result with ordinary `publish_ui` and propose
`finish`. Existing native artifact checks, work evidence, objective revision and
terminal-outcome confirmation remain authoritative. This interface does not
replace the independent selector evaluator or permit generated code to modify it.

## Verification boundary

The new regression modules are
`tests/unit/test_task_harness.py` and
`tests/integration/test_agent_task_harness.py`. Their provider/broker results are
synthetic. Actual sandbox checks exercise generated computation and kernel
denials; they do not qualify ASL recognition, a real video, Meta inference,
physical devices, UI playback or a successful live demonstration. Record actual
commands and outcomes separately after independent source review.
