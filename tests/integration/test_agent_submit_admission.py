"""#199 native logical submit slots, independent of runtime/provider admission.

All requests use an in-process authenticated Session API and real SQLite.
There is no provider, service listener, model, or external network operation.
The old API's 404 is capability evidence, not an initial-submit regression.
"""

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.models import AppendRequest, JournalEntry, SessionRequest
from services.common.auth import Auth
from services.session.api import create_app
from services.session.store import Store

ADMIT = "/v1/transcript/agent-submits"
LOOKUP = ADMIT + "/lookup"
TOKENS = {
    role: character * 32
    for role, character in zip(
        ("owner", "agent", "converse", "decisions", "resources"), "oacdr", strict=True
    )
}
AUTH = Auth(TOKENS)
TASK_A = "00000000-0000-4000-8000-000000000001"
TASK_B = "00000000-0000-4000-8000-000000000002"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def proposal(session, *, task_id=TASK_A, label="a", key="logical-submit", **changes):
    scope = changes.pop("content_scope", "local_only")
    skill = changes.pop("explicit_skill", None)
    logical = {
        "session_id": session.session_id,
        "thread_id": session.thread_id,
        "epoch": session.epoch,
        "objective": "Observe a public synthetic native admission receipt.",
        "source_event_ids": [],
        "source_revision": 1,
        "content_scope": scope,
        **changes,
    }
    if skill is not None:
        logical["explicit_skill"] = skill
    payload = {
        "op": "submit",
        "actor": "owner",
        **{name: value for name, value in logical.items() if name != "explicit_skill"},
        "task_id": task_id,
        "state": "accepted",
        "objective_revision": logical["source_revision"],
        "requested_content_scope": scope,
        "idempotency_key": key,
        "payload_digest": hashlib.sha256(encoded(logical)).hexdigest(),
        "harness_version": "synthetic-harness-" + label,
        "model_pin": {
            "schema_version": "1.0",
            "profile_id": "synthetic-profile-" + label,
            "provider": "synthetic",
            "model": "synthetic-model-" + label,
            "local": True,
            "reasoning": None,
            "settings_revision": 0,
        },
        "explicit_skill": skill,
        "plugin_versions": {},
    }
    return AppendRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        kind="agent.command",
        payload=payload,
        task_refs=[task_id],
        idempotency_key=f"agent:{task_id}:agent.command:1",
    )


def lookup(session, *, key="logical-submit"):
    return {
        "schema_version": "1.0",
        "session_id": session.session_id,
        "thread_id": session.thread_id,
        "epoch": session.epoch,
        "idempotency_key": key,
    }


def headers(role):
    return {"Authorization": "Bearer " + TOKENS[role]} if role is not None else {}


@asynccontextmanager
async def api(store):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, AUTH)), base_url="http://session.test"
    ) as client:
        yield client


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "journal.sqlite"
    store = Store(path)
    session = store.create_session(SessionRequest(channel="agent"))
    try:
        yield SimpleNamespace(store=store, path=path, session=session, tmp_path=tmp_path)
    finally:
        store.close()


def native_submits(store):
    return [
        entry
        for entry in store.read(kinds=["agent.command"])
        if entry.payload.get("op") == "submit"
    ]


async def admitted(client, body):
    response = await client.post(ADMIT, json=body.model_dump(mode="json"), headers=headers("agent"))
    assert response.status_code == 200, (
        "Native admission is absent or refused a canonical proposal",
        response.status_code,
    )
    return response.json()


def legacy_database(path, session, bodies, *, mutation=None, writer="agent"):
    """Create the actual pre-admission tables, without using the forbidden new path."""
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE schema_meta(version INTEGER NOT NULL);
            INSERT INTO schema_meta VALUES(1);
            CREATE TABLE sessions(session_id TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE entries(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                thread_id TEXT NOT NULL,epoch INTEGER NOT NULL,kind TEXT NOT NULL,
                writer TEXT NOT NULL,idem TEXT NOT NULL,request TEXT NOT NULL,value TEXT NOT NULL,
                UNIQUE(session_id,writer,idem));
        """)
        db.execute(
            "INSERT INTO sessions VALUES(?,?)", (session.session_id, session.model_dump_json())
        )
        for sequence, body in enumerate(bodies, 1):
            entry = JournalEntry(**body.model_dump(), sequence=sequence, origin="agent-supervisor")
            if mutation is not None:
                entry = mutation(entry)
            db.execute(
                "INSERT INTO entries(sequence,event_id,session_id,thread_id,epoch,kind,writer,"
                "idem,request,value) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    sequence,
                    entry.event_id,
                    entry.session_id,
                    entry.thread_id,
                    entry.epoch,
                    entry.kind,
                    writer,
                    body.idempotency_key,
                    body.model_dump_json(),
                    entry.model_dump_json(),
                ),
            )


def entry_rows(store):
    return tuple(tuple(row) for row in store.db.execute("SELECT * FROM entries ORDER BY sequence"))


def test_two_sqlite_connections_choose_one_created_winner(env):
    second = Store(env.path)
    try:
        first_admit = getattr(env.store, "admit_submit", None)
        second_admit = getattr(second, "admit_submit", None)
        assert callable(first_admit) and callable(second_admit), (
            "Native logical admission method is not installed (capability baseline)"
        )
        bodies = [proposal(env.session), proposal(env.session, task_id=TASK_B, label="b")]
        barrier = threading.Barrier(2)

        def compete(method, body):
            barrier.wait(timeout=2)
            return method(body, "agent")

        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [
                workers.submit(compete, method, body)
                for method, body in zip((first_admit, second_admit), bodies, strict=True)
            ]
            receipts = [future.result(timeout=5) for future in futures]
        assert sorted(receipt.disposition for receipt in receipts) == ["created", "retained"]
        assert receipts[0].entry == receipts[1].entry
        winner = next(receipt.entry for receipt in receipts if receipt.disposition == "created")
        assert winner.payload["task_id"] in {TASK_A, TASK_B}
        assert winner.origin == "agent-supervisor"
        assert len(native_submits(env.store)) == 1
        assert native_submits(second) == [winner]
    finally:
        second.close()


async def test_retained_ack_and_lookup_preserve_canonical_winner(env):
    original = proposal(env.session)
    competing = proposal(env.session, task_id=TASK_B, label="b")
    async with api(env.store) as client:
        created = await admitted(client, original)
        retained = await admitted(client, competing)
        assert created["disposition"] == "created"
        assert retained["disposition"] == "retained"
        assert retained["entry"] == created["entry"]
        assert retained["entry"]["payload"]["model_pin"] == original.payload["model_pin"]
        assert (
            retained["entry"]["payload"]["harness_version"] == original.payload["harness_version"]
        )
        response = await client.post(LOOKUP, json=lookup(env.session), headers=headers("agent"))
        assert response.status_code == 200
        assert response.json()["status"] == "recorded"
        assert response.json()["entry"] == created["entry"]
        missing = await client.post(
            LOOKUP, json=lookup(env.session, key="never-attempted"), headers=headers("agent")
        )
        assert missing.status_code == 200
        assert missing.json()["status"] == "unobserved"
        assert missing.json()["entry"] is None
    assert len(native_submits(env.store)) == 1


@pytest.mark.parametrize("changed", ["objective", "content_scope", "explicit_skill"])
async def test_occupied_key_rejects_changed_logical_content(env, changed):
    changes = {
        "objective": "A different public synthetic objective.",
        "content_scope": "public_synthetic",
        "explicit_skill": "synthetic-skill",
    }
    async with api(env.store) as client:
        await admitted(client, proposal(env.session))
        body = proposal(env.session, task_id=TASK_B, **{changed: changes[changed]})
        response = await client.post(
            ADMIT, json=body.model_dump(mode="json"), headers=headers("agent")
        )
        assert response.status_code == 409
    assert len(native_submits(env.store)) == 1


async def test_occupied_key_from_earlier_epoch_is_conflict_not_unobserved(env):
    async with api(env.store) as client:
        await admitted(client, proposal(env.session))
        current = env.store.reset(env.session.session_id)
        response = await client.post(LOOKUP, json=lookup(current), headers=headers("agent"))
        assert response.status_code == 409
        response = await client.post(
            ADMIT,
            json=proposal(current, task_id=TASK_B).model_dump(mode="json"),
            headers=headers("agent"),
        )
        assert response.status_code == 409
    assert len(native_submits(env.store)) == 1


@pytest.mark.parametrize("route", [ADMIT, LOOKUP], ids=["admit", "lookup"])
@pytest.mark.parametrize("role", [None, "owner", "decisions", "converse", "resources"])
async def test_native_submit_endpoints_require_agent_authentication(env, route, role):
    body = proposal(env.session).model_dump(mode="json") if route == ADMIT else lookup(env.session)
    async with api(env.store) as client:
        response = await client.post(route, json=body, headers=headers(role))
    assert response.status_code == (401 if role is None else 403)
    assert native_submits(env.store) == []


@pytest.mark.parametrize("route", [ADMIT, LOOKUP], ids=["admit", "lookup"])
@pytest.mark.parametrize("bad", ["duplicate", "non_finite", "invalid_utf8", "trailing_json"])
async def test_native_wire_is_strict_before_mutation(env, route, bad):
    value = proposal(env.session).model_dump(mode="json") if route == ADMIT else lookup(env.session)
    raw = encoded(value)
    if bad == "duplicate":
        raw = b'{"epoch":1,' + raw[1:]
    elif bad == "non_finite":
        raw = raw.replace(b'"epoch":1', b'"epoch":NaN', 1)
    elif bad == "invalid_utf8":
        raw = raw[:-1] + b',"invalid":"\xff"}'
    else:
        raw += b"{}"
    async with api(env.store) as client:
        response = await client.post(
            route, content=raw, headers={**headers("agent"), "Content-Type": "application/json"}
        )
    assert response.status_code == 422
    assert native_submits(env.store) == []


@pytest.mark.parametrize("route,limit", [(ADMIT, 65536), (LOOKUP, 4096)], ids=["admit", "lookup"])
@pytest.mark.parametrize("extra", [0, 1], ids=["exact_bound", "over_bound"])
async def test_native_request_byte_bounds(env, route, limit, extra):
    value = proposal(env.session).model_dump(mode="json") if route == ADMIT else lookup(env.session)
    raw = encoded(value)
    raw += b" " * (limit + extra - len(raw))
    assert len(raw) == limit + extra
    async with api(env.store) as client:
        response = await client.post(
            route, content=raw, headers={**headers("agent"), "Content-Type": "application/json"}
        )
    assert response.status_code == (200 if extra == 0 else 422)
    assert len(native_submits(env.store)) == (route == ADMIT and extra == 0)


@pytest.mark.parametrize("retained", [False, True], ids=["new", "legacy_exact_replay"])
async def test_generic_append_cannot_bypass_native_submit_admission(env, retained):
    body = proposal(env.session)
    path = env.tmp_path / "old-generic.sqlite"
    legacy_database(path, env.session, [body] if retained else [])
    store = Store(path)
    try:
        before = entry_rows(store)
        async with api(store) as client:
            response = await client.post(
                "/v1/transcript/entries",
                json=body.model_dump(mode="json"),
                headers=headers("agent"),
            )
        assert response.status_code in {403, 409, 422}
        assert entry_rows(store) == before
    finally:
        store.close()


async def test_generic_non_submit_append_remains_available(env):
    body = proposal(env.session).model_copy(
        update={"kind": "agent.state", "payload": {"state": "synthetic"}, "task_refs": []}
    )
    async with api(env.store) as client:
        response = await client.post(
            "/v1/transcript/entries", json=body.model_dump(mode="json"), headers=headers("agent")
        )
    assert response.status_code == 200
    assert response.json()["kind"] == "agent.state"
    assert native_submits(env.store) == []


@pytest.mark.parametrize("variant", ["full", "missing_pin", "missing_optionals", "pre_scope"])
async def test_valid_legacy_receipt_is_retained_without_inventing_metadata(env, variant):
    body = proposal(env.session)
    payload = dict(body.payload)
    if variant in {"missing_pin", "pre_scope"}:
        payload.pop("model_pin")
    if variant == "missing_optionals":
        payload.pop("explicit_skill")
        payload.pop("plugin_versions")
        payload["model_pin"] = None
    if variant == "pre_scope":
        payload.pop("requested_content_scope")
        logical = {
            name: payload[name]
            for name in (
                "session_id",
                "thread_id",
                "epoch",
                "objective",
                "source_event_ids",
                "source_revision",
            )
        }
        payload["payload_digest"] = hashlib.sha256(encoded(logical)).hexdigest()
    body = body.model_copy(update={"payload": payload}, deep=True)
    path = env.tmp_path / "legacy-valid.sqlite"
    legacy_database(path, env.session, [body])
    store = Store(path)
    try:
        before = entry_rows(store)
        original = native_submits(store)[0]
        async with api(store) as client:
            response = await client.post(LOOKUP, json=lookup(env.session), headers=headers("agent"))
            assert response.status_code == 200
            assert response.json()["status"] == "recorded"
            assert response.json()["entry"] == original.model_dump(mode="json")
            retained = await admitted(client, proposal(env.session, task_id=TASK_B, label="b"))
            assert retained["disposition"] == "retained"
            assert retained["entry"] == original.model_dump(mode="json")
        assert entry_rows(store) == before
    finally:
        store.close()


@pytest.mark.parametrize("cross_epoch", [False, True], ids=["same_epoch", "cross_epoch"])
async def test_ambiguous_legacy_slots_disable_only_agent_admission(env, cross_epoch):
    old = proposal(env.session)
    current = env.session.model_copy(update={"epoch": 2}) if cross_epoch else env.session
    other = proposal(current, task_id=TASK_B, label="b")
    path = env.tmp_path / "legacy-duplicate.sqlite"
    legacy_database(path, current, [old, other])
    store = Store(path)
    try:
        before = entry_rows(store)
        async with api(store) as client:
            for route, body in (
                (LOOKUP, lookup(current)),
                (ADMIT, proposal(current).model_dump(mode="json")),
            ):
                response = await client.post(route, json=body, headers=headers("agent"))
                assert response.status_code == 503
            read = await client.get("/v1/transcript", headers=headers("agent"))
            assert read.status_code == 200 and len(read.json()) == 2
            assert entry_rows(store) == before
            conversation = store.create_session(SessionRequest(channel="converse"))
            body = AppendRequest(
                session_id=conversation.session_id,
                thread_id=conversation.thread_id,
                epoch=conversation.epoch,
                kind="conversation.converse",
                payload={"text": "Public synthetic conversation.", "final": True},
                idempotency_key="unrelated-converse",
            )
            write = await client.post(
                "/v1/transcript/entries",
                json=body.model_dump(mode="json"),
                headers=headers("converse"),
            )
            assert write.status_code == 200
        assert len(native_submits(store)) == 2
    finally:
        store.close()


@pytest.mark.parametrize("invalid", ["origin", "task_refs", "digest"])
async def test_malformed_legacy_submit_is_not_silently_chosen(env, invalid):
    def mutation(entry):
        if invalid == "origin":
            return entry.model_copy(update={"origin": "untrusted"})
        if invalid == "task_refs":
            return entry.model_copy(update={"task_refs": [TASK_B]})
        return entry.model_copy(update={"payload": {**entry.payload, "payload_digest": "0" * 64}})

    path = env.tmp_path / "legacy-invalid.sqlite"
    legacy_database(path, env.session, [proposal(env.session)], mutation=mutation)
    store = Store(path)
    try:
        before = entry_rows(store)
        async with api(store) as client:
            response = await client.post(LOOKUP, json=lookup(env.session), headers=headers("agent"))
            assert response.status_code == 503
            read = await client.get("/v1/transcript", headers=headers("agent"))
            assert read.status_code == 200 and len(read.json()) == 1
        assert entry_rows(store) == before
    finally:
        store.close()
