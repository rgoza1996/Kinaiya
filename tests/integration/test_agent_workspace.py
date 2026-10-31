"""Synthetic owner workspace metadata, affinity and native admission checks."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from core.extensions import EvaluationReport, Registry
from core.policy import PolicyDenied, ToolPolicy
from evaluation.harness.policy import DEFAULT_POLICY, POLICY_VERSION
from evaluation.harness.suite import suite_hash_of
from packages.commands import Registry as Commands
from packages.contracts.agent_workspace import (
    AgentSessionCreate,
    AgentSessionPage,
    AgentSessionPageRequest,
)
from packages.contracts.client import JournalClient
from packages.contracts.models import (
    AppendRequest,
    CommandRequest,
    Session,
    SessionRequest,
    TaskRequest,
)
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime, create_app
from services.common.auth import Auth
from services.session.api import create_app as session_app
from services.session.store import LocalJournal, Store


@pytest.fixture
async def env(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    suite = tmp_path / "suite.json"
    suite.write_text(
        json.dumps(
            {
                "suite_version": "synthetic",
                "cases": [
                    {
                        "case_id": "public",
                        "payload": {
                            "query": "public",
                            "files": [{"id": "a", "path": "a", "text": "public"}],
                            "max_chars": 10,
                        },
                        "expected_ids": ["a"],
                    }
                ],
            }
        )
    )
    registry = Registry(tmp_path / "protected", suite)
    baseline = registry.active
    candidate = registry.register_bytes(
        b"def select_context(payload):\n    return []\n", {"name": "public-selector"}
    )
    provider = ScriptedProvider([])
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        provider,
        workspace,
        ToolPolicy(workspace),
        selector_registry=registry,
    )
    commands = Commands(runtime, LocalJournal(store, "owner"))
    tokens = {
        role: value * 32
        for role, value in {
            "owner": "o",
            "agent": "a",
            "decisions": "d",
            "resources": "r",
            "converse": "c",
        }.items()
    }
    auth = Auth(tokens)
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime, auth, commands)),
            base_url="http://synthetic",
            headers={"Authorization": "Bearer " + tokens["owner"]},
        ) as client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=session_app(store, auth)),
            base_url="http://synthetic",
            headers={"Authorization": "Bearer " + tokens["owner"]},
        ) as sessions,
    ):
        yield SimpleNamespace(
            store=store,
            registry=registry,
            runtime=runtime,
            client=client,
            sessions=sessions,
            baseline=baseline,
            candidate=candidate,
            tokens=tokens,
            auth=auth,
            provider=provider,
            commands=commands,
        )
    await runtime.close()
    commands.db.close()
    store.close()


def authoritative_report(env, *, passed=True):
    """Protected stored-report fixture; no provider/evaluation process is invoked."""
    metadata = {v["version_id"]: v for v in env.registry.versions()}
    report = EvaluationReport(
        report_id="rep-" + "1" * 16,
        candidate_version_id=env.candidate,
        candidate_source_sha256=metadata[env.candidate]["source_sha256"],
        baseline_version_id=env.baseline,
        baseline_source_sha256=metadata[env.baseline]["source_sha256"],
        suite_hash=suite_hash_of(env.registry.suite_path),
        suite_cases=1,
        policy=dict(DEFAULT_POLICY),
        policy_version=POLICY_VERSION,
        model_profile=None,
        candidate={"n_fail": 0},
        baseline={"n_fail": 0},
        passed=passed,
        reasons=[],
        created_at=0,
    )
    (env.registry.root / "reports" / f"{report.report_id}.json").write_text(
        report.model_dump_json()
    )
    return report


def request_for(session, key="public-task"):
    return TaskRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective="Inspect public synthetic data",
        idempotency_key=key,
    )


async def test_catalog_inspects_candidates_without_granting_execution(env):
    response = await env.client.get("/v1/agent/harnesses")
    assert response.status_code == 200
    rows = {row["version_id"]: row for row in response.json()["items"]}
    assert rows[env.baseline]["selectable"] is True
    assert rows[env.baseline]["eligibility"] == "active"
    assert rows[env.candidate]["kind"] == "context_selector.v1"
    assert rows[env.candidate]["selectable"] is False
    assert rows[env.candidate]["eligibility"] == "unapproved"
    assert env.registry.active == env.baseline and not env.provider.calls
    refused = await env.client.post("/v1/agent/sessions", json={"harness_version": env.candidate})
    assert refused.status_code == 409
    assert env.store.sessions() == [] and env.registry.active == env.baseline
    first = await env.client.get("/v1/agent/harnesses", params={"limit": 1})
    second = await env.client.get(
        "/v1/agent/harnesses", params={"limit": 1, "after": first.json()["next_cursor"]}
    )
    assert first.json()["items"][0]["version_id"] != second.json()["items"][0]["version_id"]
    assert second.json()["next_cursor"] is None


async def test_only_owner_can_list_or_create_affinity_and_scope_is_strict(env):
    for role in ["agent", "decisions", "resources", "converse"]:
        headers = {"Authorization": "Bearer " + env.tokens[role]}
        assert (await env.client.get("/v1/agent/harnesses", headers=headers)).status_code == 403
        assert (
            await env.client.post(
                "/v1/agent/sessions", headers=headers, json={"harness_version": env.baseline}
            )
        ).status_code == 403
        assert (await env.sessions.get("/v1/sessions/page", headers=headers)).status_code == 403
        assert (
            await env.sessions.post(
                "/v1/sessions",
                headers=headers,
                json={"channel": "agent", "harness_version": env.baseline},
            )
        ).status_code == 403
    for forged in [
        {"channel": "converse"},
        {"eligible": True},
        {"actor": "owner"},
        {"permissions": "all"},
        {"model": "invented"},
    ]:
        response = await env.client.post(
            "/v1/agent/sessions",
            json={
                "harness_version": env.baseline,
                **forged,
            },
        )
        assert response.status_code == 422
    assert (
        await env.sessions.post(
            "/v1/sessions",
            json={
                "channel": "converse",
                "harness_version": env.baseline,
            },
        )
    ).status_code == 422
    assert (
        await env.sessions.get("/v1/sessions/page", params={"channel": "converse"})
    ).status_code == 422


async def test_pinned_empty_sessions_page_persist_reset_and_delete(env):
    ids = []
    for index in range(5):
        response = await env.client.post(
            "/v1/agent/sessions",
            json={
                "harness_version": env.baseline,
                "name": f"Public synthetic {index}",
            },
        )
        assert response.status_code == 201
        ids.append(response.json()["session_id"])
    legacy = env.store.create_session(SessionRequest(channel="agent", name="Unknown past"))
    env.store.create_session(SessionRequest(channel="converse"))
    found, cursor = [], None
    while True:
        params = {"harness_version": env.baseline, "limit": 2}
        if cursor is not None:
            params["after"] = cursor
        response = await env.sessions.get("/v1/sessions/page", params=params)
        assert response.status_code == 200
        page = AgentSessionPage.model_validate(response.json())
        assert all(s.harness_version == env.baseline for s in page.items)
        found.extend(s.session_id for s in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert found == list(reversed(ids))
    unknown = (
        await env.sessions.get("/v1/sessions/page", params={"harness_version": "unassociated"})
    ).json()
    assert [s["session_id"] for s in unknown["items"]] == [legacy.session_id]
    assert unknown["items"][0]["harness_version"] is None
    assert env.store.reset(ids[0]).harness_version == env.baseline
    assert (await env.sessions.delete(f"/v1/sessions/{ids[0]}")).status_code == 200
    assert ids[0] not in [s.session_id for s in env.store.sessions_page().items]
    recovered = Store(env.store.path)
    try:
        assert recovered.session(ids[1]).harness_version == env.baseline
    finally:
        recovered.close()
    assert not env.provider.calls


async def test_promoted_pin_survives_rollback_without_activation_or_fallback(env):
    report = authoritative_report(env)
    # A passing report alone is insufficient: only native reviewed promotion
    # contributes a protected approval receipt.
    assert env.registry.selection_eligibility(env.candidate) == "unapproved"
    env.registry.promote(env.candidate, report.report_id, reviewed=True)
    response = await env.client.post("/v1/agent/sessions", json={"harness_version": env.candidate})
    pinned = Session.model_validate(response.json())
    env.registry.rollback()
    assert env.registry.active == env.baseline
    assert env.registry.selection_eligibility(env.candidate) == "owner_promoted"
    task = await env.runtime.submit(request_for(pinned))
    assert task.harness_version == env.candidate
    assert env.registry.active == env.baseline
    assert (await env.client.get(f"/v1/agent/tasks/{task.task_id}")).json()[
        "harness_version"
    ] == env.candidate
    assert env.store.session(pinned.session_id).harness_version == env.candidate


async def test_forged_owner_metadata_cannot_grant_task_eligibility(env):
    for version in [env.candidate, "v-" + "f" * 24, "seed-v1"]:
        session = env.store.create_session(SessionRequest(channel="agent", harness_version=version))
        with pytest.raises(PolicyDenied, match="pinned harness"):
            await env.runtime.submit(request_for(session, version))
    assert await env.runtime.status() == [] and not env.provider.calls
    valid = env.store.create_session(SessionRequest(channel="agent", harness_version=env.baseline))
    with pytest.raises(ValidationError):
        TaskRequest(**request_for(valid).model_dump(), harness_version=env.candidate)


async def test_legacy_association_stays_unknown_and_baseline_pin_stays_approved(env):
    legacy = env.store.create_session(SessionRequest(channel="agent"))
    pinned = env.store.create_session(SessionRequest(channel="agent", harness_version=env.baseline))
    report = authoritative_report(env)
    env.registry.promote(env.candidate, report.report_id, reviewed=True)
    assert env.registry.selection_eligibility(env.baseline) == "approved_baseline"
    legacy_task = await env.runtime.submit(request_for(legacy, "legacy"))
    pinned_task = await env.runtime.submit(request_for(pinned, "pinned"))
    assert legacy_task.harness_version == env.candidate
    assert pinned_task.harness_version == env.baseline
    assert env.store.session(legacy.session_id).harness_version is None


@pytest.mark.parametrize("invalidate", ["report", "suite", "audit", "source", "profile", "policy"])
async def test_current_native_proof_is_rechecked_on_every_admission(env, invalidate):
    report = authoritative_report(env)
    env.registry.promote(env.candidate, report.report_id, reviewed=True)
    session = env.store.create_session(
        SessionRequest(channel="agent", harness_version=env.candidate)
    )
    env.registry.rollback()
    if invalidate == "report":
        (env.registry.root / "reports" / f"{report.report_id}.json").unlink()
    elif invalidate == "suite":
        env.registry.suite_path.write_text('{"suite_version":"changed","cases":[]}')
    elif invalidate == "audit":
        (env.registry.root / "audit.jsonl").write_text("malformed public synthetic receipt")
    elif invalidate == "source":
        (env.registry.root / "versions" / env.candidate / "source.py").write_text("changed")
    elif invalidate == "profile":
        env.registry.model_profile = {"model": "different synthetic profile"}
    else:
        changed = report.model_copy(update={"policy_version": "invented-policy"})
        (env.registry.root / "reports" / f"{report.report_id}.json").write_text(
            changed.model_dump_json()
        )
    with pytest.raises(PolicyDenied, match="pinned harness"):
        await env.runtime.submit(request_for(session))
    assert await env.runtime.status() == [] and not env.provider.calls


async def test_concurrent_creation_keeps_each_explicit_pin_without_global_changes(env):
    report = authoritative_report(env)
    env.registry.promote(env.candidate, report.report_id, reviewed=True)
    env.registry.rollback()
    versions = [env.baseline, env.candidate] * 5
    responses = await asyncio.gather(
        *[
            env.client.post("/v1/agent/sessions", json={"harness_version": version, "name": str(i)})
            for i, version in enumerate(versions)
        ]
    )
    assert all(r.status_code == 201 for r in responses)
    assert [r.json()["harness_version"] for r in responses] == versions
    assert len({r.json()["session_id"] for r in responses}) == len(versions)
    assert env.registry.active == env.baseline and not env.provider.calls


async def test_seed_runtime_and_http_journal_page_contract(env):
    env.runtime.selector_registry = None
    item = (await env.client.get("/v1/agent/harnesses")).json()["items"][0]
    assert item["version_id"] == "seed-v1" and item["kind"] == "seed_runtime"
    response = await env.client.post("/v1/agent/sessions", json={"harness_version": "seed-v1"})
    session = Session.model_validate(response.json())
    journal = JournalClient(
        "http://synthetic",
        env.tokens["owner"],
        transport=httpx.ASGITransport(app=session_app(env.store, env.auth)),
    )
    try:
        page = await journal.sessions_page(harness_version="seed-v1")
        assert [s.session_id for s in page.items] == [session.session_id]
    finally:
        await journal.close()
    task = await env.runtime.submit(request_for(session))
    assert task.harness_version == "seed-v1"


async def test_global_promotion_during_source_read_never_rebinds_a_session_pin(env, monkeypatch):
    session = env.store.create_session(
        SessionRequest(channel="agent", harness_version=env.baseline)
    )
    source = env.store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=1,
            kind="conversation.user",
            payload={"text": "Public synthetic instruction", "final": True, "revision": 1},
            idempotency_key="public-source",
        ),
        "owner",
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.runtime._source_authority_texts

    async def paused_read(*args, **kwargs):
        value = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return value

    monkeypatch.setattr(env.runtime, "_source_authority_texts", paused_read)
    submit = asyncio.create_task(
        env.runtime.submit(
            request_for(session).model_copy(update={"source_event_ids": [source.event_id]})
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    report = authoritative_report(env)
    env.registry.promote(env.candidate, report.report_id, reviewed=True)
    release.set()
    task = await submit
    assert task.harness_version == env.baseline
    assert env.registry.active == env.candidate
    assert env.store.session(session.session_id).harness_version == env.baseline


@pytest.mark.parametrize("mutation", ["harness", "channel", "limit", "cursor"])
async def test_http_page_rejects_scope_and_pagination_substitution(env, mutation):
    session = env.store.create_session(
        SessionRequest(channel="agent", harness_version=env.baseline)
    )
    payload = AgentSessionPage(items=[session]).model_dump(mode="json")
    if mutation == "harness":
        payload["items"][0]["harness_version"] = env.candidate
    elif mutation == "channel":
        payload["items"][0].update(channel="converse", harness_version=None)
    elif mutation == "limit":
        payload["items"].append({**payload["items"][0], "session_id": "other"})
    else:
        payload["next_cursor"] = "10"
    journal = JournalClient(
        "http://synthetic",
        env.tokens["owner"],
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    try:
        with pytest.raises(ValueError):
            await journal.sessions_page(harness_version=env.baseline, limit=1, after="5")
    finally:
        await journal.close()


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 21},
        {"after": "0"},
        {"after": "../private"},
        {"after": "9223372036854775808"},
        {"harness_version": "invented"},
    ],
)
async def test_page_invalid_bounds_are_refused(env, params):
    assert (await env.sessions.get("/v1/sessions/page", params=params)).status_code == 422


def test_legacy_session_json_and_new_strict_contracts():
    session = Session.model_validate({"session_id": "s", "thread_id": "t", "channel": "agent"})
    assert session.harness_version is None
    for value in ["unknown", "../v-forged", "v-" + "A" * 24, 1, True]:
        with pytest.raises(ValidationError):
            AgentSessionCreate(harness_version=value)
    with pytest.raises(ValidationError):
        AgentSessionPageRequest(channel="converse")


async def test_catalog_and_historical_proof_bounds_fail_closed(env, monkeypatch):
    import core.extensions.registry as registry_module

    report = authoritative_report(env)
    env.registry.promote(env.candidate, report.report_id, reviewed=True)
    env.registry.rollback()
    monkeypatch.setattr(registry_module, "MAX_SELECTION_AUDIT_BYTES", 1)
    assert env.registry.selection_eligibility(env.candidate) == "unavailable"
    assert env.registry.selection_eligibility(env.baseline) == "active"
    monkeypatch.setattr(registry_module, "MAX_SELECTION_VERSIONS", 1)
    assert (await env.client.get("/v1/agent/harnesses")).status_code == 503
    assert not env.provider.calls


@pytest.mark.parametrize("association", ["approved", "legacy", "unapproved"])
async def test_new_inherits_only_the_native_invoking_session_pin(env, association):
    pin = (
        env.baseline
        if association == "approved"
        else (env.candidate if association == "unapproved" else None)
    )
    parent = env.store.create_session(SessionRequest(channel="agent", harness_version=pin))
    if association == "approved":
        report = authoritative_report(env)
        env.registry.promote(env.candidate, report.report_id, reviewed=True)
    result = await env.commands.execute(
        CommandRequest(
            session_id=parent.session_id,
            thread_id=parent.thread_id,
            epoch=parent.epoch,
            input="/new Public synthetic work",
            idempotency_key="new-workspace-session",
        )
    )
    if association == "unapproved":
        assert result["status"] == "error"
        assert len(env.store.sessions()) == 1
    else:
        assert result["status"] == "ok"
        created = Session.model_validate(result["result"])
        assert created.harness_version == pin
        assert env.store.session(created.session_id).harness_version == pin
    assert not env.provider.calls
