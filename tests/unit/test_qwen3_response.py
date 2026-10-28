"""ASGI send ownership: the broker remains the only disconnect receiver."""

import asyncio

import pytest

from services.resources.api import ManagedTextResponse


class Body:
    def __init__(self):
        self.started = False
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.started:
            raise StopAsyncIteration
        self.started = True
        return b'{"kind":"delta","text":"synthetic"}\n'

    async def aclose(self):
        self.closed = True


async def never_receive():
    raise AssertionError("Only the broker may watch disconnect")


async def test_header_send_failure_closes_unstarted_owned_body():
    body = Body()

    async def send(message):
        assert message["type"] == "http.response.start"
        raise ConnectionError("synthetic closed connection")

    with pytest.raises(ConnectionError):
        await ManagedTextResponse(body, timeout_s=1)({}, never_receive, send)
    assert not body.started and body.closed


async def test_stalled_send_has_a_deadline_and_closes_the_owned_body():
    body = Body()
    entered = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body":
            entered.set()
            await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await ManagedTextResponse(body, timeout_s=0.05)({}, never_receive, send)
    assert entered.is_set() and body.started and body.closed


async def test_normal_stream_uses_no_receive_and_closes_after_terminal_send():
    body, sent = Body(), []

    async def send(message):
        sent.append(message)

    await ManagedTextResponse(body, timeout_s=1)({}, never_receive, send)
    assert body.closed
    assert sent[0]["type"] == "http.response.start"
    assert (b"cache-control", b"no-store") in sent[0]["headers"]
    assert all(key != b"content-length" for key, value in sent[0]["headers"])
    assert sent[1]["body"] == b'{"kind":"delta","text":"synthetic"}\n'
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
