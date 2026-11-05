"""Exact Wonder Standard declarations; synthetic adapters, no network inference."""

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from packages.contracts.agent_media import (
    MediaPrimaryResultTarget,
    MediaProviderResult,
    MediaTarget,
)
from packages.contracts.models import ProviderInfo
from packages.providers.meta_policy import (
    LEGACY_META_EFFORTS,
    WONDER_STANDARD_EFFORTS,
    WONDER_STANDARD_MODEL,
    meta_reasoning_options,
)
from packages.providers.text import MetaResponsesProvider, ProviderError, ScriptedProvider
from services.agent.model_settings import AgentModelSettings
from services.settings.provider_catalog import MetaProfile

WONDER = "muse-wonder-1.0"
CONTRIBUTOR = "muse-spark-1.3-contributor"
OTHER_MODELS = (
    CONTRIBUTOR,
    "muse-wonder-1.0-full",
    "muse-wonder-1.0-contributor",
    "Muse Wonder Full",
    "muse-wonder-1.0 ",
)
MESSAGES = [
    {"role": "system", "content": "Public synthetic fixture."},
    {"role": "user", "content": "A synthetic question."},
]


def profile(model=WONDER, effort="max"):
    return {
        "kind": "meta",
        "base_url": "https://synthetic.invalid/v1",
        "model": model,
        "allow_cloud": True,
        "credential": {"id": "synthetic", "generation": "a" * 32},
        "max_tokens": 128,
        "context_tokens": 8192,
        "reasoning_effort": effort,
    }


def adapter(model=WONDER, effort="max", **kwargs):
    return MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="SYNTHETIC_POLICY_CREDENTIAL",
        allow_cloud=True,
        model=model,
        reasoning_effort=effort,
        max_tokens=128,
        context_tokens=8192,
        transport=kwargs.pop(
            "transport",
            httpx.MockTransport(lambda _: pytest.fail("Policy inspection must not dispatch")),
        ),
        **kwargs,
    )


def declared(model, effort="max", provider="meta"):
    value = ScriptedProvider([])
    value.info = ProviderInfo(provider=provider, model=model, local=False)
    value.reasoning_effort = effort
    return value


def test_only_exact_standard_identity_gains_max():
    assert WONDER_STANDARD_MODEL == WONDER
    assert meta_reasoning_options(WONDER) == WONDER_STANDARD_EFFORTS
    assert WONDER_STANDARD_EFFORTS == ("minimal", "low", "medium", "high", "xhigh", "max")
    for model in OTHER_MODELS:
        assert meta_reasoning_options(model) == LEGACY_META_EFFORTS
        assert "max" not in meta_reasoning_options(model)


def test_media_contract_accepts_exact_standard_max_and_primary_export_target():
    target = MediaTarget(
        operation="transcribe_video",
        model=WONDER,
        reasoning="max",
        prompt="Describe only the explicitly authorized synthetic clip.",
    )
    assert target.model == WONDER and target.reasoning == "max"
    assert MediaPrimaryResultTarget(model=WONDER).model == WONDER
    observed = MediaProviderResult(
        model=WONDER,
        provider_request_sha256="a" * 64,
        provider_response_sha256="b" * 64,
        text="Synthetic result.",
        data={},
    )
    assert observed.model == WONDER


@pytest.mark.parametrize(
    "model,effort",
    [
        (WONDER, "xhigh"),
        (WONDER, None),
        (CONTRIBUTOR, "max"),
        ("muse-wonder-1.0-full", "max"),
        ("muse-wonder-1.0-contributor", "max"),
    ],
)
def test_media_target_refuses_wrong_standard_or_contributor_effort(model, effort):
    with pytest.raises(ValidationError):
        MediaTarget(
            operation="transcribe_video",
            model=model,
            reasoning=effort,
            prompt="Synthetic trusted owner prompt.",
        )


@pytest.mark.parametrize("model", OTHER_MODELS[1:])
def test_full_or_alias_cannot_become_media_primary_target(model):
    with pytest.raises(ValidationError):
        MediaPrimaryResultTarget(model=model)


def test_generic_meta_profile_has_model_specific_effort_boundary():
    assert MetaProfile.model_validate(profile()).reasoning_effort == "max"
    assert MetaProfile.model_validate(profile(CONTRIBUTOR, "xhigh")).reasoning_effort == "xhigh"
    with pytest.raises(ValidationError):
        MetaProfile.model_validate(profile(WONDER, "none"))


@pytest.mark.parametrize("model", OTHER_MODELS)
def test_generic_profile_and_direct_adapter_refuse_nonstandard_max(model):
    with pytest.raises(ValidationError):
        MetaProfile.model_validate(profile(model))
    with pytest.raises(ProviderError):
        adapter(model)


@pytest.mark.parametrize("effort", [None, "none", "MAX", "unknown"])
def test_direct_standard_adapter_refuses_unsupported_effort(effort):
    with pytest.raises(ProviderError):
        adapter(effort=effort)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "structured,wire", [(False, "flat-v1"), (True, "flat-v1"), (True, "typed-v2")]
)
async def test_standard_headers_and_stateless_body_preserve_structured_wire(structured, wire):
    standard = adapter(structured_actions=structured, action_format=wire)
    contributor = adapter(CONTRIBUTOR, "xhigh")
    try:
        assert "x-tbh-contributor-mode" not in standard.http.headers
        assert contributor.http.headers["x-tbh-contributor-mode"] == "true"
        for stream in (False, True):
            body = standard.body(MESSAGES, stream)
            assert body["model"] == WONDER and body["reasoning"] == {"effort": "max"}
            assert body["store"] is False and body["stream"] is stream
            assert "previous_response_id" not in body and "conversation" not in body
            assert ("text" in body) is structured
        assert contributor.body(MESSAGES, False)["reasoning"] == {"effort": "xhigh"}
    finally:
        await standard.close()
        await contributor.close()


class _Count(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = 0

    async def __aiter__(self):
        yield b'{"object":"response.input_tokens","input_tokens":123}'

    async def aclose(self):
        self.closed += 1


@pytest.mark.asyncio
async def test_measured_agent_complete_count_uses_standard_headers_and_exact_buffered_body():
    calls, stream = [], _Count()

    def respond(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    value = adapter(transport=httpx.MockTransport(respond))
    try:
        measured = await value.prepare_complete(MESSAGES)
        assert len(calls) == 1 and calls[0].url.path == "/v1/responses/input_tokens"
        assert "x-tbh-contributor-mode" not in calls[0].headers
        assert json.loads(calls[0].content) == value.body(MESSAGES, False)
        assert json.loads(calls[0].content)["store"] is False
        assert measured.measurement.input_tokens == 123
        measured.discard()
        assert stream.closed == 1
    finally:
        await value.close()


@pytest.mark.parametrize(
    "model,provider,expected",
    [
        (WONDER, "meta", "max"),
        (CONTRIBUTOR, "meta", None),
        ("muse-wonder-1.0-full", "meta", None),
        (WONDER, "synthetic", None),
    ],
)
def test_native_effort_projection_limits_max_to_exact_meta_standard(model, provider, expected):
    value = declared(model, provider=provider)
    assert AgentModelSettings.effort(value) == expected
    assert AgentModelSettings.effort(SimpleNamespace(provider=value)) == expected


@pytest.mark.asyncio
async def test_model_settings_export_max_only_for_declared_standard_profile(tmp_path):
    values = []

    def factory(model):
        def create(effort):
            value = declared(model, effort or ("max" if model == WONDER else "xhigh"))
            values.append(value)
            return value

        return create

    settings = AgentModelSettings(
        ScriptedProvider([]),
        {"wonder": factory(WONDER), "spark": factory(CONTRIBUTOR)},
        path=tmp_path / "models.sqlite",
        reasoning_levels={
            "wonder": list(WONDER_STANDARD_EFFORTS),
            "spark": list(LEGACY_META_EFFORTS),
        },
    )
    try:
        profiles = {row["id"]: row for row in settings.snapshot()["profiles"]}
        assert profiles["wonder"]["reasoning_options"] == list(WONDER_STANDARD_EFFORTS)
        assert profiles["wonder"]["reasoning"] == "max"
        assert "max" not in profiles["spark"]["reasoning_options"]
        assert not any(value.calls for value in values)
    finally:
        await settings.close()


@pytest.mark.asyncio
async def test_unavailable_optional_max_declaration_preserves_working_control_plane(tmp_path):
    def unavailable(_effort):
        raise ProviderError("SYNTHETIC_UNAVAILABLE_OPTIONAL_PROFILE")

    host = ScriptedProvider([])
    settings = AgentModelSettings(
        host,
        {"optional-wonder": unavailable},
        path=tmp_path / "models.sqlite",
        reasoning_levels={"optional-wonder": ["max"]},
    )
    try:
        profiles = {row["id"]: row for row in settings.snapshot()["profiles"]}
        assert profiles["host-default"]["selectable"] is True
        assert profiles["optional-wonder"]["selectable"] is False
        assert profiles["optional-wonder"]["provider"] is None
        assert profiles["optional-wonder"]["reasoning_options"] == ["max"]
        assert host.calls == []
    finally:
        await settings.close()


@pytest.mark.parametrize("model", OTHER_MODELS)
def test_host_reasoning_declarations_cannot_add_max_to_other_models(model):
    with pytest.raises(ValueError, match="reasoning options"):
        AgentModelSettings(
            ScriptedProvider([]),
            {"other": lambda _: declared(model)},
            reasoning_levels={"other": ["max"]},
        )


@pytest.mark.asyncio
async def test_catalog_reasoning_projection_is_exact_model_specific(tmp_path, monkeypatch):
    settings = AgentModelSettings(ScriptedProvider([]), {}, path=tmp_path / "models.sqlite")
    rows = [
        {
            "id": name,
            "generation": 1,
            "configuration_sha256": "a" * 64,
            "label": name,
            "removed": False,
            "credential_available": True,
            "config": profile(model, "max" if model == WONDER else "xhigh"),
        }
        for name, model in (
            ("wonder", WONDER),
            ("spark", CONTRIBUTOR),
            ("full-name", "muse-wonder-1.0-full"),
        )
    ]
    settings.catalog = SimpleNamespace(snapshot=lambda: {"profiles": rows})
    monkeypatch.setattr("services.agent.model_settings.inspect_recipe", lambda *_: object())
    try:
        projected = {row["id"]: row for row in settings._catalog_profiles()}
        assert projected["wonder"]["reasoning_options"] == list(WONDER_STANDARD_EFFORTS)
        assert all(
            "max" not in projected[name]["reasoning_options"] for name in ("spark", "full-name")
        )
    finally:
        settings.catalog = None
        await settings.close()
