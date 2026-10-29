"""Typed Converse preserves published text when compatible completion is refused."""

import asyncio
import json

import httpx
import pytest

from packages.contracts.models import ConverseRequest, SessionRequest
from packages.providers.text import CompatibleTextProvider
from services.converse.runtime import ConverseRuntime
from services.session.store import LocalJournal, Store

SENTENCE = "A small synthetic garden looks peaceful."


def data_event(value):
    return ("data: " + json.dumps(value) + "\n\n").encode()


def choice(content=None, finish=None):
    delta = {} if content is None else {"content": content}
    return data_event({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


class GatedCompletion(httpx.AsyncByteStream):
    def __init__(self, ending):
        self.ending = ending
        self.tail, self.closing, self.release_close = (asyncio.Event() for _ in range(3))
        self.closed = self.close_cancelled = False
        self.close_calls = 0

    async def __aiter__(self):
        yield choice(SENTENCE + " ")
        await self.tail.wait()
        if self.ending == "malformed":
            yield b"data: {\n\n"
        elif self.ending != "eof":
            yield choice(finish="length" if self.ending == "length" else "stop")
            yield b"data: [DONE]\n\n"
            if self.ending == "trailing":
                yield choice("Unexpected extra sentence. ")

    async def aclose(self):
        self.close_calls += 1
        self.closing.set()
        try:
            await self.release_close.wait()
            self.closed = True
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise


@pytest.mark.parametrize("ending", ["stop", "length", "eof", "malformed", "trailing"])
async def test_typed_completion_keeps_published_prefix_and_confirms_real_close(tmp_path, ending):
    stream, calls = GatedCompletion(ending), []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic", transport=httpx.MockTransport(respond)
    )
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    request = ConverseRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        text="Describe the public synthetic garden.",
        content_scope="public_synthetic",
        idempotency_key="synthetic-completion",
    )
    events = runtime.events(request)
    pending = None
    try:
        assert (await asyncio.wait_for(anext(events), 1))["type"] == "accepted"
        published = await asyncio.wait_for(anext(events), 1)
        assert published["type"] == "text"
        assert published["entry"]["payload"]["text"] == SENTENCE
        operation = runtime._text[session.session_id]
        # Release the terminal only after this exact sentence was journaled and emitted.
        stream.tail.set()
        pending = asyncio.create_task(anext(events))
        await asyncio.wait_for(stream.closing.wait(), 1)
        assert not pending.done() and not operation.cleanup.done()
        assert not stream.closed and not stream.close_cancelled
        stream.release_close.set()
        terminal = await asyncio.wait_for(pending, 1)
        if ending == "stop":
            assert terminal["type"] == "done"
        else:
            assert terminal == {"type": "error", "error": "ProviderRefusal"}
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(events), 1)
        await asyncio.wait_for(operation.cleanup, 1)
        entries = store.read(session_id=session.session_id, kinds=["conversation.converse"])
        assert [entry.model_dump(mode="json") for entry in entries] == [published["entry"]]
        assert len(calls) == 1
        assert stream.closed and not stream.close_cancelled and stream.close_calls == 1
        assert session.session_id not in runtime._text
        assert all(
            runtime.text_status()[key] == 0
            for key in ("active", "admitting", "cleanup_pending", "cleanup_failed")
        )
    finally:
        stream.tail.set()
        stream.release_close.set()
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await events.aclose()
        await runtime.close()
        await provider.close()
        store.close()
