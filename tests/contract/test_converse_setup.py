"""Owner setup is a bounded declaration, not a live model or disclosure grant."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import jsonschema
import pytest
from pydantic import ValidationError

from packages.contracts.converse_setup import ConverseSetup
from packages.contracts.models import ProviderInfo
from services import launcher
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.setup import (
    HOST_SETUP_UNAVAILABLE,
    host_setup,
    safe_host_setup,
    setup_state,
)

TOKENS = {"owner": "o" * 32, "converse": "c" * 32, "resources": "r" * 32}
AUTH = Auth(TOKENS, ("https://ui.invalid",))


def configuration():
    return {
        "providers": {
            "converse": {
                "kind": "lmstudio",
                "model": "synthetic-conversation",
                "base_url": "http://127.0.0.1:1234",
                "reasoning_effort": "none",
                "api_key": "synthetic-provider-secret",
            },
            "decisions": {
                "kind": "laya",
                "model": "synthetic-routing",
                "base_url": "http://127.0.0.1:8936",
                "api_key_env": "SYNTHETIC_CREDENTIAL_REFERENCE",
            },
        },
        "voice": {"url": "http://127.0.0.1:8935", "profile": "Kokoro 82M"},
        "resources": {
            "enabled": True,
            "stt_model": "tiny.en",
            "voice_device": "cuda",
            "voice_stt_checkpoint": "/synthetic-private-models/tiny.en.pt",
            "voice_python": "/synthetic-private-environment/bin/python",
        },
        "private_journal": "/synthetic-private-data/journal.sqlite",
    }


class Runtime:
    def __init__(self):
        self.provider = SimpleNamespace(
            info=ProviderInfo(
                provider="lmstudio", model="synthetic-conversation", local=True, streaming=True
            )
        )
        self.text = {
            "active": 0,
            "cleanup_pending": 0,
            "cleanup_failed": 0,
            "admitting": 0,
            "capacity": 32,
        }
        self.speech = {"active": 0, "cancellation_pending": 0, "cleanup_failed": 0}

    def text_status(self):
        return dict(self.text)

    def speech_status(self):
        return dict(self.speech)


@pytest.fixture
async def owner_setup():
    runtime = Runtime()
    host = host_setup(configuration())
    app = create_app(runtime, AUTH, setup=host)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        yield runtime, host, client


def owner_headers(**extra):
    return {"Authorization": "Bearer " + TOKENS["owner"], **extra}


@pytest.mark.parametrize("alias", ["/synthetic/model", "bad\u202ename", "\ud800", "x" * 129])
def test_safe_projection_refuses_display_data_without_inventing_embedded_configuration(alias):
    config = configuration()
    config["providers"]["decisions"]["model"] = alias
    assert safe_host_setup(config) is HOST_SETUP_UNAVAILABLE
    assert HOST_SETUP_UNAVAILABLE is not None
    assert vars(HOST_SETUP_UNAVAILABLE) == {}
    with pytest.raises(ValueError, match="projection is unavailable"):
        setup_state(Runtime(), HOST_SETUP_UNAVAILABLE)
    embedded = setup_state(Runtime(), None)
    assert embedded.roles.decisions.configured is False
    assert embedded.reasoning.selected == "unavailable"


def test_safe_projection_preserves_valid_declarations():
    config = configuration()
    assert safe_host_setup(config) == host_setup(config)


@pytest.mark.parametrize("error", [RuntimeError, OSError, TypeError])
def test_safe_projection_does_not_swallow_unrelated_errors(monkeypatch, error):
    from services.converse import setup as setup_module

    def unexpected_failure(_config):
        raise error("Synthetic non-validation failure")

    monkeypatch.setattr(setup_module, "host_setup", unexpected_failure)
    with pytest.raises(error, match="non-validation failure"):
        safe_host_setup(configuration())


async def test_opaque_decisions_alias_cannot_prevent_converse_startup_or_owner_controls(
    tmp_path, monkeypatch
):
    import uvicorn

    from services.converse import runtime as runtime_module

    alias = "/synthetic/local/model"
    config = {
        "session_url": "http://127.0.0.1:6",
        "providers": {
            "converse": {
                "kind": "lmstudio",
                "base_url": "http://127.0.0.1:1234",
                "model": "synthetic-conversation",
                "reasoning_effort": "none",
            },
            "decisions": {
                "kind": "compatible",
                "base_url": "http://127.0.0.1:6/v1",
                "model": alias,
            },
        },
        "streaming_windows": {"enabled": False},
    }
    calls = []

    async def unexpected_network(_self, request):
        calls.append(request.url)
        raise AssertionError("Setup inspection and owner controls must not make model calls")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", unexpected_network)
    selected = launcher.provider(config, "decisions")
    assert selected.info.model == alias and selected.info.local
    await selected.http.aclose()

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setattr(launcher, "load_or_create_tokens", lambda _path: TOKENS)
    runtimes, apps = [], []
    constructor = runtime_module.ConverseRuntime

    def capture_runtime(*args, **kwargs):
        runtime = constructor(*args, **kwargs)
        runtimes.append(runtime)
        return runtime

    def capture_app(app, **kwargs):
        assert kwargs["host"] == "127.0.0.1"
        apps.append(app)

    monkeypatch.setattr(runtime_module, "ConverseRuntime", capture_runtime)
    monkeypatch.setattr(uvicorn, "run", capture_app)
    try:
        launcher.serve(
            SimpleNamespace(
                config=config_path,
                data_dir=tmp_path / "data",
                workspace=tmp_path / "workspace",
                service="converse",
                port=None,
            )
        )
        assert len(apps) == 1 and len(runtimes) == 1
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps[0]), base_url="http://fixture"
        ) as client:
            setup = await client.get("/v1/converse/setup", headers=owner_headers())
            assert setup.status_code == 503
            assert setup.headers["cache-control"] == "no-store"
            assert setup.json() == {"detail": "Converse setup declarations are unavailable"}
            assert alias not in setup.text
            health = await client.get("/health")
            assert health.status_code == 200 and health.json()["enabled"] is True
            assert health.json()["provider"]["model"] == "synthetic-conversation"
            assert health.json()["context_history"] == "measured"
            for enabled, revision in ((False, 1), (True, 2)):
                control = await client.post(
                    "/v1/converse/enabled",
                    json={"enabled": enabled},
                    headers=owner_headers(),
                )
                assert control.status_code == 200
                assert control.json()["enabled"] is enabled
                assert control.json()["owner_intent_revision"] == revision
                assert (await client.get("/health")).json()["enabled"] is enabled
            assert (await client.get("/v1/converse/setup")).status_code == 401
            unavailable = await client.get("/v1/converse/setup", headers=owner_headers())
            assert unavailable.status_code == 503 and alias not in unavailable.text
        assert calls == []
    finally:
        for runtime in runtimes:
            await runtime.close()
            await runtime.journal.close()
            await runtime.provider.close()


async def test_only_owner_with_permitted_origin_can_inspect_setup(owner_setup):
    runtime, _, client = owner_setup
    assert (await client.get("/v1/converse/setup")).status_code == 401
    assert (
        await client.get(
            "/v1/converse/setup", headers={"Authorization": "Bearer " + TOKENS["resources"]}
        )
    ).status_code == 403
    assert (
        await client.get(
            "/v1/converse/setup", headers={"Authorization": "Bearer " + TOKENS["converse"]}
        )
    ).status_code == 403
    assert (
        await client.get(
            "/v1/converse/setup", headers=owner_headers(Origin="https://untrusted.invalid")
        )
    ).status_code == 403
    assert runtime.text["active"] == 0 and runtime.speech["active"] == 0


async def test_setup_projects_roles_without_model_calls_or_secret_configuration(owner_setup):
    _, _, client = owner_setup
    response = await client.get("/v1/converse/setup", headers=owner_headers())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    value = response.json()
    assert value["contract_version"] == 1
    assert len(value["revision"]) == 64
    assert value["roles"]["conversation"] == {
        "selected_id": "lmstudio:synthetic-conversation",
        "label": "synthetic-conversation",
        "configured": True,
        "source": "runtime",
        "options": [{"id": "lmstudio:synthetic-conversation", "label": "synthetic-conversation"}],
        "mutable": False,
        "reason": "Configured on host. Changing this model requires a service restart.",
    }
    expected = {
        "stt": ("whisper:tiny.en", "Whisper tiny.en"),
        "tts": ("tts:Kokoro 82M", "Kokoro 82M"),
        "decisions": ("laya:synthetic-routing", "synthetic-routing"),
    }
    for role, (identity, label) in expected.items():
        declaration = value["roles"][role]
        assert declaration["selected_id"] == identity and declaration["label"] == label
        assert declaration["configured"] is True and declaration["mutable"] is False
        assert declaration["source"] == "host_configuration"
        assert declaration["options"] == [{"id": identity, "label": label}]
        assert "loaded state is not checked" in declaration["reason"]
    assert value["inputs"] == {
        "supported": ["text"],
        "selected": ["text"],
        "direct_audio": False,
        "stt_required": True,
    }
    assert value["reasoning"]["selected"] == "none"
    assert value["reasoning"]["options"] == ["none"]
    assert value["reasoning"]["mutable"] is False
    for private in (
        "http://",
        "8935",
        "synthetic-provider-secret",
        "SYNTHETIC_CREDENTIAL_REFERENCE",
        "synthetic-private-models",
        "synthetic-private-environment",
        "synthetic-private-data",
        "voice_device",
        "api_key",
        "local",
        "ready",
    ):
        assert private not in response.text


async def test_endpoint_has_no_model_mutation_or_post_fallback(owner_setup):
    runtime, _, client = owner_setup
    before = runtime.provider
    response = await client.post(
        "/v1/converse/setup", json={"conversation": "different"}, headers=owner_headers()
    )
    assert response.status_code == 405
    assert runtime.provider is before


def test_embedded_runtime_does_not_invent_other_services_or_reasoning():
    value = setup_state(Runtime()).model_dump()
    for role in ("stt", "tts", "decisions"):
        assert value["roles"][role] == {
            "selected_id": None,
            "label": None,
            "configured": False,
            "source": "host_configuration",
            "options": [],
            "mutable": False,
            "reason": "No model is declared by this host configuration.",
        }
    assert value["reasoning"]["selected"] == "unavailable"
    assert value["reasoning"]["options"] == ["unavailable"]


@pytest.mark.parametrize("voice", [None, {}])
def test_unconfigured_voice_advertises_no_tts_or_stt(voice):
    config = configuration()
    config["voice"] = voice
    value = setup_state(Runtime(), host_setup(config)).model_dump()
    assert not value["roles"]["stt"]["configured"]
    assert not value["roles"]["tts"]["configured"]


def test_unmanaged_voice_does_not_invent_stt_model():
    config = configuration()
    del config["resources"]
    value = setup_state(Runtime(), host_setup(config)).model_dump()
    assert value["roles"]["stt"]["selected_id"] is None
    assert value["roles"]["tts"]["selected_id"] == "tts:Kokoro 82M"


def test_whistle_requires_its_explicit_managed_declaration():
    config = configuration()
    config["voice"]["stt"] = {"provider": "whistle", "url": "http://127.0.0.1:8934"}
    assert host_setup(config).stt.configured is False
    config["resources"]["whistle"] = {"weights": "/synthetic/whistle.cact"}
    assert host_setup(config).stt.selected_id == "whistle:Whistle"
    config["resources"]["enabled"] = False
    assert host_setup(config).stt.configured is False


@pytest.mark.parametrize(
    ("kind", "effort", "expected"),
    [
        ("lmstudio", "none", "none"),
        ("compatible", None, "auto"),
        ("compatible", "high", "high"),
        ("meta", None, "unavailable"),
        ("meta", "low", "low"),
        ("compatible", "future_level", "future_level"),
    ],
)
def test_reasoning_reports_only_the_actual_declared_setting(kind, effort, expected):
    config = configuration()
    config["providers"]["converse"]["kind"] = kind
    config["providers"]["converse"]["reasoning_effort"] = effort
    reasoning = setup_state(Runtime(), host_setup(config)).reasoning
    assert reasoning.selected == expected and reasoning.options == [expected]
    assert reasoning.mutable is False


@pytest.mark.parametrize(
    ("kind", "expected"), [("lmstudio", "none"), ("compatible", "auto"), ("meta", "xhigh")]
)
def test_reasoning_defaults_match_provider_construction(kind, expected):
    config = configuration()
    conversation = config["providers"]["converse"]
    conversation["kind"] = kind
    del conversation["reasoning_effort"]
    assert setup_state(Runtime(), host_setup(config)).reasoning.selected == expected


def test_revision_binds_visible_declarations_not_counts_secrets_or_mutable_config():
    runtime, config = Runtime(), configuration()
    host = host_setup(config)
    first = setup_state(runtime, host)
    runtime.text.update(active=1, admitting=1)
    runtime.speech.update(active=1)
    config["providers"]["decisions"]["model"] = "synthetic-new-routing"
    config["providers"]["converse"]["api_key"] = "another-synthetic-secret"
    busy = setup_state(runtime, host)
    assert busy.revision == first.revision
    assert busy.operation_state.text.active == 1 and busy.operation_state.speech.active == 1
    assert setup_state(runtime, host_setup(config)).revision != first.revision
    config["providers"]["decisions"]["model"] = "synthetic-routing"
    assert setup_state(runtime, host_setup(config)).revision == first.revision
    runtime.provider.info.model = "synthetic-replacement-conversation"
    assert setup_state(runtime, host).revision != first.revision


@pytest.mark.parametrize(
    "model",
    [
        "/private/model.gguf",
        "http://private/model",
        "C:\\private\\model",
        "bad\nname",
        "bad\u202ename",
        "\ud800",
        "x" * 129,
    ],
)
async def test_invalid_runtime_display_identity_is_sanitized_not_exposed(model):
    runtime = Runtime()
    runtime.provider.info.model = model
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, AUTH)), base_url="http://fixture"
    ) as client:
        response = await client.get("/v1/converse/setup", headers=owner_headers())
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Converse setup declarations are unavailable"}


def test_exported_setup_schema_matches_model_and_real_response():
    path = Path(__file__).resolve().parents[2] / "packages/contracts/schemas/ConverseSetup.json"
    schema = json.loads(path.read_text())
    assert schema == ConverseSetup.model_json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    value = setup_state(Runtime(), host_setup(configuration())).model_dump()
    jsonschema.validate(value, schema)
    for change in (
        {"secret": "must-not-appear"},
        {"roles": {**value["roles"], "agent": value["roles"]["conversation"]}},
        {"inputs": {**value["inputs"], "direct_audio": True}},
        {"reasoning": {**value["reasoning"], "options": ["none", "high"]}},
    ):
        invalid = {**copy.deepcopy(value), **change}
        with pytest.raises((ValidationError, jsonschema.ValidationError)):
            ConverseSetup.model_validate(invalid)
            jsonschema.validate(invalid, schema)
