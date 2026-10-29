# Optional whole-chat prompt measurement

Contract for #119, a prerequisite for optional bounded Converse recall in
clarification.md §12. This adds a measurement capability, not memory injection,
model acceptance, a new default or improved conversational recall.

## Exact request and result

The optional async provider method `measure_prompt(messages)` returns the frozen
`packages.providers.prompt_budget.PromptTokenCount`, or raises the fixed
`PromptCountUnavailable`. The ordinary TextProvider interface stays unchanged.
Unsupported providers remain usable for ordinary dialogue. ResourceBoundProvider
delegates only for local Converse, checks policy before and after the await,
and refuses unsupported capabilities. No Agent or Decisions measurement path.

The managed GGUF adapter POSTs `chat/completions/input_tokens` under its existing
resource `/v1` base. The body is exactly its ordinary streaming generation body:
`model`, complete ordered `messages`, protected `max_tokens`, and `stream: true`.
No omitted personality/history, truncation, alternate template flags, retry,
generation request or guessed byte-to-token conversion is allowed. Existing
512 KiB JSON / 32-message / 128 KiB decoded-content limits still apply.

The authenticated host route `/v1/chat/completions/input_tokens` accepts owner or
Converse service credentials, authenticates before receiving bounded JSON, and
uses the existing GGUF parser. Success is no-store JSON with exactly:
`version: 1`, positive safe-integer `input_tokens`, protected `context_tokens`,
`reserved_output_tokens` equal to max_new_tokens, `request_sha256` and
`binding_sha256`. Counts may exceed context. `fits` means precisely
input_tokens + reserved_output_tokens <= context_tokens; exact equality fits.

Request SHA256 is canonical JSON (sorted keys, compact separators, unescaped
Unicode encoded as strict UTF-8) of the exact generation body. Binding SHA256
uses the same canonical form of provider, model, profile, manifest_sha256,
context_tokens and max_new_tokens. It excludes the client-side resources URL;
the alias is already in the request digest. The provider validates both hashes
and both limits against its captured request/configuration after actual close.
Wire JSON is bounded to 4096 bytes, strict UTF-8, no duplicate/nonfinite fields,
exact keys and types. Errors expose no input, raw backend errors or paths.

The client uses a logical timeout of min(configured native deadline, 10 seconds)
plus five seconds of transport grace. Normal responses close before validation
or success. Retaining real client close through cancellation uses the existing
`owned_http_stream_cleanup` caller context; its close can outlast that logical
deadline. Ordinary-context cancellation does not establish retained client
cleanup. The resource broker independently owns native work and release in both
cases, so closing a client request cannot admit a replacement native operation.

## Resident worker and retained ownership

Measurement reserves the same single ManagedTextBroker operation as generation,
including retained cleanup. It never invokes admit/start, before_start, memory
allocation admission, model download or loading. GGUFProtocol.resident returns
only the coordinator's existing owned GGUF identity under its admission lock;
absent, changed or nonready workers refuse. Existing policy, peer/lease and
directory identity checks remain authoritative throughout the request.

GGUFEndpoint.measure_prompt(messages, lease) sends exactly the same protected
native JSON body as stream, but only to the native count route above, over the
existing verified owned Unix transport. It returns a positive safe integer only
after native EOF and real response/client/socket close plus currentness checks.
Native success is exactly `input_tokens` and `object: "response.input_tokens"`
(the shape in upstream's OpenAI chat route). Bound all response bodies to 4096
bytes; reject compression, redirects, duplicate/nonfinite/malformed JSON and
wrong counts/types/keys. Native 404/405/501 means unsupported: consume the bounded
body, close fully, recheck identity, then return None. GGUFProtocol.measure_prompt
wraps a count in PromptTokenCount using its protected configuration and exact
body, or returns None for unsupported.

Broker.measure_prompt(request) shares bounded body parsing and operation
ownership, starts a disconnect/policy/deadline monitor, awaits the protocol work
and retains actual close. No successful result precedes real close. Unsupported
after successful close releases gracefully and returns fixed 503, preserving
the healthy resident worker. A cold worker also returns fixed 503 without stop
or start. Cancellation, disconnect, owner-off, stale identity, deadline or other
native failure abort through the existing captured-worker stop and retained
cleanup barrier. Unconfirmed cleanup stays busy and cannot admit replacement.
The count deadline is min(existing broker timeout, 10 seconds), including body
receipt. No automatic retry or generation occurs.
After joining graceful monitor/disconnect retirement, recheck the captured
policy and worker again before returning a count or unsupported result. A
completed native close is not permission to publish after an awaited revocation.

## Compatibility and verification

Upstream llama.cpp [count routing](https://github.com/ggml-org/llama.cpp/blob/a4699400e/tools/server/server.cpp)
uses [the chat parameter parser and tokenization](https://github.com/ggml-org/llama.cpp/blob/a4699400e/tools/server/server-context.cpp)
without scheduling a generation task. Compatibility with the installed fork is
unverified. Test transports establish Kinaiya behavior, not native tokenizer
correctness or installed support. This change performs no model execution.

Measurement is a snapshot, not a generation reservation. A later recall caller
must recheck its runtime/provider/memory generation after awaiting it and omit
an optional complete recall closure if unsupported or too large. Native
generation limits remain final. This slice changes no dialogue prompt, memory
state, defaults, hardware policy, worker arguments or Qwen IPC.

Root owns this contract/shared result and the resource broker/coordinator/API.
Visible Muse Contributor xhigh owns services/resources/gguf.py plus a new
tests/unit/test_gguf_prompt_measurement.py. Codex owns packages/providers/gguf.py,
services/resources/providers.py and a new tests/unit/test_prompt_measurement_provider.py.
Tests use synthetic transports only, cover exact boundary/template-inclusive
counts, malformed/binding errors, unsupported/cold workers, zero generation,
held/failed close, cancellation and policy revocation. Independent source and
lifecycle review plus full CI precede merge. Human voice testing remains deferred.
