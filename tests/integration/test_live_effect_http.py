"""Real ASGI/client ownership and role parity for original live consumption."""

import asyncio

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.live_effects import PROTOCOL, LiveEffectStart
from services.common.auth import Auth
from services.session.api import create_app
from services.session.live_effect_api import BASE
from services.session.store import LocalJournal
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_capture_permission import fence
from tests.integration.test_live_decisions_attempts import live as live
from tests.integration.test_live_effects import completion
from tests.integration.test_live_effects import effects as effects

TOKENS = {
    role: role + "-public-consumer-token-000000000000000"
    for role in ("owner", "converse", "decisions", "agent", "resources")
}


def wire(e):
    return httpx.ASGITransport(app=create_app(e.store, Auth(TOKENS)))


@pytest.mark.parametrize("role", ["owner", "converse", "decisions", "agent", "resources", None])
async def test_lookup_roles_are_read_only_and_match_local_authority(effects, role):
    e = effects
    headers = {} if role is None else {"Authorization": "Bearer " + TOKENS[role]}
    async with httpx.AsyncClient(transport=wire(e), base_url="http://session.local") as client:
        result = await client.post(
            BASE + "/lookup", headers=headers, json=e.query.model_dump(mode="json")
        )
    if role in {"owner", "converse", "decisions", "agent"}:
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert result.json() == (
            await LocalJournal(e.store, role).live_effect_lookup(e.query)
        ).model_dump(mode="json")
    else:
        assert result.status_code == (401 if role is None else 403)
        if role is not None:
            with pytest.raises(PermissionError):
                await LocalJournal(e.store, role).live_effect_lookup(e.query)
    assert not e.effects._present()


@pytest.mark.parametrize("role", ["owner", "converse", "decisions", "agent", "resources", None])
async def test_only_the_effect_consumer_can_admit_or_finish(effects, role):
    e = effects
    headers = {} if role is None else {"Authorization": "Bearer " + TOKENS[role]}
    async with httpx.AsyncClient(transport=wire(e), base_url="http://session.local") as client:
        result = await client.post(
            BASE + "/admit", headers=headers, json=e.request.model_dump(mode="json")
        )
        if role == "converse":
            assert result.status_code == 200
            record = await LocalJournal(e.store, role).live_effect_admit(e.request)
            assert result.json() == record.model_dump(mode="json")
            finished = await client.post(
                BASE + "/finish",
                headers=headers,
                json=completion(record.receipt).model_dump(mode="json"),
            )
            assert (
                finished.status_code == 200
                and finished.json()["completion"]["status"] == "declined"
            )
        else:
            assert result.status_code == (
                401 if role is None else 409 if role == "decisions" else 403
            )
            assert not e.effects._present()
            if role is not None:
                with pytest.raises(ValueError):
                    await LocalJournal(e.store, role).live_effect_admit(e.request)


@pytest.mark.parametrize("committed", [False, True])
async def test_lost_admission_ack_is_observed_without_another_mutation(effects, committed):
    e, asgi, sends = effects, wire(effects), []

    class Lost(httpx.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream

        async def __aiter__(self):
            raise httpx.ReadError("Public synthetic lost consumer admission ACK")
            yield b""

        async def aclose(self):
            await self.stream.aclose()

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/live-effects/admit"):
                sends.append(request.content)
                result = (
                    await asgi.handle_async_request(request)
                    if committed
                    else httpx.Response(200, stream=httpx.ByteStream(b""))
                )
                result.stream = Lost(result.stream)
                return result
            return await asgi.handle_async_request(request)

    journal = JournalClient("http://session.local", TOKENS["converse"], transport=Transport())
    try:
        with pytest.raises(httpx.ReadError):
            await journal.live_effect_admit(e.request)
        observed = await journal.live_effect_lookup(e.query)
        assert (observed.record is not None) == committed
        assert observed.source_current
        assert await journal.live_effect_lookup(e.query) == observed
        if committed:
            assert observed.record.receipt.original == e.request
        assert len(sends) == 1 and not journal._submit_exchanges
    finally:
        await journal.close()


async def test_cancelled_admission_keeps_original_http_and_cannot_grant_after_revocation(effects):
    e, asgi = effects, wire(effects)
    entered, release = asyncio.Event(), asyncio.Event()
    sends = []

    class Held(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/live-effects/admit"):
                sends.append(request.content)
                entered.set()
                await release.wait()
            return await asgi.handle_async_request(request)

    journal = JournalClient("http://session.local", TOKENS["converse"], transport=Held())
    task = asyncio.create_task(journal.live_effect_admit(e.request))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(journal._submit_exchanges) == 1
        e.store.live_captures.fence(fence(e.source.capture.original))
        release.set()
        async with asyncio.timeout(3):
            while journal._submit_exchanges:
                await asyncio.sleep(0)
        state = await journal.live_effect_lookup(e.query)
        assert state.record is None and not state.source_current
        assert len(sends) == 1 and not e.effects._present()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await journal.close()


@pytest.mark.parametrize(
    "raw,extra,status",
    [
        (b'{"x":1,"x":2}', {}, 422),
        (b'{"sequence":1.0}', {}, 422),
        (b'{"sequence":NaN}', {}, 422),
        (b"{}", {"Content-Encoding": "gzip"}, 415),
        (b"{}", {"Content-Type": "text/plain"}, 415),
        (b" " * 16385, {}, 413),
    ],
)
async def test_consumer_envelope_is_bounded_before_any_claim(effects, raw, extra, status):
    async with httpx.AsyncClient(
        transport=wire(effects), base_url="http://session.local"
    ) as client:
        result = await client.post(
            BASE + "/admit",
            content=raw,
            headers={
                "Authorization": "Bearer " + TOKENS["converse"],
                "Content-Type": "application/json",
                **extra,
            },
        )
    assert result.status_code == status and not effects.effects._present()


@pytest.mark.parametrize("role", ["owner", "converse", "decisions", "agent", "resources", None])
async def test_start_has_the_same_effect_role_boundary(effects, role):
    e = effects
    record = e.effects.admit(e.request, "converse")
    request = LiveEffectStart(
        schema_version="2.0",
        protocol=PROTOCOL,
        original=record.receipt,
        owner_instance="e" * 32,
        operation_id="f" * 32,
    )
    headers = {} if role is None else {"Authorization": "Bearer " + TOKENS[role]}
    async with httpx.AsyncClient(transport=wire(e), base_url="http://session.local") as client:
        response = await client.post(
            BASE + "/start", headers=headers, json=request.model_dump(mode="json")
        )
    if role == "converse":
        assert response.status_code == 200 and response.json()["started"][
            "original"
        ] == request.model_dump(mode="json")
        assert (await LocalJournal(e.store, role).live_effect_start(request)).model_dump(
            mode="json"
        ) == response.json()
    else:
        assert response.status_code == (
            401 if role is None else 409 if role == "decisions" else 403
        )
        assert e.effects.lookup(e.query).record.started is None


@pytest.mark.parametrize("committed", [False, True])
async def test_lost_start_ack_never_repeats_start_and_exposes_the_exact_owner(effects, committed):
    e, asgi, sends = effects, wire(effects), []
    record = e.effects.admit(e.request, "converse")
    request = LiveEffectStart(
        schema_version="2.0",
        protocol=PROTOCOL,
        original=record.receipt,
        owner_instance="e" * 32,
        operation_id="f" * 32,
    )

    class Lost(httpx.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream

        async def __aiter__(self):
            raise httpx.ReadError("Public synthetic lost original start ACK")
            yield b""

        async def aclose(self):
            await self.stream.aclose()

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, native):
            if native.url.path.endswith("/live-effects/start"):
                sends.append(native.content)
                response = (
                    await asgi.handle_async_request(native)
                    if committed
                    else httpx.Response(200, stream=httpx.ByteStream(b""))
                )
                response.stream = Lost(response.stream)
                return response
            return await asgi.handle_async_request(native)

    journal = JournalClient("http://session.local", TOKENS["converse"], transport=Transport())
    try:
        with pytest.raises(httpx.ReadError):
            await journal.live_effect_start(request)
        state = await journal.live_effect_lookup(e.query)
        assert (state.record.started is not None) == committed
        if committed:
            assert state.record.started.original == request
        assert await journal.live_effect_lookup(e.query) == state
        assert len(sends) == 1 and not journal._submit_exchanges
    finally:
        await journal.close()
