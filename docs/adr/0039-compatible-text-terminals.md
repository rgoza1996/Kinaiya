# ADR 0039: require native compatible-text completion evidence

Tracks [#99](https://github.com/rgoza1996/Kinaiya/issues/99), found during
the local Converse investigation for #49/#97. This repairs an existing
provider boundary; managed GGUF integration and dialogue quality remain open.

## Failure and behavior

The previous compatible adapter returned text after a bare streaming EOF,
ignored `finish_reason=length`, and accepted nonstream text without checking
its finish reason. Three synthetic baseline probes reproduced those failures.

Nonstream responses now require one text choice, a string message and native
`stop`. Streaming responses require one text choice, exactly one `stop`, then
`[DONE]`, followed by HTTP EOF. A final event may carry both text and `stop`.
Bounded usage-only events remain permitted before `[DONE]`. New content after
stop, duplicate terminals, trailing data, unsupported tool/refusal output,
malformed UTF-8/JSON, nonfinite numbers and duplicate JSON fields are refused.
SSE supports fragmented UTF-8/CRLF, comments, optional field spacing and
multiline data. EOF does not dispatch an unfinished data event.

Limits are inclusive: 1 MiB entity-body bytes, 64 KiB per SSE line excluding
its CRLF/LF delimiter, 64 KiB per assembled data event, 256 KiB aggregate
UTF-8 output text, and 8,192 data events including usage, terminal and DONE.
Nonstream JSON has the same body/text limits. Bounds apply before buffer
growth; assembled-event accounting is incremental. Identity encoding is
requested and compressed responses are refused before reading. Configured
HTTP timeouts and caller deadlines still apply while waiting for EOF.

Deterministic protocol failures raise `ProviderRefusal`, so Decisions cannot
retry them as transient transport errors. HTTP/transport failures remain
`ProviderError`. No response is repaired, retried or replaced with canned
text. Already-published Converse sentences remain in the journal; an error
replaces the successful completion event. This is generation evidence, not
authority for Agent actions or task completion.

Both response modes retain the existing opt-in HTTP cleanup owner before
reads, including header/parser refusal. Cancellation cannot discard that
owner while real close is pending. Actual close failure remains
`ProviderCleanupError`. Provider identity, request JSON, disclosure checks,
Meta, managed Qwen, resource leases and model defaults are unchanged. The
compatible client explicitly disables environment proxies and redirects.

## Verification and limits

234 focused provider, Converse, retained cleanup, window and Decisions
backlog checks passed on Python 3.12. Ruff and formatting checks cover the
changed Python files. Tests include inclusive/overflow bounds, final text
with a finish reason, terminal order, actual split UTF-8/CRLF, malformed
types, held close through repeated cancellation, and close failure priority.
Native caller tests retain the exact published sentence on refusal and prove
one Decisions request, durable error, no dispatch and no retry after reopening
the journal.

A local replay of 16 retained native llama.cpp SSE captures accepted all 15
normal stopped responses with their exact raw text and refused the one native
length-limited response. This used synthetic HTTP transport and made no new
model or Meta calls. Replay receipt SHA-256:
`62dd3422bc17fce40083df0a3855bb53d79a7be3e4584c9572ed7025b8bc3a28`.
This is framing regression evidence; the prior dialogue-quality failures are
unchanged. It proves neither a live managed GGUF worker nor the owner's 8 GB
combined STT/LLM/TTS VRAM target, realtime voice, physical playback or full M2.

The genuine visible Muse Contributor xhigh implementation session exited
normally. Its first delivery failed 44 of 59 provider checks; root repairs
and independent caller tests are distinguished from that original delivery.
The original files, CLI output, baseline failure receipt and review records
remain preserved locally. No failed criteria were redefined: an erroneous
fixture that rejected a valid omitted choice index was corrected to test a
missing finish reason, and masked boundary fixtures were replaced with real
inclusive/overflow cases. A wrong verification environment path and an initial
replay-script type error are retained as preparation failures.

The implementation used 25 Contributor calls, estimated at $0.012618768.
Combined catalog estimate at this checkpoint was $1.091234956; the conservative
guard plus retained uncertainty was $3.738279962 of the shared $25 authorization.
Provider billing was unavailable; these are estimates, not invoices. The CLI
requested xhigh; backend effort was not independently reported.
