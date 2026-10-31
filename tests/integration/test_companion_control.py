"""Actual Session ASGI/store authority, synthetic secrets/clock only."""

import concurrent.futures
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from packages.contracts.companion import (
    CallAction,
    CallNote,
    CallRequest,
    PairingRedeem,
    PairingRequest,
)
from packages.contracts.models import SessionRequest
from services.common.auth import Auth
from services.session.api import create_app
from services.session.companion import CompanionError, CompanionStore
from services.session.store import Store

OWNER = {"Authorization": "Bearer " + "o" * 32}
PREFIX = "/v1/companion"


@pytest.fixture
def host(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest())
    now = [1000]
    auth = Auth({"owner": "o" * 32, "agent": "a" * 32}, ("https://ui.invalid",))
    app = create_app(store, auth, companion_clock=lambda: now[0])
    with TestClient(app, base_url="https://host.invalid") as client:
        yield store, session, now, auth, app, client
    store.close()


def pairing(client, session, **extra):
    # Session metadata can grow independently of the scoped companion wire.
    payload = PairingRequest(
        schema_version=session.schema_version,
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        label="Synthetic phone",
    ).model_dump(mode="json")
    return client.post(PREFIX + "/pairings", json={**payload, **extra}, headers=OWNER)


def paired(client, session):
    p = pairing(client, session)
    assert p.status_code == 200, p.text
    issued = client.post(
        PREFIX + "/pairings/redeem",
        json={"code": p.json()["pairing_code"], "device_name": "Public fixture"},
    )
    assert issued.status_code == 200, issued.text
    return issued.json(), p.json()


def bearer(issued):
    return {"Authorization": "Bearer " + issued["device_token"]}


def invite(client, issued, key="call", title="Synthetic invitation"):
    response = client.post(
        PREFIX + "/calls",
        json={"device_id": issued["device"]["device_id"], "idempotency_key": key, "title": title},
        headers=OWNER,
    )
    assert response.status_code == 200, response.text
    return response.json()


def act(client, issued, call, action, key="action", **override):
    return client.post(
        PREFIX + "/self/calls/" + call["call_id"] + "/actions",
        json={
            "action": action,
            "expected_revision": call["revision"],
            "idempotency_key": key,
            **override,
        },
        headers=bearer(issued),
    )


def test_pairing_projects_binding_while_session_harness_metadata_stays_visible(host):
    store, session, now, auth, app, client = host
    current = client.get("/v1/sessions/" + session.session_id, headers=OWNER).json()
    assert current["harness_version"] is None
    projected = pairing(client, session)
    assert projected.status_code == 200, projected.text
    assert set(projected.json()) == {
        "schema_version",
        "session_id",
        "thread_id",
        "epoch",
        "label",
        "pairing_code",
        "expires_at",
    }
    # Even null metadata is outside the pairing contract, not a new grant.
    for harness in (None, "seed-v1"):
        rejected = pairing(client, session, harness_version=harness)
        assert rejected.status_code == 422
    assert store.db.execute("SELECT count(*) FROM companion_pairings").fetchone()[0] == 1

    agent = store.create_session(SessionRequest(channel="agent", harness_version="seed-v1"))
    assert (
        client.get("/v1/sessions/" + agent.session_id, headers=OWNER).json()["harness_version"]
        == "seed-v1"
    )
    # Projecting a valid wire shape cannot turn an Agent session into Converse.
    assert pairing(client, agent).status_code == 409
    assert store.session(agent.session_id).harness_version == "seed-v1"
    assert store.db.execute("SELECT count(*) FROM companion_pairings").fetchone()[0] == 1


def test_single_use_concurrent_redemption_and_hash_only_restart(host):
    store, session, now, auth, app, client = host
    p = pairing(client, session).json()
    request = PairingRedeem(code=p["pairing_code"], device_name="Concurrent public fixture")

    def redeem():
        try:
            return app.state.companion.redeem(request)
        except CompanionError as exc:
            return exc.status

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: redeem(), range(8)))
    successes = [r for r in results if not isinstance(r, int)]
    assert len(successes) == 1
    assert results.count(403) == 7
    issued = successes[0].model_dump()
    with store.lock:
        raw = "\n".join(store.db.iterdump())
    assert p["pairing_code"] not in raw and issued["device_token"] not in raw
    assert hashlib.sha256(issued["device_token"].encode()).hexdigest() in raw
    assert store.db.execute("SELECT count(*) FROM companion_pairings").fetchone()[0] == 0
    restarted = CompanionStore(store, clock=lambda: now[0])
    assert restarted.self_device(issued["device_token"]).device_id == issued["device"]["device_id"]
    assert store.db.execute("SELECT version FROM schema_meta").fetchone()[0] == 1


def test_auth_privilege_and_origin_separation(host):
    store, session, now, auth, app, client = host
    issued, _ = paired(client, session)
    phone = bearer(issued)
    for path in ("/v1/sessions", "/v1/transcript", "/v1/cursors/phone"):
        assert client.get(path, headers=phone).status_code == 401
    assert client.post("/v1/sessions", json={}, headers=phone).status_code == 401
    assert client.get(PREFIX + "/devices", headers=phone).status_code == 401
    for headers in (OWNER, {"Authorization": "Bearer " + "a" * 32}, {}):
        assert client.get(PREFIX + "/self", headers=headers).status_code == 401
    code = pairing(client, session).json()["pairing_code"]
    body = {"code": code, "device_name": "Another public device"}
    assert client.post(PREFIX + "/pairings/redeem", json=body, headers=OWNER).status_code == 401
    assert (
        client.post(
            PREFIX + "/pairings/redeem",
            json=body,
            headers={"Origin": "https://untrusted.invalid"},
        ).status_code
        == 403
    )
    assert (
        client.get(PREFIX + "/self", headers={**phone, "Origin": "https://ui.invalid"}).status_code
        == 200
    )
    assert (
        client.get(PREFIX + "/self?token=" + issued["device_token"], headers=phone).status_code
        == 422
    )


def test_observed_device_expiry_survives_clock_rollback_and_restart(host):
    store, session, now, _, _, client = host
    issued, _ = paired(client, session)
    call = invite(client, issued)
    now[0] = issued["device"]["expires_at"]
    assert client.get(PREFIX + "/self", headers=bearer(issued)).status_code == 403
    ended = client.get(PREFIX + "/calls/" + call["call_id"], headers=OWNER).json()
    assert (ended["state"], ended["end_reason"]) == ("ended", "device_expired")
    now[0] = 1000
    restarted = CompanionStore(store, clock=lambda: now[0])
    with pytest.raises(CompanionError) as error:
        restarted.self_device(issued["device_token"])
    assert error.value.status == 403
    assert restarted.devices()[0].status == "expired"
    assert client.get(PREFIX + "/self", headers=bearer(issued)).status_code == 403


@pytest.mark.parametrize(
    ("base", "peer", "expected"),
    [
        ("https://remote.invalid", "203.0.113.9", 200),
        ("http://127.0.0.1", "127.0.0.1", 200),
        ("http://localhost", "::1", 200),
        ("http://remote.invalid", "127.0.0.1", 403),
        ("http://127.0.0.1", "203.0.113.9", 403),
    ],
)
async def test_transport_uses_actual_peer_and_host(host, base, peer, expected):
    _, session, _, _, app, client = host
    issued, _ = paired(client, session)
    transport = httpx.ASGITransport(app=app, client=(peer, 1111))
    async with httpx.AsyncClient(transport=transport, base_url=base) as c:
        r = await c.get(
            PREFIX + "/self",
            headers={
                **bearer(issued),
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": "127.0.0.1",
            },
        )
        assert r.status_code == expected


def test_deadlines_replay_conflict_and_truthful_no_media(host):
    _, session, now, _, _, client = host
    issued, _ = paired(client, session)
    call = invite(client, issued)
    assert invite(client, issued) == call
    answer = act(client, issued, call, "answer")
    assert answer.status_code == 200
    answered = answer.json()
    assert answered["state"] == "accepted_waiting_media" and answered["revision"] == 2
    assert act(client, issued, call, "answer").json() == answered
    assert act(client, issued, call, "decline").status_code == 409
    assert act(client, issued, call, "end", "stale").status_code == 409
    now[0] += 15
    current = client.get(PREFIX + "/self/calls/" + call["call_id"], headers=bearer(issued)).json()
    assert (current["state"], current["end_reason"], current["revision"]) == (
        "ended",
        "media_unavailable",
        3,
    )
    assert act(client, issued, call, "answer").status_code == 409
    next_call = invite(client, issued, "next")
    now[0] += 30
    missed = client.get(PREFIX + "/calls/" + next_call["call_id"], headers=OWNER).json()
    assert missed["state"] == "missed" and missed["end_reason"] == "unanswered"
    assert act(client, issued, next_call, "answer", "late").status_code == 409
    assert (
        client.post(
            PREFIX + "/self/calls/" + call["call_id"] + "/media-ready",
            json={},
            headers=bearer(issued),
        ).status_code
        == 404
    )


@pytest.mark.parametrize("invalidation", ["revoke", "reset", "expiry", "delete"])
def test_invalidation_defeats_reads_mutations_and_exact_replays(host, invalidation):
    store, session, now, _, _, client = host
    issued, _ = paired(client, session)
    call = invite(client, issued)
    answer = act(client, issued, call, "answer").json()
    if invalidation == "revoke":
        r = client.post(
            PREFIX + "/devices/" + issued["device"]["device_id"] + "/revoke", json={}, headers=OWNER
        )
        assert r.status_code == 200
    elif invalidation == "reset":
        store.reset(session.session_id)
    elif invalidation == "expiry":
        now[0] += 86400
    else:
        store.delete(session.session_id)
    assert client.get(PREFIX + "/self", headers=bearer(issued)).status_code in (401, 403)
    assert act(client, issued, call, "answer").status_code in (401, 403)
    assert client.get(PREFIX + "/self/calls", headers=bearer(issued)).status_code in (401, 403)
    if invalidation != "delete":
        current = client.get(PREFIX + "/calls/" + call["call_id"], headers=OWNER).json()
        assert current["state"] == "ended"
        assert current["revision"] > answer["revision"]
        assert (
            client.post(
                PREFIX + "/calls/" + call["call_id"] + "/note",
                json={"expected_revision": current["revision"], "note": "Forbidden stale update"},
                headers=OWNER,
            ).status_code
            == 403
        )
    else:
        assert client.get(PREFIX + "/calls/" + call["call_id"], headers=OWNER).status_code == 404
        for table in (
            "companion_devices",
            "companion_calls",
            "companion_actions",
            "companion_pairings",
        ):
            assert store.db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_wrong_device_single_open_and_strict_bodies(host):
    _, session, _, _, _, client = host
    first, _ = paired(client, session)
    second, _ = paired(client, session)
    call = invite(client, first)
    assert (
        client.get(PREFIX + "/self/calls/" + call["call_id"], headers=bearer(second)).status_code
        == 404
    )
    assert act(client, second, call, "answer").status_code == 404
    assert (
        client.post(
            PREFIX + "/calls",
            headers=OWNER,
            json={
                "device_id": first["device"]["device_id"],
                "title": "Other",
                "idempotency_key": "different",
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            PREFIX + "/calls",
            headers=OWNER,
            json={
                "device_id": second["device"]["device_id"],
                "title": "Other",
                "idempotency_key": "different",
                "session_id": session.session_id,
            },
        ).status_code
        == 422
    )
    assert act(client, first, call, "answer", expected_revision=True).status_code == 422
    assert act(client, first, call, "answer", expected_revision=1.0).status_code == 422
    secret = "x" * 43
    r = client.post(
        PREFIX + "/pairings/redeem",
        json={
            "code": secret,
            "device_name": "bad",
            "unexpected": secret,
        },
    )
    assert r.status_code == 422 and secret not in r.text


def test_literal_note_durable_after_end_and_restart(host):
    store, session, now, auth, app, client = host
    issued, _ = paired(client, session)
    call = invite(client, issued)
    literal = '<b>{"url":"https://example.invalid"}</b>\nprint("literal only")'
    updated = client.post(
        PREFIX + "/calls/" + call["call_id"] + "/note",
        json={"expected_revision": 1, "note": literal},
        headers=OWNER,
    ).json()
    assert updated["note"] == literal and updated["revision"] == 2
    assert (
        client.post(
            PREFIX + "/calls/" + call["call_id"] + "/note",
            json={"expected_revision": 1, "note": "stale"},
            headers=OWNER,
        ).status_code
        == 409
    )
    ended = client.post(
        PREFIX + "/calls/" + call["call_id"] + "/end",
        json={"expected_revision": 2, "idempotency_key": "owner-end"},
        headers=OWNER,
    )
    assert ended.status_code == 200 and ended.json()["note"] == literal
    with TestClient(
        create_app(store, auth, companion_clock=lambda: now[0]), base_url="https://host.invalid"
    ) as restarted:
        result = restarted.get(PREFIX + "/self/calls/" + call["call_id"], headers=bearer(issued))
        assert result.json()["note"] == literal and result.json()["state"] == "ended"
    assert store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


def test_request_bounds_plain_note_utf8_and_pending_pairing_cap(host):
    _, session, now, _, _, client = host
    issued, _ = paired(client, session)
    call = invite(client, issued)
    good = "🙂" * 1024
    r = client.post(
        PREFIX + "/calls/" + call["call_id"] + "/note",
        json={"expected_revision": 1, "note": good},
        headers=OWNER,
    )
    assert r.status_code == 200
    for bad in (good + "x", "bad\x00note", "bad\x7fnote", "\ud800"):
        r = client.post(
            PREFIX + "/calls/" + call["call_id"] + "/note",
            content=json.dumps({"expected_revision": 2, "note": bad}).encode(),
            headers={**OWNER, "Content-Type": "application/json"},
        )
        assert r.status_code == 422
    assert pairing(client, session, label="🙂" * 120).status_code == 200
    for _ in range(7):
        assert pairing(client, session).status_code == 200
    assert pairing(client, session).status_code == 409
    now[0] += 120
    assert pairing(client, session).status_code == 200
    r = client.post(
        PREFIX + "/pairings/redeem",
        content=b"{" + b"x" * 16384,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert (
        client.post(
            PREFIX + "/pairings/redeem",
            content=b'{"code":1,"code":2}',
            headers={"Content-Type": "application/json"},
        ).status_code
        == 422
    )


def test_retained_call_cap_sparse_pagination_and_device_cap(host):
    store, session, _, _, app, client = host
    issued, _ = paired(client, session)
    other, _ = paired(client, session)
    service = app.state.companion
    for i in range(100):
        call = service.invite(
            CallRequest(
                device_id=issued["device"]["device_id"], idempotency_key=f"c{i}", title="Retained"
            )
        )
        service.action(
            call.call_id,
            CallAction(
                action="decline", expected_revision=call.revision, idempotency_key="decline"
            ),
            token=issued["device_token"],
        )
        if i < 5:
            extra = service.invite(
                CallRequest(
                    device_id=other["device"]["device_id"], idempotency_key=f"o{i}", title="Other"
                )
            )
            service.action(
                extra.call_id,
                CallAction(action="end", expected_revision=1, idempotency_key="end"),
                token=other["device_token"],
            )
    r = client.post(
        PREFIX + "/calls",
        json={
            "device_id": issued["device"]["device_id"],
            "idempotency_key": "overflow",
            "title": "no eviction",
        },
        headers=OWNER,
    )
    assert r.status_code == 409
    seen, after = [], 0
    for _ in range(5):
        page = client.get(PREFIX + f"/self/calls?after={after}", headers=bearer(issued)).json()
        assert len(page["calls"]) == 20
        seen.extend(c["call_id"] for c in page["calls"])
        if page["next_after"] is not None:
            assert page["next_after"] > after
        after = page["next_after"]
    assert after is None and len(set(seen)) == 100
    assert store.db.execute("SELECT count(*) FROM companion_actions").fetchone()[0] == 105
    for _ in range(30):
        paired(client, session)
    code = pairing(client, session).json()["pairing_code"]
    assert (
        client.post(
            PREFIX + "/pairings/redeem", json={"code": code, "device_name": "overflow"}
        ).status_code
        == 409
    )
    assert store.db.execute("SELECT count(*) FROM companion_devices").fetchone()[0] == 32


def test_current_binding_pairing_and_actual_database_restart(tmp_path):
    path = tmp_path / "persistent.sqlite"
    store = Store(path)
    current = store.create_session(SessionRequest())
    agent = store.create_session(SessionRequest(channel="agent"))
    clock = [2000]
    service = CompanionStore(store, clock=lambda: clock[0])
    bad = PairingRequest(
        session_id=agent.session_id, thread_id=agent.thread_id, epoch=1, label="wrong"
    )
    with pytest.raises(CompanionError):
        service.pair(bad)
    request = PairingRequest(
        session_id=current.session_id, thread_id=current.thread_id, epoch=1, label="durable"
    )
    code = service.pair(request).pairing_code
    store.close()
    store = Store(path)
    service = CompanionStore(store, clock=lambda: clock[0])
    issued = service.redeem(PairingRedeem(code=code, device_name="restart"))
    call = service.invite(
        CallRequest(device_id=issued.device.device_id, idempotency_key="one", title="Persist")
    )
    service.note(call.call_id, CallNote(expected_revision=1, note="Retained literal"))
    store.close()
    clock[0] += 30
    store = Store(path)
    service = CompanionStore(store, clock=lambda: clock[0])
    result = service.get_call(call.call_id, token=issued.device_token)
    assert result.state == "missed" and result.note == "Retained literal"
    store.close()


@pytest.mark.parametrize("cause", ["elapsed", "reset", "delete"])
def test_pairing_invalidation_before_redemption(host, cause):
    store, session, now, _, _, client = host
    code = pairing(client, session).json()["pairing_code"]
    if cause == "elapsed":
        now[0] += 120
    elif cause == "reset":
        store.reset(session.session_id)
    else:
        store.delete(session.session_id)
    result = client.post(PREFIX + "/pairings/redeem", json={"code": code, "device_name": "late"})
    assert result.status_code == 403
    assert store.db.execute("SELECT count(*) FROM companion_devices").fetchone()[0] == 0


def test_historical_owner_note_after_reset_and_terminal_note_update(host):
    store, session, _, _, _, client = host
    issued, _ = paired(client, session)
    call = invite(client, issued)
    ended = act(client, issued, call, "end").json()
    updated = client.post(
        PREFIX + "/calls/" + call["call_id"] + "/note",
        json={"expected_revision": ended["revision"], "note": "After hangup"},
        headers=OWNER,
    )
    assert updated.status_code == 200
    assert updated.json()["state"] == "ended" and updated.json()["note"] == "After hangup"
    store.reset(session.session_id)
    historical = client.get(PREFIX + "/calls/" + call["call_id"], headers=OWNER)
    assert historical.status_code == 200 and historical.json()["note"] == "After hangup"
    assert client.get(PREFIX + "/devices", headers=OWNER).json()["devices"][0]["status"] == "stale"
    assert (
        client.get(PREFIX + "/self/calls/" + call["call_id"], headers=bearer(issued)).status_code
        == 403
    )
    assert (
        client.post(
            PREFIX + "/calls/" + call["call_id"] + "/note",
            json={"expected_revision": updated.json()["revision"], "note": "no"},
            headers=OWNER,
        ).status_code
        == 403
    )


def test_concurrent_answer_decline_only_one_transition(host):
    _, session, _, _, app, client = host
    issued, _ = paired(client, session)
    call = invite(client, issued)

    def action(name):
        try:
            return app.state.companion.action(
                call["call_id"],
                CallAction(action=name, expected_revision=1, idempotency_key=name),
                token=issued["device_token"],
            ).state
        except CompanionError as exc:
            return exc.status

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(action, ["answer", "decline"]))
    assert results.count(409) == 1
    assert len([v for v in results if v in {"accepted_waiting_media", "declined"}]) == 1


def test_page_max_notes_bounded_literal_response(host):
    _, session, _, _, app, client = host
    issued, _ = paired(client, session)
    service = app.state.companion
    for i in range(20):
        call = service.invite(
            CallRequest(
                device_id=issued["device"]["device_id"], idempotency_key=str(i), title="🙂" * 120
            )
        )
        call = service.note(call.call_id, CallNote(expected_revision=1, note="🙂" * 1024))
        service.action(
            call.call_id,
            CallAction(
                action="decline", expected_revision=call.revision, idempotency_key="decline"
            ),
            token=issued["device_token"],
        )
    result = client.get(PREFIX + "/self/calls", headers=bearer(issued))
    assert result.status_code == 200 and len(result.content) < 256 * 1024
    assert len(result.json()["calls"]) == 20
    assert all(c["note"] == "🙂" * 1024 for c in result.json()["calls"])


@pytest.mark.parametrize("character", ['"', "\\", "\t", "\n"])
def test_page_max_escaped_notes_round_trip(host, character):
    _, session, _, _, app, client = host
    issued, _ = paired(client, session)
    service = app.state.companion
    note = character * 4096
    for i in range(20):
        call = service.invite(
            CallRequest(
                device_id=issued["device"]["device_id"],
                idempotency_key=str(i),
                title="🙂" * 120,
            )
        )
        call = service.note(call.call_id, CallNote(expected_revision=1, note=note))
        service.action(
            call.call_id,
            CallAction(
                action="decline", expected_revision=call.revision, idempotency_key="decline"
            ),
            token=issued["device_token"],
        )
    result = client.get(PREFIX + "/self/calls", headers=bearer(issued))
    assert result.status_code == 200
    assert 128 * 1024 < len(result.content) < 256 * 1024
    assert len(result.json()["calls"]) == 20
    assert all(c["note"] == note for c in result.json()["calls"])


@pytest.mark.parametrize("name", ["x" * 121, "line\nbreak", "tab\tname", " "])
def test_names_and_titles_are_bounded_plain_labels(host, name):
    _, session, _, _, _, client = host
    assert pairing(client, session, label=name).status_code == 422


def test_additive_schema_preserves_journal_and_rejects_unknown_version(tmp_path):
    store = Store(tmp_path / "prior.sqlite")
    session = store.create_session(SessionRequest())
    before = store.db.execute("SELECT * FROM sessions").fetchall()
    CompanionStore(store, clock=lambda: 1000)
    assert store.db.execute("SELECT * FROM sessions").fetchall() == before
    assert store.session(session.session_id) == session
    assert store.db.execute("SELECT version FROM schema_meta").fetchone()[0] == 1
    with store.lock, store.db:
        store.db.execute("UPDATE companion_schema SET version=2")
    with pytest.raises(CompanionError, match="Unsupported companion schema"):
        CompanionStore(store, clock=lambda: 1000)
    assert store.db.execute("SELECT version FROM companion_schema").fetchone()[0] == 2
    store.close()
