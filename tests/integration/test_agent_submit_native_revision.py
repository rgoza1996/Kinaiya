"""Private #199 revision oracles for actual native SQLite/API boundaries.

Only public synthetic data, local SQLite, and in-process ASGI are used. These
tests establish no Agent runtime, provider, worker, or execution behavior.
"""

import json
import re
import sqlite3

import pytest
from test_agent_submit_admission import (
    ADMIT,
    LOOKUP,
    TASK_B,
    api,
    encoded,
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
    AgentSubmitLookupResult,
    AgentSubmitReceipt,
    agent_submit_json,
    submit_entry,
    submit_request,
)
from packages.contracts.models import AppendRequest, SessionRequest
from services.session.store import Store

UNAVAILABLE = {"detail": "Agent submission admission is unavailable"}


def table_rows(db):
    """Snapshot actual tables after app startup, including SQLite sequence state."""
    names = [
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    return tuple(
        (
            name,
            tuple(
                sorted(
                    (
                        tuple(row)
                        for row in db.execute('SELECT * FROM "' + name.replace('"', '""') + '"')
                    ),
                    key=repr,
                )
            ),
        )
        for name in names
    )


def slots(db):
    return tuple(
        tuple(row)
        for row in db.execute(
            "SELECT session_id,logical_key,event_id FROM agent_submit_slots"
            " ORDER BY session_id,logical_key,event_id"
        )
    )


def native_rows(db):
    return tuple(
        tuple(row)
        for row in db.execute("SELECT * FROM entries WHERE kind='agent.command' ORDER BY sequence")
    )


def committed_native_state(path):
    with sqlite3.connect(path) as db:
        return native_rows(db), slots(db)


def checked_legacy(path, session, bodies):
    legacy_database(path, session, bodies)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("SELECT * FROM entries ORDER BY sequence")]
    assert len(rows) == len(bodies)
    for row in rows:
        raw_request = agent_submit_json(row["request"].encode())
        raw_entry = agent_submit_json(
            row["value"].encode(), maximum=MAX_AGENT_SUBMIT_RESPONSE_BYTES
        )
        assert submit_request(raw_request).model_dump(mode="json") == raw_request
        entry = submit_entry(raw_entry)
        assert entry.model_dump(mode="json") == raw_entry
        assert {name: raw_entry[name] for name in AppendRequest.model_fields} == raw_request
        assert row["writer"] == "agent"
        for name in ("sequence", "event_id", "session_id", "thread_id", "epoch", "kind"):
            assert row[name] == raw_entry[name]
        assert row["idem"] == raw_entry["idempotency_key"]
    return rows


async def create_conversation(client):
    response = await client.post(
        "/v1/sessions", json={"channel": "converse"}, headers=headers("owner")
    )
    assert response.status_code == 200
    return response.json()


async def write_conversation(client, conversation):
    body = AppendRequest(
        session_id=conversation["session_id"],
        thread_id=conversation["thread_id"],
        epoch=conversation["epoch"],
        kind="conversation.converse",
        payload={"text": "Public synthetic independent Converse write.", "final": True},
        idempotency_key="independent-native-revision-converse",
    )
    return await client.post(
        "/v1/transcript/entries", json=body.model_dump(mode="json"), headers=headers("converse")
    )


async def warm_native_lookup(client, session):
    query = AgentSubmitLookupRequest.model_validate(lookup(session, key="untouched-warmup"))
    response = await client.post(
        LOOKUP, json=query.model_dump(mode="json"), headers=headers("agent")
    )
    assert response.status_code == 200
    result = AgentSubmitLookupResult.model_validate(response.json()).validate_request(query)
    assert result.status == "unobserved" and result.entry is None


@pytest.mark.parametrize("invalid", ["bool_epoch", "string_epoch", "float_epoch", "missing_causes"])
async def test_native_api_validates_original_raw_proposal(env, invalid):
    raw = proposal(env.session).model_dump(mode="json")
    assert submit_request(raw).model_dump(mode="json") == raw
    if invalid == "missing_causes":
        raw.pop("causes")
    else:
        raw["epoch"] = {"bool_epoch": True, "string_epoch": "1", "float_epoch": 1.0}[invalid]
    received = agent_submit_json(encoded(raw))
    with pytest.raises(ValueError, match="Invalid Agent submit request"):
        submit_request(received)
    async with api(env.store) as client:
        before = table_rows(env.store.db)
        response = await client.post(
            ADMIT,
            content=encoded(raw),
            headers={**headers("agent"), "Content-Type": "application/json"},
        )
        observed = {
            "status": response.status_code,
            "all_tables_unchanged": table_rows(env.store.db) == before,
            "transaction_open": env.store.db.in_transaction,
            "native_entry_count": len(native_rows(env.store.db)),
        }
    assert observed == {
        "status": 422,
        "all_tables_unchanged": True,
        "transaction_open": False,
        "native_entry_count": 0,
    }, (invalid, observed)


async def test_agent_role_admits_and_looks_up_work_in_converse_session(env):
    session = env.store.create_session(SessionRequest(channel="converse"))
    body = submit_request(proposal(session))
    query = AgentSubmitLookupRequest.model_validate(lookup(session))
    async with api(env.store) as client:
        empty = await client.post(
            LOOKUP, json=query.model_dump(mode="json"), headers=headers("agent")
        )
        created = await client.post(
            ADMIT, json=body.model_dump(mode="json"), headers=headers("agent")
        )
        recorded = await client.post(
            LOOKUP, json=query.model_dump(mode="json"), headers=headers("agent")
        )
    assert (empty.status_code, created.status_code, recorded.status_code) == (200, 200, 200)
    absent = AgentSubmitLookupResult.model_validate(empty.json()).validate_request(query)
    assert absent.status == "unobserved" and absent.entry is None
    receipt = AgentSubmitReceipt.model_validate(created.json()).validate_request(body)
    assert receipt.disposition == "created"
    result = AgentSubmitLookupResult.model_validate(recorded.json()).validate_request(query)
    assert result.status == "recorded"
    assert result.entry == receipt.entry
    assert len(native_rows(env.store.db)) == 1


async def test_canonical_sql_cannot_hide_two_strictly_corrupt_json_cells(env):
    path = env.tmp_path / "both-stored-json-corrupt.sqlite"
    rows = checked_legacy(path, env.session, [proposal(env.session)])
    corrupt = {}
    for column in ("request", "value"):
        original = rows[0][column]
        corrupt[column] = '{"schema_version":"1.0",' + original[1:]
        assert json.loads(corrupt[column]) == json.loads(original)
        with pytest.raises(ValueError, match="Invalid Agent submit admission JSON"):
            agent_submit_json(corrupt[column].encode(), maximum=MAX_AGENT_SUBMIT_RESPONSE_BYTES)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE entries SET request=?,value=? WHERE sequence=1",
            (corrupt["request"], corrupt["value"]),
        )
    store = Store(path)
    try:
        async with api(store) as client:
            before = entry_rows(store)
            assert slots(store.db) == ()
            found = await client.post(LOOKUP, json=lookup(env.session), headers=headers("agent"))
            admitted = await client.post(
                ADMIT,
                json=proposal(env.session, task_id=TASK_B, label="competing").model_dump(
                    mode="json"
                ),
                headers=headers("agent"),
            )
            observed = {
                "lookup_status": found.status_code,
                "lookup_body": found.json(),
                "admission_status": admitted.status_code,
                "admission_disposition": admitted.json().get("disposition"),
                "entries_unchanged": entry_rows(store) == before,
                "slot_count": len(slots(store.db)),
                "transaction_open": store.db.in_transaction,
            }
        assert observed == {
            "lookup_status": 503,
            "lookup_body": UNAVAILABLE,
            "admission_status": 503,
            "admission_disposition": None,
            "entries_unchanged": True,
            "slot_count": 0,
            "transaction_open": False,
        }, json.dumps(observed, sort_keys=True)
        assert admitted.json() == UNAVAILABLE
    finally:
        store.close()


@pytest.mark.parametrize("column", ["request", "value"])
async def test_invalid_blob_after_valid_upgrade_prefix_rolls_back_before_converse(env, column):
    path = env.tmp_path / ("invalid-blob-" + column + ".sqlite")
    rows = checked_legacy(
        path,
        env.session,
        [
            proposal(env.session, key="valid-prefix"),
            proposal(env.session, task_id=TASK_B, label="later", key="invalid-later"),
        ],
    )
    with sqlite3.connect(path) as db:
        db.execute(
            f"UPDATE entries SET {column}=? WHERE sequence=2",
            (sqlite3.Binary(b"\xffpublic-synthetic-invalid-json"),),
        )
        assert db.execute(f"SELECT typeof({column}) FROM entries WHERE sequence=2").fetchone() == (
            "blob",
        )
    store = Store(path)
    try:
        async with api(store) as client:
            conversation = await create_conversation(client)
            before = entry_rows(store)
            before_all = table_rows(store.db)
            assert slots(store.db) == ()
            found = await client.post(
                LOOKUP, json=lookup(env.session, key="valid-prefix"), headers=headers("agent")
            )
            partial = slots(store.db)
            at_failure = {
                "status": found.status_code,
                "body": found.json(),
                "transaction_open": store.db.in_transaction,
                "original_entries_unchanged": entry_rows(store) == before,
                "slot_count": len(partial),
                "all_tables_unchanged": table_rows(store.db) == before_all,
            }
            written = await write_conversation(client, conversation)
            persisted_entries, persisted_slots = committed_native_state(path)
            observed = {
                "at_failure": at_failure,
                "converse_status": written.status_code,
                "transaction_open_after_converse": store.db.in_transaction,
                "committed_original_entries_unchanged": persisted_entries == before,
                "committed_slot_count": len(persisted_slots),
                "committed_prefix_event": bool(persisted_slots)
                and persisted_slots[0][2] == rows[0]["event_id"],
            }
        diagnostic = json.dumps(observed, sort_keys=True)
        assert at_failure["status"] == 503 and at_failure["body"] == UNAVAILABLE, diagnostic
        assert at_failure["original_entries_unchanged"], diagnostic
        assert not at_failure["transaction_open"] and at_failure["slot_count"] == 0, diagnostic
        assert written.status_code == 200, diagnostic
        assert observed["transaction_open_after_converse"] is False, diagnostic
        assert observed["committed_original_entries_unchanged"], diagnostic
        assert observed["committed_slot_count"] == 0, diagnostic
    finally:
        store.close()


class RuntimeFailureAfterNativeInsert:
    """Raise only after the requested native SQLite INSERT has actually run."""

    def __init__(self, connection, target):
        self.connection = connection
        self.target = target
        self.fired = False
        self.rows_at_fault = 0
        self.transaction_at_fault = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def execute(self, sql, *args):
        result = self.connection.execute(sql, *args)
        match = re.match(r'\s*insert\s+(?:or\s+\w+\s+)?into\s+"?(\w+)"?\s*\(', sql, re.I)
        if match and match[1].lower() == self.target and not self.fired:
            self.fired = True
            self.rows_at_fault = self.connection.execute(
                f"SELECT count(*) FROM {self.target}"
            ).fetchone()[0]
            self.transaction_at_fault = self.connection.in_transaction
            raise RuntimeError("Public synthetic failure after an actual native insertion")
        return result


@pytest.mark.parametrize("target", ["entries", "agent_submit_slots"])
async def test_non_sqlite_failure_after_actual_native_insert_rolls_back_everything(env, target):
    connection = env.store.db
    proxy = RuntimeFailureAfterNativeInsert(connection, target)
    try:
        async with api(env.store) as client:
            conversation = await create_conversation(client)
            await warm_native_lookup(client, env.session)
            before = table_rows(connection)
            assert not connection.in_transaction
            assert native_rows(connection) == slots(connection) == ()
            env.store.db = proxy
            failed = await client.post(
                ADMIT,
                json=submit_request(proposal(env.session)).model_dump(mode="json"),
                headers=headers("agent"),
            )
            at_failure = {
                "status": failed.status_code,
                "body": failed.json(),
                "fault_fired": proxy.fired,
                "actual_rows_at_fault": proxy.rows_at_fault,
                "transaction_open_at_fault": proxy.transaction_at_fault,
                "transaction_open_after_failure": connection.in_transaction,
                "all_tables_unchanged_after_failure": table_rows(connection) == before,
                "native_entries_after_failure": len(native_rows(connection)),
                "slots_after_failure": len(slots(connection)),
            }
            written = await write_conversation(client, conversation)
            persisted_entries, persisted_slots = committed_native_state(env.path)
            observed = {
                "at_failure": at_failure,
                "converse_status": written.status_code,
                "transaction_open_after_converse": connection.in_transaction,
                "committed_native_entries": len(persisted_entries),
                "committed_slots": len(persisted_slots),
            }
        diagnostic = json.dumps(observed, sort_keys=True)
        assert proxy.fired and proxy.rows_at_fault == 1 and proxy.transaction_at_fault, diagnostic
        assert failed.status_code == 503 and failed.json() == UNAVAILABLE, diagnostic
        assert not at_failure["transaction_open_after_failure"], diagnostic
        assert at_failure["all_tables_unchanged_after_failure"], diagnostic
        assert (
            at_failure["native_entries_after_failure"] == at_failure["slots_after_failure"] == 0
        ), diagnostic
        assert written.status_code == 200 and not connection.in_transaction, diagnostic
        assert persisted_entries == persisted_slots == (), diagnostic
    finally:
        env.store.db = connection
