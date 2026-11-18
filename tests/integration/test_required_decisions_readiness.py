"""#566 required-service control plane, with no provider inference."""

import asyncio
import json
import socket
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from packages.contracts.models import AppendRequest, SessionRequest
from packages.providers.decision_capture import CaptureNativeUnavailable
from packages.providers.text import ScriptedProvider
from services import launcher
from services.common.auth import Auth
from services.converse.api import create_app as converse_app
from services.converse.dependencies import RequiredDecisions
from services.converse.intent import ConverseIntent
from services.converse.runtime import ConverseRuntime
from services.decisions import api as decisions_api
from services.decisions.api import create_app
from services.distribution.start_supervisor import _probe_health
from services.session.api import create_app as session_app
from services.session.store import Store

TOKENS = {role: role[0] * 40 for role in ("owner", "converse", "agent", "decisions", "resources")}


class ControlledWorker:
    def __init__(self):
        self.running = self.ready = False
        self.enabled = True
        self.last_error = None
        self.classifier = SimpleNamespace(provider=ScriptedProvider([]))

    async def run(self):
        self.running = self.ready = True
        try:
            await asyncio.Event().wait()
        finally:
            self.running = self.ready = False


def test_authenticated_routing_readiness_comes_from_live_lifespan():
    worker = ControlledWorker()
    app = create_app(worker, Auth(TOKENS))
    headers = {"Authorization": "Bearer " + TOKENS["converse"]}
    with TestClient(app) as client:
        response = client.get("/v1/decisions/readiness", headers=headers)
        assert response.status_code == 200
        assert response.json()["ready"] is True
        assert response.json()["consumer"] == "converse"
    assert worker.running is False and worker.ready is False


@pytest.mark.parametrize("role", [None, "wrong", "agent", "decisions", "resources"])
def test_only_owner_or_converse_can_observe_required_readiness(role):
    app = create_app(ControlledWorker(), Auth(TOKENS))
    headers = {} if role is None else {"Authorization": "Bearer " + TOKENS.get(role, "x" * 40)}
    with TestClient(app) as client:
        response = client.get("/v1/decisions/readiness", headers=headers)
        assert response.status_code == (401 if role in {None, "wrong"} else 403)


def test_stopped_or_uninitialized_workers_cannot_report_ready():
    worker = ControlledWorker()
    windows = ControlledWorker()
    app = create_app(worker, Auth(TOKENS), window_worker=windows)
    headers = {"Authorization": "Bearer " + TOKENS["owner"]}
    client = TestClient(app)
    assert client.get("/v1/decisions/readiness", headers=headers).json()["phase"] == "starting"
    with client:
        worker.enabled = False  # Agent dispatch policy is not Converse readiness.
        assert client.get("/v1/decisions/readiness", headers=headers).json()["ready"] is True
        windows.ready = False
        assert client.get("/v1/decisions/readiness", headers=headers).json()["ready"] is False
        windows.ready = True
        worker.running = False
        assert client.get("/v1/decisions/readiness", headers=headers).json()["ready"] is False
    stopped = client.get("/v1/decisions/readiness", headers=headers)
    assert stopped.json()["phase"] == "stopped" and stopped.json()["ready"] is False


def test_readiness_checks_configuration_without_a_model_lease_or_probe():
    class Models:
        unavailable = False
        reads = leases = probes = 0

        def cache_stamp(self):
            self.reads += 1
            if self.unavailable:
                raise CaptureNativeUnavailable
            return "a" * 64

        def operation(self):
            self.leases += 1
            pytest.fail("Readiness acquired an inference lease")

        async def close(self):
            pass

    worker, models = ControlledWorker(), Models()

    async def probe():
        models.probes += 1
        pytest.fail("Readiness probed the model")

    worker.classifier.provider.health = probe
    app = create_app(worker, Auth(TOKENS), model_settings=models)
    headers = {"Authorization": "Bearer " + TOKENS["converse"]}
    with TestClient(app) as client:
        good = client.get("/v1/decisions/readiness", headers=headers)
        assert good.json()["ready"] is True
        assert good.headers["cache-control"] == "no-store"
        public = client.get("/health/runtime")
        assert public.json() == {"service": "decisions", "routing_runtime_ready": True}
        assert public.headers["cache-control"] == "no-store"
        models.unavailable = True
        bad = client.get("/v1/decisions/readiness", headers=headers)
        assert bad.status_code == 200 and bad.json()["phase"] == "unavailable"
        assert client.get("/health/runtime").json() == {
            "service": "decisions",
            "routing_runtime_ready": False,
        }
    assert models.reads == 4 and models.leases == models.probes == 0


@asynccontextmanager
async def loopback(app, *, port=0):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    root = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, lifespan="on", log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    task.result()
                    raise AssertionError("Temporary service stopped before readiness")
                await asyncio.sleep(0.01)
        yield root
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            sock.close()
        assert task.done()


@pytest.mark.parametrize(
    "mode,legacy_enabled,dispatch_enabled",
    [
        ("converse-decisions", True, False),
        ("decisions", True, False),
        ("decisions", None, False),
        ("all", True, True),
        ("all", False, False),
    ],
)
async def test_captured_topology_fences_native_agent_routing(
    tmp_path, monkeypatch, mode, legacy_enabled, dispatch_enabled
):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    store = Store(data / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    turn = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.converse",
            payload={"text": "Public synthetic response", "final": True},
            idempotency_key="topology-test-turn",
        ),
        "converse",
    )
    observed = []
    previous_agent = FastAPI()

    @previous_agent.get("/v1/agent/tasks")
    async def status():
        observed.append("status")
        return []

    fixture = ScriptedProvider([])
    monkeypatch.setattr(launcher, "load_or_create_tokens", lambda _: TOKENS)
    monkeypatch.setattr(launcher, "provider", lambda *_args, **_kwargs: fixture)
    monkeypatch.setattr(launcher, "ADAPTIVE_LIVE_ENABLED", True)
    apps = []
    live_dispatchers = []

    def build(*args, **kwargs):
        live_dispatchers.append(kwargs["live_dispatcher"])
        return create_app(*args, **kwargs)

    monkeypatch.setattr(decisions_api, "create_app", build)
    monkeypatch.setattr(uvicorn, "run", lambda app, **_: apps.append(app))

    async def check(app):
        async with (
            loopback(app) as decisions,
            httpx.AsyncClient(
                base_url=decisions,
                headers={"Authorization": "Bearer " + TOKENS["owner"]},
                trust_env=False,
                timeout=2,
            ) as client,
        ):
            report, readiness = {}, {}
            try:
                async with asyncio.timeout(5):
                    while report.get("cursor", 0) < turn.sequence or not readiness.get("ready"):
                        status_response = await client.get("/v1/decisions/status")
                        status_response.raise_for_status()
                        report = status_response.json()
                        readiness = (await client.get("/v1/decisions/readiness")).json()
                        await asyncio.sleep(0.02)
            except TimeoutError:
                pytest.fail(
                    f"Decisions did not process the turn: {report}, {readiness}, {observed}"
                )
            assert (report["enabled"], observed) == (
                dispatch_enabled,
                ["status"] if dispatch_enabled else [],
            )

    try:
        async with (
            loopback(session_app(store, Auth(TOKENS))) as journal,
            loopback(previous_agent) as agent,
        ):
            config = tmp_path / "config.json"
            host = {
                "distribution": {"agent": True, "decisions": True, "converse": True},
                "session_url": journal,
            }
            if legacy_enabled is not None:
                host.update(agent_enabled=legacy_enabled, agent_url=agent)
            original_config = json.dumps(host).encode()
            config.write_bytes(original_config)
            launcher.serve(
                SimpleNamespace(
                    data_dir=data,
                    workspace=tmp_path / "workspace",
                    config=config,
                    service="decisions",
                    port=None,
                    start_paused=False,
                    startup_subsystem_mode=mode,
                ),
            )
            (app,) = apps
            await check(app)
            (live_dispatcher,) = live_dispatchers
            assert (live_dispatcher is not None) is dispatch_enabled
            assert config.read_bytes() == original_config
    finally:
        store.close()
    assert fixture.calls == []


async def test_real_http_peer_loss_restart_and_resource_wake_cannot_bypass_dependency(tmp_path):
    decisions = AsyncExitStack()
    provider = ScriptedProvider([])
    runtime = None
    try:
        origin = await decisions.enter_async_context(
            loopback(create_app(ControlledWorker(), Auth(TOKENS)))
        )
        peer_port = int(origin.rsplit(":", 1)[1])
        gate = RequiredDecisions(origin, TOKENS["converse"])
        runtime = ConverseRuntime(
            None,
            provider,
            intent=ConverseIntent(tmp_path / "intent.json"),
            decisions_dependency=gate,
        )
        before = runtime.intent.path.read_bytes()
        assert not runtime.enabled
        await gate.start()
        old = runtime.admission()
        async with loopback(converse_app(runtime, Auth(TOKENS))) as converse:
            async with httpx.AsyncClient(base_url=converse, trust_env=False, timeout=2) as client:
                health = (await client.get("/health")).json()
                assert health["enabled"] and health["decisions"]["ready"]
                await decisions.aclose()
                await gate.refresh()
                health = (await client.get("/health")).json()
                assert health["enabled"] is False and not runtime.admission_current(old)
                wake = await client.post(
                    "/v1/converse/enabled",
                    headers={"Authorization": "Bearer " + TOKENS["resources"]},
                    json={"enabled": True},
                )
                assert wake.status_code == 200 and wake.json()["enabled"] is False
                refused = await client.post(
                    "/v1/converse/turns",
                    headers={"Authorization": "Bearer " + TOKENS["owner"]},
                    json={
                        "session_id": str(uuid4()),
                        "thread_id": str(uuid4()),
                        "epoch": 1,
                        "text": "Public synthetic greeting",
                        "idempotency_key": "dependency-off",
                    },
                )
                assert refused.status_code == 409
                await decisions.enter_async_context(
                    loopback(create_app(ControlledWorker(), Auth(TOKENS)), port=peer_port)
                )
                await gate.refresh()
                recovered = (await client.get("/health")).json()
                assert recovered["enabled"] is True and recovered["decisions"]["ready"]
                assert runtime.admission_current(old) is False
                assert runtime.admission_current(runtime.admission())
                assert runtime.intent.path.read_bytes() == before
    finally:
        if runtime is not None:
            await runtime.close()
        await decisions.aclose()
    assert provider.calls == []


@pytest.mark.parametrize("startup_mode", [None, "converse-decisions"])
def test_real_launcher_cleanup_continues_after_dependency_close_refuses(
    tmp_path, monkeypatch, startup_mode
):
    from services.converse import api

    original_app = api.create_app
    observed = []

    def build(runtime, *args, **kwargs):
        assert (runtime.task_context_client is not None) is (startup_mode is None)
        dependency_close = runtime.decisions_dependency.close
        model_close = runtime.model_settings.close

        async def failed_close():
            await dependency_close()
            observed.append("dependency")
            raise RuntimeError("Synthetic dependency teardown refusal")

        async def close_model():
            observed.append("model")
            await model_close()

        runtime.decisions_dependency.close = failed_close
        runtime.model_settings.close = close_model
        app = original_app(runtime, *args, **kwargs)

        def listen(*args, **kwargs):
            async def lifecycle():
                with pytest.raises(RuntimeError, match="Synthetic dependency teardown refusal"):
                    async with app.router.lifespan_context(app):
                        await asyncio.sleep(0)
                assert runtime.decisions_dependency._task.done()
                assert runtime._memory_shutdown.done()
                assert runtime.journal.http.is_closed
                if startup_mode is None:
                    assert runtime._task_context_shutdown.done()
                else:
                    assert runtime._task_context_shutdown is None
                assert "model" in observed

            asyncio.run(lifecycle())

        monkeypatch.setattr(uvicorn, "run", listen)
        return app

    monkeypatch.setattr(api, "create_app", build)
    monkeypatch.setattr(launcher, "provider", lambda *_args, **_kwargs: ScriptedProvider([]))
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "session_url": "http://127.0.0.1:1",
                "agent_url": "http://127.0.0.1:1",
                "decisions_url": "http://127.0.0.1:1",
            }
        )
    )
    launcher.serve(
        SimpleNamespace(
            data_dir=tmp_path,
            workspace=tmp_path / "workspace",
            config=config,
            service="converse",
            port=None,
            start_paused=False,
            startup_subsystem_mode=startup_mode,
        )
    )
    assert observed.count("dependency") == 2 and observed.count("model") == 1


async def test_startup_probe_observes_actual_routing_readiness_over_http():
    worker = ControlledWorker()
    app = create_app(worker, Auth(TOKENS))
    async with loopback(app) as root:
        port = urlsplit(root).port
        assert await asyncio.to_thread(_probe_health, "decisions", port, require_identity=True)
        worker.ready = False
        assert not await asyncio.to_thread(_probe_health, "decisions", port, require_identity=True)
        worker.ready = True
        assert await asyncio.to_thread(_probe_health, "decisions", port, require_identity=True)
    assert not await asyncio.to_thread(_probe_health, "decisions", port, require_identity=True)
