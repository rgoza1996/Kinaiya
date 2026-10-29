"""Managed GGUF selection/auth wiring; no native process, server or model load."""

import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from packages.providers.gguf import ManagedGGUFProvider
from packages.resources.processes import WorkerManager, WorkerSpec
from services.common.auth import Auth
from services.launcher import provider
from services.resources import launcher
from services.resources.api import create_app
from services.resources.gguf import ManagedGGUFConfig, configured_manifest
from services.resources.providers import ResourceBoundProvider

TOKENS = {
    role: f"synthetic-{role}-credential-" + role[0] * 24
    for role in ("owner", "converse", "agent", "decisions", "resources")
}


@pytest.fixture
def selected(tmp_path):
    owner, workspace = tmp_path / "owner", tmp_path / "agent"
    owner.mkdir()
    workspace.mkdir()
    assets = []
    for guest, name in [
        ("/model.gguf", "synthetic.gguf"),
        ("/runtime/llama-server", "synthetic-server"),
    ]:
        path = owner / name
        raw = b"public synthetic pin: " + name.encode()
        path.write_bytes(raw)
        assets.append(
            dict(
                path=str(path), guest=guest, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()
            )
        )
    manifest = dict(
        version=1,
        profile="synthetic-cpu-native",
        model="Public display name",
        alias="public-runtime-alias",
        assets=assets,
        sampling=dict(
            temperature=0.7, top_p=0.8, top_k=20, min_p=0, presence_penalty=0, repeat_penalty=1
        ),
    )
    path = owner / "manifest.json"
    raw = json.dumps(manifest).encode()
    path.write_bytes(raw)
    config = {
        "session_url": "http://127.0.0.1:8910",
        "agent_url": "http://127.0.0.1:8911",
        "converse_url": "http://127.0.0.1:8912",
        "resources_url": "http://127.0.0.1:8914",
        "providers": {
            "converse": {"kind": "gguf", "base_url": "http://127.0.0.1:8914"},
            "agent": {
                "kind": "compatible",
                "base_url": "http://localhost:8991/v1",
                "model": "independent-agent",
            },
            "decisions": {
                "kind": "compatible",
                "base_url": "http://localhost:8992/v1",
                "model": "independent-decisions",
            },
        },
        "voice": {"url": "http://127.0.0.1:8915", "profile": "Kokoro 82M"},
        "resources": {
            "enabled": True,
            "voice_python": sys.executable,
            "voice_port": 8915,
            "gguf": {
                "python": sys.executable,
                "manifest": str(path),
                "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            },
        },
    }
    return SimpleNamespace(
        config=config,
        workspace=workspace,
        manifest=manifest,
        path=path,
        root=tmp_path,
        assets={Path(row["path"]) for row in assets},
    )


@pytest.fixture
async def host(selected, monkeypatch):
    apps, managers, endpoints, clients, states = [], [], [], [], []
    http_client = httpx.AsyncClient
    state_type = launcher.ResourceState

    def no_network(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        connection = http_client(*args, **kwargs)
        clients.append(connection)
        return connection

    def state(*args, **kwargs):
        result = state_type(*args, **kwargs)
        states.append(result)
        return result

    def manager(specs, *, readiness):
        result = WorkerManager(specs, readiness=readiness)
        managers.append(SimpleNamespace(manager=result, specs=specs, readiness=readiness))
        return result

    class Endpoint:
        def __init__(self, settings, manifest):
            self.manifest, self.closed = manifest, False
            self.limits = {
                "context_tokens": settings.context_tokens,
                "max_new_tokens": settings.max_new_tokens,
            }
            self.leases, self.exchanges = {}, set()
            self.releases = []

        async def probe(self, spec, process):
            return spec.id == "gguf" and process.pid == 42

        async def release_after_stop(self, identity, result):
            # This synthetic endpoint never acquired a native lease. Actual
            # manager status confirms that no process was granted.
            self.releases.append((identity, result))
            assert identity is None and result.get("ok") is True
            return True

        def close(self):
            self.closed = True

    def worker(settings, root, workspace):
        endpoint = Endpoint(settings, configured_manifest(settings, workspace))
        endpoints.append(endpoint)
        return WorkerSpec(
            "gguf",
            (settings.python, "-m", "synthetic-gguf-never-started"),
            root,
            env={"PYTHONPATH": str(root)},
        ), endpoint

    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    monkeypatch.setattr(launcher, "WorkerManager", manager)
    monkeypatch.setattr(launcher, "ResourceState", state)
    monkeypatch.setattr(launcher, "gguf_worker", worker)

    async def no_start(*args, **kwargs):
        pytest.fail("Configuration tests must not start a native process")

    monkeypatch.setattr(WorkerManager, "start", no_start)

    def build(config=None):
        app = launcher.resource_app(
            SimpleNamespace(
                data_dir=selected.root / f"state-{len(states)}",
                workspace=selected.workspace,
                port=8914,
            ),
            copy.deepcopy(selected.config if config is None else config),
            Auth(TOKENS),
            TOKENS,
            Path(launcher.__file__).resolve().parents[2],
        )
        apps.append(app)
        return app, managers[-1]

    yield SimpleNamespace(build=build, endpoints=endpoints, managers=managers, apps=apps)
    for app in apps:
        await app.state.coordinator.close()
    for row in managers:
        await row.manager.close()  # Real never-started manager closes legitimately.
    for state in states:
        state.close()
    for connection in clients:
        await connection.aclose()
    for endpoint in endpoints:
        endpoint.close()


async def test_selected_host_and_provider_match_exact_static_binding(host, selected):
    saved = copy.deepcopy(selected.config)
    app, row = host.build()
    assert isinstance(row.manager, WorkerManager)
    assert [spec.id for spec in row.specs] == ["voice", "gguf"]
    assert all(
        value["owned_pid"] is None and not value["ready"]
        for value in row.manager.status()["workers"].values()
    )
    assert row.manager.owned_identity("gguf") is None
    assert await row.readiness(row.specs[1], SimpleNamespace(pid=42))
    wrapped = provider(
        selected.config,
        "converse",
        resource_state=app.state.coordinator.state.path,
        service_token=TOKENS["converse"],
        workspace=selected.workspace,
    )
    try:
        assert isinstance(wrapped, ResourceBoundProvider) and isinstance(
            wrapped.provider, ManagedGGUFProvider
        )
        assert (
            wrapped.managed_binding
            == app.state.coordinator.managed_text_binding
            == {
                "provider": "managed-gguf",
                "model": "Public display name",
                "profile": "synthetic-cpu-native",
                "manifest_sha256": selected.config["resources"]["gguf"]["manifest_sha256"],
                "resources_url": "http://127.0.0.1:8914",
                "context_tokens": 2048,
                "max_new_tokens": 128,
            }
        )
        assert wrapped.provider.model == "public-runtime-alias"
        assert app.state.qwen3 is None and app.state.gguf.request_timeout_s == 120
        assert app.state.coordinator.restore_reserve == 10 * 1024**3
        assert all(
            token not in str(row.specs[1].argv) + str(row.specs[1].env) for token in TOKENS.values()
        )
        assert selected.config == saved
    finally:
        await wrapped.provider.close()


@pytest.mark.parametrize(
    "change",
    [
        "disabled",
        "missing-settings",
        "wrong-kind",
        "missing-origin",
        "other-origin",
        "remote-origin",
        "extra-key",
        "both-managed",
    ],
)
async def test_invalid_host_selection_refuses_before_native_allocation(host, selected, change):
    config = copy.deepcopy(selected.config)
    if change == "disabled":
        config["resources"]["enabled"] = False
    elif change == "missing-settings":
        del config["resources"]["gguf"]
    elif change == "wrong-kind":
        config["providers"]["converse"]["kind"] = "compatible"
    elif change == "missing-origin":
        del config["providers"]["converse"]["base_url"]
    elif change == "other-origin":
        config["resources_url"] = "http://127.0.0.1:8999"
    elif change == "remote-origin":
        config["providers"]["converse"]["base_url"] = "http://remote.invalid"
    elif change == "extra-key":
        config["providers"]["converse"]["model"] = "unmanaged-override"
    else:
        config["resources"]["qwen3"] = {"python": sys.executable, "model_dir": str(selected.root)}
    with pytest.raises(ValueError):
        host.build(config)
    assert host.endpoints == [] and host.managers == []


@pytest.mark.parametrize(
    "change",
    [
        "no-state",
        "no-workspace",
        "no-token",
        "bad-pin",
        "foreign-origin",
        "extra-argv",
        "extra-model",
        "extra-key",
        "extra-context",
        "both-managed",
    ],
)
async def test_provider_factory_refuses_unprotected_selection(selected, change):
    config = copy.deepcopy(selected.config)
    kwargs = dict(
        resource_state=selected.root / "unused-policy.json",
        service_token=TOKENS["converse"],
        workspace=selected.workspace,
    )
    if change == "no-state":
        kwargs["resource_state"] = None
    elif change == "no-workspace":
        kwargs["workspace"] = None
    elif change == "no-token":
        kwargs["service_token"] = None
    elif change == "bad-pin":
        config["resources"]["gguf"]["manifest_sha256"] = "0" * 64
    elif change == "foreign-origin":
        config["providers"]["converse"]["base_url"] = "http://localhost:8999"
    elif change == "both-managed":
        config["resources"]["qwen3"] = {}
    else:
        key = {
            "extra-argv": "argv",
            "extra-model": "model",
            "extra-key": "api_key",
            "extra-context": "context_tokens",
        }[change]
        config["providers"]["converse"][key] = "synthetic-forbidden-override"
    with pytest.raises(ValueError):
        provider(config, "converse", **kwargs)


@pytest.mark.parametrize("role", ["agent", "decisions"])
def test_managed_kind_cannot_replace_independent_roles(selected, role):
    config = copy.deepcopy(selected.config)
    config["providers"][role] = config["providers"]["converse"]
    with pytest.raises(SystemExit):
        provider(
            config,
            role,
            resource_state=selected.root / "policy.json",
            service_token=TOKENS["converse"],
            workspace=selected.workspace,
        )


async def test_unselected_defaults_and_independent_providers_stay_unchanged(host, selected):
    config = copy.deepcopy(selected.config)
    config["resources"].pop("gguf")
    config["providers"]["converse"] = {
        "kind": "compatible",
        "base_url": "http://localhost:8993/v1",
        "model": "independent-converse",
    }
    app, row = host.build(config)
    assert [spec.id for spec in row.specs] == ["voice"]
    assert app.state.gguf is None and app.state.qwen3 is None
    assert app.state.coordinator.managed_text_binding is None
    assert host.endpoints == []
    providers = [provider(config, role) for role in ("agent", "decisions", "converse")]
    try:
        assert [p.info.model for p in providers] == [
            "independent-agent",
            "independent-decisions",
            "independent-converse",
        ]
        assert all(p.info.provider == "openai-compatible" for p in providers)
    finally:
        for item in providers:
            await item.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("argv", ["native"]),
        ("context_tokens", True),
        ("cpu_affinity_count", 1),
        ("max_new_tokens", True),
        ("memory_bytes", 7 * 1024**3),
        ("host_memory_reserve_bytes", 7 * 1024**3),
        ("request_timeout_s", float("nan")),
    ],
)
def test_protected_config_is_strict(selected, field, value):
    with pytest.raises(ValidationError):
        ManagedGGUFConfig.model_validate({**selected.config["resources"]["gguf"], field: value})


def test_metadata_configuration_never_reads_native_assets(selected, monkeypatch):
    original = Path.read_bytes
    original_open = os.open

    def forbid_native_read(path):
        assert path not in selected.assets, "Parent read native bytes instead of metadata"
        return original(path)

    def forbid_native_open(path, flags, *args, **kwargs):
        assert Path(path) not in selected.assets, "Parent opened native bytes"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", forbid_native_read)
    monkeypatch.setattr(os, "open", forbid_native_open)
    settings = ManagedGGUFConfig.model_validate(selected.config["resources"]["gguf"])
    assert configured_manifest(settings, selected.workspace) == selected.manifest


class Body:
    remaining_timeout_s = 1

    def __init__(self):
        self.closed = False
        self.sent = False
        self.close_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return b"data: [DONE]\n\n"

    async def aclose(self):
        self.close_calls += 1
        self.closed = True


@pytest.mark.parametrize(
    "role,status",
    [
        (None, 401),
        ("unknown", 401),
        ("agent", 403),
        ("decisions", 403),
        ("resources", 403),
        ("owner", 200),
        ("converse", 200),
    ],
)
async def test_native_http_route_accepts_only_owner_or_converse(role, status):
    calls, bodies = [], []

    class Broker:
        async def stream(self, request):
            calls.append((request.method, request.url.path, await request.json()))
            body = Body()
            bodies.append(body)
            return body

    app = create_app(SimpleNamespace(), Auth(TOKENS), gguf=Broker())
    token = TOKENS.get(role, "unknown-token")
    headers = {} if role is None else {"Authorization": "Bearer " + token}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Synthetic"}]},
            headers=headers,
        )
    assert response.status_code == status
    if status == 200:
        assert calls == [
            (
                "POST",
                "/v1/chat/completions",
                {"messages": [{"role": "user", "content": "Synthetic"}]},
            )
        ]
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        assert bodies[0].closed and bodies[0].close_calls == 1
    else:
        assert calls == []


async def test_native_route_does_not_fall_back_to_qwen_or_other_worker():
    class Unexpected:
        async def stream(self, request):
            pytest.fail("Wrong managed provider fallback")

    app = create_app(SimpleNamespace(), Auth(TOKENS), qwen3=Unexpected())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/v1/chat/completions", json={}, headers={"Authorization": "Bearer " + TOKENS["owner"]}
        )
    assert result.status_code == 503


@pytest.mark.parametrize(
    "mismatch",
    [
        "none",
        "unmanaged",
        "foreign-profile",
        "wrong-pin",
        "wrong-context",
        "wrong-provider",
        "missing-binding",
        "cloud",
    ],
)
async def test_only_selected_exact_binding_has_managed_release_authority(host, mismatch):
    app, _ = host.build()
    coordinator = app.state.coordinator
    binding = copy.deepcopy(coordinator.managed_text_binding)
    health = {
        "provider": {
            "provider": "managed-gguf",
            "model": binding["model"],
            "streaming": True,
            "local": True,
        },
        "managed_text": binding,
    }
    if mismatch == "unmanaged":
        health["provider"]["provider"] = "openai-compatible"
        health.pop("managed_text")
    elif mismatch == "foreign-profile":
        binding["profile"] = "other-profile"
    elif mismatch == "wrong-pin":
        binding["manifest_sha256"] = "0" * 64
    elif mismatch == "wrong-context":
        binding["context_tokens"] = 4096
    elif mismatch == "wrong-provider":
        health["provider"]["provider"] = "managed-qwen3"
    elif mismatch == "missing-binding":
        health.pop("managed_text")
    elif mismatch == "cloud":
        health["provider"]["local"] = False
    coordinator._provider_locality(health)
    assert coordinator.local_converse_unmanaged is (mismatch != "none")
    assert coordinator.manager.owned_identity("gguf") is None


async def test_real_factory_binds_fixed_worker_with_no_credential_or_native_start(selected):
    from services.resources.gguf import gguf_worker

    settings = ManagedGGUFConfig.model_validate(selected.config["resources"]["gguf"])
    root = Path(launcher.__file__).resolve().parents[2]
    spec, endpoint = gguf_worker(settings, root, selected.workspace)
    manager = WorkerManager([spec])
    try:
        assert spec.id == "gguf"
        assert spec.argv[:3] == (sys.executable, "-m", "packages.providers.gguf_worker")
        flags = dict(zip(spec.argv[3::2], spec.argv[4::2], strict=True))
        assert flags["--manifest"] == settings.manifest
        assert flags["--manifest-sha256"] == settings.manifest_sha256
        assert flags["--workspace"] == str(selected.workspace)
        assert flags["--ctx-size"] == "2048" and flags["--max-new-tokens"] == "128"
        assert flags["--cpu-count"] == "2" and flags["--memory-bytes"] == str(16 * 1024**3)
        assert spec.env == {
            "PYTHONPATH": str(root),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        assert not any(token in str(spec.argv) + str(spec.env) for token in TOKENS.values())
        assert endpoint.manifest == selected.manifest
        assert endpoint.ready is None and endpoint.leases == {} and not endpoint.exchanges
        assert manager.owned_identity("gguf") is None
        assert manager.status()["workers"]["gguf"]["owned_pid"] is None
    finally:
        await manager.close()  # Never launched; no invented native extinction.
        endpoint.close()
    assert not endpoint.directory.exists()
