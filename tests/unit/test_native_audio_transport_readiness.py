"""Deterministic regressions for #468 native-audio transport dependency readiness."""

import sys
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from test_native_audio_converse import AUTH, NoLegacyText, body

from packages.contracts.models import SessionRequest
from packages.providers.realtime_audio import NativeAudioInput, RealtimeAudioProvider
from packages.providers.text import ProviderRefusal
from services.converse.api import create_app
from services.converse.native_audio import NativeAudioOwner
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.session.store import LocalJournal, Store


@asynccontextmanager
async def _native_environment(tmp_path, native):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    provider = NoLegacyText()
    runtime = ConverseRuntime(
        LocalJournal(store, "converse"),
        provider,
        personality="Public synthetic companion personality.",
        personality_scope="public_synthetic",
        context_budgeted=True,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    path = tmp_path / "native-audio.sqlite"
    owner = NativeAudioOwner(runtime, native, path=path, workspace=workspace)
    runtime.native_audio = owner
    controller = WindowConverseController(runtime)
    app = create_app(runtime, AUTH, None, controller)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://synthetic.invalid",
        headers={"Authorization": "Bearer " + AUTH.tokens["owner"]},
    ) as client:
        env = SimpleNamespace(
            store=store,
            session=session,
            provider=provider,
            runtime=runtime,
            native=native,
            owner=owner,
            controller=controller,
            http=client,
            path=path,
            workspace=workspace,
        )
        try:
            yield env
        finally:
            await owner.close()
            await controller.close()
            await runtime.close()
            store.close()


def _install_synthetic_websockets(monkeypatch, *, version="15.0.1", connect_callable=True):
    calls = []

    async def fake_connect(url, **options):
        calls.append((url, options))
        raise AssertionError("Readiness checks must never open a WebSocket connection")

    ws_root = ModuleType("websockets")
    if version is not None:
        ws_root.__version__ = version
    ws_asyncio = ModuleType("websockets.asyncio")
    ws_client = ModuleType("websockets.asyncio.client")
    ws_client.connect = fake_connect if connect_callable else "not-callable"
    ws_asyncio.client = ws_client
    ws_root.asyncio = ws_asyncio

    monkeypatch.setitem(sys.modules, "websockets", ws_root)
    monkeypatch.setitem(sys.modules, "websockets.asyncio", ws_asyncio)
    monkeypatch.setitem(sys.modules, "websockets.asyncio.client", ws_client)
    return calls


@pytest.mark.asyncio
async def test_missing_default_websockets_marks_health_unavailable_without_side_effects(
    tmp_path, monkeypatch
):
    monkeypatch.setitem(sys.modules, "websockets", None)
    monkeypatch.setitem(sys.modules, "websockets.asyncio", None)
    monkeypatch.setitem(sys.modules, "websockets.asyncio.client", None)

    provider = RealtimeAudioProvider("http://127.0.0.1:8817", "public-native-fixture")
    assert provider.transport_available() is False

    async with _native_environment(tmp_path, provider) as env:
        health = (await env.http.get("/health")).json()["native_audio"]
        assert health["configured"] is True
        assert health["available"] is False
        assert health["transport_available"] is False
        assert health["readiness_reason"] == "missing_transport"
        assert health["active"] == 0
        assert health["pending"] is False
        assert health["cleanup_failed"] is False

        refused = await env.http.post("/v1/converse/native-audio/clips", json=body(env))
        assert refused.status_code == 503
        assert refused.json() == {"detail": "Native audio is unavailable"}
        assert env.owner.db.execute("SELECT count(*) FROM native_audio_intents").fetchone()[0] == 0
        assert env.provider.counts == [] and env.provider.streams == []


@pytest.mark.parametrize(
    ("version", "connect_callable"),
    [
        ("13.1.0", True),
        ("invalid", True),
        (None, True),
        ("15.0.1", False),
    ],
)
@pytest.mark.asyncio
async def test_incompatible_websockets_transport_is_unavailable_and_refuses_cleanly(
    tmp_path, monkeypatch, version, connect_callable
):
    calls = _install_synthetic_websockets(
        monkeypatch, version=version, connect_callable=connect_callable
    )
    provider = RealtimeAudioProvider("http://127.0.0.1:8817", "public-native-fixture")
    assert provider.transport_available() is False

    async with _native_environment(tmp_path, provider) as env:
        status = env.owner.status()
        assert status["configured"] is True
        assert status["available"] is False
        assert status["transport_available"] is False
        assert status["readiness_reason"] == "missing_transport"

        with pytest.raises(
            ProviderRefusal, match="Native audio requires the optional websockets transport"
        ):
            await provider.transcribe(
                NativeAudioInput(b"\x00\x00" * 160, 16000),
                current=lambda: _async_true(),
            )
        assert calls == []


async def _async_true():
    return True


@pytest.mark.asyncio
async def test_valid_declared_transport_passes_readiness_without_probing_network(
    tmp_path, monkeypatch
):
    calls = _install_synthetic_websockets(monkeypatch, version="15.0.1", connect_callable=True)
    provider = RealtimeAudioProvider("http://127.0.0.1:8817", "public-native-fixture")
    assert provider.transport_available() is True

    async with _native_environment(tmp_path, provider) as env:
        health = (await env.http.get("/health")).json()["native_audio"]
        assert health["configured"] is True
        assert health["available"] is True
        assert health["transport_available"] is True
        assert health["readiness_reason"] == "ready"
        assert health["active"] == 0 and health["pending"] is False
        assert calls == []
        assert env.owner.db.execute("SELECT count(*) FROM native_audio_intents").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_default_host_without_native_audio_keeps_unchanged_health_shape(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    runtime = ConverseRuntime(LocalJournal(store, "converse"), NoLegacyText())
    app = create_app(runtime, AUTH)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8762",
        ) as client:
            health = (await client.get("/health")).json()["native_audio"]
            assert health == {
                "configured": False,
                "available": False,
                "mode": "buffered_native_audio",
                "input_sample_rate": 16000,
                "max_seconds": 30,
            }
    finally:
        await runtime.close()
        store.close()


@pytest.mark.asyncio
async def test_non_boolean_or_raising_provider_transport_check_fails_closed(tmp_path):
    provider = SimpleNamespace(
        info=NoLegacyText.info,
        model=NoLegacyText.info.model,
        base_url="http://127.0.0.1:8817",
        transcribe=lambda *args, **kwargs: None,
        respond=lambda *args, **kwargs: None,
        close=lambda: _async_true(),
        transport_available=lambda: "truthy-string-is-not-bool",
    )
    async with _native_environment(tmp_path, provider) as env:
        status = env.owner.status()
        assert status["available"] is False
        assert status["transport_available"] is False
        assert status["readiness_reason"] == "missing_transport"
