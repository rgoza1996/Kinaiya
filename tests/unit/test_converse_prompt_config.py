"""Owner instructions remain editable and independent of execution authority."""

from types import SimpleNamespace

import pytest

from services import launcher
from services.converse.prompts import configured_prompt
from services.converse.runtime import ConverseRuntime


def test_default_and_explicit_blank_or_custom_instructions_are_distinct():
    assert configured_prompt({}) == configured_prompt({"converse_prompt": None}) == {}
    default = ConverseRuntime(None, None)
    assert "await Agent’s" in default.personality or "await Agent's" in default.personality
    assert "keep listening, accept corrections" in default.personality
    for text in ("", "Answer my questions yourself in the character I choose."):
        options = configured_prompt({"converse_prompt": {"contract_version": 1, "text": text}})
        runtime = ConverseRuntime(None, None, **options)
        assert runtime.personality == text
        assert runtime.personality_scope == "local_only"
    options = configured_prompt(
        {
            "converse_prompt": {
                "text": "Public owner persona.",
                "content_scope": "public_synthetic",
            }
        }
    )
    assert ConverseRuntime(None, None, **options).personality_scope == "public_synthetic"


@pytest.mark.parametrize(
    "value",
    [
        {"text": "PRIVATE_CONFIG_FIXTURE", "contract_version": True},
        {"text": "PRIVATE_CONFIG_FIXTURE", "contract_version": 2},
        {"text": "PRIVATE_CONFIG_FIXTURE", "content_scope": "public"},
        {"text": "PRIVATE_CONFIG_FIXTURE", "unknown": True},
        {"text": "\x00PRIVATE_CONFIG_FIXTURE"},
        {"text": "é" * 32769},
        {"text": 3},
        [],
        "bad",
    ],
)
def test_invalid_owner_prompt_is_refused_without_printing_its_contents(value):
    with pytest.raises(ValueError) as caught:
        configured_prompt({"converse_prompt": value})
    assert str(caught.value) == "Invalid owner-configured Converse prompt"


class StopConstruction(Exception):
    pass


def host(monkeypatch, tmp_path, prompt):
    config = {
        "session_url": "http://127.0.0.1:8760",
        "agent_url": "http://127.0.0.1:8761",
        "converse_prompt": prompt,
    }
    tokens = {role: role[0] * 32 for role in ("owner", "converse")}
    monkeypatch.setattr(launcher, "load_config", lambda _: config)
    monkeypatch.setattr(launcher, "load_or_create_tokens", lambda _: tokens)
    return (
        SimpleNamespace(
            config=tmp_path / "config.json",
            data_dir=tmp_path / "data",
            workspace=tmp_path / "workspace",
            service="converse",
            port=None,
        ),
        config,
        tokens,
    )


def test_invalid_host_override_stops_before_provider_or_catalog_construction(monkeypatch, tmp_path):
    from services.settings.provider_catalog import ProviderCatalog

    args, _, _ = host(monkeypatch, tmp_path, {"text": "Private fixture", "contract_version": 2})

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid prompt must fail before constructing a provider/catalog")

    monkeypatch.setattr(launcher, "provider", forbidden)
    monkeypatch.setattr(ProviderCatalog, "__init__", forbidden)
    with pytest.raises(ValueError, match="Invalid owner-configured Converse prompt"):
        launcher.serve(args)


@pytest.mark.parametrize("text", ["", "Public custom host instructions."])
def test_actual_launcher_passes_owner_override_and_scoped_metadata_client(
    monkeypatch, tmp_path, text
):
    import packages.agent_adapters.task_context
    import packages.contracts.client
    import services.converse.runtime

    args, config, tokens = host(
        monkeypatch,
        tmp_path,
        {
            "contract_version": 1,
            "text": text,
            "content_scope": "public_synthetic",
        },
    )
    captured = {}
    marker = object()

    def client(url, token):
        captured["client"] = url, token
        return marker

    def runtime(journal, provider, **options):
        captured["options"] = options
        raise StopConstruction()

    monkeypatch.setattr(packages.contracts.client, "JournalClient", lambda *a: object())
    monkeypatch.setattr(packages.agent_adapters.task_context, "TaskContextClient", client)
    monkeypatch.setattr(launcher, "provider", lambda *a, **kw: object())
    monkeypatch.setattr(services.converse.runtime, "ConverseRuntime", runtime)
    with pytest.raises(StopConstruction):
        launcher.serve(args)
    assert captured["client"] == (config["agent_url"], tokens["converse"])
    assert captured["options"]["personality"] == text
    assert captured["options"]["personality_scope"] == "public_synthetic"
    assert captured["options"]["task_context_client"] is marker
