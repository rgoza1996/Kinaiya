"""Independent closure HTTP/client oracles; actual native fixtures, no worker."""

import asyncio
import importlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from test_standing_dispatch_api import headers, rows
from test_standing_execution_api import (
    CAP_HEADER,
    MARKER,
    SECRET,
    BodyWitness,
    checked,
    close_env,
    original,
    prepared,
    send,
)
from test_standing_execution_api import env as env
from test_standing_execution_api import factory as factory

from packages.contracts.agent_submit import AgentSubmitLookupRequest, AgentSubmitUnavailable
from packages.contracts.client import JournalClient
from packages.contracts.standing_execution import StandingExecutionIntent
from packages.contracts.standing_work import StandingConflict, StandingUnavailable

CLOSE = "/v1/standing/tasks/close-admission"
LOOKUP = "/v1/standing/tasks/closure-lookup"
ROUTES = (CLOSE, LOOKUP)


def closure_type():
    try:
        return importlib.import_module(
            "packages.contracts.standing_closure"
        ).StandingAdmissionClosure
    except ModuleNotFoundError as error:
        if error.name != "packages.contracts.standing_closure":
            raise
        pytest.fail("Frozen standing admission closure contract is absent", pytrace=False)


def call(client, lookup, packet, **kwargs):
    name = "standing_closure_lookup" if lookup else "standing_close_admission"
    method = getattr(client, name, None)
    assert callable(method), "Frozen standing closure JournalClient capability is absent"
    return method(packet, **kwargs)


def safe(error):
    assert MARKER not in str(error) and SECRET.hex() not in str(error)
    assert len(str(error)) <= 256 and error.__cause__ is None


async def joined(client):
    while client._submit_exchanges:
        tasks = {
            task
            for exchange in client._submit_exchanges
            for task in (exchange.task, exchange.close_task)
            if task is not None
        }
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)


@asynccontextmanager
async def journal(env, *, transport=None):
    token = headers("agent")["Authorization"].removeprefix("Bearer ")
    client = JournalClient(
        "http://standing.test",
        token,
        transport=transport or httpx.ASGITransport(app=env.app),
    )
    try:
        yield client
    finally:
        await client.close()
        await asyncio.wait_for(joined(client), 3)


@pytest.fixture
async def closed_data(env):
    kind = closure_type()
    packet = await original(env)
    await prepared(env, packet)
    async with journal(env) as client:
        result = await call(client, False, packet, capability=SECRET)
    assert type(result) is kind and result.validate_request(packet) == result
    assert result.initial is None
    return SimpleNamespace(env=env, packet=packet, closure=result)


async def test_actual_client_close_first_fences_every_later_admit_and_keeps_lookup_nullable(env):
    kind = closure_type()
    packet = await original(env)
    async with journal(env) as client:
        assert await call(client, True, packet) is None
        claim = await client.standing_prepare(packet)
        assert await call(client, True, packet) is None
        result = await call(client, False, packet, capability=SECRET)
        assert type(result) is kind and result.validate_request(packet) == result
        assert result.initial is None
        before = rows(env.store)
        assert await call(client, False, packet, capability=SECRET) == result
        assert await call(client, True, packet) == result
        for cap in (None, bytes(32)):
            with pytest.raises(StandingConflict) as caught:
                await call(client, False, packet, capability=cap)
            safe(caught.value)
        with pytest.raises(StandingConflict):
            await client.standing_admit(packet, capability=SECRET)
        assert await client.standing_claim_lookup(packet) == claim.claim
        assert await client.standing_task_lookup(packet) is None
        assert rows(env.store) == before
        assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
        assert SECRET.hex() not in repr(before)


async def test_actual_client_admit_first_closes_only_future_admission_not_retained_proof(env):
    kind = closure_type()
    packet = await original(env)
    await prepared(env, packet)
    async with journal(env) as client:
        admission = await client.standing_admit(packet, capability=SECRET)
        assert admission.disposition == "created"
        result = await call(client, False, packet, capability=SECRET)
        assert type(result) is kind and result.initial == admission.proof
        assert result.validate_request(packet) == result
        before = rows(env.store)
        retained = await client.standing_admit(packet)
        assert retained.disposition == "retained" and retained.proof == admission.proof
        assert await call(client, True, packet) == result
        assert await client.standing_task_lookup(packet) == admission.proof
        assert rows(env.store) == before
        assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 1


async def test_actual_committed_close_lost_http_ack_is_reconciled_without_new_admission(env):
    closure_type()
    packet = await original(env)
    await prepared(env, packet)

    class LostAck(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner, self.dropped = httpx.ASGITransport(app=env.app), False

        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if request.url.path == CLOSE and not self.dropped:
                assert response.status_code == 200
                self.dropped = True
                await response.aclose()
                raise httpx.ReadError(MARKER, request=request)
            return response

        async def aclose(self):
            await self.inner.aclose()

    transport = LostAck()
    async with journal(env, transport=transport) as client:
        with pytest.raises(StandingUnavailable) as caught:
            await call(client, False, packet, capability=SECRET)
        safe(caught.value)
        assert transport.dropped
        result = await call(client, True, packet)
        assert result is not None and result.initial is None
        assert result.validate_request(packet) == result
        with pytest.raises(StandingConflict):
            await client.standing_admit(packet, capability=SECRET)
        assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


async def test_same_sqlite_restart_retains_closure_while_replacement_backend_refuses(factory):
    kind = closure_type()
    first = await factory()
    packet = await original(first)
    await prepared(first, packet)
    async with journal(first) as client:
        saved = await call(client, False, packet, capability=SECRET)
    await close_env(first)
    second = await factory(first.path)
    async with journal(second) as client:
        found = await call(client, True, packet)
        assert type(found) is kind and found == saved
        assert await call(client, False, packet, capability=SECRET) == saved
        with pytest.raises(StandingConflict):
            await client.standing_admit(packet, capability=SECRET)
    third = await factory()
    async with journal(third) as client:
        with pytest.raises(StandingConflict):
            await call(client, True, packet)


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("role", (None, "owner", "converse"))
async def test_closure_routes_authenticate_before_body_query_and_private_header(env, path, role):
    body, before = BodyWitness(), rows(env.store)
    response = await env.client.post(
        path + "?bad=1",
        content=body,
        headers=headers(role, **{"Content-Type": "not/json", CAP_HEADER: MARKER}),
    )
    checked(response, 401 if role is None else 403)
    assert body.reads == 0 and rows(env.store) == before


@pytest.mark.parametrize("path", ROUTES)
async def test_closure_ingress_raw_media_duplicate_json_and_query_bounds(env, path):
    packet = await original(env)
    before = rows(env.store)
    for raw in (b" " * 131073, b'{"schema_version":"1.0",' + packet.canonical_bytes()[1:]):
        checked(await send(env, path, packet, raw=raw), 422)
    for extra in ({"Content-Type": "text/plain"}, {"Content-Encoding": "gzip"}):
        checked(await send(env, path, packet, extra=extra), 415)
    for query in ("?bad=1", "?bad=1&bad=2", "?capability=" + SECRET.hex()):
        checked(await send(env, path + query, packet), 422)
    assert rows(env.store) == before


async def test_close_header_is_strict_but_lookup_never_interprets_private_capability(env):
    kind = closure_type()
    packet = await original(env)
    await prepared(env, packet)
    before = rows(env.store)
    for cap in ("A" * 64, "a" * 63, MARKER):
        checked(await send(env, CLOSE, packet, cap=cap), 422)
    duplicate = list(headers("agent", **{"Content-Type": "application/json"}).items()) + [
        (CAP_HEADER, SECRET.hex()),
        (CAP_HEADER, SECRET.hex()),
    ]
    checked(await env.client.post(CLOSE, content=packet.canonical_bytes(), headers=duplicate), 422)
    checked(await send(env, LOOKUP, packet, cap=MARKER), 200)
    checked(await env.client.post(LOOKUP, content=packet.canonical_bytes(), headers=duplicate), 200)
    assert rows(env.store) == before
    raw = packet.canonical_bytes()
    padded = b" " * (131072 - len(raw)) + raw
    result = checked(await send(env, CLOSE, packet, cap=SECRET.hex(), raw=padded), 200, kind)
    assert result.validate_request(packet) == result and result.initial is None


async def test_api_null_close_and_foreign_full_original_reply_are_never_success(
    closed_data, factory, monkeypatch
):
    data, kind = closed_data, closure_type()
    foreign = await factory()
    other = await original(foreign)
    await prepared(foreign, other)
    async with journal(foreign) as client:
        wrong = await call(client, False, other, capability=SECRET)
    assert wrong.claim_id != data.closure.claim_id
    before = rows(data.env.store)
    native = data.env.app.state.standing_task_owner
    with monkeypatch.context() as patch:
        patch.setattr(native, "close_admission", lambda *a, **kw: None)
        checked(await send(data.env, CLOSE, data.packet, cap=SECRET.hex()), 503)
    for path, method in ((CLOSE, "close_admission"), (LOOKUP, "lookup_closure")):
        with monkeypatch.context() as patch:
            patch.setattr(native, method, lambda *a, **kw: wrong)
            checked(await send(data.env, path, data.packet, cap=SECRET.hex()), 503)
    assert rows(data.env.store) == before
    checked(await send(data.env, LOOKUP, data.packet), 200, kind).validate_request(data.packet)


@pytest.mark.parametrize("phase", ("ingress", "after-native-ack"))
async def test_new_route_keeps_five_second_ingress_and_total25_deadline(env, monkeypatch, phase):
    closure_type()
    api = importlib.import_module("services.session.standing_work_api")
    packet = await original(env)
    await prepared(env, packet)
    offset, real = [0.0], time.monotonic
    native = env.app.state.standing_task_owner
    close = getattr(native, "close_admission", None)
    assert callable(close), "Frozen native close capability is absent"
    observed = []

    def late(*args, **kwargs):
        observed.append(kwargs)
        result = close(*args, **kwargs)
        offset[0] = 26
        return result

    class LateBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            offset[0] = 6
            yield packet.canonical_bytes()

    with monkeypatch.context() as patch:
        patch.setattr(api, "time", SimpleNamespace(monotonic=lambda: real() + offset[0]))
        patch.setattr(native, "close_admission", late)
        response = await env.client.post(
            CLOSE,
            content=LateBody() if phase == "ingress" else packet.canonical_bytes(),
            headers=headers(
                "agent", **{"Content-Type": "application/json", CAP_HEADER: SECRET.hex()}
            ),
        )
        checked(response, 503)
    async with journal(env) as client:
        retained = await call(client, True, packet)
    if phase == "ingress":
        assert retained is None and observed == []
    else:
        assert retained is not None and retained.initial is None
        assert len(observed) == 1 and observed[0]["capability"] == SECRET
        assert callable(observed[0]["cancelled"]) and type(observed[0]["deadline"]) in (int, float)


async def resistant(gate):
    while not gate.is_set():
        try:
            await gate.wait()
        except asyncio.CancelledError:
            continue


class Body(httpx.AsyncByteStream):
    def __init__(self, raw):
        self.raw, self.close_calls = raw, 0
        self.read_entered, self.close_entered, self.release = (asyncio.Event() for _ in range(3))
        self.closed = asyncio.Event()
        self.hold_close = False

    async def __aiter__(self):
        self.read_entered.set()
        yield self.raw

    async def aclose(self):
        self.close_calls += 1
        self.close_entered.set()
        if self.hold_close:
            await resistant(self.release)
        self.closed.set()


@asynccontextmanager
async def scenario(data, *, raw=None, status=200, extra=None):
    body = Body(data.closure.canonical_bytes() if raw is None else raw)
    state = SimpleNamespace(
        body=body,
        requests=[],
        tasks=[],
        entered=asyncio.Event(),
        release=asyncio.Event(),
        hold=False,
    )

    async def handle(request):
        state.requests.append(request)
        state.entered.set()
        if state.hold:
            await resistant(state.release)
        state.response = httpx.Response(
            status, stream=body, headers={"Content-Type": "application/json", **(extra or {})}
        )
        return state.response

    async with journal(data.env, transport=httpx.MockTransport(handle)) as client:
        state.client = client
        try:
            yield state
        finally:
            state.release.set()
            body.release.set()
            for task in state.tasks:
                task.cancel()
            await asyncio.gather(*state.tasks, return_exceptions=True)


@pytest.mark.parametrize("lookup", (False, True))
async def test_only_closure_lookup_accepts_null_and_each_new_request_is_exact(closed_data, lookup):
    data = closed_data
    async with scenario(data, raw=b"null") as state:
        if lookup:
            assert await call(state.client, True, data.packet) is None
        else:
            with pytest.raises(StandingUnavailable):
                await call(state.client, False, data.packet, capability=SECRET)
        (request,) = state.requests
        assert request.url.path == (LOOKUP if lookup else CLOSE) and request.method == "POST"
        assert request.content == data.packet.canonical_bytes() and request.url.query == b""
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers.get(CAP_HEADER) == (None if lookup else SECRET.hex())
        assert state.body.close_calls == 1 and state.body.closed.is_set()


@pytest.mark.parametrize("status", (409, 302, 503))
async def test_new_client_fixed_errors_never_read_echo_redirect_or_retry(closed_data, status):
    async with scenario(
        closed_data,
        raw=MARKER.encode(),
        status=status,
        extra={"Location": "https://untrusted.invalid"},
    ) as state:
        with pytest.raises(StandingConflict if status == 409 else StandingUnavailable) as caught:
            await call(state.client, False, closed_data.packet, capability=SECRET)
        safe(caught.value)
        assert len(state.requests) == 1 and not state.body.read_entered.is_set()
        assert state.body.close_calls == 1 and state.body.closed.is_set()


@pytest.mark.parametrize(
    "bad", ("media", "encoding", "length", "duplicate", "utf8", "oversize", "foreign")
)
async def test_new_response_transport_and_full_original_proof_are_strict(closed_data, factory, bad):
    data, extra, raw = closed_data, {}, closed_data.closure.canonical_bytes()
    if bad == "media":
        extra["Content-Type"] = "text/plain"
    elif bad == "encoding":
        extra["Content-Encoding"] = "gzip"
    elif bad == "length":
        extra["Content-Length"] = str(len(raw) + 1)
    elif bad == "duplicate":
        raw = b'{"schema_version":"1.0",' + raw[1:]
    elif bad == "utf8":
        raw = b"\xff"
    elif bad == "oversize":
        raw += b" " * (closure_type().wire_bytes + 1 - len(raw))
    else:
        other_env = await factory()
        other = await original(other_env)
        await prepared(other_env, other)
        async with journal(other_env) as client:
            raw = (await call(client, False, other, capability=SECRET)).canonical_bytes()
    async with scenario(data, raw=raw, extra=extra) as state:
        with pytest.raises(StandingUnavailable) as caught:
            await call(state.client, True, data.packet)
        safe(caught.value)
        assert state.body.close_calls == 1 and state.body.closed.is_set()


async def test_close_bad_actual_capability_types_refuse_before_any_transport(closed_data):
    async with scenario(closed_data) as state:
        for capability in (SECRET[:-1], bytearray(SECRET), SECRET.hex()):
            with pytest.raises(StandingUnavailable):
                await call(state.client, False, closed_data.packet, capability=capability)
        assert state.requests == []


@pytest.mark.parametrize("phase", ("acquire", "close"))
@pytest.mark.parametrize("refusal", ("cancel", "deadline"))
async def test_new_late_owner_retains_capacity_and_sole_close_after_public_refusal(
    closed_data, monkeypatch, phase, refusal
):
    async with scenario(closed_data) as state:
        state.hold = phase == "acquire"
        state.body.hold_close = phase == "close"
        loop, offset = asyncio.get_running_loop(), [0.0]
        real = loop.time
        with monkeypatch.context() as patch:
            patch.setattr(loop, "time", lambda: real() + offset[0])
            task = asyncio.create_task(
                call(state.client, False, closed_data.packet, capability=SECRET)
            )
            state.tasks.append(task)
            entered = state.entered if phase == "acquire" else state.body.close_entered
            await asyncio.wait_for(entered.wait(), 1)
            if refusal == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 1)
            else:
                offset[0] = 24
                await asyncio.sleep(0)
                assert not task.done()
                offset[0] = 26
                with pytest.raises(StandingUnavailable):
                    await asyncio.wait_for(asyncio.shield(task), 1)
        assert len(state.client._submit_exchanges) == 1
        if phase == "close":
            assert state.response.is_closed and not state.body.closed.is_set()
        state.release.set()
        state.body.release.set()
        await asyncio.wait_for(joined(state.client), 2)
        assert (
            len(state.requests) == 1 and state.body.close_calls == 1 and state.body.closed.is_set()
        )


async def test_new_original_is_detached_before_actual_late_acquisition(closed_data):
    data = closed_data
    packet = StandingExecutionIntent.from_json(data.packet.canonical_bytes())
    before = packet.canonical_bytes()
    async with scenario(data) as state:
        state.hold = True
        task = asyncio.create_task(call(state.client, False, packet, capability=SECRET))
        state.tasks.append(task)
        await asyncio.wait_for(state.entered.wait(), 1)
        object.__setattr__(packet.task, "objective", "Mutated after actual acquisition entered")
        state.release.set()
        assert await asyncio.wait_for(task, 2) == data.closure
        assert state.requests[0].content == before


async def test_new64_actual_client_owners_block_new_and_old_calls_until_real_cleanup(closed_data):
    data = closed_data
    entered, release = asyncio.Event(), asyncio.Event()
    requests, bodies, public = [], [], []

    async def handle(request):
        requests.append(request)
        if len(requests) <= 64:
            if len(requests) == 64:
                entered.set()
            await resistant(release)
        body = Body(data.closure.canonical_bytes())
        bodies.append(body)
        return httpx.Response(200, stream=body, headers={"Content-Type": "application/json"})

    async with journal(data.env, transport=httpx.MockTransport(handle)) as client:
        try:
            for index in range(64):
                public.append(
                    asyncio.create_task(
                        call(
                            client,
                            bool(index % 2),
                            data.packet,
                            **({} if index % 2 else {"capability": SECRET}),
                        )
                    )
                )
            await asyncio.wait_for(entered.wait(), 3)
            owners = set(client._submit_exchanges)
            assert len(owners) == len(requests) == 64
            for task in public:
                task.cancel()
            await asyncio.gather(*public, return_exceptions=True)
            assert client._submit_exchanges == owners
            with pytest.raises(StandingUnavailable):
                await call(client, False, data.packet, capability=SECRET)
            with pytest.raises(StandingUnavailable):
                await client.agent_control_state()
            query = AgentSubmitLookupRequest(
                schema_version="1.0",
                session_id=data.packet.task.task_session.session_id,
                thread_id=data.packet.task.task_session.thread_id,
                epoch=data.packet.task.task_session.epoch,
                idempotency_key="ordinary-unobserved",
            )
            with pytest.raises(AgentSubmitUnavailable):
                await client.lookup_submit(query)
            assert len(requests) == 64 and client._submit_exchanges == owners
            release.set()
            await asyncio.wait_for(joined(client), 3)
            assert len(bodies) == 64 and all(
                body.close_calls == 1 and body.closed.is_set() for body in bodies
            )
            assert await call(client, True, data.packet) == data.closure
            assert len(requests) == 65
        finally:
            release.set()
            for task in public:
                task.cancel()
            await asyncio.gather(*public, return_exceptions=True)


@pytest.mark.asyncio(loop_scope="function")
async def test_new_and_old_http_routes_share64_actual_threads_and_shutdown_joins(env, monkeypatch):
    closure_type()
    packet = await original(env)
    await prepared(env, packet)
    native = env.app.state.standing_task_owner
    lookup = getattr(native, "lookup_closure", None)
    assert callable(lookup), "Frozen native closure lookup capability is absent"
    loop = asyncio.get_running_loop()
    previous = getattr(loop, "_default_executor", None)
    pool = ThreadPoolExecutor(max_workers=64, thread_name_prefix="closure-http-oracle")
    loop.set_default_executor(pool)
    release, entered, lock = threading.Event(), threading.Event(), threading.Lock()
    count, requests, closing = 0, [], None

    def blocked(method):
        def invoke(*args, **kwargs):
            nonlocal count
            with lock:
                count += 1
                if count == 64:
                    entered.set()
            assert release.wait(10), "Owned native fixture gate expired"
            return method(*args, **kwargs)

        return invoke

    monkeypatch.setattr(native, "lookup_closure", blocked(lookup))
    dispatch = env.app.state.standing_dispatch_owner
    monkeypatch.setattr(dispatch, "snapshot", blocked(dispatch.snapshot))
    try:
        for index in range(64):
            pending = (
                send(env, LOOKUP, packet)
                if index % 2
                else env.client.get("/v1/standing/dispatch", headers=headers())
            )
            requests.append(asyncio.create_task(pending))
        async with asyncio.timeout(4):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        assert count == 64 and env.operations.active_operations == 64
        checked(await send(env, CLOSE, packet, cap=SECRET.hex()), 503)
        for request in requests:
            request.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        assert env.operations.active_operations == 64
        closing = asyncio.create_task(close_env(env))
        async with asyncio.timeout(2):
            while not env.operations._closed:
                await asyncio.sleep(0)
        assert not closing.done() and not native._closed
        release.set()
        await asyncio.wait_for(asyncio.shield(closing), 4)
        assert env.operations.active_operations == 0 and native._closed
        assert env.app.state.agent_control.instance is None
    finally:
        release.set()
        await asyncio.gather(*requests, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await close_env(env)
        if previous is not None:
            loop.set_default_executor(previous)
        pool.shutdown(wait=True)
