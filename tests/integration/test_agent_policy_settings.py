"""Real protected policy settings, Decisions ownership and native lookup."""

from contextlib import asynccontextmanager

import httpx
import pytest
from test_agent_advice_export import TOKENS
from test_agent_advice_operations import env as env
from test_agent_advice_operations import save

from packages.contracts.agent_advice import encoded
from packages.contracts.decisions_consumers import (
    MAX_POLICY_CHANGE_BYTES,
    DecisionsAgentPolicyChange,
    DecisionsAgentPolicyLookup,
    DecisionsAgentPolicyReceipt,
    DecisionsAgentPolicyRefusal,
    DecisionsAgentPolicySettingsSnapshot,
)
from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.decisions.agent_operations import AgentAdviceOperations
from services.decisions.agent_policy import AgentPolicyOwner
from services.decisions.agent_policy_settings import (
    POLICY_STORE_NAME,
    AgentPolicySettings,
    AgentPolicySettingsUnavailable,
    read_agent_policy_selection,
)
from services.decisions.api import create_app
from services.decisions.prompts import configured_prompt
from services.decisions.runtime import DecisionsWorker, ModelClassifier

SETTINGS = "/v1/decisions/options/agent-advice-settings"
LEGACY = "/v1/decisions/options/agent-policy"
RUNTIME = "/v1/decisions/options/agent-advice-runtime"


@asynccontextmanager
async def serving(env, path, config, *, lose_save_ack=False):
    storage = AgentPolicySettings(path, env.catalog.workspace)
    owner = AgentPolicyOwner(config, env.catalog, settings=storage)
    auth = Auth(dict(TOKENS))
    primary = ScriptedProvider([])
    worker = DecisionsWorker(
        env.owner.journal,
        object(),
        ModelClassifier(primary, prompt=configured_prompt(config)),
        enabled=False,
    )
    operations = AgentAdviceOperations(owner, env.owner.journal) if owner.configured else None
    app = create_app(worker, auth, agent_policy=owner, agent_operations=operations)

    async def observe_response(response):
        nonlocal lose_save_ack
        if (
            lose_save_ack
            and response.request.method == "POST"
            and response.request.url.path == SETTINGS
        ):
            lose_save_ack = False
            assert response.status_code == 200
            await response.aclose()
            raise httpx.ReadError("Synthetic lost save acknowledgement", request=response.request)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://decisions",
            headers={"Authorization": "Bearer " + TOKENS["owner"]},
            event_hooks={"response": [observe_response]},
        ) as client:
            yield client, auth, worker
    assert storage.closed
    assert primary.calls == [] and env.adapters == []


async def snapshot(client):
    response = await client.get(SETTINGS)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    return DecisionsAgentPolicySettingsSnapshot.model_validate(response.json())


def change(state, key, source, policy=None):
    return DecisionsAgentPolicyChange(
        expected_backend=state.backend_id,
        expected_instance=state.instance_id,
        expected_revision=state.revision,
        idempotency_key=key,
        selection={"source": source, "policy": policy},
    )


async def post(client, path, original, expected=200):
    response = await client.post(path, json=original.model_dump(mode="json"))
    assert response.status_code == expected
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def test_saved_policy_activates_on_restart_and_restores_exact_receipt(env, tmp_path):
    path = tmp_path / "control" / POLICY_STORE_NAME
    legacy = {"catalog_binding": env.binding, "instructions": "  Legacy synthetic advice Ω\n"}
    saved = {"catalog_binding": env.binding, "instructions": "  Saved synthetic advice Ω\n"}
    config = {
        "decisions_agent_policy": legacy,
        "decisions_prompt": {"window": "Independent window", "finalized": "Independent classifier"},
    }
    async with serving(env, path, config, lose_save_ack=True) as (client, _, worker):
        baseline = await snapshot(client)
        assert baseline.active == baseline.next_launch
        assert baseline.active.policy.instructions == legacy["instructions"]
        original_host = (await client.get(LEGACY)).json()
        runtime = (await client.get(RUNTIME)).json()
        assert runtime["new_request_available"]
        assert runtime["policy_sha256"] == baseline.active.policy_sha256
        original = change(baseline, "original-policy", "saved", saved)
        with pytest.raises(httpx.ReadError, match="Synthetic lost save acknowledgement"):
            await client.post(SETTINGS, json=original.model_dump(mode="json"))
        first_lookup = DecisionsAgentPolicyLookup.model_validate(
            await post(client, SETTINGS + "/lookup", original)
        )
        receipt = first_lookup.receipt
        assert type(receipt) is DecisionsAgentPolicyReceipt and not first_lookup.mutation_fenced
        assert receipt.request_sha256 == original.fingerprint()
        assert receipt.revision == 1
        assert await post(client, SETTINGS, original) == receipt.model_dump(mode="json")
        after = await snapshot(client)
        assert after.active == baseline.active and after.selection_source == "saved"
        assert after.next_launch.policy.instructions == saved["instructions"]
        assert read_agent_policy_selection(path, env.catalog.workspace).policy == (
            after.next_launch.policy
        )
        assert (await client.get(LEGACY)).json() == original_host
        assert (await client.get(RUNTIME)).json() == runtime
        assert worker.classifier.prompt == configured_prompt(config)
        await post(client, SETTINGS, change(baseline, "stale", "disabled"), 409)
        await post(client, SETTINGS, change(baseline, original.idempotency_key, "disabled"), 409)
        unobserved = change(after, "original-never-dispatched", "disabled")
        before_restart = DecisionsAgentPolicyLookup.model_validate(
            await post(client, SETTINGS + "/lookup", unobserved)
        )
        assert before_restart.receipt is None and not before_restart.mutation_fenced

    async with serving(env, path, config) as (client, _, worker):
        restarted = await snapshot(client)
        assert restarted.backend_id == baseline.backend_id
        assert restarted.instance_id != baseline.instance_id
        assert restarted.active == restarted.next_launch == after.next_launch
        assert (await client.get(RUNTIME)).json()["policy_sha256"] == receipt.policy_sha256
        assert (await client.get(LEGACY)).json() == original_host
        lookup = DecisionsAgentPolicyLookup.model_validate(
            await post(client, SETTINGS + "/lookup", original)
        )
        assert lookup.receipt == receipt and not lookup.mutation_fenced
        retired = DecisionsAgentPolicyLookup.model_validate(
            await post(client, SETTINGS + "/lookup", unobserved)
        )
        assert retired.receipt is None and retired.mutation_fenced
        await post(client, SETTINGS, unobserved, 409)
        assert worker.classifier.prompt == configured_prompt(config)
        await post(client, SETTINGS, change(restarted, "disable-advice", "disabled"))
        disabled = await snapshot(client)
        assert disabled.next_launch.source == "disabled"
        assert disabled.active == restarted.active

    async with serving(env, path, config) as (client, _, _):
        disabled = await snapshot(client)
        assert (
            disabled.active.source == "disabled" and (await client.get(RUNTIME)).status_code == 404
        )
        await post(client, SETTINGS, change(disabled, "reset-to-host", "host"))
        reset = await snapshot(client)
        assert reset.active == disabled.active and reset.next_launch == baseline.active
    async with serving(env, path, config) as (client, _, _):
        observed = await snapshot(client)
        assert observed.revision == 3 and observed.active == baseline.active
        assert (await client.get(LEGACY)).json() == original_host


async def test_write_checks_owner_after_ingress_and_preserves_store_on_denial(env, tmp_path):
    async with serving(env, tmp_path / "control" / POLICY_STORE_NAME, {}) as (client, auth, _):
        old = await snapshot(client)
        original = change(old, "never-saved", "disabled")
        for path, method in ((SETTINGS, "GET"), (SETTINGS, "POST"), (SETTINGS + "/lookup", "POST")):
            result = await client.request(
                method,
                path,
                json=original.model_dump(),
                headers={"Authorization": "Bearer " + TOKENS["agent"]},
            )
            assert result.status_code == 403
        for malformed, expected in (
            (b'{"selection":{},"selection":{}}', 422),
            (b" " * (MAX_POLICY_CHANGE_BYTES + 1), 413),
        ):
            response = await client.post(
                SETTINGS, content=malformed, headers={"Content-Type": "application/json"}
            )
            assert response.status_code == expected
        raw = encoded(original.model_dump())

        async def rotated():
            yield raw[:20]
            auth.tokens["owner"] = "replacement-public-synthetic-owner-token-000000"
            yield raw[20:]

        result = await client.post(
            SETTINGS, content=rotated(), headers={"Content-Type": "application/json"}
        )
        assert result.status_code == 401
        client.headers["Authorization"] = "Bearer " + auth.tokens["owner"]
        assert await snapshot(client) == old
        assert (await post(client, SETTINGS + "/lookup", original))["receipt"] is None


async def test_catalog_retirement_leaves_exact_saved_generation_visible(env, tmp_path):
    path = tmp_path / "control" / POLICY_STORE_NAME
    config = {
        "decisions_agent_policy": {"catalog_binding": env.binding, "instructions": "\x02" * 8192}
    }
    async with serving(env, path, config) as (client, _, _):
        selected = {"catalog_binding": env.binding, "instructions": "\x01" * 8192}
        before = await snapshot(client)
        await post(client, SETTINGS, change(before, "large-exact-policy", "saved", selected))
        expected = await snapshot(client)
        assert expected.next_launch.policy.instructions == selected["instructions"]
        assert expected.next_launch.catalog_status == "eligible"
    async with serving(env, path, config) as (client, _, _):
        active = await snapshot(client)
        assert active.active == active.next_launch == expected.next_launch
        assert save(env.catalog, model="replacement-advice")["generation"] == 2
        retired = await snapshot(client)
        assert retired.next_launch.policy == expected.next_launch.policy
        assert retired.active.policy_sha256 == expected.next_launch.policy_sha256
        assert retired.active.catalog_status == retired.next_launch.catalog_status == "unavailable"
        assert (await client.get(RUNTIME)).json()["new_request_available"] is False
        original = change(retired, "retired-catalog", "saved", selected)
        refusal = DecisionsAgentPolicyRefusal.model_validate(
            await post(client, SETTINGS, original, 400)
        )
        assert refusal.request_sha256 == original.fingerprint()
        assert refusal.reason == "selection_unavailable"
        assert await snapshot(client) == retired


def test_private_policy_authority_cannot_be_replaced_or_taken_by_a_peer(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / "control" / POLICY_STORE_NAME
    assert read_agent_policy_selection(path, workspace).source == "host"
    assert not path.exists()
    owner = AgentPolicySettings(path, workspace)
    try:
        with pytest.raises(AgentPolicySettingsUnavailable):
            AgentPolicySettings(path, workspace)
        assert read_agent_policy_selection(path, workspace).source == "host"
        path.rename(path.with_suffix(".previous"))
        path.write_text("Synthetic replacement")
        path.chmod(0o600)
        with pytest.raises(AgentPolicySettingsUnavailable):
            owner.inspect()
        with pytest.raises(AgentPolicySettingsUnavailable):
            read_agent_policy_selection(path, workspace)
        assert path.read_text() == "Synthetic replacement"
    finally:
        owner.close()
    with pytest.raises(AgentPolicySettingsUnavailable):
        AgentPolicySettings(workspace / "protected.sqlite", workspace)
