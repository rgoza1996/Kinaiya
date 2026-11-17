"""#623: actual launcher assembly and lifecycle, with synthetic native peers."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from packages.providers.text import ScriptedProvider
from services import launcher
from services.common.auth import Auth
from services.converse.dependencies import RequiredLiveDecisions
from services.converse.live import LiveConverseController
from services.session.api import create_app as session_app
from services.session.store import Store
from tests.integration.test_live_effect_http import TOKENS


@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize("rolling", [False, True])
async def test_actual_converse_host_uses_one_native_owner_and_its_api_lifespan(
    tmp_path, monkeypatch, qualified, rolling
):
    data, workspace = tmp_path / "data", tmp_path / "workspace"
    data.mkdir(mode=0o700)
    workspace.mkdir()
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "session_url": "http://session.local",
                "agent_url": "http://agent.local",
                "decisions_url": "http://decisions.local",
                "distribution": {"agent": False, "converse": True, "decisions": True},
                "agent_enabled": False,
                "streaming_windows": {"enabled": rolling},
            }
        )
    )
    store = Store(tmp_path / "session.sqlite")
    transport = httpx.ASGITransport(app=session_app(store, Auth(TOKENS)))
    made, calls = [], []
    original_client = httpx.AsyncClient

    async def route(request):
        calls.append((request.url.host, request.url.path))
        if request.url.host == "session.local":
            return await transport.handle_async_request(request)
        if request.url.host == "decisions.local" and request.url.path in {
            "/v1/decisions/readiness",
            "/v2/decisions/live-readiness",
        }:
            live = "/v2/" in request.url.path
            body = dict(
                contract_version=2 if live else 1,
                service="decisions",
                consumer="converse",
                instance_id="a" * 32,
                phase="ready",
                ready=True,
            )
            if live:
                body["protocol"] = "native_live_decisions_v2"
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=httpx.ByteStream(json.dumps(body).encode()),
            )
        raise AssertionError("Unexpected synthetic peer request")

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(route)
        return original_client(*args, **kwargs)

    provider = ScriptedProvider([])
    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(launcher, "provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(launcher, "load_or_create_tokens", lambda _p: TOKENS)
    monkeypatch.setattr(launcher, "ADAPTIVE_LIVE_ENABLED", qualified, raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda app, **_k: made.append(app))
    try:
        launcher.serve(
            SimpleNamespace(
                config=config,
                data_dir=data,
                workspace=workspace,
                service="converse",
                port=None,
                start_paused=False,
            )
        )
        assert len(made) == 1
        app = made[0]
        registry = app.state.live_streams
        runtime = registry.runtime
        controller = (
            runtime._window_text_ready.__self__ if runtime._window_text_ready is not None else None
        )
        async with app.router.lifespan_context(app):
            assert isinstance(controller, LiveConverseController) is qualified
            assert isinstance(registry.dependency, RequiredLiveDecisions) is qualified
            snapshot = runtime.capture_settings.snapshot()
            assert snapshot["adaptive_live_enabled"] is qualified
            assert snapshot["selected_mode"] == (
                "adaptive_live" if qualified else "rolling_windows" if rolling else "legacy_silence"
            )
            async with original_client(
                transport=httpx.ASGITransport(app=app), base_url="http://converse.local"
            ) as owner:
                assert (await owner.get("/health")).json()["streaming_windows"] is rolling
            async with asyncio.timeout(2):
                while not runtime.decisions_dependency.ready or (
                    qualified and (not registry.dependency.ready or not controller.running)
                ):
                    await asyncio.sleep(0)
            assert provider.calls == []
            assert store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
            assert registry.captures == {}
            assert registry.available() is False  # no selected STT interface was fabricated
        assert runtime.journal.http.is_closed
        assert runtime.decisions_dependency._closed
        if qualified:
            assert controller.closed and not controller.running
            assert registry.dependency._closed
            assert app.state.live_consumer_cleanup_confirmed is True
            assert ("decisions.local", "/v2/decisions/live-readiness") in calls
        else:
            assert ("decisions.local", "/v2/decisions/live-readiness") not in calls
        assert provider.calls == []
    finally:
        store.close()


@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize("rolling", [False, True])
async def test_actual_decisions_host_starts_native_workers_with_the_original_shared_owners(
    tmp_path, monkeypatch, qualified, rolling
):
    from services.decisions import api as decisions_api
    from services.decisions.live_agent import LiveAgentDispatcher
    from services.decisions.live_worker import LiveDecisionsWorker

    data, workspace = tmp_path / "data", tmp_path / "workspace"
    data.mkdir(mode=0o700)
    workspace.mkdir()
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "session_url": "http://session.local",
                "agent_url": "http://agent.local",
                "agent_enabled": False,
                "streaming_windows": {"enabled": rolling},
            }
        )
    )
    store = Store(tmp_path / "session.sqlite")
    transport = httpx.ASGITransport(app=session_app(store, Auth(TOKENS)))
    made, owners, calls = [], [], []
    original_client, original_app = httpx.AsyncClient, decisions_api.create_app

    async def route(request):
        calls.append((request.url.host, request.url.path))
        if request.url.host == "session.local":
            return await transport.handle_async_request(request)
        raise AssertionError("Idle native routing entered an Agent or model endpoint")

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(route)
        return original_client(*args, **kwargs)

    def app(worker, auth, windows, dispatcher, **kwargs):
        owners.append((worker, windows, dispatcher, kwargs))
        return original_app(worker, auth, windows, dispatcher, **kwargs)

    provider = ScriptedProvider([])
    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(decisions_api, "create_app", app)
    monkeypatch.setattr(launcher, "provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(launcher, "load_or_create_tokens", lambda _p: TOKENS)
    monkeypatch.setattr(launcher, "ADAPTIVE_LIVE_ENABLED", qualified)
    monkeypatch.setattr(uvicorn, "run", lambda app, **_k: made.append(app))
    try:
        launcher.serve(
            SimpleNamespace(
                config=config,
                data_dir=data,
                workspace=workspace,
                service="decisions",
                port=None,
                start_paused=False,
            )
        )
        worker, windows, dispatcher, bindings = owners[0]
        native, agent = bindings["live_worker"], bindings["live_dispatcher"]
        assert (windows is not None) is rolling
        assert (dispatcher is not None) is rolling
        assert isinstance(native, LiveDecisionsWorker) is qualified
        assert isinstance(agent, LiveAgentDispatcher) is qualified
        if qualified:
            assert native.journal is agent.journal is worker.journal
            assert native.agent is agent.agent is worker.agent
            assert native.model_settings is worker.model_settings is bindings["model_settings"]
            assert native.agent_enabled is False
        app = made[0]
        async with original_client(
            transport=httpx.ASGITransport(app=app),
            base_url="http://decisions.local",
            headers={"Authorization": "Bearer " + TOKENS["owner"]},
        ) as owner:
            assert (await owner.get("/v2/decisions/live-readiness")).json()["phase"] == (
                "starting" if qualified else "disabled"
            )
            async with app.router.lifespan_context(app):
                async with asyncio.timeout(3):
                    while (
                        not worker.ready
                        or (qualified and (not native.ready or not agent.ready))
                        or (rolling and not windows.ready)
                    ):
                        await asyncio.sleep(0)
                result = (await owner.get("/v2/decisions/live-readiness")).json()
                assert result["ready"] is qualified
                assert result["phase"] == ("ready" if qualified else "disabled")
                assert provider.calls == []
                assert not bindings["model_settings"].leases
                assert store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
            assert (await owner.get("/v2/decisions/live-readiness")).json()["phase"] == "stopped"
        assert not worker.running and bindings["model_settings"].closed
        if qualified:
            assert not native.running and not agent.running
            assert native.cleanup.done() and native.cleanup.result()
            assert agent.closed
        assert provider.calls == []
        assert all(host == "session.local" for host, _ in calls)
    finally:
        store.close()
