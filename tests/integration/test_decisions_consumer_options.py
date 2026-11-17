"""Native consumer inspection preserves the original Converse owner and receipts."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.decisions_consumers import DecisionsAgentPolicyChange
from packages.contracts.decisions_models import DecisionsModelChange
from packages.providers.text import ScriptedProvider
from services import launcher
from services.common.auth import Auth
from services.decisions.agent_policy import AgentPolicyOwner
from services.decisions.agent_policy_settings import (
    POLICY_STORE_NAME,
    AgentPolicySettings,
    AgentPolicySettingsUnavailable,
)
from services.decisions.api import create_app
from services.decisions.model_settings import DecisionsModelSettings
from services.decisions.prompts import configured_prompt
from services.decisions.runtime import DecisionsWorker, ModelClassifier
from services.session.store import LocalJournal, Store

TOKENS = {role: role[0] * 40 for role in ("owner", "agent", "converse", "decisions", "resources")}
POLICY = {
    "catalog_binding": {"id": "advisor", "generation": 1, "configuration_sha256": "a" * 64},
    "instructions": "Synthetic Agent advice preference.\n",
}


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "unconfigured"),
        (POLICY, "host"),
        ({"instructions": "Synthetic invalid policy"}, "invalid_host"),
    ],
)
async def test_independent_policy_inspection_preserves_native_converse_receipts(
    tmp_path, value, expected
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = {
        "decisions_agent_policy": value,
        "decisions_prompt": {
            "finalized": "Existing finalized preference",
            "window": "Existing window preference",
        },
    }
    prompt = configured_prompt(config)
    provider = ScriptedProvider([])
    models = DecisionsModelSettings(
        provider, None, path=tmp_path / "models.sqlite", workspace=workspace
    )
    observed = models.snapshot()
    request = DecisionsModelChange(
        schema_version="1.0",
        expected_backend=observed["backend_id"],
        expected_instance=observed["instance_id"],
        expected_revision=observed["revision"],
        expected_state=observed["state_token"],
        idempotency_key="original-converse-selection",
        selection={"source": "host", "catalog_binding": None},
    )
    original_receipt = models.change(request)
    original_snapshot = models.snapshot()
    original_rows = models.db.execute("SELECT * FROM decisions_model_operations").fetchall()
    store = Store(tmp_path / "journal.sqlite")
    worker = DecisionsWorker(
        LocalJournal(store, "decisions"),
        object(),
        ModelClassifier(provider, prompt=prompt),
        enabled=False,
        model_settings=models,
    )
    settings = AgentPolicySettings(tmp_path / POLICY_STORE_NAME, workspace)
    app = create_app(
        worker,
        Auth(TOKENS),
        model_settings=models,
        agent_policy=AgentPolicyOwner(config, None, settings=settings),
    )
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                base_url="http://decisions",
                transport=httpx.ASGITransport(app=app),
                headers={"Authorization": "Bearer " + TOKENS["owner"]},
            ) as client:
                response = await client.get("/v1/decisions/options/agent-policy")
                assert (
                    response.status_code == 200 and response.headers["cache-control"] == "no-store"
                )
                assert response.json()["source"] == expected
                assert response.json()["request_admission"] == "unavailable"
                advice = "/v1/decisions/options/agent-advice-settings"
                observed_advice = (await client.get(advice)).json()
                saved_advice = await client.post(
                    advice,
                    json=DecisionsAgentPolicyChange(
                        expected_backend=observed_advice["backend_id"],
                        expected_instance=observed_advice["instance_id"],
                        expected_revision=observed_advice["revision"],
                        idempotency_key="disable-optional-advice",
                        selection={"source": "disabled"},
                    ).model_dump(),
                )
                assert saved_advice.status_code == 200
                assert worker.classifier.prompt is prompt
                assert configured_prompt(config) == prompt
                assert (await client.get("/v1/decisions/options/model")).json() == original_snapshot
                lookup = await client.post(
                    "/v1/decisions/options/model-changes/lookup", json=request.model_dump()
                )
                assert lookup.status_code == 200 and lookup.json() == models.lookup(request)
                assert models.change(request) == original_receipt
                assert (
                    models.db.execute("SELECT * FROM decisions_model_operations").fetchall()
                    == original_rows
                )
                assert provider.calls == []
        assert not worker.running and not worker.ready and settings.closed
    finally:
        await models.close()
        if models._host_cleanup is not None:
            await asyncio.shield(models._host_cleanup)
        assert models._db_closed and models._owner_fd is None and not models.leases
        store.close()


@pytest.mark.parametrize(
    "raw,source,with_agent,persisted,active_source",
    [
        (None, "unconfigured", True, None, "unconfigured"),
        (POLICY, "host", True, None, "host"),
        (POLICY, "host", False, None, "host"),
        ({"instructions": "Invalid synthetic"}, "invalid_host", True, None, "invalid_host"),
        (None, "unconfigured", True, "saved", "saved"),
        (None, "unconfigured", False, "saved", "saved"),
        (POLICY, "host", True, "disabled", "disabled"),
        (POLICY, "host", True, "corrupt", "unavailable"),
    ],
)
def test_official_launcher_wires_separate_agent_policy_without_modifying_converse(
    monkeypatch, tmp_path, raw, source, with_agent, persisted, active_source
):
    config = {
        "session_url": "http://127.0.0.1:8760",
        "agent_url": "http://127.0.0.1:8761",
        "decisions_prompt": {"finalized": "Original Converse", "window": "Original window"},
        "decisions_agent_policy": raw,
        "distribution": {"agent": with_agent, "decisions": True, "converse": True},
        "streaming_windows": {"enabled": False},
    }
    expected_prompt = configured_prompt(config)
    monkeypatch.setattr(launcher, "load_config", lambda _: config)
    monkeypatch.setattr(launcher, "load_or_create_tokens", lambda _: TOKENS)
    monkeypatch.setattr(launcher, "provider", lambda *a, **kw: object())
    monkeypatch.setattr("services.settings.provider_catalog.ProviderCatalog", lambda *a, **kw: None)
    monkeypatch.setattr(
        "services.decisions.model_settings.DecisionsModelSettings", lambda *a, **kw: object()
    )

    def worker(journal, agent, classifier, **kwargs):
        assert classifier.prompt == expected_prompt
        return SimpleNamespace(journal=journal, agent=agent, classifier=classifier, enabled=True)

    class StopBeforeServing(Exception):
        pass

    def observe(worker, *_args, **kwargs):
        policy_owner = kwargs["agent_policy"]
        state = policy_owner.snapshot()
        assert state.source == source and state.request_admission == "unavailable"
        assert state.catalog_status == ("unavailable" if source == "host" else "not_selected")
        assert configured_prompt(config) == expected_prompt
        assert policy_owner.active().source == active_source
        if persisted == "corrupt":
            with pytest.raises(AgentPolicySettingsUnavailable):
                policy_owner.settings_snapshot()
        operations = kwargs["agent_operations"]
        assert (operations is not None) is (with_agent and active_source in {"host", "saved"})
        if operations is not None:
            assert operations.journal is worker.journal
            assert operations.policy is policy_owner
            assert not operations.runtime_snapshot().new_request_available
        raise StopBeforeServing

    monkeypatch.setattr("services.decisions.runtime.DecisionsWorker", worker)
    monkeypatch.setattr("services.decisions.api.create_app", observe)
    args = SimpleNamespace(
        config=tmp_path / "config.json",
        data_dir=tmp_path / "data",
        workspace=tmp_path / "workspace",
        service="decisions",
        port=None,
    )
    store = args.data_dir / POLICY_STORE_NAME
    if persisted == "corrupt":
        args.data_dir.mkdir(mode=0o700)
        store.write_text("Synthetic corrupt policy")
        store.chmod(0o600)
    elif persisted is not None:
        settings = AgentPolicySettings(store, args.workspace)
        try:
            settings.change(
                DecisionsAgentPolicyChange(
                    expected_backend=settings.backend,
                    expected_instance=settings.instance,
                    expected_revision=0,
                    idempotency_key="persisted-selection",
                    selection={
                        "source": persisted,
                        "policy": POLICY if persisted == "saved" else None,
                    },
                ),
                check_policy=lambda _: None,
            )
        finally:
            settings.close()
    with pytest.raises(StopBeforeServing):
        launcher.serve(args)
