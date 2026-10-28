"""Managed selection and service wiring, with no process or model startup."""

import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from packages.providers.qwen3_ipc import MANIFEST_SHA256, MODEL, PROFILE
from packages.resources.processes import WorkerSpec
from services.common.auth import Auth
from services.resources import launcher


@pytest.fixture
async def host(tmp_path, monkeypatch):
    apps, managers, endpoints, validated, clients = [], [], [], [], []
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        result = original_client(*args, **kwargs)
        clients.append(result)
        return result

    class Endpoint:
        closed = False

        async def probe(self, spec, process):
            return spec.id == "qwen3" and process.pid == 42

        def close(self):
            self.closed = True

    def worker(settings, root, workspace):
        endpoint = Endpoint()
        endpoints.append(endpoint)
        return WorkerSpec(
            "qwen3", (settings.python, "synthetic-qwen-worker"), root, env={}
        ), endpoint

    class Manager:
        closed = False

        def __init__(self, specs, readiness):
            self.specs, self.readiness = specs, readiness
            managers.append(self)

        def status(self):
            return {
                "workers": {spec.id: {"state": "stopped", "ready": False} for spec in self.specs}
            }

        async def close(self):
            self.closed = True

    monkeypatch.setattr(launcher, "qwen3_worker", worker)
    monkeypatch.setattr(launcher, "WorkerManager", Manager)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(
        launcher,
        "validate_qwen3_inputs",
        lambda settings, workspace: validated.append((settings, workspace)),
    )

    def config():
        return {
            "session_url": "http://127.0.0.1:8910",
            "agent_url": "http://127.0.0.1:8911",
            "converse_url": "http://127.0.0.1:8912",
            "resources_url": "http://127.0.0.1:8914",
            "providers": {"converse": {"kind": "qwen3", "base_url": "http://127.0.0.1:8914"}},
            "voice": {"url": "http://127.0.0.1:8915", "profile": "Kokoro 82M"},
            "resources": {
                "enabled": True,
                "voice_python": sys.executable,
                "voice_port": 8915,
                "qwen3": {"python": sys.executable, "model_dir": str(tmp_path / "model")},
            },
        }

    def build(value):
        tokens = {role: role[0] * 40 for role in ("owner", "converse", "resources")}
        app = launcher.resource_app(
            SimpleNamespace(
                data_dir=tmp_path / f"state-{len(apps)}", workspace=tmp_path / "agent", port=8914
            ),
            copy.deepcopy(value),
            Auth(tokens),
            tokens,
            Path.cwd(),
        )
        apps.append(app)
        return app, managers[-1]

    yield SimpleNamespace(
        build=build, config=config, endpoints=endpoints, validated=validated, apps=apps
    )
    for app in apps:
        await app.state.coordinator.close()
    for connection in clients:
        await connection.aclose()


async def test_selected_worker_static_binding_and_fresh_start_hook(host):
    config = host.config()
    config["resources"]["qwen3"].update(
        max_input_tokens=3072, max_new_tokens=192, request_timeout_s=90
    )
    app, manager = host.build(config)
    assert [spec.id for spec in manager.specs] == ["voice", "qwen3"]
    assert app.state.qwen3.request_timeout_s == 90
    assert app.state.coordinator.managed_text_binding == {
        "provider": "managed-qwen3",
        "model": MODEL,
        "profile": PROFILE,
        "manifest_sha256": MANIFEST_SHA256,
        "resources_url": "http://127.0.0.1:8914",
        "max_input_tokens": 3072,
        "max_new_tokens": 192,
    }
    assert app.state.coordinator.restore_reserve == 10 * 1024**3
    assert app.state.coordinator.qwen3_restore_reserve == 8 * 1024**3
    assert await manager.readiness(manager.specs[1], SimpleNamespace(pid=42))
    await app.state.coordinator.before_start("qwen3")
    assert len(host.validated) == 1
    assert host.validated[0][0].max_input_tokens == 3072
    assert "Authorization" not in manager.specs[1].env


@pytest.mark.parametrize(
    "change",
    [
        "missing-settings",
        "wrong-kind",
        "missing-origin",
        "other-origin",
        "remote-origin",
        "extra-model",
        "input-limit",
        "output-limit",
        "timeout",
        "bool-limit",
    ],
)
async def test_mismatched_selection_is_refused_before_worker_allocation(host, change):
    value = host.config()
    profile = value["providers"]["converse"]
    if change == "missing-settings":
        del value["resources"]["qwen3"]
    elif change == "wrong-kind":
        profile["kind"] = "compatible"
    elif change == "missing-origin":
        del profile["base_url"]
    elif change == "other-origin":
        value["resources_url"] = "http://127.0.0.1:8764"
    elif change == "remote-origin":
        profile["base_url"] = "https://example.invalid"
    elif change == "extra-model":
        profile["model"] = "unmanaged-other-model"
    elif change == "input-limit":
        profile["max_input_tokens"] = 1024
    elif change == "output-limit":
        profile["max_new_tokens"] = 64
    elif change == "timeout":
        profile["request_timeout_s"] = 30
    else:
        profile["max_new_tokens"] = True
    with pytest.raises(ValueError):
        host.build(value)
    assert host.endpoints == []


async def test_default_existing_voice_selection_does_not_allocate_text_worker(host):
    value = host.config()
    del value["resources"]["qwen3"]
    value["providers"]["converse"] = {"kind": "compatible"}
    app, manager = host.build(value)
    assert [spec.id for spec in manager.specs] == ["voice"]
    assert app.state.qwen3 is None
    assert app.state.coordinator.managed_text_binding is None
    assert host.endpoints == []


async def test_failed_broker_close_still_reconciles_manager_before_endpoint_pruning(
    host, monkeypatch
):
    app, manager = host.build(host.config())

    async def fail_close():
        raise RuntimeError("synthetic transport close failure")

    monkeypatch.setattr(app.state.qwen3, "close", fail_close)
    try:
        with pytest.raises(RuntimeError, match="synthetic transport close failure"):
            async with app.router.lifespan_context(app):
                pass
    finally:
        host.apps.remove(app)  # Lifespan already closed coordinator's durable handles.
    assert manager.closed and host.endpoints[0].closed


async def test_unconfirmed_manager_close_preserves_endpoint_for_inspection(host, monkeypatch):
    app, manager = host.build(host.config())

    async def fail_close():
        raise RuntimeError("synthetic group release unconfirmed")

    monkeypatch.setattr(manager, "close", fail_close)
    try:
        with pytest.raises(RuntimeError, match="synthetic group release unconfirmed"):
            async with app.router.lifespan_context(app):
                pass
    finally:
        host.apps.remove(app)
    assert not host.endpoints[0].closed


def test_startup_timeout_matches_managers_supported_bound(host):
    value = host.config()["resources"]
    value["qwen3"]["startup_timeout_s"] = 301
    with pytest.raises(ValidationError):
        launcher.LocalResourceConfig.model_validate(value)


async def test_real_provider_factory_constructs_the_selected_local_binding(host):
    from packages.providers.qwen3 import ManagedQwen3Provider
    from services.launcher import provider

    config = host.config()
    app, manager = host.build(config)
    wrapped = provider(
        config,
        "converse",
        resource_state=app.state.coordinator.state.path,
        service_token="synthetic-converse-token",
    )
    try:
        assert isinstance(wrapped.provider, ManagedQwen3Provider)
        assert wrapped.managed_binding == app.state.coordinator.managed_text_binding
        assert wrapped.provider.http.headers["authorization"] == "Bearer synthetic-converse-token"
        assert wrapped.provider.http.base_url == "http://127.0.0.1:8914"
        assert [spec.id for spec in manager.specs] == ["voice", "qwen3"]
    finally:
        await wrapped.provider.close()
