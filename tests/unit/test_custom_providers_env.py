"""Unit, security, concurrency, and API tests for custom OpenAI-compatible .env providers (#352, #369, #370)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.settings.custom_providers_env import (
    MASKED_API_KEY_DISPLAY,
    MAX_DISCOVERY_RESPONSE_BYTES,
    MAX_ENV_FILE_BYTES,
    UNVERIFIED_CUSTOM_CAPABILITIES,
    CustomProviderConflictError,
    CustomProviderError,
    CustomProviderReferencedError,
    CustomProvidersEnvStore,
    CustomProviderStaleDiscoveryError,
    CustomProviderValidationError,
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
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        '# Existing user environment comments\nUNRELATED_FOO="keep-$HOME-literal"\nOTHER_FLAG=1\n',
        encoding="utf-8",
    )
    env_path.chmod(0o600)

    store = CustomProvidersEnvStore(env_path, workspace=workspace)
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
    assert "KINAIYA_CUSTOM_PROVIDER_REVISION=7" in raw_text

    # Reload in a fresh store instance
    reloaded = CustomProvidersEnvStore(env_path, workspace=workspace)
    snap = reloaded.snapshot()
    assert snap["unlimited"] is True
    assert snap["revision"] == 7
    assert len(snap["state_sha256"]) == 64
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


def test_deletion_never_renumbers_ids_and_retains_unavailable_role_references_on_confirm(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "custom_providers.env"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    store = CustomProvidersEnvStore(env_path, workspace=workspace)
    store.add_profile(name="One", base_url="https://one.example.com/v1", api_key="k1")
    store.add_profile(name="Two", base_url="https://two.example.com/v1", api_key="k2")
    store.add_profile(name="Three", base_url="https://three.example.com/v1", api_key="k3")

    # Delete p02 (unreferenced) -> remaining are p01, p03
    snap = store.remove_profile("p02")
    assert [p["profile_id"] for p in snap["profiles"]] == ["p01", "p03"]

    # Adding a new profile allocates p04 (never reuses p02 or renumbers p03)
    snap = store.add_profile(name="Four", base_url="https://four.example.com/v1", api_key="k4")
    assert [p["profile_id"] for p in snap["profiles"]] == ["p01", "p03", "p04"]

    # Bind p03 to converse and agent -> reported as proposed_unavailable (not active)
    bound_snap = store.bind_role_model(
        role="converse", profile_id="p03", model_id="shared/model-v1"
    )
    assert bound_snap["role_bindings"]["converse"]["status"] == "proposed_unavailable"
    assert bound_snap["role_bindings"]["converse"]["active"] is False
    assert (
        bound_snap["role_bindings"]["converse"]["unavailable_reason"]
        == "native_role_runtime_not_wired"
    )
    store.bind_role_model(role="agent", profile_id="p03", model_id="shared/model-v1")

    with pytest.raises(CustomProviderReferencedError) as exc_info:
        store.remove_profile("p03", confirm_referenced=False)
    assert exc_info.value.profile_id == "p03"
    assert exc_info.value.referenced_by == ["converse", "agent"]

    # Confirming removal deletes p03 and retains role bindings in unavailable_needs_reselection (r56)
    snap_after = store.remove_profile("p03", confirm_referenced=True)
    assert [p["profile_id"] for p in snap_after["profiles"]] == ["p01", "p04"]
    for role in ("converse", "agent"):
        rb = snap_after["role_bindings"][role]
        assert rb is not None
        assert rb["profile_id"] == "p03"
        assert rb["qualified_model_id"] == "p03:shared/model-v1"
        assert rb["status"] == "unavailable_needs_reselection"
        assert rb["active"] is False
        assert rb["unavailable_reason"] == "referenced_profile_removed"
        assert rb["needs_reselection"] is True

    raw_env = env_path.read_text(encoding="utf-8")
    assert 'KINAIYA_CUSTOM_ROLE_CONVERSE="p03:shared/model-v1"' in raw_env
    assert 'KINAIYA_CUSTOM_ROLE_AGENT="p03:shared/model-v1"' in raw_env


def test_identical_model_ids_across_two_custom_providers_do_not_conflate(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "custom_providers.env"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    store = CustomProvidersEnvStore(env_path, workspace=workspace)
    store.add_profile(name="OpenRouter", base_url="https://openrouter.ai/api/v1", api_key="k1")
    store.add_profile(name="Together", base_url="https://api.together.xyz/v1", api_key="k2")

    shared_model = "meta-llama/Llama-3.3-70B-Instruct"
    store.bind_role_model(role="converse", profile_id="p01", model_id=shared_model)
    store.bind_role_model(role="agent", profile_id="p02", model_id=shared_model)

    reloaded = CustomProvidersEnvStore(env_path, workspace=workspace).snapshot()
    conv_binding = reloaded["role_bindings"]["converse"]
    agent_binding = reloaded["role_bindings"]["agent"]

    assert conv_binding is not None
    assert agent_binding is not None
    assert conv_binding["profile_id"] == "p01"
    assert conv_binding["provider_name"] == "OpenRouter"
    assert conv_binding["qualified_model_id"] == f"p01:{shared_model}"
    assert conv_binding["status"] == "proposed_unavailable"
    assert conv_binding["active"] is False
    assert agent_binding["profile_id"] == "p02"
    assert agent_binding["provider_name"] == "Together"
    assert agent_binding["qualified_model_id"] == f"p02:{shared_model}"
    assert agent_binding["status"] == "proposed_unavailable"
    assert agent_binding["active"] is False


def test_api_key_preservation_rotation_and_clearing(tmp_path: Path) -> None:
    env_path = tmp_path / "custom_providers.env"
    store = CustomProvidersEnvStore(env_path)
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
        normalize_openai_base_url("https://openrouter.ai/api/v1/") == "https://openrouter.ai/api/v1"
    )
    assert (
        normalize_openai_base_url("https://api.example.com/v1/v1/models")
        == "https://api.example.com/v1"
    )
    assert normalize_openai_base_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434/v1"
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
    store = CustomProvidersEnvStore(env_path, http_opener=fake_opener)
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


def test_disabled_profile_discovery_refused_before_credential_access(tmp_path: Path) -> None:
    opener_calls: list[str] = []

    def forbidden_opener(req: urllib.request.Request, timeout: float = 5.0):
        opener_calls.append(req.full_url)
        raise AssertionError("Network opener must never be called for a disabled profile")

    env_path = tmp_path / "custom_providers.env"
    store = CustomProvidersEnvStore(env_path, http_opener=forbidden_opener)
    store.add_profile(
        name="Disabled Gateway",
        base_url="https://gateway.example.com/v1",
        api_key="top-secret-never-disclose",
        enabled=False,
    )

    with pytest.raises(CustomProviderError, match="disabled"):
        store.discover_models("p01")
    assert opener_calls == []

    with pytest.raises(CustomProviderError, match="disabled"):
        asyncio.run(store.discover_models_async("p01"))
    assert opener_calls == []


def test_strict_operation_specific_wire_validation_rejects_type_coercion_and_unknown_fields(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "custom_providers.env"
    store = CustomProvidersEnvStore(env_path)
    store.add_profile(
        name="Strict Profile",
        base_url="https://strict.example.com/v1",
        api_key="strict-key",
        selected_model="org/model-a",
    )

    # 1. String booleans ("false" / "true") must be rejected, never coerced via bool("false") -> True!
    for bad_payload in [
        {
            "action": "add_custom_provider",
            "name": "Bad Bool",
            "base_url": "http://remote.example.com/v1",
            "allow_remote_http": "false",
        },
        {
            "action": "add_custom_provider",
            "name": "Bad Enabled",
            "base_url": "https://ok.example.com/v1",
            "enabled": "false",
        },
        {
            "action": "update_custom_provider",
            "profile_id": "p01",
            "clear_api_key": "false",
        },
        {
            "action": "set_custom_provider_enabled",
            "profile_id": "p01",
            "enabled": "false",
        },
        {
            "action": "remove_custom_provider",
            "profile_id": "p01",
            "confirm_referenced": "false",
        },
    ]:
        with pytest.raises(CustomProviderValidationError):
            store.apply_action(bad_payload)

    # Verify the stored key and enabled state were NOT mutated by any rejected request
    snap = store.snapshot()
    assert snap["profiles"][0]["has_api_key"] is True
    assert snap["profiles"][0]["enabled"] is True
    assert len(snap["profiles"]) == 1

    # 2. Non-string values where strings are required and string ints where int | None is required
    for bad_types in [
        {
            "action": "add",
            "name": 12345,
            "base_url": "https://ok.example.com/v1",
        },
        {
            "action": "add",
            "name": "Bad Max Tokens String",
            "base_url": "https://ok.example.com/v1",
            "max_output_tokens": "4096",
        },
        {
            "action": "add",
            "name": "Bad Max Tokens Bool",
            "base_url": "https://ok.example.com/v1",
            "max_output_tokens": True,
        },
        {
            "action": "update",
            "profile_id": "p01",
            "max_output_tokens": "",
        },
        {
            "action": "bind_role",
            "role": "converse",
            "profile_id": 1,
            "model_id": "org/model-a",
        },
        {
            "action": "reconcile",
            "client_operation_id": 999,
        },
    ]:
        with pytest.raises(CustomProviderValidationError):
            store.apply_action(bad_types)

    # 3. Unknown fields on any action are rejected with CustomProviderValidationError
    for action_with_unknown in [
        {
            "action": "add_custom_provider",
            "name": "X",
            "base_url": "https://ok.example.com/v1",
            "unexpected_field": 1,
        },
        {
            "action": "update_custom_provider",
            "profile_id": "p01",
            "role": "converse",
        },
        {
            "action": "remove_custom_provider",
            "profile_id": "p01",
            "extra": True,
        },
        {
            "action": "set_custom_provider_enabled",
            "profile_id": "p01",
            "enabled": False,
            "clear_api_key": True,
        },
        {
            "action": "discover_custom_models",
            "profile_id": "p01",
            "api_key": "leaked",
        },
        {
            "action": "bind_custom_role_model",
            "role": "converse",
            "profile_id": "p01",
            "model_id": "org/model-a",
            "unknown": "nope",
        },
        {
            "action": "reconcile",
            "client_operation_id": "op-1",
            "profile_id": "p01",
        },
    ]:
        with pytest.raises(CustomProviderValidationError):
            store.apply_action(action_with_unknown)


def test_protected_file_descriptor_admission_rejects_symlinks_hardlinks_permissions_oversized_and_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # 1. Rejects .env path inside agent workspace
    with pytest.raises(CustomProviderError, match="outside the agent workspace"):
        CustomProvidersEnvStore(workspace / "custom_providers.env", workspace=workspace)

    # 2. Rejects symlink .env file
    real_env = tmp_path / "real.env"
    real_env.write_text("", encoding="utf-8")
    real_env.chmod(0o600)
    symlink_env = tmp_path / "symlink.env"
    symlink_env.symlink_to(real_env)
    with pytest.raises(CustomProviderError, match="symlink"):
        CustomProvidersEnvStore(symlink_env, workspace=workspace)

    # Also if replaced with symlink after store creation, _read_protected_bytes rejects via O_NOFOLLOW
    late_symlink_env = tmp_path / "late_symlink.env"
    store_late = CustomProvidersEnvStore(late_symlink_env, workspace=workspace)
    late_symlink_env.symlink_to(real_env)
    with pytest.raises(CustomProviderError):
        store_late.snapshot()

    # 3. Rejects group/world-accessible permissions (e.g. 0644)
    loose_env = tmp_path / "loose.env"
    loose_env.write_text("FOO=1\n", encoding="utf-8")
    loose_env.chmod(0o644)
    store_loose = CustomProvidersEnvStore(loose_env, workspace=workspace)
    with pytest.raises(CustomProviderError, match="0600"):
        store_loose.snapshot()

    # 4. Rejects hard-linked file (st_nlink > 1)
    hl_source = tmp_path / "hl_source.env"
    hl_source.write_text("FOO=1\n", encoding="utf-8")
    hl_source.chmod(0o600)
    hl_link = tmp_path / "hl_link.env"
    os.link(hl_source, hl_link)
    store_hl = CustomProvidersEnvStore(hl_source, workspace=workspace)
    with pytest.raises(CustomProviderError, match="hard links"):
        store_hl.snapshot()

    # 5. Rejects non-regular file (FIFO / directory)
    fifo_path = tmp_path / "fifo.env"
    os.mkfifo(fifo_path, 0o600)
    store_fifo = CustomProvidersEnvStore(fifo_path, workspace=workspace)
    with pytest.raises(CustomProviderError):
        store_fifo.snapshot()

    # 6. Rejects oversized .env file (> MAX_ENV_FILE_BYTES)
    oversized_env = tmp_path / "oversized.env"
    oversized_env.write_bytes(b"#" + b"a" * (MAX_ENV_FILE_BYTES + 10))
    oversized_env.chmod(0o600)
    store_big = CustomProvidersEnvStore(oversized_env, workspace=workspace)
    with pytest.raises(CustomProviderError, match="exceeds maximum size"):
        store_big.snapshot()


def test_cas_expected_revision_idempotent_receipts_and_reconcile(tmp_path: Path) -> None:
    env_path = tmp_path / "custom_providers.env"
    store = CustomProvidersEnvStore(env_path)

    # Initial revision is 0
    snap0 = store.snapshot()
    assert snap0["revision"] == 0

    # Add p01 with expected_revision=0 and client_operation_id="op-add-1"
    add_req = {
        "action": "add_custom_provider",
        "name": "First Provider",
        "base_url": "https://first.example.com/v1",
        "api_key": "secret-1",
        "expected_revision": 0,
        "client_operation_id": "op-add-1",
    }
    snap1 = store.apply_action(add_req)
    assert snap1["revision"] == 1
    assert snap1["reconciled"] is False
    assert [p["profile_id"] for p in snap1["profiles"]] == ["p01"]

    # Another mutation advances revision to 2
    snap2 = store.apply_action(
        {
            "action": "add_custom_provider",
            "name": "Second Provider",
            "base_url": "https://second.example.com/v1",
            "expected_revision": 1,
            "client_operation_id": "op-add-2",
        }
    )
    assert snap2["revision"] == 2
    assert [p["profile_id"] for p in snap2["profiles"]] == ["p01", "p02"]

    # Retrying "op-add-1" with its original expected_revision=0 returns idempotent committed
    # outcome with reconciled=True instead of raising conflict or duplicating p01!
    replayed = store.apply_action(add_req)
    assert replayed["reconciled"] is True
    assert replayed["client_operation_id"] == "op-add-1"
    assert replayed["committed_revision"] == 1
    assert [p["profile_id"] for p in store.snapshot()["profiles"]] == ["p01", "p02"]

    # Reusing "op-add-1" with a different payload raises CustomProviderConflictError
    with pytest.raises(CustomProviderConflictError):
        store.apply_action(
            {
                **add_req,
                "name": "Tampered Name",
            }
        )

    # Sending a new operation with stale expected_revision=1 raises CustomProviderConflictError
    with pytest.raises(CustomProviderConflictError):
        store.apply_action(
            {
                "action": "update_custom_provider",
                "profile_id": "p01",
                "name": "Stale Edit",
                "expected_revision": 1,
                "client_operation_id": "op-stale-edit",
            }
        )

    # Reconciling "op-add-1" recovers the committed outcome
    reconciled = store.reconcile("op-add-1")
    assert reconciled["status"] == "committed"
    assert reconciled["found"] is True
    assert reconciled["reconciled"] is True
    assert reconciled["committed_revision"] == 1
    assert reconciled["current_revision"] == 2
    assert reconciled["target_profile_id"] == "p01"

    # Reconciling an unknown operation ID reports not_found without failing
    missing = store.reconcile("op-never-sent")
    assert missing["status"] == "not_found"
    assert missing["found"] is False
    assert missing["reconciled"] is False
    assert missing["current_revision"] == 2


def test_async_discovery_nonblocking_event_loop_and_multi_writer_cas_safety(
    tmp_path: Path,
) -> None:
    """Verify #369 (nonblocking async discovery + bounded stream/cleanup) and #370 (multi-writer CAS)."""

    async def _run() -> None:
        env_path = tmp_path / "custom_providers.env"
        env_path.write_text('UNRELATED_INITIAL="preserve-me"\n', encoding="utf-8")
        env_path.chmod(0o600)

        discovery_started = asyncio.Event()
        release_discovery = asyncio.Event()
        stream_closed = {"count": 0}
        mode = {"behavior": "controlled_ok"}

        class _TrackedAsyncStream(httpx.AsyncByteStream):
            def __init__(self, chunks: list[bytes], *, wait_gate: bool = False) -> None:
                self._chunks = chunks
                self._wait_gate = wait_gate

            async def __aiter__(self):
                if self._wait_gate:
                    discovery_started.set()
                    await release_discovery.wait()
                for chunk in self._chunks:
                    yield chunk

            async def aclose(self) -> None:
                stream_closed["count"] += 1

        class _TestAsyncTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                current = mode["behavior"]
                if current == "controlled_ok":
                    body = json.dumps(
                        {"data": [{"id": "discovered/model-1"}, {"id": "discovered/model-2"}]}
                    ).encode("utf-8")
                    return httpx.Response(
                        200,
                        headers={"Content-Type": "application/json"},
                        stream=_TrackedAsyncStream([body[:12], body[12:]], wait_gate=True),
                        request=request,
                    )
                if current == "oversized":
                    # Yield chunks totaling MAX_DISCOVERY_RESPONSE_BYTES + 1024
                    chunk = b"x" * 65536
                    chunks = [chunk] * ((MAX_DISCOVERY_RESPONSE_BYTES // 65536) + 2)
                    return httpx.Response(
                        200,
                        headers={"Content-Type": "application/json"},
                        stream=_TrackedAsyncStream(chunks, wait_gate=False),
                        request=request,
                    )
                if current == "hang":
                    return httpx.Response(
                        200,
                        headers={"Content-Type": "application/json"},
                        stream=_TrackedAsyncStream([b'{"data":['], wait_gate=True),
                        request=request,
                    )
                raise RuntimeError(f"Unexpected transport mode: {current}")

        store = CustomProvidersEnvStore(
            env_path,
            async_transport=_TestAsyncTransport(),
            discovery_timeout_s=0.5,
        )
        store.add_profile(
            name="Primary Async",
            base_url="https://primary.example.com/v1",
            api_key="key-v1",
        )

        app = FastAPI()
        app.include_router(provider_router(None, _AllowOwnerAuth(), custom_store=store))

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as asgi_client:
            # 1. Start discovery on p01 and hold it in flight
            disc_task = asyncio.create_task(asgi_client.post("/v1/providers/custom/p01/discover"))
            await asyncio.wait_for(discovery_started.wait(), timeout=2.0)
            assert not disc_task.done()

            # 2. While p01 discovery is blocked on network I/O, concurrent ASGI requests
            #    (GET /v1/providers/custom and POST adding p02 + updating p01 name) complete!
            inspect_resp = await asyncio.wait_for(
                asgi_client.get("/v1/providers/custom"), timeout=1.0
            )
            assert inspect_resp.status_code == 200

            add_p02_resp = await asyncio.wait_for(
                asgi_client.post(
                    "/v1/providers/custom",
                    json={
                        "action": "add_custom_provider",
                        "name": "Concurrent Secondary",
                        "base_url": "https://secondary.example.com/v1",
                        "api_key": "key-p02",
                    },
                ),
                timeout=1.0,
            )
            assert add_p02_resp.status_code == 200

            # Also append an unrelated .env line while discovery is in flight
            with store._exclusive_lock():
                current_raw = env_path.read_text(encoding="utf-8")
                store._write_protected_bytes(
                    env_path,
                    ('UNRELATED_CONCURRENT="survived"\n' + current_raw).encode("utf-8"),
                    max_bytes=MAX_ENV_FILE_BYTES,
                )

            # Release discovery and verify both p02, unrelated .env line, and p01 discovered models survive!
            release_discovery.set()
            disc_resp = await asyncio.wait_for(disc_task, timeout=2.0)
            assert disc_resp.status_code == 200
            disc_json = disc_resp.json()
            assert disc_json["status"] == "ok"
            assert [m["model_id"] for m in disc_json["models"]] == [
                "discovered/model-1",
                "discovered/model-2",
            ]
            assert stream_closed["count"] >= 1

            raw_after = env_path.read_text(encoding="utf-8")
            assert 'UNRELATED_INITIAL="preserve-me"' in raw_after
            assert 'UNRELATED_CONCURRENT="survived"' in raw_after
            snap_after = store.snapshot()
            assert [p["profile_id"] for p in snap_after["profiles"]] == ["p01", "p02"]
            assert snap_after["profiles"][0]["discovered_models"] == [
                "discovered/model-1",
                "discovered/model-2",
            ]
            assert snap_after["profiles"][1]["name"] == "Concurrent Secondary"

            # 3. Stale discovery conflict (#370): rotate p01's API key while discovery is in flight!
            discovery_started.clear()
            release_discovery.clear()
            stale_task = asyncio.create_task(asgi_client.post("/v1/providers/custom/p01/discover"))
            await asyncio.wait_for(discovery_started.wait(), timeout=2.0)

            # Rotate p01 API key while discovery is in flight
            rot_resp = await asgi_client.post(
                "/v1/providers/custom",
                json={
                    "action": "update_custom_provider",
                    "profile_id": "p01",
                    "api_key": "rotated-key-v2",
                },
            )
            assert rot_resp.status_code == 200

            release_discovery.set()
            stale_resp = await asyncio.wait_for(stale_task, timeout=2.0)
            assert stale_resp.status_code == 409
            assert stale_resp.json()["error"] == "custom_provider_stale_discovery"
            assert 'KINAIYA_PROVIDER_P01_API_KEY="rotated-key-v2"' in env_path.read_text(
                encoding="utf-8"
            )

            # Direct store call also raises CustomProviderStaleDiscoveryError when endpoint changes in flight
            discovery_started.clear()
            release_discovery.clear()
            direct_stale_task = asyncio.create_task(store.discover_models_async("p01"))
            await asyncio.wait_for(discovery_started.wait(), timeout=2.0)
            store.update_profile("p01", base_url="https://changed.example.com/v1")
            release_discovery.set()
            with pytest.raises(CustomProviderStaleDiscoveryError):
                await asyncio.wait_for(direct_stale_task, timeout=2.0)

            # 4. Oversized response (> MAX_DISCOVERY_RESPONSE_BYTES) is refused and stream closed
            mode["behavior"] = "oversized"
            before_closes = stream_closed["count"]
            over_res = await store.discover_models_async("p01")
            assert over_res["status"] == "invalid_response"
            assert "exceeded maximum size" in over_res["message"]
            assert stream_closed["count"] > before_closes

            # 5. Slow/hanging response times out cleanly and closes stream
            mode["behavior"] = "hang"
            discovery_started.clear()
            release_discovery.clear()
            before_closes = stream_closed["count"]
            timeout_res = await store.discover_models_async("p01", timeout_s=0.1)
            assert timeout_res["status"] == "connection_error"
            assert stream_closed["count"] > before_closes

    asyncio.run(_run())


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
        assert b_resp.json()["role_bindings"]["converse"]["status"] == "proposed_unavailable"
        assert b_resp.json()["role_bindings"]["converse"]["active"] is False

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
                "client_operation_id": "del-p02-op",
            },
        )
        assert del_ok.status_code == 200
        del_json = del_ok.json()
        assert [p["profile_id"] for p in del_json["profiles"]] == [
            "p01",
            "p03",
            "p04",
            "p05",
            "p06",
        ]
        assert del_json["role_bindings"]["converse"]["status"] == "unavailable_needs_reselection"
        assert (
            del_json["role_bindings"]["converse"]["unavailable_reason"]
            == "referenced_profile_removed"
        )

        # Verify receipt lookup via GET /v1/providers/custom/receipts/{key}
        rec_resp = client.get("/v1/providers/custom/receipts/del-p02-op")
        assert rec_resp.status_code == 200
        assert rec_resp.json()["found"] is True
        assert rec_resp.json()["reconciled"] is True
        assert rec_resp.json()["target_profile_id"] == "p02"
    finally:
        catalog.close()


def test_custom_endpoints_optional_max_output_tokens_and_reasoning_effort(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "custom_providers.env"
    store = CustomProvidersEnvStore(env_path)

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
            "max_output_tokens": None,
            "clear_max_output_tokens": True,
        }
    )
    assert updated_snap["profiles"][1]["max_output_tokens"] is None
    assert "KINAIYA_PROVIDER_P02_MAX_OUTPUT_TOKENS" not in env_path.read_text(encoding="utf-8")

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
    assert after_bind["role_bindings"]["converse"]["status"] == "proposed_unavailable"
    assert after_bind["role_bindings"]["converse"]["active"] is False
    assert after_bind["role_bindings"]["agent"]["reasoning_effort"] == "low"
    assert after_bind["role_bindings"]["agent"]["effective_reasoning_effort"] == "low"
    assert after_bind["role_bindings"]["agent"]["status"] == "proposed_unavailable"
    assert after_bind["role_bindings"]["agent"]["active"] is False
