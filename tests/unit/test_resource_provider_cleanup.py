"""The policy wrapper retains the selected provider's stream cleanup owner."""

import asyncio
from contextlib import suppress
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.models import ProviderInfo
from packages.providers.text import (
    CompatibleTextProvider,
    ProviderCleanupError,
    ProviderError,
    owned_http_stream_cleanup,
)
from services.resources.providers import ResourceBoundProvider
from services.resources.state import ResourceState


@pytest.fixture
def resource_state(tmp_path):
    state = ResourceState(tmp_path / "resources.json")
    state.record.phase = "ready"
    state.save()
    yield state
    state.close()


class HeldResponse(httpx.AsyncByteStream):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.close_calls = 0

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
        await asyncio.Event().wait()

    async def aclose(self):
        self.close_calls += 1
        self.started.set()
        await self.release.wait()
        self.closed = True


async def test_early_wrapper_close_joins_http_cleanup_through_repeated_cancellation(resource_state):
    body = HeldResponse()
    provider = CompatibleTextProvider(
        "http://127.0.0.1:18999/v1",
        model="synthetic",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body)),
    )
    wrapped = ResourceBoundProvider(provider, resource_state.path, "converse")
    source, closing = wrapped.stream([]), None
    try:
        with owned_http_stream_cleanup():
            assert await anext(source) == "partial"
            closing = asyncio.create_task(source.aclose())
            await asyncio.wait_for(body.started.wait(), 1)
            assert not closing.done(), "wrapper close escaped its actual HTTP cleanup"
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done() and not body.closed
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done() and not body.closed
            body.release.set()
            with pytest.raises(asyncio.CancelledError):
                await closing
        assert body.closed and body.close_calls == 1
    finally:
        body.release.set()
        if closing is not None:
            with suppress(asyncio.CancelledError):
                await closing
        await source.aclose()
        await provider.close()
        # The original defect can schedule inner cleanup through finalization.
        # Release it too, rather than leaving a pending test-owned HTTP body.
        if body.started.is_set():
            for _ in range(20):
                if body.closed:
                    break
                await asyncio.sleep(0)


class FiniteSource:
    def __init__(self):
        self.sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return "public fixture"


class FailedClose(FiniteSource):
    async def aclose(self):
        raise ProviderCleanupError("synthetic unconfirmed close")


@pytest.mark.parametrize("early", [False, True])
async def test_wrapper_propagates_underlying_close_failure(resource_state, early):
    provider = SimpleNamespace(
        info=ProviderInfo(provider="synthetic", model="fixture", local=True),
        stream=lambda messages: FailedClose(),
    )
    source = ResourceBoundProvider(provider, resource_state.path, "converse").stream([])
    assert await anext(source) == "public fixture"
    with pytest.raises(ProviderCleanupError, match="unconfirmed close"):
        if early:
            await source.aclose()
        else:
            await anext(source)


async def test_wrapper_preserves_async_iterator_without_optional_close(resource_state):
    provider = SimpleNamespace(
        info=ProviderInfo(provider="synthetic", model="fixture", local=True),
        stream=lambda messages: FiniteSource(),
    )
    source = ResourceBoundProvider(provider, resource_state.path, "converse").stream([])
    assert [part async for part in source] == ["public fixture"]


async def test_paused_wrapper_never_constructs_underlying_stream(resource_state):
    calls = []
    provider = SimpleNamespace(
        info=ProviderInfo(provider="synthetic", model="fixture", local=True),
        stream=lambda messages: calls.append(messages),
    )
    resource_state.record.phase = "off"
    resource_state.record.desired_converse = False
    resource_state.save()
    source = ResourceBoundProvider(provider, resource_state.path, "converse").stream([])
    with pytest.raises(ProviderError, match="paused"):
        await anext(source)
    assert calls == []
