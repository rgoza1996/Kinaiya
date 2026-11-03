"""Unit and API tests for unlimited .env custom OpenAI-compatible providers (#352)."""

from __future__ import annotations

import io
import json
from pathlib import Path
import stat
import urllib.error
import urllib.request

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from services.settings.custom_providers_env import (
    MASKED_API_KEY_DISPLAY,
    UNVERIFIED_CUSTOM_CAPABILITIES,
    CustomProviderError,
    CustomProviderReferencedError,
    CustomProvidersEnvStore,
    normalize_openai_base_url,
)
from services.settings.provider_api import router as provider_router
from services.settings.provider_catalog import ProviderCatalog


class _AllowOwnerAuth:
    def require(self, _role: str):
        def _dep() -> None:
            return None

        return _dep


def test_persists_six_plus_custom_profiles_and_preserves_unrelated_dotenv_lines(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "custom_providers.env"
    env_path.write_text(
        "# Existing user environment comments\n"
        "UNRELATED_FOO=\"keep-$HOME-literal\"\n"
        "OTHER_FLAG=1\n",
        encoding="utf-8",
    )

    store = CustomProvidersEnvStore(env_path, workspace=tmp_path)
    endpoints = [
        ("OpenRouter", "https://openrouter.ai/api/v1", "sk-or-secret-1"),
        ("Together AI", "https://api.together.xyz/v1/", "sk-together-2"),
        ("Groq", "https://api.groq.com/openai/v1", "sk-groq-3"),
        ("DeepSeek", "https://api.deepseek.com", "sk-ds-4"),
        ("Fireworks", "https://api.fireworks.ai/inference/v1", "sk-fw-5"),
        ("Local vLLM", "http://127.0.0.1:8000", ""),
        ("Local llama.cpp", "http://localhost:8080/v1/models", "local-token-7"),
    ]
    for name, url, key in endpoints:
        store.add_profile(name=name, base_url=url, api_key=key, enabled=True)

    # Verify file mode is 0600
    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600

    raw_text = env_path.read_text(encoding="utf-8")
    assert "# Existing user environment comments" in raw_text
    assert 'UNRELATED_FOO="keep-$HOME-literal"' in raw_text
    assert "OTHER_FLAG=1" in raw_text
    assert "KINAIYA_CUSTOM_PROVIDER_IDS=p01,p02,p03,p04,p05,p06,p07" in raw_text

    # Reload in a fresh store instance
    reloaded = CustomProvidersEnvStore(env_path, workspace=tmp_path)
    snap = reloaded.snapshot()
    assert snap["unlimited"] is True
    profiles = snap["profiles"]
    assert len(profiles) == 7
    assert [p["profile_id"] for p in profiles] == [
        "p01",
        "p02",
        "p03",
        "p04",
        "p05",
        "p06",
        "p07",
    ]
    assert profiles[0]["name"] == "OpenRouter"
    assert profiles[0]["base_url"] == "https://openrouter.ai/api/v1"
    assert profiles[0]["has_api_key"] is True
    assert profiles[0]["masked_api_key"] == MASKED_API_KEY_DISPLAY
    assert "sk-or-secret-1" not in json.dumps(snap)

    # Verify Local vLLM normalized /v1 and has_api_key False
    assert profiles[5]["base_url"] == "http://127.0.0.1:8000/v1"
    assert profiles[5]["has_api_key"] is False
    assert profiles[5]["masked_api_key"] == ""

    # Verify Local llama.cpp stripped trailing /models
    assert profiles[6]["base_url"] == "http://localhost:8080/v1"


def test_deletion_never_renumbers_ids_and_requires_confirmation_when_referenced(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "custom_providers.env"
    store = CustomProvidersEnvStore(env_path, workspace=tmp_path)
    store.add_profile(name="One", base_url="https://one.example.com/v1", api_key="k1")
    store.add_profile(name="Two", base_url="https://two.example.com/v1", api_key="k2")
    store.add_profile(name="Three", base_url="https://three.example.com/v1", api_key="k3")

    # Delete p02 (unreferenced) -> remaining are p01, p03
    snap = store.remove_profile("p02")
    assert [p["profile_id"] for p in snap["profiles"]] == ["p01", "p03"]

    # Adding a new profile allocates p04 (never reuses p02 or renumbers p03)
    snap = store.add_profile(name="Four", base_url="https://four.example.com/v1", api_key="k4")
    assert [p["profile_id"] for p in snap["profiles"]] == ["p01", "p03", "p04"]

    # Bind p03 to converse and agent
    store.bind_role_model(role="converse", profile_id="p03", model_id="shared/model-v1")
    store.bind_role_model(role="agent", profile_id="p03", model_id="shared/model-v1")

    with pytest.raises(CustomProviderReferencedError) as exc_info:
        store.remove_profile("p03", confirm_referenced=False)
    assert exc_info.value.profile_id == "p03"
    assert exc_info.value.referenced_by == ["converse", "agent"]

    # Confirming removal deletes p03 and clears its role bindings
    snap_after = store.remove_profile("p03", confirm_referenced=True)
    assert [p["profile_id"] for p in snap_after["profiles"]] == ["p01", "p04"]
    assert snap_after["role_bindings"]["converse"] is None
    assert snap_after["role_bindings"]["agent"] is None


def test_identical_model_ids_across_two_custom_providers_do_not_conflate(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "custom_providers.env"
    store = CustomProvidersEnvStore(env_path, workspace=tmp_path)
    store.add_profile(name="OpenRouter", base_url="https://openrouter.ai/api/v1", api_key="k1")
    store.add_profile(name="Together", base_url="https://api.together.xyz/v1", api_key="k2")

    shared_model = "meta-llama/Llama-3.3-70B-Instruct"
    store.bind_role_model(role="converse", profile_id="p01", model_id=shared_model)
    store.bind_role_model(role="agent", profile_id="p02", model_id=shared_model)

    reloaded = CustomProvidersEnvStore(env_path, workspace=tmp_path).snapshot()
    conv_binding = reloaded["role_bindings"]["converse"]
    agent_binding = reloaded["role_bindings"]["agent"]

    assert conv_binding is not None
    assert agent_binding is not None
    assert conv_binding["profile_id"] == "p01"
    assert conv_binding["provider_name"] == "OpenRouter"
    assert conv_binding["qualified_model_id"] == f"p01:{shared_model}"
    assert agent_binding["profile_id"] == "p02"
    assert agent_binding["provider_name"] == "Together"
    assert agent_binding["qualified_model_id"] == f"p02:{shared_model}"


def test_api_key_preservation_rotation_and_clearing(tmp_path: Path) -> None:
    env_path = tmp_path / "custom_providers.env"
    store = CustomProvidersEnvStore(env_path, workspace=tmp_path)
    store.add_profile(
        name="Endpoint A",
        base_url="https://api.example.com/v1",
        api_key="initial-secret-key",
    )

    # Editing name/URL without providing new api_key preserves initial-secret-key
    store.update_profile("p01", name="Endpoint A Renamed", api_key="")
    assert 'KINAIYA_PROVIDER_P01_API_KEY="initial-secret-key"' in env_path.read_text(
        encoding="utf-8"
    )

    # Passing the UI mask placeholder also preserves initial-secret-key
    store.update_profile("p01", api_key=MASKED_API_KEY_DISPLAY)
    assert 'KINAIYA_PROVIDER_P01_API_KEY="initial-secret-key"' in env_path.read_text(
        encoding="utf-8"
    )

    # Providing a new key rotates it
    store.update_profile("p01", api_key="rotated-secret-key")
    assert 'KINAIYA_PROVIDER_P01_API_KEY="rotated-secret-key"' in env_path.read_text(
        encoding="utf-8"
    )
    assert "initial-secret-key" not in env_path.read_text(encoding="utf-8")

    # Explicit clear removes the key
    snap = store.update_profile("p01", clear_api_key=True)
    assert snap["profiles"][0]["has_api_key"] is False
    assert snap["profiles"][0]["masked_api_key"] == ""


def test_base_url_normalization_and_remote_http_guard() -> None:
    assert (
        normalize_openai_base_url("https://openrouter.ai/api/v1/")
        == "https://openrouter.ai/api/v1"
    )
    assert (
        normalize_openai_base_url("https://api.example.com/v1/v1/models")
        == "https://api.example.com/v1"
    )
    assert (
        normalize_openai_base_url("http://127.0.0.1:11434")
        == "http://127.0.0.1:11434/v1"
    )
    with pytest.raises(CustomProviderError, match="Remote HTTP"):
        normalize_openai_base_url("http://remote.example.com/v1")
    assert (
        normalize_openai_base_url("http://remote.example.com/v1", allow_remote_http=True)
        == "http://remote.example.com/v1"
    )


def test_model_discovery_statuses_and_manual_fallback(tmp_path: Path) -> None:
    requests_seen: list[tuple[str, dict[str, str]]] = []
    mode = {"response": "ok"}

    class _FakeResponse:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self._body = body

        def read(self, _amt: int = -1) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    def fake_opener(req: urllib.request.Request, timeout: float = 5.0):
        headers = {k.lower(): v for k, v in req.header_items()}
        requests_seen.append((req.full_url, headers))
        current = mode["response"]
        if current == "ok":
            payload = {
                "object": "list",
                "data": [
                    {"id": "qwen/qwen-2.5-72b-instruct"},
                    {"id": "deepseek/deepseek-r1"},
                    {"id": "qwen/qwen-2.5-72b-instruct"},  # duplicate
                ],
            }
            return _FakeResponse(200, json.dumps(payload).encode("utf-8"))
        if current == "empty":
            return _FakeResponse(200, b'{"object": "list", "data": []}')
        if current == "401":
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, io.BytesIO(b""))
        if current == "404":
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, io.BytesIO(b""))
        if current == "302":
            raise urllib.error.HTTPError(req.full_url, 302, "Found", None, io.BytesIO(b""))
        if current == "invalid_json":
            return _FakeResponse(200, b"<html>not json</html>")
        raise urllib.error.URLError("connection refused")

    env_path = tmp_path / "custom_providers.env"
    store = CustomProvidersEnvStore(env_path, workspace=tmp_path, http_opener=fake_opener)
    store.add_profile(
        name="Gateway",
        base_url="https://gateway.example.com/v1/models",
        api_key="secret-token",
    )

    # 1. 200 OK with models
    res_ok = store.discover_models("p01")
    assert res_ok["status"] == "ok"
    assert res_ok["models_url"] == "https://gateway.example.com/v1/models"
    assert [m["model_id"] for m in res_ok["models"]] == [
        "qwen/qwen-2.5-72b-instruct",
        "deepseek/deepseek-r1",
    ]
    assert res_ok["capabilities"] == UNVERIFIED_CUSTOM_CAPABILITIES
    assert requests_seen[-1][0] == "https://gateway.example.com/v1/models"
    assert requests_seen[-1][1].get("authorization") == "Bearer secret-token"

    # 2. 200 OK with empty list
    mode["response"] = "empty"
    res_empty = store.discover_models("p01")
    assert res_empty["status"] == "empty"
    assert res_empty["manual_fallback_allowed"] is True

    # 3. 401 auth failure
    mode["response"] = "401"
    res_401 = store.discover_models("p01")
    assert res_401["status"] == "auth_error"
    assert res_401["http_status"] == 401
    assert res_401["manual_fallback_allowed"] is True

    # 4. 404 /models unsupported -> manual model entry still works
    mode["response"] = "404"
    res_404 = store.discover_models("p01")
    assert res_404["status"] == "unsupported"
    assert res_404["manual_fallback_allowed"] is True
    updated = store.update_profile("p01", selected_model="custom/manual-model-id")
    assert updated["profiles"][0]["selected_model"] == "custom/manual-model-id"

    # 5. 302 redirect blocked
    mode["response"] = "302"
    res_302 = store.discover_models("p01")
    assert res_302["status"] == "redirect_blocked"

    # 6. Invalid JSON
    mode["response"] = "invalid_json"
    res_bad = store.discover_models("p01")
    assert res_bad["status"] == "invalid_response"

    # 7. Connection error
    mode["response"] = "conn_err"
    res_conn = store.discover_models("p01")
    assert res_conn["status"] == "connection_error"


def test_provider_api_custom_routes_end_to_end(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    catalog = ProviderCatalog(data_dir / "providers.sqlite", workspace_dir)
    try:
        app = FastAPI()
        app.include_router(provider_router(catalog, _AllowOwnerAuth()))
        client = TestClient(app)

        # Initial empty custom snapshot
        r0 = client.get("/v1/providers/custom")
        assert r0.status_code == 200
        assert r0.json()["profiles"] == []

        # Add 6 custom profiles via HTTP API
        for idx in range(1, 7):
            resp = client.post(
                "/v1/providers/custom",
                json={
                    "action": "add_custom_provider",
                    "name": f"Custom API {idx}",
                    "base_url": f"https://api{idx}.example.com/v1",
                    "api_key": f"secret-{idx}",
                    "enabled": True,
                    "selected_model": f"org/model-{idx}",
                },
            )
            assert resp.status_code == 200

        snap = client.get("/v1/providers/custom").json()
        assert len(snap["profiles"]) == 6

        # Bind p02 to converse and verify 409 when removing without confirmation
        b_resp = client.post(
            "/v1/providers/custom",
            json={
                "action": "bind_custom_role_model",
                "role": "converse",
                "profile_id": "p02",
                "model_id": "org/model-2",
            },
        )
        assert b_resp.status_code == 200

        del_conflict = client.post(
            "/v1/providers/custom",
            json={
                "action": "remove_custom_provider",
                "profile_id": "p02",
                "confirm_referenced": False,
            },
        )
        assert del_conflict.status_code == 409
        assert del_conflict.json()["error"] == "custom_provider_referenced"
        assert del_conflict.json()["referenced_by"] == ["converse"]

        del_ok = client.post(
            "/v1/providers/custom",
            json={
                "action": "remove_custom_provider",
                "profile_id": "p02",
                "confirm_referenced": True,
            },
        )
        assert del_ok.status_code == 200
        assert [p["profile_id"] for p in del_ok.json()["profiles"]] == [
            "p01",
            "p03",
            "p04",
            "p05",
            "p06",
        ]
    finally:
        catalog.close()


def test_custom_endpoints_optional_max_output_tokens_and_reasoning_effort(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "custom_providers.env"
    store = CustomProvidersEnvStore(env_path, workspace=tmp_path)

    # 1. llama.cpp profile with /v1/systemone custom endpoint and blank max_output_tokens (up to model)
    store.add_profile(
        name="llama.cpp Local Server",
        base_url="http://127.0.0.1:8080/v1",
        chat_completions_path="/chat/completions",
        models_path="/models",
        custom_endpoints="/v1/systemone, /props",
        max_output_tokens=None,
        selected_model="qwen2.5-coder-32b",
    )

    # 2. LM Studio profile with no extra custom endpoints and explicit max_output_tokens
    store.add_profile(
        name="LM Studio Local",
        base_url="http://127.0.0.1:1234/v1",
        chat_completions_path="/chat/completions",
        models_path="/models",
        custom_endpoints="",
        max_output_tokens=8192,
        selected_model="deepseek-r1-distill-qwen-14b",
    )

    raw_env = env_path.read_text(encoding="utf-8")
    assert 'KINAIYA_PROVIDER_P01_CUSTOM_ENDPOINTS="/v1/systemone, /props"' in raw_env
    assert "KINAIYA_PROVIDER_P01_MAX_OUTPUT_TOKENS" not in raw_env
    assert "KINAIYA_PROVIDER_P02_MAX_OUTPUT_TOKENS=8192" in raw_env

    snap = store.snapshot()
    p01 = snap["profiles"][0]
    p02 = snap["profiles"][1]
    assert p01["provider_type"] == "custom_openai_compatible"
    assert p01["chat_completions_path"] == "/chat/completions"
    assert p01["models_path"] == "/models"
    assert p01["custom_endpoints"] == "/v1/systemone, /props"
    assert p01["max_output_tokens"] is None

    assert p02["custom_endpoints"] == ""
    assert p02["max_output_tokens"] == 8192

    # Clearing max_output_tokens on p02 leaves it up to the model (None)
    updated_snap = store.apply_action(
        {
            "action": "update_custom_provider",
            "profile_id": "p02",
            "max_output_tokens": "",
        }
    )
    assert updated_snap["profiles"][1]["max_output_tokens"] is None
    assert "KINAIYA_PROVIDER_P02_MAX_OUTPUT_TOKENS" not in env_path.read_text(
        encoding="utf-8"
    )

    # 3. Global reasoning effort & per-model reasoning effort override
    assert updated_snap["global_reasoning_effort"] == "medium"
    after_global = store.apply_action(
        {
            "action": "set_global_reasoning_effort",
            "reasoning_effort": "high",
        }
    )
    assert after_global["global_reasoning_effort"] == "high"

    # Bind converse without per-model override -> inherits global "high"
    store.apply_action(
        {
            "action": "bind_custom_role_model",
            "role": "converse",
            "profile_id": "p01",
            "model_id": "qwen2.5-coder-32b",
        }
    )
    # Bind agent with explicit per-model override "low"
    after_bind = store.apply_action(
        {
            "action": "bind_custom_role_model",
            "role": "agent",
            "profile_id": "p02",
            "model_id": "deepseek-r1-distill-qwen-14b",
            "reasoning_effort": "low",
        }
    )
    assert after_bind["role_bindings"]["converse"]["reasoning_effort"] is None
    assert after_bind["role_bindings"]["converse"]["effective_reasoning_effort"] == "high"
    assert after_bind["role_bindings"]["agent"]["reasoning_effort"] == "low"
    assert after_bind["role_bindings"]["agent"]["effective_reasoning_effort"] == "low"

