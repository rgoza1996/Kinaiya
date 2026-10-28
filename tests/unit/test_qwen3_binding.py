"""Protected configuration and static health binding; synthetic providers only."""

import copy
import json
import sys
from types import MappingProxyType, ModuleType, SimpleNamespace

import httpx
import pytest

from packages.contracts.models import ProviderInfo
from packages.providers.qwen3_ipc import MANIFEST_SHA256, MODEL, PROFILE
from packages.providers.text import ProviderError, ScriptedProvider
from services import launcher
from services.common.auth import Auth
from services.converse.api import create_app
from services.resources.providers import ResourceBoundProvider, managed_text_binding
from services.resources.state import ResourceState

TOKEN = "public-synthetic-converse-credential"


def binding(**changes):
    return {
        "provider": "managed-qwen3",
        "model": MODEL,
        "profile": PROFILE,
        "manifest_sha256": MANIFEST_SHA256,
        "resources_url": "http://127.0.0.1:8764",
        "max_input_tokens": 2048,
        "max_new_tokens": 128,
        **changes,
    }


def config(**profile):
    return {
        "providers": {
            "converse": {"kind": "qwen3", "base_url": "http://127.0.0.1:8764/", **profile}
        },
        "resources": {"enabled": True, "qwen3": {}},
    }


class SyntheticManaged:
    info = ProviderInfo(provider="managed-qwen3", model=MODEL, local=True, streaming=True)

    def __init__(self, base_url="http://127.0.0.1:8764", token=TOKEN, **settings):
        self.base_url, self.token, self.settings = base_url, token, settings
        self.binding = binding(
            resources_url=base_url,
            **{key: value for key, value in settings.items() if key != "request_timeout_s"},
        )
        self.calls = []

    @property
    def managed_binding(self):
        return self.binding

    async def complete(self, messages):
        self.calls.append(messages)
        return "Public synthetic reply"

    async def stream(self, messages):
        yield await self.complete(messages)

    async def health(self):
        raise AssertionError("Static binding must not probe provider health")


@pytest.fixture
def provider_module(monkeypatch):
    created = []

    def construct(*args, **kwargs):
        result = SyntheticManaged(*args, **kwargs)
        created.append(result)
        return result

    module = ModuleType("packages.providers.qwen3")
    module.ManagedQwen3Provider = construct
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return created


def build(value, tmp_path, **kwargs):
    return launcher.provider(
        value,
        "converse",
        resource_state=tmp_path / "unread-resource-policy",
        service_token=TOKEN,
        **kwargs,
    )


def test_factory_uses_only_protected_token_and_fixed_managed_target(provider_module, tmp_path):
    value = config()
    saved = copy.deepcopy(value)
    result = build(value, tmp_path)
    assert isinstance(result, ResourceBoundProvider)
    assert result.role == "converse" and result.info.local and result.info.streaming
    assert value == saved
    native = provider_module[0]
    assert native.token == TOKEN
    assert native.base_url == "http://127.0.0.1:8764"
    assert native.settings == {
        "max_input_tokens": 2048,
        "max_new_tokens": 128,
        "request_timeout_s": 120,
    }
    assert result.managed_binding == binding()
    assert TOKEN not in json.dumps(result.managed_binding)
    assert native.calls == []


def test_omitted_provider_limits_inherit_protected_worker_settings(provider_module, tmp_path):
    value = config()
    value["resources"]["qwen3"] = {
        "max_input_tokens": 1024,
        "max_new_tokens": 64,
        "request_timeout_s": 90,
    }
    result = build(value, tmp_path)
    assert result.provider.settings == value["resources"]["qwen3"]
    assert result.managed_binding == binding(max_input_tokens=1024, max_new_tokens=64)


@pytest.mark.parametrize("role", ["agent", "decisions"])
def test_qwen_selection_is_converse_only(provider_module, tmp_path, role):
    value = config()
    value["providers"][role] = value["providers"]["converse"]
    with pytest.raises(SystemExit, match="managed Converse"):
        launcher.provider(value, role, resource_state=tmp_path / "policy", service_token=TOKEN)
    assert not provider_module


@pytest.mark.parametrize(
    "resources",
    [
        None,
        {},
        {"enabled": False, "qwen3": {}},
        {"enabled": 1, "qwen3": {}},
        {"enabled": True},
        {"enabled": True, "qwen3": []},
    ],
)
def test_factory_requires_explicit_resource_opt_in(provider_module, tmp_path, resources):
    value = config()
    value["resources"] = resources
    with pytest.raises(ValueError):
        build(value, tmp_path)
    assert not provider_module


@pytest.mark.parametrize("token", [None, "", " ", True, "public\ncredential"])
def test_factory_cannot_invent_a_missing_service_credential(provider_module, tmp_path, token):
    with pytest.raises(ValueError):
        launcher.provider(
            config(), "converse", resource_state=tmp_path / "policy", service_token=token
        )
    assert not provider_module


def test_factory_requires_protected_policy_binding(provider_module):
    with pytest.raises(ValueError):
        launcher.provider(config(), "converse", service_token=TOKEN)
    assert not provider_module


@pytest.mark.parametrize(
    "key", ["token", "service_token", "api_key", "api_key_env", "model", "allow_cloud", "transport"]
)
def test_profile_cannot_supply_credentials_or_replace_the_model(provider_module, tmp_path, key):
    with pytest.raises(ValueError):
        build(config(**{key: "untrusted-profile-value"}), tmp_path)
    assert not provider_module


@pytest.mark.parametrize(
    "url",
    [
        None,
        "https://provider.invalid",
        "http://127.0.0.1:9999",
        "http://localhost:8764",
        "http://127.0.0.1:8764/v1",
        "http://127.0.0.1:8764?proxy=1",
        "http://key@127.0.0.1:8764",
    ],
)
def test_profile_cannot_change_managed_origin(provider_module, tmp_path, url):
    with pytest.raises(ValueError):
        build(config(base_url=url), tmp_path)
    assert not provider_module


@pytest.mark.parametrize(
    "changes",
    [
        {"max_input_tokens": True},
        {"max_new_tokens": 128.0},
        {"max_new_tokens": 513},
        {"max_input_tokens": 1024},
        {"max_new_tokens": 64},
        {"request_timeout_s": 90},
        {"request_timeout_s": True},
        {"request_timeout_s": float("inf")},
    ],
)
def test_provider_limits_cannot_drift_from_worker(provider_module, tmp_path, changes):
    with pytest.raises(ValueError):
        build(config(**changes), tmp_path)
    assert not provider_module


@pytest.mark.parametrize(
    "settings",
    [
        {"max_input_tokens": True},
        {"max_new_tokens": 0},
        {"request_timeout_s": False},
        {"request_timeout_s": float("nan")},
        {"request_timeout_s": 0},
    ],
)
def test_invalid_protected_limits_fail_before_constructing_provider(
    provider_module, tmp_path, settings
):
    value = config()
    value["resources"]["qwen3"] = settings
    with pytest.raises(ValueError):
        build(value, tmp_path)
    assert not provider_module


def test_wrapper_returns_detached_mapping_without_policy_or_readiness_probe(tmp_path):
    original = SyntheticManaged()
    original.binding = MappingProxyType(binding())
    wrapped = ResourceBoundProvider(original, tmp_path / "missing-policy", "converse")
    first = wrapped.managed_binding
    first["max_new_tokens"] = 16
    assert wrapped.managed_binding == binding()
    assert original.calls == [] and wrapped.info is original.info


async def get_health(provider):
    runtime = SimpleNamespace(
        provider=provider,
        availability=lambda: {"enabled": False, "resource_enabled": False},
        speech_status=lambda: {},
        text_status=lambda: {},
    )
    auth = Auth({"owner": "o" * 32, "resources": "r" * 32})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, auth)),
        base_url="http://fixture",
    ) as http:
        return await http.get("/health")


async def test_health_reports_startup_identity_even_when_worker_is_paused(tmp_path):
    original = SyntheticManaged()
    wrapped = ResourceBoundProvider(original, tmp_path / "missing-policy", "converse")
    response = await get_health(wrapped)
    assert response.status_code == 200
    assert response.json()["managed_text"] == binding()
    assert response.json()["enabled"] is False
    assert (
        "ready" not in response.json()["managed_text"]
        and "pid" not in response.json()["managed_text"]
    )
    assert TOKEN not in response.text and original.calls == []
    response.json()["managed_text"]["max_new_tokens"] = 16
    assert wrapped.managed_binding["max_new_tokens"] == 128


@pytest.mark.parametrize(
    "bad",
    [
        {"token": TOKEN},
        {**binding(), "token": TOKEN},
        binding(model="other"),
        binding(manifest_sha256="f" * 64),
        binding(resources_url="https://provider.invalid"),
        binding(max_input_tokens=True),
        [],
        "invalid",
    ],
)
async def test_malformed_binding_cannot_leak_into_health(bad):
    provider = SyntheticManaged()
    provider.binding = bad
    response = await get_health(provider)
    assert response.status_code == 503
    assert response.json() == {"detail": "Managed text configuration identity is unavailable"}
    assert TOKEN not in response.text and provider.calls == []


async def test_binding_exception_does_not_expose_raw_provider_details():
    class Broken(SyntheticManaged):
        @property
        def managed_binding(self):
            raise RuntimeError("raw public-synthetic credential " + TOKEN)

    response = await get_health(Broken())
    assert response.status_code == 503 and TOKEN not in response.text


async def test_info_mismatch_does_not_claim_managed_health():
    provider = SyntheticManaged()
    provider.info = ProviderInfo(provider="managed-qwen3", model=MODEL, local=False, streaming=True)
    assert (await get_health(provider)).status_code == 503


async def test_existing_unmanaged_public_health_omits_managed_binding(tmp_path):
    provider = ScriptedProvider([])
    wrapped = ResourceBoundProvider(provider, tmp_path / "missing-policy", "converse")
    response = await get_health(wrapped)
    assert response.status_code == 200 and "managed_text" not in response.json()
    assert managed_text_binding(provider) is None


async def test_static_binding_does_not_bypass_resource_pause(tmp_path):
    state = ResourceState(tmp_path / "policy.json")
    state.record.phase = "off"
    state.record.desired_converse = False
    state.save()
    original = SyntheticManaged()
    wrapped = ResourceBoundProvider(original, state.path, "converse")
    try:
        assert wrapped.managed_binding == binding()
        with pytest.raises(ProviderError, match="paused"):
            await wrapped.complete([{"role": "user", "content": "Public fixture"}])
        assert original.calls == []
    finally:
        state.close()


async def test_existing_compatible_factory_does_not_import_qwen(monkeypatch):
    monkeypatch.setitem(sys.modules, "packages.providers.qwen3", None)
    value = {
        "providers": {
            "converse": {
                "kind": "compatible",
                "base_url": "http://127.0.0.1:8080/v1",
                "model": "synthetic",
            }
        }
    }
    result = launcher.provider(value, "converse")
    try:
        assert result.info.provider == "openai-compatible"
        assert managed_text_binding(result) is None
    finally:
        await result.close()


def test_converse_serve_supplies_its_protected_service_token(
    monkeypatch, tmp_path, provider_module
):
    import uvicorn

    import packages.contracts.client

    value = config()
    value.update(session_url="http://fixture", streaming_windows={"enabled": False})
    tokens = {"owner": "o" * 32, "converse": TOKEN, "resources": "r" * 32}
    monkeypatch.setattr(launcher, "load_config", lambda _: value)
    monkeypatch.setattr(launcher, "load_or_create_tokens", lambda _: tokens)
    monkeypatch.setattr(packages.contracts.client, "JournalClient", lambda *args: SimpleNamespace())
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append((app, kwargs)))
    launcher.serve(
        SimpleNamespace(
            config=tmp_path / "config.json",
            data_dir=tmp_path / "data",
            workspace=tmp_path / "agent",
            service="converse",
            port=None,
        )
    )
    assert len(served) == 1 and served[0][1]["host"] == "127.0.0.1"
    assert provider_module[0].token == TOKEN and provider_module[0].calls == []
