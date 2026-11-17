"""#566/#567: consumer ownership and metadata inspection, with no model calls."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from packages.contracts.decisions_consumers import DecisionsAgentPolicy, DecisionsAgentPolicyState
from services.common.auth import Auth
from services.decisions.agent_policy import AgentPolicyOwner, configured_agent_policy, router
from services.decisions.prompts import DEFAULT_PROMPT, configured_prompt
from services.settings.provider_catalog import ProviderCatalog, ProviderChange

TOKENS = {role: role[0] * 40 for role in ("owner", "agent", "converse", "decisions", "resources")}
BINDING = {"id": "advisor", "generation": 1, "configuration_sha256": "a" * 64}


def policy(**changes):
    return {"catalog_binding": BINDING.copy(), "instructions": "Synthetic advice Ω\n", **changes}


def host(**changes):
    return {"decisions_agent_policy": policy(**changes)}


@pytest.fixture
def catalog(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    value = ProviderCatalog(tmp_path / "protected" / "providers.sqlite", workspace)
    try:
        yield value
    finally:
        value.close()


def change(catalog, operation, operation_key, **fields):
    return catalog.change(
        ProviderChange.model_validate(
            {
                "schema_version": "1.0",
                "expected_revision": catalog.snapshot()["revision"],
                "idempotency_key": operation_key,
                "operation": operation,
                **fields,
            }
        )
    )["result"]


def save(catalog, kind="lmstudio", **fields):
    profile = change(
        catalog,
        "profile_upsert",
        f"profile-{catalog.snapshot()['revision']}",
        profile_id="advisor",
        label="Synthetic advisor",
        config={
            "kind": kind,
            "base_url": "http://127.0.0.1:1234/v1",
            "model": "synthetic",
            **fields,
        },
    )
    return {name: profile[name] for name in BINDING}


@pytest.mark.parametrize("raw", [None, {}])
def test_absence_never_inherits_another_consumers_policy(raw):
    config = {
        "providers": {"agent": {"model": "work"}, "decisions": {"model": "classifier"}},
        "decisions_prompt": {"finalized": "Converse only", "window": "Converse window"},
    }
    if raw is None:
        config["decisions_agent_policy"] = None
    before = copy.deepcopy(config)
    assert configured_agent_policy(config) is None
    state = AgentPolicyOwner(config, None).snapshot()
    assert state.source == "unconfigured" and state.policy is None
    assert state.catalog_status == "not_selected" and state.request_admission == "unavailable"
    assert config == before


@pytest.mark.parametrize("text", ["", "  Unfiltered synthetic instruction Ω\n", "é" * 4096])
def test_exact_instructions_and_model_binding_survive_host_snapshot(text):
    config = host(instructions=text)
    owner = AgentPolicyOwner(config, None)
    expected = DecisionsAgentPolicy.model_validate(config["decisions_agent_policy"])
    config["decisions_agent_policy"]["instructions"] = "Later host edit"
    config["decisions_agent_policy"]["catalog_binding"]["id"] = "replacement"
    state = owner.snapshot()
    assert state.policy == expected and state.policy.instructions == text
    assert state.policy.catalog_binding.id == "advisor"
    encoded = json.dumps(
        expected.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert state.policy_sha256 == hashlib.sha256(encoded).hexdigest()
    assert state.source == "host" and state.catalog_status == "unavailable"
    assert state.change_boundary == "restart_required"
    with pytest.raises(ValidationError):
        state.policy.instructions = "mutation"
    with pytest.raises(ValidationError):
        state.policy.catalog_binding.generation = 2


@pytest.mark.parametrize(
    "update",
    [
        {"contract_version": True},
        {"contract_version": 2},
        {"consumer": "converse"},
        {"protocol": "native_decisions_v1"},
        {"reasoning": "unsupported"},
        {"instructions": None},
        {"instructions": "é" * 4097},
        {"instructions": "\ud800"},
        {"instructions": "\x00SYNTHETIC_PRIVATE_DETAIL"},
        {"content_scope": "public"},
        {"catalog_binding": {**BINDING, "generation": True}},
        {"credential": "SYNTHETIC_PRIVATE_DETAIL"},
        {"finalized": "SYNTHETIC_PRIVATE_DETAIL"},
    ],
)
def test_invalid_optional_policy_is_redacted_and_cannot_modify_converse(update):
    config = host(**update)
    with pytest.raises(ValueError) as caught:
        configured_agent_policy(config)
    assert str(caught.value) == "Invalid owner-configured Agent decision policy"
    state = AgentPolicyOwner(config, None).snapshot()
    assert state.source == "invalid_host" and state.policy is None and state.policy_sha256 is None
    assert state.catalog_status == "not_selected" and state.request_admission == "unavailable"
    assert "SYNTHETIC_PRIVATE_DETAIL" not in state.model_dump_json()
    assert configured_prompt(config) is DEFAULT_PROMPT


def test_native_catalog_inspection_never_constructs_adapters_or_resolves_keys(catalog, monkeypatch):
    binding = save(catalog)

    def forbidden(*args, **kwargs):
        pytest.fail("Consumer inspection attempted a credential or provider operation")

    monkeypatch.setattr(catalog, "credential", forbidden)
    monkeypatch.setattr(catalog.vault, "resolve", forbidden)
    monkeypatch.setattr("packages.providers.lmstudio.LMStudioProvider", forbidden)
    before = catalog.snapshot()
    owner = AgentPolicyOwner(host(catalog_binding=binding), catalog)
    for _ in range(3):
        state = owner.snapshot()
        assert state.catalog_status == "eligible"
        assert state.observation == "catalog_metadata_only"
        assert state.request_admission == "unavailable"
    assert catalog.snapshot() == before


@pytest.mark.parametrize("kind", ["compatible", "measured_chat", "laya"])
def test_converse_or_unmeasured_adapters_cannot_become_agent_advisors(catalog, kind):
    binding = save(catalog, kind, **({"context_tokens": 4096} if kind == "measured_chat" else {}))
    assert AgentPolicyOwner(host(catalog_binding=binding), catalog).snapshot().catalog_status == (
        "unavailable"
    )


def test_reasoning_and_exact_generation_are_independent_of_converse(catalog):
    binding = save(catalog)
    owner = AgentPolicyOwner(host(catalog_binding=binding, reasoning="none"), catalog)
    original = owner.snapshot()
    assert original.catalog_status == "eligible"
    assert (
        AgentPolicyOwner(host(catalog_binding=binding, reasoning="high"), catalog)
        .snapshot()
        .catalog_status
        == "unavailable"
    )
    replacement = save(catalog, model="synthetic-replacement")
    assert replacement["generation"] == 2
    observed = owner.snapshot()
    assert observed.policy == original.policy and observed.policy_sha256 == original.policy_sha256
    assert observed.catalog_status == "unavailable"
    fresh = AgentPolicyOwner(host(catalog_binding=replacement), catalog).snapshot()
    assert fresh.catalog_status == "eligible"


@pytest.mark.parametrize(
    "scope,expected",
    [
        ("local_only", "unavailable"),
        ("private_cloud_authorized", "unavailable"),
        ("public_synthetic", "eligible"),
    ],
)
@pytest.mark.parametrize("model,effort", [("Contributor", "xhigh"), ("muse-wonder-1.0", "max")])
def test_meta_instruction_disclosure_ceiling_is_metadata_only(
    catalog, monkeypatch, scope, expected, model, effort
):
    credential = change(
        catalog,
        "credential_import",
        "synthetic-credential",
        credential_id="synthetic-key",
        key="public-synthetic-no-real-credential-0123456789",
    )
    binding = save(
        catalog,
        "meta",
        base_url="https://api.meta.ai/v1",
        model=model,
        allow_cloud=True,
        credential={"id": credential["id"], "generation": credential["generation"]},
        context_tokens=32768,
        reasoning_effort=effort,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Metadata observation resolved a credential")

    monkeypatch.setattr(catalog, "credential", forbidden)
    owner = AgentPolicyOwner(
        host(catalog_binding=binding, content_scope=scope, reasoning=effort), catalog
    )
    assert owner.snapshot().catalog_status == expected


@pytest.mark.parametrize(
    "role", [None, "wrong", "agent", "converse", "decisions", "resources", "owner"]
)
def test_only_owner_can_inspect_policy_without_request_admission(role):
    app = FastAPI()
    app.include_router(router(AgentPolicyOwner(host(), None), Auth(TOKENS)))
    headers = {} if role is None else {"Authorization": "Bearer " + TOKENS.get(role, "x" * 40)}
    with TestClient(app) as client:
        response = client.get("/v1/decisions/options/agent-policy", headers=headers)
        expected = 200 if role == "owner" else 401 if role in {None, "wrong"} else 403
        assert response.status_code == expected
        if expected == 200:
            assert response.headers["cache-control"] == "no-store"
            result = DecisionsAgentPolicyState.model_validate(response.json())
            assert result.request_admission == "unavailable"
            assert (
                client.post(
                    "/v1/decisions/options/agent-policy", headers=headers, json=host()
                ).status_code
                == 405
            )


@pytest.mark.parametrize("model", [DecisionsAgentPolicy, DecisionsAgentPolicyState])
def test_exported_contract_matches_native_closed_version_one(model):
    path = (
        Path(__file__).resolve().parents[2]
        / "packages/contracts/schemas"
        / f"{model.__name__}.json"
    )
    assert json.loads(path.read_bytes()) == model.model_json_schema()
    assert model.model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize(
    "update",
    [
        {"contract_version": True},
        {"source": "host"},
        {"policy_sha256": "a" * 64},
        {"catalog_status": "eligible"},
        {"request_admission": "ready"},
        {"observation": "model_ready"},
    ],
)
def test_state_cannot_claim_model_readiness_or_inconsistent_provenance(update):
    baseline = AgentPolicyOwner({}, None).snapshot().model_dump()
    with pytest.raises(ValidationError):
        DecisionsAgentPolicyState.model_validate(baseline | update)


def test_policy_observation_rejects_a_digest_from_different_instructions():
    first = AgentPolicyOwner(host(), None).snapshot().model_dump()
    other = AgentPolicyOwner(host(instructions="Different synthetic preference"), None).snapshot()
    with pytest.raises(ValidationError):
        DecisionsAgentPolicyState.model_validate(first | {"policy_sha256": other.policy_sha256})
