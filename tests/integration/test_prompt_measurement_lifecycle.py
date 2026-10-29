"""Production resource ownership/API with synthetic worker and count protocol.

No process, socket, model or external service is used. Native HTTP ownership has
its own endpoint tests; these checks do not establish tokenizer compatibility.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from test_text_resource_barrier import BINDING, Control, Manager, wait_until

from packages.providers.prompt_budget import PromptTokenCount, binding_digest, request_digest
from services.common.auth import Auth
from services.resources.api import create_app
from services.resources.coordinator import ResourceCoordinator
from services.resources.gguf import GGUFProtocol
from services.resources.managed_text import ManagedTextBroker
from services.resources.state import ResourceState

MESSAGES = [
    {"role": "system", "content": "Public synthetic personality"},
    {"role": "user", "content": "Public synthetic question"},
]
BODY = {"model": "synthetic", "messages": MESSAGES, "max_tokens": 128, "stream": True}
TOKENS = {role: "synthetic-" + role + "-" * 30 for role in ("owner", "converse", "agent")}
PATH = "/v1/chat/completions/input_tokens"


def request(raw=None, *, headers=None):
    queue = asyncio.Queue()
    queue.put_nowait(
        {"type": "http.request", "body": json.dumps(BODY).encode() if raw is None else raw}
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": PATH,
        "headers": headers if headers is not None else [(b"content-type", b"application/json")],
    }
    return Request(scope, queue.get), queue


class Protocol:
    worker_id = "gguf"
    max_request_bytes = 512 * 1024

    def __init__(self):
        self.endpoint = SimpleNamespace(
            manifest={"alias": "synthetic"}, limits={"max_new_tokens": 128}
        )
        self.native = GGUFProtocol(self.endpoint, BINDING["manifest_sha256"])
        self.entered, self.release, self.closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.release.set()
        self.close_gate = asyncio.Event()
        self.close_gate.set()
        self.count, self.error, self.identity = 1920, None, None
        self.calls = []

    def policy(self, coordinator):
        return coordinator.text_policy()

    async def resident(self, coordinator, policy):
        return await coordinator.resident_text(policy)

    async def admit(self, *args):
        pytest.fail("Measurement must never enter allocating admission")

    def parse(self, raw):
        return self.native.parse(raw)

    def ready_for(self, identity):
        self.identity = identity
        return identity

    def current(self, lease, identity):
        if lease is not self.identity or identity is not self.identity:
            raise ValueError("Synthetic stale lease")

    async def measure_prompt(self, messages, identity, lease):
        self.calls.append(messages)
        self.entered.set()
        try:
            await self.release.wait()
            if self.error is not None:
                raise self.error
            if self.count is None:
                return None
            return PromptTokenCount(
                self.count, 2048, 128, request_digest(BODY), binding_digest(BINDING)
            )
        finally:
            self.closing.set()
            await self.close_gate.wait()


@pytest.fixture
async def host(tmp_path):
    manager, protocol = Manager(), Protocol()
    coordinator = ResourceCoordinator(
        manager,
        ResourceState(tmp_path / "control/resources.json"),
        Control(BINDING),
        text_worker_id="gguf",
        managed_text_binding=BINDING,
        memory_probe=lambda: 1024**3,
        text_restore_reserve_bytes=512 * 1024**2,
    )
    broker = ManagedTextBroker(coordinator, protocol.endpoint, protocol, request_timeout_s=5)

    async def released(identity, result):
        await protocol.close_gate.wait()
        return True

    coordinator.register_text_lifecycle(
        abort=broker.abort_active, drain=broker.drain, release=released
    )
    await coordinator.initialize()
    manager.starts.clear()
    manager.stops.clear()
    value = SimpleNamespace(
        manager=manager, protocol=protocol, coordinator=coordinator, broker=broker
    )
    yield value
    protocol.release.set()
    protocol.close_gate.set()
    await broker.close()
    await coordinator.close()


async def test_resident_count_finishes_without_generation_start_or_stop(host):
    old = host.manager.owned_identity("gguf")
    result = await host.broker.measure_prompt(request()[0])
    assert result["input_tokens"] == 1920 and result["reserved_output_tokens"] == 128
    assert host.protocol.calls == [MESSAGES]
    assert host.manager.starts == host.manager.stops == []
    assert host.manager.owned_identity("gguf") is old and host.broker.operation is None


async def test_cold_worker_refuses_without_allocation_stop_or_native_request(host):
    await host.coordinator.stop_text()
    host.manager.stops.clear()
    with pytest.raises(HTTPException) as caught:
        await host.broker.measure_prompt(request()[0])
    assert caught.value.status_code == 503
    assert host.manager.starts == host.manager.stops == host.protocol.calls == []
    assert host.broker.operation is None


async def test_unsupported_route_preserves_resident_after_real_close(host):
    old = host.manager.owned_identity("gguf")
    host.protocol.count = None
    host.protocol.close_gate.clear()
    task = asyncio.create_task(host.broker.measure_prompt(request()[0]))
    await host.protocol.closing.wait()
    assert not task.done() and host.broker.operation is not None
    with pytest.raises(HTTPException) as busy:
        await host.broker.stream(request()[0])
    assert busy.value.status_code == 409
    host.protocol.close_gate.set()
    with pytest.raises(HTTPException) as caught:
        await task
    assert caught.value.status_code == 503
    assert host.manager.owned_identity("gguf") is old
    assert host.manager.starts == host.manager.stops == [] and host.broker.operation is None


@pytest.mark.parametrize("mode", ["cancel", "disconnect", "off", "deadline"])
async def test_abort_retains_captured_worker_and_close_owner(host, mode):
    old = host.manager.owned_identity("gguf")
    host.protocol.release.clear()
    host.protocol.close_gate.clear()
    if mode == "deadline":
        host.broker.timeout = 0.05
    req, queue = request()
    task = asyncio.create_task(host.broker.measure_prompt(req))
    await host.protocol.entered.wait()
    op = host.broker.operation
    action = None
    if mode == "cancel":
        task.cancel()
    elif mode == "disconnect":
        queue.put_nowait({"type": "http.disconnect"})
    elif mode == "off":
        action = asyncio.create_task(
            host.coordinator.action(
                {"operation": "converse", "enabled": False, "idempotency_key": "off-count"}
            )
        )
    await host.protocol.closing.wait()
    assert host.broker.operation is op and not op.cleanup_confirmed
    assert host.manager.starts == []
    assert all(identity is old for _, identity in host.manager.stops)
    host.protocol.close_gate.set()
    with pytest.raises((asyncio.CancelledError, HTTPException)):
        await task
    if action is not None:
        assert (await action)["ok"]
    await wait_until(lambda: host.broker.operation is None)
    assert host.manager.owned_identity("gguf") is None


async def test_cancelled_resident_lookup_releases_without_waiting_for_allocation_lock(host):
    await host.coordinator.admission_lock.acquire()
    old = host.manager.owned_identity("gguf")
    task = asyncio.create_task(host.broker.measure_prompt(request()[0]))
    try:
        await wait_until(
            lambda: (
                host.broker.operation is not None and host.broker.operation.admission is not None
            )
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host.broker.operation is None
        assert host.manager.starts == host.manager.stops == host.protocol.calls == []
        assert host.manager.owned_identity("gguf") is old
    finally:
        host.coordinator.admission_lock.release()


@pytest.mark.parametrize("revocation", ["off", "identity_lost"])
async def test_completed_count_rechecks_selection_after_disconnect_retirement(host, revocation):
    first, retirement_waiting, retired = True, asyncio.Event(), asyncio.Event()

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": json.dumps(BODY).encode()}
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            retirement_waiting.set()
            await retired.wait()
            raise

    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": PATH,
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )
    task = asyncio.create_task(host.broker.measure_prompt(req))
    off = None
    try:
        await retirement_waiting.wait()
        assert host.protocol.closing.is_set()
        assert host.broker.operation.closed.is_set() and not task.done()
        if revocation == "off":
            off = asyncio.create_task(
                host.coordinator.action(
                    {"operation": "converse", "enabled": False, "idempotency_key": "retire-off"}
                )
            )
            await wait_until(lambda: not host.coordinator.intent.read().enabled)
        else:
            await host.manager.stop("gguf")
        retired.set()
        with pytest.raises(HTTPException) as caught:
            await task
        assert caught.value.status_code == 503
        if off is not None:
            assert (await off)["ok"]
        assert host.broker.operation is None and host.manager.starts == []
    finally:
        retired.set()
        await asyncio.gather(task, *([off] if off is not None else []), return_exceptions=True)


@pytest.mark.parametrize(
    "raw,status", [(b"{}", 400), (b"{" * 1000, 400), (b"x" * (512 * 1024 + 1), 413)]
)
async def test_bounded_invalid_body_releases_without_native_or_stop(host, raw, status):
    with pytest.raises(HTTPException) as caught:
        await host.broker.measure_prompt(request(raw)[0])
    assert caught.value.status_code == status and host.broker.operation is None
    assert host.manager.starts == host.manager.stops == host.protocol.calls == []


@pytest.mark.parametrize(
    "role,origin,status",
    [(None, None, 401), ("agent", None, 403), ("owner", "https://untrusted.invalid", 403)],
)
async def test_api_auth_and_origin_precede_body_receipt(host, role, origin, status):
    app = create_app(host.coordinator, Auth(TOKENS), gguf=host.broker)
    headers = [(b"content-type", b"application/json")]
    if role:
        headers.append((b"authorization", ("Bearer " + TOKENS[role]).encode()))
    if origin:
        headers.append((b"origin", origin.encode()))
    received, sent = [], []

    async def receive():
        received.append(True)
        pytest.fail("Unauthorized request body must not be received")

    async def send(value):
        sent.append(value)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": PATH,
            "raw_path": PATH.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "server": ("127.0.0.1", 80),
            "client": ("127.0.0.1", 1234),
        },
        receive,
        send,
    )
    assert not received and sent[0]["status"] == status
    assert not host.protocol.calls and host.broker.operation is None


@pytest.mark.parametrize("role", ["owner", "converse"])
async def test_authenticated_http_result_is_bounded_versioned_and_no_store(host, role):
    app = create_app(host.coordinator, Auth(TOKENS), gguf=host.broker)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://synthetic"
    ) as client:
        response = await client.post(
            PATH, json=BODY, headers={"Authorization": "Bearer " + TOKENS[role]}
        )
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert len(response.content) <= 4096
    result = response.json()
    assert result["version"] == 1 and result["input_tokens"] == 1920
    assert result["request_sha256"] == request_digest(BODY)
    assert result["binding_sha256"] == binding_digest(BINDING)
    assert host.manager.starts == host.manager.stops == []


async def test_production_provider_wrapper_and_asgi_broker_share_exact_count_binding(host):
    from packages.providers.gguf import ManagedGGUFProvider
    from services.resources.providers import ResourceBoundProvider

    app = create_app(host.coordinator, Auth(TOKENS), gguf=host.broker)
    provider = ManagedGGUFProvider(
        BINDING["resources_url"],
        TOKENS["converse"],
        model=BINDING["model"],
        alias=BODY["model"],
        profile=BINDING["profile"],
        manifest_sha256=BINDING["manifest_sha256"],
        context_tokens=BINDING["context_tokens"],
        max_new_tokens=BINDING["max_new_tokens"],
        transport=httpx.ASGITransport(app),
    )
    selected = ResourceBoundProvider(provider, host.coordinator.state.path, "converse")
    try:
        result = await selected.measure_prompt(MESSAGES)
        assert result.fits and result.input_tokens == 1920
        assert result.request_sha256 == request_digest(BODY)
        assert result.binding_sha256 == binding_digest(BINDING)
        assert host.protocol.calls == [MESSAGES]
        assert host.broker.operation is None
        assert host.manager.starts == host.manager.stops == []
    finally:
        await provider.close()


@pytest.mark.parametrize("count", [1920, 1921, None])
async def test_complete_managed_count_path_with_synthetic_native_http(
    host, tmp_path, monkeypatch, count
):
    from packages.providers.gguf import ManagedGGUFProvider
    from packages.providers.prompt_budget import PromptCountUnavailable
    from services.resources import gguf
    from services.resources.providers import ResourceBoundProvider

    identity = host.manager.owned_identity("gguf")
    directory = tmp_path / "native"
    directory.mkdir(mode=0o700)
    (directory / "ipc").mkdir(mode=0o700)
    native = gguf.GGUFEndpoint(
        directory,
        {
            "alias": BODY["model"],
            "model": BINDING["model"],
            "profile": BINDING["profile"],
            "assets": [{"guest": "/runtime/llama-server", "bytes": 12, "sha256": "a" * 64}],
        },
    )

    class BorrowedLease:
        # Synthetic identity only: no descriptor, socket or process is acquired.
        socket_path = native.ipc / "model.sock"

        def bind(self, selected):
            self.current(selected)
            return self

        def current(self, selected):
            assert selected is identity and host.manager.is_current(identity)

    lease = BorrowedLease()
    lease.identity = identity
    native.ready = lease
    requests, streams, transports = [], [], []

    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield json.dumps({"input_tokens": count, "object": "response.input_tokens"}).encode()

        async def aclose(self):
            self.closed = True

    class Transport(httpx.AsyncBaseTransport):
        cleanup_pending = False
        closed = False

        def __init__(self, *args):
            transports.append(self)

        async def handle_async_request(self, request):
            requests.append(request)
            stream = Body()
            streams.append(stream)
            return httpx.Response(404 if count is None else 200, stream=stream)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(gguf, "OwnedUnixTransport", Transport)
    old_protocol, old_endpoint = host.broker.protocol, host.broker.endpoint
    host.broker.protocol = gguf.GGUFProtocol(native, BINDING["manifest_sha256"])
    host.broker.endpoint = native
    app = create_app(host.coordinator, Auth(TOKENS), gguf=host.broker)
    provider = ManagedGGUFProvider(
        BINDING["resources_url"],
        TOKENS["converse"],
        model=BINDING["model"],
        alias=BODY["model"],
        profile=BINDING["profile"],
        manifest_sha256=BINDING["manifest_sha256"],
        transport=httpx.ASGITransport(app),
    )
    selected = ResourceBoundProvider(provider, host.coordinator.state.path, "converse")
    try:
        if count is None:
            with pytest.raises(PromptCountUnavailable):
                await selected.measure_prompt(MESSAGES)
        else:
            result = await selected.measure_prompt(MESSAGES)
            assert result.input_tokens == count and result.fits is (count == 1920)
            assert result.request_sha256 == request_digest(BODY)
            assert result.binding_sha256 == binding_digest(BINDING)
        assert len(requests) == 1 and requests[0].url.path == PATH
        assert json.loads(requests[0].content) == BODY
        assert streams[0].closed and transports[0].closed and not native.exchanges
        assert host.broker.operation is None
        assert host.manager.owned_identity("gguf") is identity
        assert host.manager.starts == host.manager.stops == []
    finally:
        await provider.close()
        host.broker.protocol, host.broker.endpoint = old_protocol, old_endpoint
        native.ready = None  # Borrowed fixture identity owns no real handles.
        native.close()
