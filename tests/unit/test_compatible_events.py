"""Validated compatible SSE data keeps its native lexical representation."""

import asyncio
from dataclasses import FrozenInstanceError

import httpx
import pytest

from packages.providers.text import (
    ProviderRefusal,
    _CompatibleStreamEvent,
    _iter_compatible_events,
    _iter_compatible_stream,
)

TEXT = '{"choices":[{"index":0,"delta":{"content":"ok"}}]}'
STOP = '{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'
DONE = "[DONE]"


def wire(data):
    return ("data: " + data + "\n\n").encode("utf-8")


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


async def collect(raw, iterator=_iter_compatible_events, *, fragmented=False):
    chunks = [raw[i : i + 1] for i in range(len(raw))] if fragmented else [raw]
    response = httpx.Response(200, stream=Chunks(chunks))
    try:
        return [event async for event in iterator(response)]
    finally:
        await response.aclose()


async def test_original_json_metadata_and_lexemes_survive_validation():
    role = ' { "id":"synthetic-1", "choices" : [{"delta":{"role":"assistant"}}] } '
    text = r'{"choices":[{"delta":{"content":"h\u00e9llo"}}], "native_number":1.2500}'
    usage = ' { "usage" : {"prompt_tokens":11,"completion_tokens":2}, "choices":[] } '
    final = '{"choices":[{"delta":{"content":"!"},"finish_reason":"stop"}]}'
    data = [role, text, usage, final, DONE]

    events = await collect(b"".join(wire(value) for value in data))

    assert all(isinstance(event, _CompatibleStreamEvent) for event in events)
    assert [event.data for event in events] == data
    assert [event.text for event in events] == [None, "héllo", None, "!", None]
    assert [event.done for event in events] == [False, False, False, False, True]


def test_validated_event_is_frozen():
    event = _CompatibleStreamEvent(TEXT, "ok", False)
    with pytest.raises(FrozenInstanceError):
        event.done = True


async def test_empty_delta_null_and_empty_text_remain_distinct_from_usage():
    data = [
        '{"choices":[{"delta":{}}]}',
        '{"choices":[{"delta":{"content":null}}]}',
        '{"choices":[{"delta":{"content":""}}]}',
        '{"choices":[{"delta":{"role":"assistant"}}]}',
        '{"choices":[{"delta":{"content":" "}}]}',
        '{"choices":[],"usage":{}}',
        STOP,
        DONE,
    ]
    raw = b"".join(wire(value) for value in data)

    events = await collect(raw)

    assert [event.data for event in events] == data
    assert [event.text for event in events] == [None, None, "", None, " ", None, None, None]
    assert [event.done for event in events] == [False] * 7 + [True]
    assert await collect(raw, _iter_compatible_stream) == [" "]


async def test_multiline_data_is_preserved_through_fragmented_crlf_and_utf8():
    assembled = '{ "choices": [\n{"index":0,"delta":{"content":"café"}}\n] }'
    raw = (
        b": synthetic heartbeat\r\n"
        b"id: event-1\r\nevent: message\r\nretry: 10\r\n"
        + (
            'data:{ "choices": [\r\n'
            'data: {"index":0,"delta":{"content":"café"}}\r\n'
            "data: ] }\r\n\r\n"
        ).encode()
        + wire(STOP)
        + b"data:[DONE]\r\n\r\n: trailing comment\n\n"
    )

    events = await collect(raw, fragmented=True)

    assert [event.data for event in events] == [assembled, STOP, DONE]
    assert [event.text for event in events] == ["café", None, None]
    assert [event.done for event in events] == [False, False, True]


@pytest.mark.parametrize(
    ("tail", "validated_tail"),
    [
        (b"", []),
        (wire(DONE), []),
        (wire(STOP), [STOP]),
        (wire(STOP) + b"data: [DONE]", [STOP]),
        (wire(STOP) + b"data: [DONE]\n", [STOP]),
        (wire(STOP) + wire(DONE) + wire(TEXT), [STOP, DONE]),
        (wire(STOP) + wire(DONE) + wire(DONE), [STOP, DONE]),
        (wire(STOP) + wire(STOP) + wire(DONE), [STOP]),
    ],
)
async def test_incomplete_or_trailing_data_does_not_become_validated_events(tail, validated_tail):
    response = httpx.Response(200, stream=Chunks([wire(TEXT) + tail]))
    seen = []
    try:
        with pytest.raises(ProviderRefusal):
            async for event in _iter_compatible_events(response):
                seen.append(event)
        assert [event.data for event in seen] == [TEXT, *validated_tail]
        assert [event.text for event in seen if event.text] == ["ok"]
    finally:
        await response.aclose()


@pytest.mark.parametrize(
    "invalid",
    [
        "{bad JSON}",
        '{"choices":[{"delta":{"content":"unpublished"},"finish_reason":"length"}]}',
        '{"choices":[{"delta":{"tool_calls":[]}}]}',
        '{"choices":[{"delta":{"refusal":"synthetic refusal"}}]}',
        '{"choices":[],"usage":[]}',
        r'{"choices":[{"delta":{"content":"\ud800"}}]}',
    ],
)
async def test_rejected_data_is_not_exposed_to_event_consumers(invalid):
    response = httpx.Response(
        200, stream=Chunks([wire(TEXT) + wire(invalid) + wire(STOP) + wire(DONE)])
    )
    seen = []
    try:
        with pytest.raises(ProviderRefusal):
            async for event in _iter_compatible_events(response):
                seen.append(event)
        assert seen == [_CompatibleStreamEvent(TEXT, "ok", False)]
    finally:
        await response.aclose()


@pytest.mark.parametrize("iterator", [_iter_compatible_events, _iter_compatible_stream])
async def test_done_event_does_not_bypass_actual_source_eof(iterator):
    class HeldEOF(Chunks):
        def __init__(self):
            super().__init__([wire(TEXT), wire(STOP), wire(DONE)])
            self.at_eof, self.release = asyncio.Event(), asyncio.Event()

        async def __aiter__(self):
            async for chunk in super().__aiter__():
                yield chunk
            self.at_eof.set()
            await self.release.wait()

    stream = HeldEOF()
    response = httpx.Response(200, stream=stream)
    seen = []

    async def consume():
        async for event in iterator(response):
            seen.append(event)

    pending = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(stream.at_eof.wait(), 1)
        assert not pending.done() and not stream.closed
        if iterator is _iter_compatible_events:
            assert seen == [
                _CompatibleStreamEvent(TEXT, "ok", False),
                _CompatibleStreamEvent(STOP, None, False),
                _CompatibleStreamEvent(DONE, None, True),
            ]
        else:
            assert seen == ["ok"]
        stream.release.set()
        await asyncio.wait_for(pending, 1)
        assert stream.closed
    finally:
        stream.release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await response.aclose()
