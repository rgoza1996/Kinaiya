"""Independent #447 binding oracles, authored from the frozen phase-two contract.

All declarations, credentials and adapters here are synthetic. Constructor spies
make settings/capture observable without endpoint discovery or model inference.
These checks qualify measured text wiring, not audio, video or model quality.
"""

import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from packages.contracts.models import ProviderInfo
from packages.providers import measured_chat
from services import launcher
from services.converse.model_settings import (
    OPERATION_NAMES,
    ConverseModelConflict,
    ConverseModelSettings,
)
from services.settings.provider_catalog import (
    MAX_REVISION,
    CompatibleProfile,
    MeasuredChatProfile,
    ProviderCatalog,
    ProviderChange,
)
from services.settings.provider_factory import (
    EFFORTS,
    capture_recipes,
    inspect_recipe,
    open_captured_recipe,
)

BASE = "http://127.0.0.1:19447/v1"
MODEL = "synthetic-model"
KEY_ONE = "public-synthetic-local-credential-one-0123456789"
KEY_TWO = "public-synthetic-local-credential-two-0123456789"
# The same independently specified bytes are pinned by the Dart binding oracle.
CANONICAL = (
    '{"allow_cloud":false,"base_url":"http://127.0.0.1:19447/v1",'
    '"context_tokens":4096,"credential":null,"kind":"measured_chat",'
    '"max_tokens":2048,"model":"synthetic-model",'
    '"reasoning_effort":"none","timeout_s":90}'
)
LEGACY_CANONICAL = (
    '{"allow_cloud":false,"base_url":"http://127.0.0.1:19447/v1",'
    '"context_tokens":null,"credential":null,"kind":"compatible",'
    '"max_tokens":2048,"model":"synthetic-model",'
    '"reasoning_effort":null,"timeout_s":90}'
)


def config(**changes):
    return {
        "kind": "measured_chat",
        "base_url": BASE,
        "model": MODEL,
        "context_tokens": 4096,
        **changes,
    }


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


@pytest.fixture
def catalog(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ProviderCatalog(tmp_path / "protected" / "providers.sqlite", workspace)
    try:
        yield store
    finally:
        store.close()


def change(catalog, operation, idempotency_key, **fields):
    return catalog.change(
        ProviderChange.model_validate(
            {
                "schema_version": "1.0",
                "expected_revision": catalog.snapshot()["revision"],
                "idempotency_key": idempotency_key,
                "operation": operation,
                **fields,
            }
        )
    )["result"]


def save(catalog, cfg=None, *, key="profile-one"):
    return change(
        catalog,
        "profile_upsert",
        key,
        profile_id="measured-local",
        label="Public synthetic measured chat",
        config=config() if cfg is None else cfg,
    )


def binding(profile):
    return {name: profile[name] for name in ("id", "generation", "configuration_sha256")}


@pytest.fixture
def constructions(monkeypatch):
    calls = []

    class SyntheticAdapter:
        def __init__(self, base_url, model, **kwargs):
            self.arguments = {"base_url": base_url, "model": model, **kwargs}
            self.info = ProviderInfo(
                provider="openai-compatible", model=model, local=True, streaming=True
            )
            self.closed = 0
            calls.append(self)

        def stream(self, messages):
            raise AssertionError("Binding operations must not start inference")

        async def measure_prompt(self, messages):
            raise AssertionError("Binding operations must not probe/count")

        async def close(self):
            self.closed += 1

    monkeypatch.setattr(measured_chat, "MeasuredChatProvider", SyntheticAdapter)
    return calls


def test_native_defaults_and_canonical_bytes_match_independent_dart_vector():
    normalized = MeasuredChatProfile.model_validate(config()).model_dump(mode="json")
    assert normalized == json.loads(CANONICAL)
    assert canonical(normalized) == CANONICAL
    assert (
        MeasuredChatProfile.model_validate(config(base_url=BASE + "/")).model_dump() == normalized
    )
    old = CompatibleProfile.model_validate(
        {"kind": "compatible", "base_url": BASE, "model": MODEL}
    ).model_dump(mode="json")
    assert canonical(old) == LEGACY_CANONICAL
    assert old["context_tokens"] is None and old["reasoning_effort"] is None


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1", "https://127.0.0.1:443/v1", "http://[::1]:19447/v1", "https://[::1]/v1"],
)
def test_literal_loopback_endpoints_are_available(endpoint):
    assert MeasuredChatProfile.model_validate(config(base_url=endpoint)).allow_cloud is False


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:19447/v1",
        "http://LOCALHOST:19447/v1",
        "http://127.1:19447/v1",
        "http://2130706433:19447/v1",
        "https://example.com/v1",
        "http://0.0.0.0/v1",
        "ws://127.0.0.1:19447/v1",
        "http://user@127.0.0.1:19447/v1",
        "http://@127.0.0.1:19447/v1",
        "http://127.0.0.1:19447/v1?key=x",
        "http://127.0.0.1:19447/v1#fragment",
        "http://127.0.0.1:19447/v1?",
        "http://127.0.0.1:19447/v1#",
        "http://127.0.0.1:19447/v1\\other",
        "http://127.0.0.1:19447/v1\n",
        "http://127.0.0.1:0/v1",
        "http://127.0.0.1:65536/v1",
    ],
)
def test_catalog_refuses_non_literal_or_ambiguous_local_endpoints(endpoint):
    with pytest.raises(ValidationError):
        MeasuredChatProfile.model_validate(config(base_url=endpoint))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allow_cloud", True),
        ("allow_cloud", 0),
        ("allow_cloud", "false"),
        ("allow_cloud", None),
        ("context_tokens", None),
        ("context_tokens", True),
        ("context_tokens", 4096.0),
        ("context_tokens", 63),
        ("context_tokens", MAX_REVISION + 1),
        ("max_tokens", None),
        ("max_tokens", 15),
        ("max_tokens", 65537),
        ("max_tokens", True),
        ("max_tokens", 32.0),
        ("max_tokens", 4096),
        ("max_tokens", 4097),
        ("reasoning_effort", None),
        ("reasoning_effort", "max"),
        ("reasoning_effort", False),
        ("timeout_s", None),
        ("timeout_s", 0),
        ("timeout_s", 901),
        ("timeout_s", True),
        ("timeout_s", 5.0),
        ("model", " synthetic-model"),
        ("model", "synthetic\nmodel"),
        ("model", "a\u0085b"),
        ("model", "é" * 129),
        ("credential", {"id": "local-key", "generation": "a" * 31}),
        ("credential", {"id": "local-key", "generation": "a" * 32, "key": KEY_ONE}),
        ("unknown", "must-not-be-ignored"),
    ],
)
def test_measured_profile_is_strict_and_requires_a_fitting_declared_context(field, value):
    with pytest.raises(ValidationError):
        MeasuredChatProfile.model_validate(config(**{field: value}))


def test_context_is_required_but_all_documented_integer_and_effort_edges_are_valid():
    missing = config()
    del missing["context_tokens"]
    with pytest.raises(ValidationError):
        MeasuredChatProfile.model_validate(missing)
    for effort in EFFORTS:
        profile = MeasuredChatProfile.model_validate(
            config(context_tokens=64, max_tokens=16, timeout_s=1, reasoning_effort=effort)
        )
        assert profile.reasoning_effort == effort
    largest = MeasuredChatProfile.model_validate(
        config(context_tokens=MAX_REVISION, max_tokens=65536, timeout_s=900, model="é" * 128)
    )
    assert largest.context_tokens == MAX_REVISION


def test_catalog_save_and_recipe_inspection_only_declare_text_converse(catalog, constructions):
    saved = save(catalog)
    profile = catalog.snapshot()["profiles"][0]
    assert profile["config"] == json.loads(CANONICAL)
    assert saved["configuration_sha256"] == hashlib.sha256(CANONICAL.encode()).hexdigest()
    capability = profile["capability"]
    assert capability["declared_roles"] == ["converse"]
    assert capability["supported_inputs"] == ["text"]
    assert capability["measured_complete_interface"] is True
    assert capability["measured_stream_interface"] is True
    assert capability["qualification"] == "not_checked"
    assert capability["readiness"] == "unknown"
    recipe = inspect_recipe(catalog, binding(saved), role="converse")
    assert recipe.config == json.loads(CANONICAL)
    assert recipe.reasoning == "none"
    assert recipe.info.model_dump() == {
        "schema_version": "1.0",
        "provider": "openai-compatible",
        "model": MODEL,
        "local": True,
        "streaming": True,
    }
    assert constructions == []


@pytest.mark.parametrize("role", ["agent", "auxiliary", "decisions", "stt", "tts"])
def test_measured_recipe_has_no_implicit_non_converse_eligibility(catalog, constructions, role):
    saved = save(catalog)
    with pytest.raises(ValueError):
        inspect_recipe(catalog, binding(saved), role=role)
    with pytest.raises(ValueError):
        capture_recipes(catalog, [(binding(saved), None)], role=role)
    assert constructions == []


@pytest.mark.parametrize(
    "options",
    [
        {"role": "agent"},
        {"role": "auxiliary"},
        {"role": "converse", "auxiliary": True},
        {"role": "converse", "pre_generation_proofs": True},
        {"role": "agent", "auxiliary": True, "pre_generation_proofs": True},
    ],
)
def test_captured_recipe_cannot_bypass_role_auxiliary_or_proof_fences(
    catalog, constructions, options
):
    saved = save(catalog)
    captured = capture_recipes(catalog, [(binding(saved), None)], role="converse")[0]
    try:
        with pytest.raises(ValueError):
            open_captured_recipe(captured, **options)
        assert constructions == []
    finally:
        captured.release()


@pytest.mark.asyncio
async def test_capture_keeps_protected_credential_generation_and_exact_constructor_values(
    catalog, constructions
):
    credential = change(
        catalog, "credential_import", "key-one", credential_id="local-key", key=KEY_ONE
    )
    reference = {name: credential[name] for name in ("id", "generation")}
    saved = save(
        catalog, config(credential=reference, max_tokens=128, timeout_s=17, reasoning_effort="low")
    )
    captured = capture_recipes(catalog, [(binding(saved), "minimal")], role="converse")[0]
    replacement = change(
        catalog, "credential_import", "key-two", credential_id="local-key", key=KEY_TWO
    )
    assert replacement["generation"] != reference["generation"]
    assert constructions == []
    adapter = open_captured_recipe(captured, role="converse")
    assert adapter.arguments == {
        "base_url": BASE,
        "model": MODEL,
        "api_key": KEY_ONE,
        "context_tokens": 4096,
        "max_tokens": 128,
        "reasoning_effort": "minimal",
        "timeout": 17,
    }
    assert captured.consumed is True and captured.key is None
    assert KEY_ONE not in captured.recipe.configuration
    with pytest.raises(ValueError):
        open_captured_recipe(captured, role="converse")
    assert len(constructions) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_anonymous_converse_lease_is_revision_bound_and_never_claims_media_readiness(
    catalog, constructions
):
    saved = save(catalog)
    host = SimpleNamespace(
        info=ProviderInfo(
            provider="public-host", model="synthetic-host", local=True, streaming=True
        ),
        stream=lambda _: None,
    )
    runtime = SimpleNamespace(
        provider=host, model_operations=lambda: dict.fromkeys(OPERATION_NAMES, 0)
    )
    settings = ConverseModelSettings(runtime, catalog)
    lease = None
    try:
        snapshot = settings.snapshot()
        row = snapshot["catalog_profiles"][0]
        assert row["selectable"] is True
        assert row["reasoning_options"] == list(EFFORTS)
        assert (row["qualification"], row["readiness"]) == ("not_checked", "unknown")
        selected = {"source": "catalog", "catalog_binding": binding(saved), "reasoning": None}
        request = {
            "schema_version": "1.0",
            "expected_revision": snapshot["revision"],
            "expected_state": snapshot["state_token"],
            "idempotency_key": "choose-local",
            "selection": selected,
        }
        with pytest.raises(ConverseModelConflict):
            settings.change({**request, "expected_state": "0" * 64})
        receipt = settings.change(request)
        assert receipt["role"] == "converse"
        assert constructions == []
        lease = settings.capture()
        assert lease.current() is True
        assert lease.pin.source == "catalog"
        assert lease.pin.settings_revision == receipt["revision"]
        assert lease.pin.catalog_revision == catalog.snapshot()["revision"]
        assert lease.pin.catalog_binding.model_dump() == binding(saved)
        assert (lease.pin.provider, lease.pin.model, lease.pin.local, lease.pin.reasoning) == (
            "openai-compatible",
            MODEL,
            True,
            "none",
        )
        assert lease.provider.arguments["api_key"] == ""
        assert lease.provider.arguments["context_tokens"] == 4096
        assert len(constructions) == 1
        busy = settings.snapshot()
        with pytest.raises(ConverseModelConflict):
            settings.change(
                {
                    "schema_version": "1.0",
                    "expected_revision": busy["revision"],
                    "expected_state": busy["state_token"],
                    "idempotency_key": "busy-host",
                    "selection": {"source": "host", "catalog_binding": None, "reasoning": None},
                }
            )
        await lease.close()
        assert lease.provider.closed == 1
        assert lease.current() is False
        assert settings.snapshot()["operation_state"]["leases"] == 0
    finally:
        if lease is not None and not lease.settled:
            await lease.close()
        await settings.close()


def test_binding_digest_mismatch_and_ordinary_compatible_remain_ineligible(catalog, constructions):
    saved = save(catalog)
    with pytest.raises(ValueError):
        inspect_recipe(
            catalog, {**binding(saved), "configuration_sha256": "0" * 64}, role="converse"
        )
    plain = save(
        catalog,
        {"kind": "compatible", "base_url": BASE, "model": MODEL, "context_tokens": 4096},
        key="ordinary-compatible",
    )
    for role in ("agent", "converse"):
        with pytest.raises(ValueError):
            inspect_recipe(catalog, binding(plain), role=role)
    assert constructions == []


def host_profile(**changes):
    return {
        "kind": "measured_chat",
        "base_url": BASE,
        "model": MODEL,
        "allow_cloud": False,
        "context_tokens": 4096,
        "max_tokens": 128,
        "reasoning_effort": "none",
        "timeout": 17,
        **changes,
    }


def test_host_factory_and_setup_resolve_key_without_sending_or_leaking_cloud_flag(
    monkeypatch, constructions
):
    monkeypatch.setenv("KINAIYA_SYNTHETIC_MEASURED_KEY", KEY_ONE)
    profile = host_profile(api_key_env="KINAIYA_SYNTHETIC_MEASURED_KEY")
    adapter = launcher.provider({"providers": {"converse": profile}}, "converse")
    assert adapter.arguments == {
        "base_url": BASE,
        "model": MODEL,
        "api_key": KEY_ONE,
        "context_tokens": 4096,
        "max_tokens": 128,
        "reasoning_effort": "none",
        "timeout": 17,
    }
    assert profile["api_key_env"] == "KINAIYA_SYNTHETIC_MEASURED_KEY"
    assert profile["allow_cloud"] is False
    checked = launcher.validate_setup_provider("converse", profile)
    assert checked.arguments == adapter.arguments
    assert len(constructions) == 2


@pytest.mark.parametrize("role", ["agent", "auxiliary", "decisions", "stt", "tts"])
def test_host_and_setup_refuse_non_converse_before_adapter_construction(constructions, role):
    profile = host_profile()
    with pytest.raises((ValueError, SystemExit)):
        launcher.provider({"providers": {role: profile}}, role)
    with pytest.raises(ValueError):
        launcher.validate_setup_provider(role, profile)
    assert constructions == []


@pytest.mark.parametrize("cloud", [True, 0, "false", None])
def test_host_cloud_flag_must_be_exact_false_before_adapter_construction(constructions, cloud):
    with pytest.raises((ValueError, SystemExit)):
        launcher.provider({"providers": {"converse": host_profile(allow_cloud=cloud)}}, "converse")
    assert constructions == []
