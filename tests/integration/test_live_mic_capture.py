"""Actual registration/fence and atomic live source ownership over native HTTP."""

import asyncio

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.live_captures import LiveCaptureFence
from services.common.auth import Auth
from services.converse.live_capture import LiveCaptureUnavailable, LiveMicCapture
from services.session.api import create_app
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_capture_permission import registration
from tests.integration.test_live_window_publication import original


def wire(env, transport=None):
    auth = Auth({"converse": "public-microphone-capture-token-000000000"})
    asgi = httpx.ASGITransport(app=create_app(env.store, auth))
    client = JournalClient(
        "http://session.local", auth.tokens["converse"], transport=transport or asgi
    )
    return client, asgi


async def test_logical_capture_registers_before_source_and_retains_late_text_after_fence(env):
    journal, _ = wire(env)
    request = registration(env)
    owner = LiveMicCapture(journal, request, current=lambda: True)
    try:
        assert env.store.db.execute("SELECT count(*) FROM live_captures").fetchone()[0] == 0
        state = await owner.register()
        source = original(env).request
        proof = await owner.publish(source)
        assert proof.source.payload == source.payload
        assert proof.original.owner_instance == request.owner_instance
        assert proof.original.capture_id == request.capture_id
        assert state.receipt.registered_sequence < proof.source.sequence
        assert env.store.live_captures.source_state(proof.original).capture_permits_source
        fenced = await owner.wait_fenced("owner_off")
        assert fenced.fence.request == owner.fence_request and not fenced.capture_open
        late = original(env, 1, text="PUBLIC LATE RECOGNIZED INPUT").request
        retained = await owner.publish(late)
        assert retained.source.payload == late.payload
        assert not env.store.live_captures.source_state(retained.original).capture_permits_source
        assert owner.registration == state.receipt
    finally:
        await owner.wait_fenced()
        await journal.close()


async def test_cancelled_registration_cannot_resume_after_original_negative_fence(env):
    journal, asgi = wire(env)
    entered, release = asyncio.Event(), asyncio.Event()
    sends = []

    class Held(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/register"):
                sends.append(request.content)
                entered.set()
                await release.wait()
            return await asgi.handle_async_request(request)

    await journal.close()
    auth = Auth({"converse": "public-microphone-capture-token-000000000"})
    journal = JournalClient("http://session.local", auth.tokens["converse"], transport=Held())
    owner = LiveMicCapture(journal, registration(env), current=lambda: True)
    facade = asyncio.create_task(owner.register())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        facade.cancel()
        with pytest.raises(asyncio.CancelledError):
            await facade
        assert not owner.registration_task.done() and len(journal._submit_exchanges) == 1
        closed = await owner.wait_fenced("disconnected")
        assert closed.receipt is None and closed.fence is not None and not closed.capture_open
        release.set()
        await asyncio.wait_for(owner.registration_task, 3)
        state = await owner.inspect()
        assert state == closed and not state.capture_open
        with pytest.raises(LiveCaptureUnavailable):
            await owner.register()
        assert len(sends) == 1 and not journal._submit_exchanges
    finally:
        release.set()
        await asyncio.gather(facade, return_exceptions=True)
        await owner.wait_fenced()
        await journal.close()


@pytest.mark.parametrize("committed", [False, True])
async def test_original_publication_lookup_never_replays_and_positive_reconciliation_is_usable(
    env, committed
):
    journal, asgi = wire(env)
    sends = []

    class Lost(httpx.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream

        async def __aiter__(self):
            raise httpx.ReadError("Public synthetic lost recognized publication ACK")
            yield b""

        async def aclose(self):
            await self.stream.aclose()

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/live-windows"):
                sends.append(request.content)
                result = (
                    await asgi.handle_async_request(request)
                    if committed
                    else httpx.Response(200, stream=httpx.ByteStream(b""))
                )
                result.stream = Lost(result.stream)
                return result
            return await asgi.handle_async_request(request)

    await journal.close()
    journal = JournalClient(
        "http://session.local", "public-microphone-capture-token-000000000", transport=Transport()
    )
    owner = LiveMicCapture(journal, registration(env), current=lambda: True)
    request = original(env).request
    try:
        await owner.register()
        with pytest.raises(httpx.ReadError):
            await owner.publish(request)
        observed = await owner.inspect_publication()
        assert (observed.receipt is not None) == committed
        assert len(sends) == 1
        assert await owner.inspect_publication() == observed
        assert len(sends) == 1
        if committed:
            assert await owner.publish(request) == observed.receipt
        else:
            with pytest.raises(LiveCaptureUnavailable):
                await owner.publish(original(env, 1).request)
            assert owner.publication.request == request
        assert len(sends) == 1 and not journal._submit_exchanges
    finally:
        await owner.wait_fenced()
        await journal.close()


async def test_invalidated_local_owner_fences_native_permission_before_retaining_late_input(env):
    journal, _ = wire(env)
    active = [True]
    owner = LiveMicCapture(journal, registration(env), current=lambda: active[0])
    try:
        await owner.register()
        active[0] = False
        result = await owner.publish(original(env).request)
        assert owner.fenced and owner.fence_receipt is not None
        assert not env.store.live_captures.source_state(result.original).capture_permits_source
        assert result.source.payload == original(env).request.payload
    finally:
        await owner.wait_fenced()
        await journal.close()


async def test_positive_fence_lookup_releases_late_publication_without_replaying_fence(env):
    journal, asgi = wire(env)
    sends = []

    class Lost(httpx.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream

        async def __aiter__(self):
            raise httpx.ReadError("Public synthetic lost native fence acknowledgment")
            yield b""

        async def aclose(self):
            await self.stream.aclose()

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            result = await asgi.handle_async_request(request)
            if request.url.path.endswith("/live-captures/fence"):
                sends.append(request.content)
                result.stream = Lost(result.stream)
            return result

    await journal.close()
    journal = JournalClient(
        "http://session.local", "public-microphone-capture-token-000000000", transport=Transport()
    )
    owner = LiveMicCapture(journal, registration(env), current=lambda: True)
    try:
        await owner.register()
        with pytest.raises(httpx.ReadError):
            await owner.wait_fenced("cancelled")
        observed = await owner.inspect()
        assert observed.fence.request == owner.fence_request and not observed.capture_open
        assert await owner.wait_fenced() == observed
        result = await owner.publish(original(env).request)
        assert not env.store.live_captures.source_state(result.original).capture_permits_source
        assert len(sends) == 1 and not journal._submit_exchanges
    finally:
        await journal.close()


async def test_recognized_request_is_detached_before_an_awaited_native_write(env):
    journal, asgi = wire(env)
    entered, release = asyncio.Event(), asyncio.Event()

    class Held(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/live-windows"):
                entered.set()
                await release.wait()
            return await asgi.handle_async_request(request)

    await journal.close()
    journal = JournalClient(
        "http://session.local", "public-microphone-capture-token-000000000", transport=Held()
    )
    owner = LiveMicCapture(journal, registration(env), current=lambda: True)
    request = original(env).request
    expected = request.model_dump(mode="json")
    await owner.register()
    task = asyncio.create_task(owner.publish(request))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        request.payload["text"] = "PUBLIC SYNTHETIC CALLER MUTATION AFTER SEND"
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.source.payload == expected["payload"]
        assert owner.publication.request.model_dump(mode="json") == expected
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await owner.wait_fenced()
        await journal.close()


async def test_scope_substitution_refuses_before_a_recognized_write(env):
    journal, _ = wire(env)
    owner = LiveMicCapture(journal, registration(env), current=lambda: True)
    try:
        await owner.register()
        request = original(env).request.model_copy(update={"epoch": env.session.epoch + 1})
        with pytest.raises(LiveCaptureUnavailable):
            await owner.publish(request)
        assert owner.publication is None and owner.publication_task is None
        assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
    finally:
        await owner.wait_fenced()
        await journal.close()


async def test_empty_native_lookup_is_unknown_and_does_not_close_a_current_registration(env):
    journal, _ = wire(env)
    owner = LiveMicCapture(journal, registration(env), current=lambda: True)
    try:
        unknown = await owner.inspect()
        assert unknown.receipt is None and unknown.fence is None and not unknown.capture_open
        assert unknown.backend == owner.original.backend and unknown.scope_current
        assert not owner.fenced and owner.fenced_state is None
        registered = await owner.register()
        assert registered.capture_open and owner.registration == registered.receipt
    finally:
        await owner.wait_fenced()
        await journal.close()


async def test_competing_native_fence_does_not_acknowledge_or_replace_the_local_original(env):
    journal, asgi = wire(env)
    entered, release = asyncio.Event(), asyncio.Event()
    sends = []

    class Held(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/live-captures/fence"):
                sends.append(request.content)
                entered.set()
                await release.wait()
            return await asgi.handle_async_request(request)

    await journal.close()
    journal = JournalClient(
        "http://session.local", "public-microphone-capture-token-000000000", transport=Held()
    )
    owner = LiveMicCapture(journal, registration(env), current=lambda: True)
    try:
        await owner.register()
        task = owner.fence("owner_off")
        await asyncio.wait_for(entered.wait(), 3)
        local = owner.fence_request
        other = LiveCaptureFence(
            schema_version="2.0",
            protocol="native_live_capture_v2",
            original=owner.original,
            request_id="f" * 32,
            reason="replaced",
        )
        closed = env.store.live_captures.fence(other)
        observed = await owner.inspect()
        assert observed == closed and owner.fenced_state == closed
        assert owner.fence_request == local and owner.fence_receipt is None
        assert not task.done() and task in owner.tasks
        assert await owner.wait_fenced() == closed
        release.set()
        with pytest.raises(httpx.HTTPStatusError):
            await asyncio.wait_for(asyncio.shield(task), 3)
        assert await owner.wait_fenced() == closed
        assert owner.fence_request == local and owner.fence_receipt is None
        assert len(sends) == 1 and not journal._submit_exchanges
    finally:
        release.set()
        if owner.fence_task is not None:
            await asyncio.gather(owner.fence_task, return_exceptions=True)
        await journal.close()
