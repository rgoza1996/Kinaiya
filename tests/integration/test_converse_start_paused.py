"""Real, isolated CLI startup; all endpoints and input are synthetic and local."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from services.converse.intent import ConverseIntent
from services.launcher import serve

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("service", ["session", "agent", "decisions", "resources", "all"])
def test_other_services_refuse_start_paused_before_reading_config(service):
    with pytest.raises(SystemExit, match="only supported by serve converse"):
        serve(SimpleNamespace(service=service, start_paused=True))


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("paused", [False, True])
def test_gate_precedes_window_construction_and_listener(tmp_path, monkeypatch, paused):
    import asyncio

    import uvicorn

    from packages.providers.text import ScriptedProvider
    from services import launcher
    from services.converse import api, windows

    observed = []
    create_app = api.create_app

    def window(runtime):
        assert runtime.availability()["resource_enabled"] is not paused
        observed.append("window")
        return None

    def app(runtime, *args, **kwargs):
        observed.append("app")
        result = create_app(runtime, *args, **kwargs)

        def listen(*args, **kwargs):
            observed.append("listener")
            assert runtime.availability()["resource_enabled"] is not paused
            assert runtime.intent.read().revision == 0 and runtime.intent.read().enabled

            async def close():
                await runtime.close()
                await runtime.journal.close()

            asyncio.run(close())

        monkeypatch.setattr(uvicorn, "run", listen)
        return result

    monkeypatch.setattr(launcher, "provider", lambda *args, **kwargs: ScriptedProvider([]))
    monkeypatch.setattr(windows, "WindowConverseController", window)
    monkeypatch.setattr(api, "create_app", app)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"session_url": "http://127.0.0.1:1"}))
    args = SimpleNamespace(
        data_dir=tmp_path,
        config=config,
        workspace=tmp_path / "workspace",
        service="converse",
        port=None,
    )
    if paused:
        args.start_paused = True
    serve(args)
    assert observed == ["window", "app", "listener"]


@pytest.mark.parametrize("owner_enabled", [True, False])
def test_real_cli_is_paused_at_first_listener_and_preserves_newer_owner_intent(
    tmp_path, owner_enabled
):
    requests = []

    class RejectGeneration(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            self.send_error(500, "No inference is permitted by this test")

        def log_message(self, *args):
            pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), RejectGeneration)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    intent = ConverseIntent(data / "converse-intent.json", initial_enabled=owner_enabled)
    original = intent.path.read_bytes()
    session_port, converse_port = port(), port()
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "session_url": f"http://127.0.0.1:{session_port}",
                "providers": {
                    "converse": {
                        "kind": "compatible",
                        "base_url": f"http://127.0.0.1:{provider.server_port}/v1",
                        "model": "synthetic-no-generation",
                    }
                },
                "resources": {"enabled": False},
            }
        )
    )
    children, logs = [], []
    try:
        for service, service_port in (("session", session_port), ("converse", converse_port)):
            log = (tmp_path / (service + ".log")).open("wb")
            logs.append(log)
            args = [
                sys.executable,
                "-m",
                "services.launcher",
                "--data-dir",
                str(data),
                "--config",
                str(config),
                "serve",
                service,
                "--port",
                str(service_port),
            ]
            if service == "converse":
                args.append("--start-paused")
            child = subprocess.Popen(
                args,
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            children.append(child)
            deadline = time.monotonic() + 10
            with httpx.Client(trust_env=False, timeout=0.5) as http:
                while True:
                    assert child.poll() is None, (tmp_path / (service + ".log")).read_text()
                    try:
                        response = http.get(f"http://127.0.0.1:{service_port}/health")
                        response.raise_for_status()
                        health = response.json()
                        break
                    except httpx.HTTPError:
                        assert time.monotonic() < deadline, "Isolated CLI readiness timed out"
                        time.sleep(0.02)
            if service == "converse":
                assert health["enabled"] is False and health["resource_enabled"] is False
                assert health["owner_enabled"] is owner_enabled
                assert health["owner_intent_revision"] == 0
        tokens = json.loads((data / "service-tokens.json").read_text())
        assert intent.path.read_bytes() == original
        headers = {"Authorization": "Bearer " + tokens["owner"]}
        with httpx.Client(trust_env=False, timeout=2) as http:
            session = http.post(
                f"http://127.0.0.1:{session_port}/v1/sessions",
                headers=headers,
                json={"channel": "converse"},
            ).json()
            url = f"http://127.0.0.1:{converse_port}"
            # Owner Wake changes durable intent, but cannot open the launch fence.
            awake = http.post(url + "/v1/converse/enabled", headers=headers, json={"enabled": True})
            assert awake.status_code == 200
            assert awake.json() == {
                "enabled": False,
                "owner_enabled": True,
                "resource_enabled": False,
                "owner_intent_revision": 1,
            }
            refused = http.post(
                url + "/v1/converse/turns",
                headers=headers,
                json={
                    "session_id": session["session_id"],
                    "thread_id": session["thread_id"],
                    "epoch": session["epoch"],
                    "text": "Public synthetic greeting",
                    "idempotency_key": "paused-public-turn",
                },
            )
            assert refused.status_code == 409
            # Existing resource control releases only the temporary gate.
            resumed = http.post(
                url + "/v1/converse/enabled",
                headers={"Authorization": "Bearer " + tokens["resources"]},
                json={"enabled": True},
            )
            assert resumed.status_code == 200 and resumed.json()["enabled"] is True
            assert resumed.json()["owner_intent_revision"] == 1
        assert requests == []
        assert intent.read().enabled is True and intent.read().revision == 1
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
                pytest.fail("Isolated CLI did not close gracefully")
            assert not Path(f"/proc/{child.pid}").exists()
        for log in logs:
            log.close()
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()
