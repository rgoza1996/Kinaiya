"""Bounded truthful CompatibleTextProvider adapter; synthetic MockTransport only."""

import asyncio
import json

import httpx
import pytest

from packages.providers.text import (
    CompatibleTextProvider,
    ProviderCleanupError,
    ProviderError,
    ProviderRefusal,
    owned_http_stream_cleanup,
)

MSGS = [{"role": "user", "content": "Synthetic"}]


def _provider(raw: bytes, headers=None):
    def respond(request):
        assert json.loads(request.content)["model"] == "synthetic"
        body = json.loads(request.content)
        assert set(body) == {"model", "messages", "max_tokens", "stream"}
        return httpx.Response(200, stream=_Frag([raw]), headers=headers or {})

    return CompatibleTextProvider(
        "http://localhost/v1", "synthetic", transport=httpx.MockTransport(respond)
    )


def _chunk(data: bytes, n=1):
    return [data[i : i + n] for i in range(0, len(data), n)]


class _Frag(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    async def __aiter__(self):
        for c in self.chunks:
            yield c

    async def aclose(self):
        self.closed = True


def _frag_provider(chunks, headers=None):
    stream = _Frag(chunks)

    def respond(request):
        return httpx.Response(200, stream=stream, headers=headers or {})

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic", transport=httpx.MockTransport(respond)
    )
    return provider, stream


def _ev(payload, space=True):
    sep = "data: " if space else "data:"
    return (sep + json.dumps(payload) + "\n\n").encode()


def _stop_event(text=None):
    delta = {} if text is None else {"content": text}
    return _ev({"choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}]})


async def _collect(provider):
    out = []
    async for piece in provider.stream(MSGS):
        out.append(piece)
    return "".join(out)


async def test_complete_normal_returns_text():
    body = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello."},
                "finish_reason": "stop",
            }
        ]
    }
    p = _provider(json.dumps(body).encode())
    try:
        assert await p.complete(MSGS) == "Hello."
    finally:
        await p.close()


async def test_complete_empty_string_preserved():
    body = {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}
        ]
    }
    p = _provider(json.dumps(body).encode())
    try:
        assert await p.complete(MSGS) == ""
    finally:
        await p.close()


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {"content": "x"}}]},
        {"choices": [{"index": 1, "message": {"content": "x"}, "finish_reason": "stop"}]},
        {"choices": [{"index": 0, "message": {"content": "x"}, "finish_reason": "length"}]},
        {"choices": [{"index": 0, "message": {"content": None}, "finish_reason": "stop"}]},
        {
            "choices": [
                {
                    "index": 0,
                    "message": {"content": "x", "tool_calls": []},
                    "finish_reason": "stop",
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "message": {"content": "x", "refusal": "no"},
                    "finish_reason": "stop",
                }
            ]
        },
    ],
)
async def test_complete_refusals(body):
    p = _provider(json.dumps(body).encode())
    try:
        with pytest.raises(ProviderRefusal):
            await p.complete(MSGS)
    finally:
        await p.close()


async def test_complete_malformed_json_refuses():
    p = _provider(b"{bad json")
    try:
        with pytest.raises(ProviderRefusal):
            await p.complete(MSGS)
    finally:
        await p.close()


async def test_complete_wire_overflow_refuses():
    p = _provider(b"x" * (1048576 + 1))
    try:
        with pytest.raises(ProviderRefusal):
            await p.complete(MSGS)
    finally:
        await p.close()


async def test_complete_encoded_refuses():
    body = {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}
        ]
    }
    p = _provider(json.dumps(body).encode(), headers={"content-encoding": "gzip"})
    try:
        with pytest.raises(ProviderRefusal):
            await p.complete(MSGS)
    finally:
        await p.close()


async def test_complete_http_error_is_provider_error():
    def respond(request):
        return httpx.Response(500, content=b"boom")

    p = CompatibleTextProvider(
        "http://localhost/v1", "synthetic", transport=httpx.MockTransport(respond)
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await p.complete(MSGS)
        assert not isinstance(caught.value, ProviderRefusal)
    finally:
        await p.close()


async def test_stream_normal_stop_done():
    raw = _ev({"choices": [{"index": 0, "delta": {"content": "Hi "}}]})
    raw += _ev({"choices": [{"index": 0, "delta": {"content": "there"}}]})
    raw += _stop_event() + b"data: [DONE]\n\n"
    p = _provider(raw)
    try:
        assert await _collect(p) == "Hi there"
    finally:
        await p.close()


async def test_stream_usage_only_allowed_before_done():
    raw = _ev({"choices": [{"index": 0, "delta": {"content": "A"}}]})
    raw += _ev({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    raw += _stop_event() + b"data: [DONE]\n\n"
    p = _provider(raw)
    try:
        assert await _collect(p) == "A"
    finally:
        await p.close()


async def test_stream_optional_space_multiline_crlf_comments_fragmented_utf8():
    payload = {"choices": [{"index": 0, "delta": {"content": "héllo"}}]}
    text = json.dumps(payload, ensure_ascii=False)
    raw = b": comment line\r\n"
    raw += b"event: message\r\nid: 1\r\nretry: 100\r\n"
    # A newline between JSON fields is valid assembled multiline SSE data.
    text = text.replace('{"choices": ', '{"choices":\ndata:')
    raw += ("data:" + text + "\r\n\r\n").encode()
    raw += _stop_event() + b"data:[DONE]\n\n"
    chunks = _chunk(raw, 1)
    p, _ = _frag_provider(chunks)
    try:
        assert await _collect(p) == "héllo"
    finally:
        await p.close()


async def test_stream_role_only_delta_allowed():
    raw = _ev({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    raw += _ev({"choices": [{"index": 0, "delta": {"content": "ok"}}]})
    raw += _stop_event() + b"data: [DONE]\n\n"
    p = _provider(raw)
    try:
        assert await _collect(p) == "ok"
    finally:
        await p.close()


async def test_stream_retains_prefix_on_length_refusal():
    seen = []

    async def consume(provider):
        async for piece in provider.stream(MSGS):
            seen.append(piece)

    raw = _ev({"choices": [{"index": 0, "delta": {"content": "part"}}]})
    raw += _ev({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
    raw += b"data: [DONE]\n\n"
    p = _provider(raw)
    try:
        with pytest.raises(ProviderRefusal):
            await consume(p)
        assert "".join(seen) == "part"
    finally:
        await p.close()


@pytest.mark.parametrize(
    "raw",
    [
        _ev({"choices": [{"index": 0, "delta": {"content": "x"}}]}) + b"data: [DONE]\n\n",
        _ev({"choices": [{"index": 0, "delta": {"content": "x"}}]})
        + _stop_event()
        + _stop_event()
        + b"data: [DONE]\n\n",
        _ev({"choices": [{"index": 0, "delta": {"content": "x"}}]})
        + _stop_event()
        + b"data: [DONE]\n\ndata: [DONE]\n\n",
        _stop_event()
        + _ev({"choices": [{"index": 0, "delta": {"content": "late"}}]})
        + b"data: [DONE]\n\n",
        _ev({"choices": [{"index": 0, "delta": {"content": "x"}}]}),
        _ev({"choices": [{"index": 0, "delta": {"content": "x"}}]}) + _stop_event(),
    ],
)
async def test_stream_terminal_order_failures(raw):
    p = _provider(raw)
    try:
        with pytest.raises(ProviderRefusal):
            await _collect(p)
    finally:
        await p.close()


async def test_stream_data_after_done_rejected():
    raw = _ev({"choices": [{"index": 0, "delta": {"content": "x"}}]})
    raw += _stop_event() + b"data: [DONE]\n\n" + _ev({"choices": [], "usage": {"a": 1}})
    p = _provider(raw)
    try:
        with pytest.raises(ProviderRefusal, match="after DONE"):
            await _collect(p)
    finally:
        await p.close()


async def test_stream_comments_after_done_harmless():
    raw = _ev({"choices": [{"index": 0, "delta": {"content": "x"}}]})
    raw += _stop_event() + b"data: [DONE]\n\n: trailing comment\n\n"
    p = _provider(raw)
    try:
        assert await _collect(p) == "x"
    finally:
        await p.close()


@pytest.mark.parametrize(
    "raw",
    [
        b"data: {bad}\n\n",
        b"data: [1,2]\n\n",
        b'data: "str"\n\n',
        _ev({"choices": [{"index": 0, "delta": {"content": 5}}]}),
        _ev({"choices": [{"index": 0, "delta": "x"}]}),
        _ev(
            {
                "choices": [
                    {"index": 0, "delta": {"content": "a"}},
                    {"index": 1, "delta": {"content": "b"}},
                ]
            }
        ),
        _ev({"choices": [{"index": 0, "delta": {"tool_calls": []}}]}),
        _ev({"choices": [{"index": 0, "delta": {"refusal": "no"}}]}),
        _ev(
            {
                "choices": [
                    {"index": 0, "delta": {"content": "x"}, "finish_reason": "content_filter"}
                ]
            }
        ),
        _ev({"choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "tool_calls"}]}),
        _ev({"choices": []}),
        b"data: \xff\n\n",
    ],
)
async def test_stream_malformed_types_refused(raw):
    p = _provider(raw + _stop_event() + b"data: [DONE]\n\n")
    try:
        with pytest.raises(ProviderRefusal):
            await _collect(p)
    finally:
        await p.close()


async def test_stream_wire_overflow():
    p = _provider(b"x" * (1048576 + 1))
    try:
        with pytest.raises(ProviderRefusal):
            await _collect(p)
    finally:
        await p.close()


async def test_stream_line_overflow():
    raw = b"data: " + b"y" * 65536 + b"\n\n" + _stop_event() + b"data: [DONE]\n\n"
    p = _provider(raw)
    try:
        with pytest.raises(ProviderRefusal):
            await _collect(p)
    finally:
        await p.close()


async def test_stream_event_overflow():
    # Multiple individually bounded lines can exceed the assembled-event cap.
    raw = b"data: " + b" " * 33000 + b"\ndata: " + b" " * 33000 + b"\n\n"
    p = _provider(raw)
    try:
        with pytest.raises(ProviderRefusal, match="event bound"):
            await _collect(p)
    finally:
        await p.close()


async def test_stream_text_overflow():
    raw = _ev({"choices": [{"delta": {"content": "w" * 32768}}]}) * 8
    raw += _ev({"choices": [{"delta": {"content": "w"}}]})
    p = _provider(raw)
    try:
        with pytest.raises(ProviderRefusal, match="text bound"):
            await _collect(p)
    finally:
        await p.close()


async def test_stream_event_count_overflow():
    parts = []
    for _ in range(8193):
        parts.append(_ev({"choices": [], "usage": {"a": 1}}))
    parts.append(_stop_event() + b"data: [DONE]\n\n")
    p = _provider(b"".join(parts))
    try:
        with pytest.raises(ProviderRefusal):
            await _collect(p)
    finally:
        await p.close()


async def test_stream_encoded_refuses():
    raw = _ev({"choices": [{"index": 0, "delta": {"content": "x"}}]})
    raw += _stop_event() + b"data: [DONE]\n\n"
    p = _provider(raw, headers={"content-encoding": "gzip"})
    try:
        with pytest.raises(ProviderRefusal):
            await _collect(p)
    finally:
        await p.close()


async def test_stream_http_error_is_provider_error():
    def respond(request):
        return httpx.Response(500, content=b"nope")

    p = CompatibleTextProvider(
        "http://localhost/v1", "synthetic", transport=httpx.MockTransport(respond)
    )
    try:
        with pytest.raises(ProviderError):
            await _collect(p)
    finally:
        await p.close()


async def test_stream_refusal_is_provider_refusal_not_retryable_as_error():
    raw = _ev({"choices": [{"index": 0, "delta": {"content": "x"}}]})
    raw += (
        _stop_event()
        + b"data: [DONE]\n\n"
        + b"data: "
        + json.dumps({"choices": [{"index": 0, "delta": {"content": "late"}}]}).encode()
        + b"\n\n"
    )
    p = _provider(raw)
    try:
        with pytest.raises(ProviderRefusal):
            await _collect(p)
    finally:
        await p.close()
    assert issubclass(ProviderRefusal, ProviderError)


async def test_owned_cleanup_closes_after_terminal_refusal():
    class Held(httpx.AsyncByteStream):
        def __init__(self):
            self.release = asyncio.Event()
            self.closed = False

        async def __aiter__(self):
            yield _ev({"choices": [{"index": 0, "delta": {"content": "held"}}]})
            yield _ev({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})

        async def aclose(self):
            self.closed = True

    held = Held()

    def respond(request):
        return httpx.Response(200, stream=held)

    p = CompatibleTextProvider(
        "http://localhost/v1", "synthetic", transport=httpx.MockTransport(respond)
    )
    try:
        with owned_http_stream_cleanup():
            with pytest.raises(ProviderRefusal):
                await _collect(p)
        assert held.closed
    finally:
        await p.close()


@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_final_delta_with_text_validates_finish_before_publication(finish):
    # Append a second stop only for the invalid length case. It cannot repair
    # an earlier invalid terminal, even when that event also contains text.
    raw = _ev({"choices": [{"delta": {"content": "last"}, "finish_reason": finish}]})
    if finish == "length":
        raw += _stop_event()
    p = _provider(raw + b"data: [DONE]\n\n")
    seen = []
    try:
        if finish == "length":
            with pytest.raises(ProviderRefusal):
                async for text in p.stream(MSGS):
                    seen.append(text)
            assert seen == []
        else:
            assert await _collect(p) == "last"
    finally:
        await p.close()


@pytest.mark.parametrize("index", [False, True, 0.0, "0", [], {}])
async def test_choice_index_requires_exact_integer(index):
    p = _provider(
        _ev({"choices": [{"index": index, "delta": {"content": "x"}}]})
        + _stop_event()
        + b"data: [DONE]\n\n"
    )
    try:
        with pytest.raises(ProviderRefusal):
            await _collect(p)
    finally:
        await p.close()


@pytest.mark.parametrize("value", ["user", "tool", [], {}])
async def test_non_assistant_message_role_is_refused(value):
    p = _provider(
        _ev({"choices": [{"delta": {"role": value, "content": "x"}}]})
        + _stop_event()
        + b"data: [DONE]\n\n"
    )
    try:
        with pytest.raises(ProviderRefusal):
            await _collect(p)
    finally:
        await p.close()


@pytest.mark.parametrize(
    "raw",
    [
        b'data: {"choices":[],"usage":{"n":NaN}}\n\n',
        b'data: {"choices":[],"choices":[],"usage":{}}\n\n',
        _ev({"choices": [{"delta": {"content": "\ud800"}}]}),
    ],
)
async def test_nonfinite_duplicate_or_unpaired_unicode_is_nonretryable(raw):
    p = _provider(raw + _stop_event() + b"data: [DONE]\n\n")
    try:
        with pytest.raises(ProviderRefusal):
            await _collect(p)
    finally:
        await p.close()


@pytest.mark.parametrize("tail", [b"data: [DONE]", b"data: [DONE]\n"])
async def test_unterminated_sse_event_is_not_dispatched_at_eof(tail):
    p = _provider(_stop_event() + tail)
    try:
        with pytest.raises(ProviderRefusal):
            await _collect(p)
    finally:
        await p.close()


def _padding_comments(size):
    parts = []
    while size:
        count = min(size, 60000)
        parts.append(b"\n" if count == 1 else b":" + b"p" * (count - 2) + b"\n")
        size -= count
    return b"".join(parts)


@pytest.mark.parametrize("boundary", ["wire", "line", "event", "text", "count"])
@pytest.mark.parametrize("over", [0, 1])
async def test_real_inclusive_stream_bounds(boundary, over):
    suffix = _stop_event() + b"data: [DONE]\n\n"
    expected = ""
    if boundary == "wire":
        raw = _padding_comments(1048576 + over - len(suffix)) + suffix
    elif boundary == "line":
        base = json.dumps({"choices": [], "usage": {}, "pad": ""}).encode()
        payload = base.replace(
            b'"pad": ""', b'"pad": "' + b"p" * (65536 + over - 5 - len(base)) + b'"'
        )
        assert len(b"data:" + payload) == 65536 + over
        raw = b"data:" + payload + b"\r\n\r\n" + suffix
    elif boundary == "event":
        base = b'{"choices":[],\n"usage":{},"pad":""}'
        payload = base[:-2] + b"p" * (65536 + over - len(base)) + base[-2:]
        assert len(payload) == 65536 + over
        raw = b"data:" + payload.replace(b"\n", b"\ndata:") + b"\n\n" + suffix
    elif boundary == "text":
        # Count actual multibyte UTF-8 text, not characters or JSON escapes.
        expected = "é" * 131072
        raw = b"".join(
            _ev({"choices": [{"delta": {"content": expected[i : i + 4096]}}]})
            for i in range(0, len(expected), 4096)
        )
        if over:
            raw += _ev({"choices": [{"delta": {"content": "x"}}]})
        raw += suffix
    else:
        raw = _ev({"choices": [], "usage": {}}) * (8190 + over) + suffix
    # Fragment CR from LF at the exact line boundary as well.
    p, _ = _frag_provider([raw[:65537], raw[65537:]])
    try:
        if over:
            with pytest.raises(
                ProviderRefusal, match=boundary if boundary != "count" else "event count"
            ):
                await _collect(p)
        else:
            assert await _collect(p) == expected
    finally:
        await p.close()


@pytest.mark.parametrize("method", ["complete", "stream"])
@pytest.mark.parametrize("failure", ["terminal", "encoding"])
async def test_refusal_close_is_retained_through_repeated_cancellation(method, failure):
    class Held(_Frag):
        def __init__(self, body):
            super().__init__([body])
            self.closing, self.release = asyncio.Event(), asyncio.Event()
            self.cancelled = False

        async def aclose(self):
            self.closing.set()
            try:
                await self.release.wait()
                self.closed = True
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    body = (
        json.dumps({"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}).encode()
        if method == "complete"
        else _ev({"choices": [{"delta": {}, "finish_reason": "length"}]})
    )
    held = Held(body)
    headers = {"content-encoding": "gzip"} if failure == "encoding" else {}
    p = CompatibleTextProvider(
        "http://localhost/v1",
        "synthetic",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=held, headers=headers)),
    )

    async def consume():
        with owned_http_stream_cleanup():
            return await p.complete(MSGS) if method == "complete" else await _collect(p)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(held.closing.wait(), 1)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done() and not held.cancelled and not held.closed
        held.release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert held.closed and not held.cancelled
    finally:
        held.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await p.close()


@pytest.mark.parametrize("method", ["complete", "stream"])
async def test_actual_close_failure_takes_precedence_over_terminal_refusal(method):
    class FailedClose(_Frag):
        async def aclose(self):
            raise OSError("Synthetic close failure")

    body = (
        json.dumps({"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}).encode()
        if method == "complete"
        else _ev({"choices": [{"delta": {}, "finish_reason": "length"}]})
    )
    p = CompatibleTextProvider(
        "http://localhost/v1",
        "synthetic",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=FailedClose([body]))),
    )
    try:
        with owned_http_stream_cleanup(), pytest.raises(ProviderCleanupError):
            if method == "complete":
                await p.complete(MSGS)
            else:
                await _collect(p)
    finally:
        await p.close()
