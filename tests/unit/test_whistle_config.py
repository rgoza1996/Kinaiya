"""Explicit managed selection; construction only, no worker or model execution."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from packages.resources.processes import WorkerSpec
from services.common.auth import Auth
from services.resources import launcher
from services.resources.whistle import WhistleEndpoint


@pytest.mark.parametrize("selection", ["whisper", "whistle", "missing", "wrong-port"])
async def test_resources_selects_only_explicit_matching_whistle_and_preserves_voice(
    tmp_path, monkeypatch, selection
):
    clients, selected, endpoints = [], [], []
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        value = original(*args, **kwargs)
        clients.append(value)
        return value

    def factory(settings, root, workspace):
        selected.append(settings)
        directory = tmp_path / "whistle-ipc"
        directory.mkdir(mode=0o700)
        endpoint = WhistleEndpoint(directory)
        endpoints.append(endpoint)
        return WorkerSpec(
            "whistle", (sys.executable, "-c", "raise AssertionError('no launch')"), root
        ), endpoint

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(launcher, "whistle_worker", factory)
    resources = {"enabled": True, "voice_python": sys.executable, "voice_port": 8915}
    voice = {"url": "http://127.0.0.1:8915", "profile": "Kokoro 82M"}
    if selection != "whisper":
        resources["whistle"] = {
            "python": sys.executable,
            "weights": "/owner/whistle.cact",
            "library": "/owner/libneedle3.so",
        }
    if selection in {"whistle", "wrong-port"}:
        voice["stt"] = {
            "provider": "whistle",
            "url": "http://127.0.0.1:" + ("8914" if selection == "whistle" else "8916"),
        }
    config = {
        "resources": resources,
        "resources_url": "http://127.0.0.1:8914",
        "voice": voice,
        "session_url": "http://127.0.0.1:8910",
        "agent_url": "http://127.0.0.1:8911",
        "converse_url": "http://127.0.0.1:8912",
    }
    args = SimpleNamespace(data_dir=tmp_path / "data", workspace=tmp_path / "agent", port=8914)
    tokens = {name: name[0] * 40 for name in ("owner", "resources", "converse")}
    app = None
    try:
        if selection in {"missing", "wrong-port"}:
            with pytest.raises(ValueError, match="Select Whistle STT"):
                launcher.resource_app(args, config, Auth(tokens), tokens, Path.cwd())
            assert not selected
            return
        app = launcher.resource_app(args, config, Auth(tokens), tokens, Path.cwd())
        status = app.state.coordinator.manager.status()["workers"]
        assert set(status) == ({"voice", "whistle"} if selection == "whistle" else {"voice"})
        assert all(row["state"] == "stopped" for row in status.values())
        assert bool(app.state.whistle) == bool(selected) == (selection == "whistle")
        assert voice["url"] == "http://127.0.0.1:8915" and voice["profile"] == "Kokoro 82M"
    finally:
        if app is not None:
            try:
                await app.state.coordinator.close()
            finally:
                owner = app.state.coordinator._close_owner
                if owner is not None:
                    await asyncio.shield(owner)
            assert app.state.coordinator._retirement_complete
            assert app.state.coordinator.manager._closed
        for endpoint in endpoints:
            assert endpoint.ready is None
            assert not endpoint.stream_closes.pending and not endpoint.stream_closes.owners
            assert not endpoint.stream_closes.failed
            endpoint.close()
            assert not endpoint.directory.exists()
        for connection in clients:
            await connection.aclose()
