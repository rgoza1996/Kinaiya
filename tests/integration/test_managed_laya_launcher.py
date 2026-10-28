"""Controller wiring with synthetic HTTP/process receipts, no real model or PID."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from packages.providers.laya_artifact import ARTIFACT_HEADER, WORKER_PID_HEADER
from services.common.auth import Auth
from services.resources import launcher


@pytest.mark.parametrize("wrong_pid", [None, "voice", "laya", "occupied"])
async def test_launcher_requires_both_owned_endpoints_before_ready(
    tmp_path, monkeypatch, wrong_pid
):
    model = tmp_path / "model"
    model.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    workspace = tmp_path / "agent"
    workspace.mkdir()
    monkeypatch.setenv("SYNTHETIC_LAYA_KEY", "fixture-laya-key")
    config = {
        "session_url": "http://127.0.0.1:8910",
        "agent_url": "http://127.0.0.1:8911",
        "converse_url": "http://127.0.0.1:8912",
        "voice": {"url": "http://127.0.0.1:8915"},
        "providers": {
            "decisions": {
                "kind": "laya",
                "base_url": "http://127.0.0.1:8916",
                "model": "synthetic",
                "expected_artifact_sha256": "a" * 64,
                "api_key_env": "SYNTHETIC_LAYA_KEY",
            }
        },
        "resources": {
            "enabled": True,
            "voice_python": sys.executable,
            "voice_port": 8915,
            "laya": {
                "python": sys.executable,
                "model_dir": str(model),
                "manifest": str(manifest),
                "port": 8916,
            },
        },
    }
    enabled = True

    def transport(request):
        nonlocal enabled
        headers = {}
        if request.url.port == 8915:
            body = {
                "service": "local-voice",
                "pid": 999 if wrong_pid == "voice" else 42,
                "device": "cpu",
                "loaded": [],
            }
        elif request.url.port == 8916:
            body = {
                "status": "ok",
                "loaded": ["synthetic"],
                "revisions": {"synthetic": None},
                "device": "cpu",
                "device_is_preference": False,
            }
            headers = {
                ARTIFACT_HEADER: "a" * 64,
                WORKER_PID_HEADER: "999" if wrong_pid == "laya" else "43",
            }
        elif request.url.port == 8912:
            if request.method == "POST":
                enabled = json.loads(request.content)["enabled"]
            body = {"enabled": enabled, "provider": {"local": False}}
        elif request.url.path == "/v1/sessions":
            body = []
        else:
            pytest.fail("Unexpected synthetic HTTP path")
        response = httpx.Response(200, json=body, headers=headers)
        return httpx.Response(
            200, headers=response.headers, stream=httpx.ByteStream(response.content)
        )

    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    made = []

    class Manager:
        def __init__(self, specs, readiness):
            self.specs = specs
            self.readiness = readiness
            self.started = []
            self.closed = False
            self.rows = {
                spec.id: {"state": "stopped", "ready": False, "group_rss_bytes": 1024}
                for spec in specs
            }
            made.append(self)

        def status(self):
            return {"workers": self.rows}

        async def start(self, name):
            self.started.append(name)
            spec = next(s for s in self.specs if s.id == name)
            ready = await self.readiness(spec, SimpleNamespace(pid=42 if name == "voice" else 43))
            self.rows[name].update(state="running" if ready else "failed", ready=ready)
            return {"ok": ready, "ready": ready}

        async def close(self):
            self.closed = True

    monkeypatch.setattr(launcher, "WorkerManager", Manager)

    class Writer:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connection(*args, **kwargs):
        if wrong_pid == "occupied":
            return object(), Writer()
        raise ConnectionRefusedError

    monkeypatch.setattr(launcher.asyncio, "open_connection", connection)
    args = SimpleNamespace(data_dir=tmp_path / "data", workspace=workspace)
    tokens = {"owner": "o" * 32, "resources": "r" * 32}
    app = launcher.resource_app(args, config, Auth(tokens), tokens, Path.cwd())
    coordinator = app.state.coordinator
    coordinator.memory_probe = lambda: 16 * 1024**3
    assert coordinator.restore_reserve == 4 * 1024**3
    async with app.router.lifespan_context(app):
        assert [s.id for s in made[0].specs] == ["voice", "laya"]
        assert coordinator.state.record.phase == ("ready" if wrong_pid is None else "failed")
        if wrong_pid == "occupied":
            assert made[0].started == []
        if wrong_pid is None:
            assert made[0].started == ["voice", "laya"] and enabled
    assert made[0].closed
