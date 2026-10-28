"""Real owner/service HTTP authority; synthetic generation, no paid inference."""

import asyncio

import httpx
import pytest

from packages.contracts.models import ConverseRequest, SessionRequest
from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.intent import ConverseIntent
from services.converse.runtime import ConverseRuntime
from services.session.store import LocalJournal, Store

AUTH = Auth({"owner": "o" * 32, "resources": "r" * 32, "agent": "a" * 32})


@pytest.fixture
async def control(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    provider = ScriptedProvider(["Public synthetic greeting."])
    intent = ConverseIntent(tmp_path / "converse-intent.json")
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider, intent=intent)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, AUTH)), base_url="http://test"
    ) as client:
        yield runtime, client, session
    store.close()


async def set_choice(client, value, role="owner"):
    return await client.post(
        "/v1/converse/enabled",
        json={"enabled": value},
        headers={"Authorization": "Bearer " + AUTH.tokens[role]},
    )


async def test_resource_credential_only_changes_availability_and_cannot_rewrite_owner_off(control):
    runtime, client, _ = control
    off = await set_choice(client, False)
    assert off.status_code == 200 and off.json()["owner_intent_revision"] == 1
    restored = await set_choice(client, True, "resources")
    assert restored.status_code == 200
    assert restored.json() == {
        "enabled": False,
        "owner_enabled": False,
        "resource_enabled": True,
        "owner_intent_revision": 1,
    }
    assert not runtime.enabled and runtime.provider.calls == []
    assert (await set_choice(client, True)).json()["enabled"] is True
    assert runtime.intent.read().revision == 2


async def test_owner_on_cannot_bypass_a_resource_pause(control):
    runtime, client, _ = control
    await set_choice(client, False, "resources")
    state = (await set_choice(client, True)).json()
    assert state["owner_enabled"] and not state["enabled"]
    assert state["resource_enabled"] is False
    assert (await set_choice(client, True, "resources")).json()["enabled"]
    assert runtime.intent.read().revision == 1


async def test_standalone_owner_off_survives_runtime_recreation_and_can_be_turned_on(control):
    runtime, client, _ = control
    await set_choice(client, False)
    restored = ConverseRuntime(
        runtime.journal, runtime.provider, intent=ConverseIntent(runtime.intent.path)
    )
    assert not restored.enabled
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(restored, AUTH)), base_url="http://test"
    ) as replacement:
        assert (await set_choice(replacement, True)).json()["enabled"]
    assert runtime.enabled and runtime.provider.calls == []


@pytest.mark.parametrize(
    "body", [{"enabled": "false"}, {"enabled": 0}, {"enabled": False, "owner": True}]
)
async def test_control_body_is_strict_and_cannot_select_its_actor(control, body):
    runtime, client, _ = control
    response = await client.post(
        "/v1/converse/enabled",
        json=body,
        headers={"Authorization": "Bearer " + AUTH.tokens["resources"]},
    )
    assert response.status_code == 422
    assert runtime.intent.read().revision == 0 and runtime.enabled


async def test_non_owner_or_missing_credentials_cannot_change_intent(control):
    runtime, client, _ = control
    assert (await set_choice(client, False, "agent")).status_code == 403
    assert (await client.post("/v1/converse/enabled", json={"enabled": False})).status_code == 401
    assert runtime.intent.read().revision == 0 and runtime.enabled


async def test_corrupt_owner_policy_blocks_inference_and_is_not_replaced_by_control(control):
    runtime, client, session = control
    raw = b'{"enabled":false,"revision":0,"enabled":true}'
    runtime.intent.path.write_bytes(raw)
    assert runtime.enabled is False
    health = (await client.get("/health")).json()
    assert health["enabled"] is False and health["owner_intent_unavailable"]
    assert (await set_choice(client, True)).status_code == 503
    response = await client.post(
        "/v1/converse/turns",
        json={
            "session_id": session.session_id,
            "thread_id": session.thread_id,
            "epoch": session.epoch,
            "text": "public synthetic input",
            "idempotency_key": "invalid-owner-record",
        },
        headers={"Authorization": "Bearer " + AUTH.tokens["owner"]},
    )
    assert response.status_code == 409 and not runtime.provider.calls
    assert runtime.intent.path.read_bytes() == raw


async def test_off_on_while_journal_admission_is_suspended_never_starts_old_inference(control):
    runtime, client, session = control
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.journal.session

    async def delayed(session_id):
        entered.set()
        await release.wait()
        return await original(session_id)

    runtime.journal.session = delayed
    request = ConverseRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        text="public synthetic old turn",
        idempotency_key="held-admission",
    )
    pending = asyncio.create_task(runtime.turn(request))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert (await set_choice(client, False)).status_code == 200
        assert (await set_choice(client, True)).status_code == 200
        release.set()
        with pytest.raises(RuntimeError, match="availability changed"):
            await pending
        assert runtime.provider.calls == []
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_old_speech_stays_invalid_after_owner_off_then_on(control):
    runtime, client, session = control
    result = await runtime.turn(
        ConverseRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            text="public synthetic greeting",
            idempotency_key="before-off",
        )
    )
    speech = next(e["entry"] for e in result["events"] if e["type"] == "text")
    await set_choice(client, False)
    await set_choice(client, True)
    with pytest.raises(ValueError, match="Obsolete"):
        await runtime.validate_speech(speech["event_id"], speech["payload"]["generation_epoch"])


async def test_delivery_journal_failure_leaves_off_durable_without_a_false_ack(control):
    runtime, client, session = control
    await runtime.turn(
        ConverseRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            text="public synthetic greeting",
            idempotency_key="before-journal-outage",
        )
    )

    async def failed_append(request):
        raise httpx.ConnectError("synthetic journal outage")

    runtime.journal.append = failed_append
    response = await set_choice(client, False)
    assert response.status_code == 503
    assert not runtime.enabled
    assert ConverseIntent(runtime.intent.path).read().enabled is False
