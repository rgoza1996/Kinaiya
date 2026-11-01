"""#199 retained SQLite provenance, independent of models and live services.

The canonical control and every corruption start from the actual legacy tables.
These tests use only synthetic data, real SQLite, and authenticated in-process ASGI.
Absent native routes are a 404 capability baseline, never a passing result.
"""

import json
import sqlite3

import pytest
from test_agent_submit_admission import (
    ADMIT,
    LOOKUP,
    TASK_B,
    api,
    entry_rows,
    headers,
    legacy_database,
    lookup,
    proposal,
)
from test_agent_submit_admission import (
    env as env,
)

from packages.contracts.agent_submit import (
    MAX_AGENT_SUBMIT_RESPONSE_BYTES,
    AgentSubmitLookupRequest,
    agent_submit_json,
    submit_entry,
    submit_request,
)
from packages.contracts.models import AppendRequest
from services.session.store import Store

UNAVAILABLE = {"detail": "Agent submission admission is unavailable"}
SQL_IDENTITY_COLUMNS = (
    "sequence",
    "event_id",
    "session_id",
    "thread_id",
    "epoch",
    "kind",
    "idem",
)
PROVENANCE_FAULTS = (
    *("column_" + name for name in SQL_IDENTITY_COLUMNS),
    "request_bool_epoch",
    "request_string_epoch",
    "request_logical_key",
    "duplicate_request_key",
    "duplicate_value_key",
)


def database_rows(path):
    with sqlite3.connect(path) as db:
        return tuple(db.execute("SELECT * FROM entries ORDER BY sequence"))


def canonical_legacy(path, session):
    body = proposal(session)
    legacy_database(path, session, [body])
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM entries").fetchone())
    request = agent_submit_json(row["request"].encode())
    value = agent_submit_json(row["value"].encode(), maximum=MAX_AGENT_SUBMIT_RESPONSE_BYTES)
    assert submit_request(request).model_dump(mode="json") == request
    assert submit_entry(value).model_dump(mode="json") == value
    assert {name: value[name] for name in AppendRequest.model_fields} == request
    assert row["writer"] == "agent"
    for name in SQL_IDENTITY_COLUMNS:
        value_name = "idempotency_key" if name == "idem" else name
        assert row[name] == value[value_name]
    return value


def corrupt_legacy(path, session, fault):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM entries").fetchone())
        if fault.startswith("column_"):
            column = fault.removeprefix("column_")
            assert column in SQL_IDENTITY_COLUMNS
            other_session = session.model_copy(
                update={
                    "session_id": "synthetic-other-session",
                    "thread_id": "synthetic-other-thread",
                }
            )
            db.execute(
                "INSERT INTO sessions VALUES(?,?)",
                (other_session.session_id, other_session.model_dump_json()),
            )
            changes = {
                "sequence": 9,
                "event_id": "synthetic-other-event",
                "session_id": other_session.session_id,
                "thread_id": other_session.thread_id,
                "epoch": 2,
                "kind": "agent.state",
                "idem": "different-native-key",
            }
            replacement = changes[column]
        elif fault.startswith("request_"):
            column = "request"
            request = json.loads(row[column])
            if fault == "request_bool_epoch":
                request["epoch"] = True
                assert type(request["epoch"]) is bool
            elif fault == "request_string_epoch":
                request["epoch"] = "1"
                assert type(request["epoch"]) is str
            else:
                assert fault == "request_logical_key"
                request["payload"]["idempotency_key"] = "different-original-logical-key"
                validated = submit_request(request)
                original = json.loads(row[column])
                assert validated.payload["payload_digest"] == original["payload"]["payload_digest"]
                assert (
                    validated.payload["idempotency_key"] != original["payload"]["idempotency_key"]
                )
            if fault != "request_logical_key":
                with pytest.raises(ValueError, match="Invalid Agent submit request"):
                    submit_request(request)
            replacement = json.dumps(request, separators=(",", ":"), allow_nan=False)
        else:
            assert fault in {"duplicate_request_key", "duplicate_value_key"}
            column = "request" if fault == "duplicate_request_key" else "value"
            original = row[column]
            assert json.loads(original)["schema_version"] == "1.0"
            replacement = '{"schema_version":"1.0",' + original[1:]
            assert json.loads(replacement) == json.loads(original)
            with pytest.raises(ValueError, match="Invalid Agent submit admission JSON"):
                agent_submit_json(replacement.encode(), maximum=MAX_AGENT_SUBMIT_RESPONSE_BYTES)
        assert replacement != row[column]
        db.execute(f"UPDATE entries SET {column}=?", (replacement,))
        changed = dict(db.execute("SELECT * FROM entries").fetchone())
        assert changed[column] == replacement
        assert {name: value for name, value in changed.items() if name != column} == {
            name: value for name, value in row.items() if name != column
        }


async def independent_session_and_converse(client, store, session, before):
    read_session = await client.get("/v1/sessions/" + session.session_id, headers=headers("agent"))
    assert read_session.status_code == 200
    assert read_session.json() == session.model_dump(mode="json")
    created = await client.post(
        "/v1/sessions",
        json={"channel": "converse", "name": "Public synthetic provenance control."},
        headers=headers("owner"),
    )
    assert created.status_code == 200
    conversation = created.json()
    body = AppendRequest(
        session_id=conversation["session_id"],
        thread_id=conversation["thread_id"],
        epoch=conversation["epoch"],
        kind="conversation.converse",
        payload={"text": "Public synthetic conversation remains available.", "final": True},
        idempotency_key="independent-provenance-converse",
    )
    written = await client.post(
        "/v1/transcript/entries",
        json=body.model_dump(mode="json"),
        headers=headers("converse"),
    )
    assert written.status_code == 200
    read = await client.get(
        "/v1/transcript",
        params={"session_id": conversation["session_id"]},
        headers=headers("converse"),
    )
    assert read.status_code == 200
    assert read.json() == [written.json()]
    after = entry_rows(store)
    assert len(after) == len(before) + 1
    assert after[: len(before)] == before


async def native_observations(client, session):
    competing = submit_request(proposal(session, task_id=TASK_B, label="competing"))
    query = AgentSubmitLookupRequest.model_validate(lookup(session))
    admitted = await client.post(
        ADMIT, json=competing.model_dump(mode="json"), headers=headers("agent")
    )
    found = await client.post(LOOKUP, json=query.model_dump(mode="json"), headers=headers("agent"))
    return admitted, found


async def test_canonical_legacy_sql_provenance_retains_exact_entry(env):
    path = env.tmp_path / "canonical-provenance.sqlite"
    original = canonical_legacy(path, env.session)
    before_store = database_rows(path)
    store = Store(path)
    try:
        async with api(store) as client:
            before = entry_rows(store)
            assert before == before_store
            admitted, found = await native_observations(client, env.session)
            assert entry_rows(store) == before
            await independent_session_and_converse(client, store, env.session, before)
        assert (admitted.status_code, found.status_code) == (200, 200), (
            "Native routes are absent or refused canonical retained provenance",
            admitted.status_code,
            found.status_code,
        )
        assert admitted.json()["disposition"] == "retained"
        assert admitted.json()["entry"] == original
        assert found.json()["status"] == "recorded"
        assert found.json()["entry"] == original
    finally:
        store.close()


@pytest.mark.parametrize("fault", PROVENANCE_FAULTS)
async def test_corrupt_sql_provenance_disables_only_native_submit(env, fault):
    path = env.tmp_path / (fault + ".sqlite")
    canonical_legacy(path, env.session)
    corrupt_legacy(path, env.session, fault)
    before_store = database_rows(path)
    store = Store(path)
    try:
        async with api(store) as client:
            before = entry_rows(store)
            assert before == before_store
            admitted, found = await native_observations(client, env.session)
            assert entry_rows(store) == before
            await independent_session_and_converse(client, store, env.session, before)
        assert (admitted.status_code, found.status_code) == (503, 503), (
            "Corrupt provenance must refuse both native operations",
            fault,
            admitted.status_code,
            found.status_code,
        )
        assert admitted.json() == found.json() == UNAVAILABLE
    finally:
        store.close()
