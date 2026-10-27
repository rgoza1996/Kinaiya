import pytest
from fastapi.testclient import TestClient

from packages.contracts.models import AppendRequest, SessionRequest
from services.common.auth import Auth
from services.session.api import create_app
from services.session.store import JournalError, LocalJournal, Store


def message(session, key="one", text="Synthetic greeting"):
    return AppendRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        kind="conversation.user",
        payload={"text": text, "final": True, "revision": 1},
        idempotency_key=key,
    )


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "journal.sqlite")
    yield instance
    instance.close()


def test_order_replay_idempotency_and_cursor_restart(tmp_path):
    path = tmp_path / "journal.sqlite"
    store = Store(path)
    session = store.create_session(SessionRequest())
    first = store.append(message(session), "owner")
    assert store.append(message(session), "owner") == first
    second = store.append(message(session, "two"), "owner")
    assert first.sequence < second.sequence
    store.acknowledge("decisions", "router-v1", first.sequence)
    store.close()
    recovered = Store(path)
    assert recovered.cursor("decisions", "router-v1") == first.sequence
    assert [e.event_id for e in recovered.read(after=first.sequence)] == [second.event_id]
    with pytest.raises(JournalError):
        recovered.append(message(session, text="conflicting payload"), "owner")
    recovered.close()


def test_epoch_and_revision_truth(store):
    session = store.create_session(SessionRequest())
    first = store.append(message(session), "owner")
    revision = message(session, "revision").model_copy(
        update={
            "kind": "conversation.revision",
            "causes": [first.event_id],
            "payload": {"target_event_id": first.event_id, "text": "Corrected text", "revision": 2},
        }
    )
    store.append(revision, "owner")
    assert store.entry(first.event_id).payload["text"] == "Synthetic greeting"
    assert store.resolve(first.event_id).payload["text"] == "Corrected text"
    assert store.reset(session.session_id).epoch == 2
    with pytest.raises(JournalError, match="Stale epoch"):
        store.append(message(session, "old-worker"), "owner")


def test_authentication_origin_and_writer_authority(store):
    auth = Auth({"owner": "o" * 32, "agent": "a" * 32, "converse": "c" * 32})
    client = TestClient(create_app(store, auth))
    assert client.post("/v1/sessions", json={}).status_code == 401
    owner = {"Authorization": "Bearer " + "o" * 32}
    response = client.post("/v1/sessions", json={}, headers=owner)
    session = store.session(response.json()["session_id"])
    request = message(session).model_dump(mode="json")
    assert (
        client.post(
            "/v1/transcript/entries", json={**request, "origin": "agent-supervisor"}, headers=owner
        ).status_code
        == 422
    )
    forged = {**request, "kind": "agent.outcome", "payload": {"state": "succeeded"}}
    assert client.post("/v1/transcript/entries", json=forged, headers=owner).status_code == 403
    assert (
        client.get(
            "/v1/transcript", headers={**owner, "Origin": "https://malicious.invalid"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/sessions", json={}, headers={"Authorization": "Bearer " + "a" * 32}
        ).status_code
        == 403
    )
    valid = client.post("/v1/transcript/entries", json=request, headers=owner)
    assert valid.status_code == 200
    assert valid.json()["origin"] == "authenticated-user"
    assert client.get("/v1/transcript?limit=201", headers=owner).status_code == 422


async def test_async_adapter_and_bounded_task_retrieval(store):
    session = store.create_session(SessionRequest(channel="agent"))
    journal = LocalJournal(store, "agent")
    event = message(session).model_copy(update={"kind": "agent.command", "task_refs": ["task-1"]})
    written = await journal.append(event)
    assert await journal.read(task_id="task-1") == [written]
    assert await journal.read(task_id="not-this-task") == []


def test_delete_removes_retrieval(store):
    session = store.create_session(SessionRequest())
    event = store.append(message(session), "owner")
    store.delete(session.session_id)
    assert store.read(session_id=session.session_id) == []
    with pytest.raises(KeyError):
        store.entry(event.event_id)
