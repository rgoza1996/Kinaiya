"""Independent native Unicode oracles with real in-process Session and SQLite.

Only public synthetic data and the existing test-only service tokens are used.
The absent endpoints produce HTTP404 capability failures, never skipped tests.
"""

import hashlib
import json

import pytest
from test_agent_submit_admission import (
    ADMIT,
    LOOKUP,
    api,
    encoded,
    headers,
    lookup,
    native_submits,
    proposal,
)
from test_agent_submit_admission import (
    env as env,
)

OBJECTIVE = "Public synthetic café 雪 🚀."
CLIENT_KEY = "synthetic-clé-雪"


def _digest(payload):
    logical = {
        key: payload[key]
        for key in (
            "session_id",
            "thread_id",
            "epoch",
            "objective",
            "source_event_ids",
            "source_revision",
        )
    }
    logical["content_scope"] = payload["requested_content_scope"]
    if payload["explicit_skill"] is not None:
        logical["explicit_skill"] = payload["explicit_skill"]
    return hashlib.sha256(
        json.dumps(logical, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _value(environment, route, *, objective=OBJECTIVE, key=CLIENT_KEY):
    if route == LOOKUP:
        return lookup(environment.session, key=key)
    # Construct each changed objective before computing its logical digest.
    value = proposal(environment.session, objective=objective, key=key).model_dump(mode="json")
    assert value["payload"]["payload_digest"] == _digest(value["payload"])
    assert value["payload"]["objective"] == objective
    assert value["payload"]["idempotency_key"] == key
    return value


def _json_text(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _rows(store):
    # Include every actual table, including future admission metadata, without
    # assuming an unpublished slot-table name or schema.
    return tuple(store.db.iterdump())


def _status(response, expected, route):
    assert response.status_code != 404, (
        f"Native submit endpoint is absent (HTTP404 capability baseline): {route}"
    )
    assert response.status_code == expected


async def test_valid_non_ascii_utf8_admission_and_lookup_preserve_native_content(env):
    value = _value(env, ADMIT)
    raw = _json_text(value).encode("utf-8")
    assert b"caf\xc3\xa9" in raw and "雪".encode() in raw
    assert json.loads(raw) == value
    async with api(env.store) as client:
        created = await client.post(
            ADMIT, content=raw, headers={**headers("agent"), "Content-Type": "application/json"}
        )
        _status(created, 200, ADMIT)
        receipt = created.json()
        assert receipt["disposition"] == "created"
        entry = receipt["entry"]
        assert entry["payload"] == value["payload"]
        assert entry["idempotency_key"] == value["idempotency_key"]
        assert entry["task_refs"] == value["task_refs"]
        assert entry["origin"] == "agent-supervisor"
        assert len(native_submits(env.store)) == 1
        assert native_submits(env.store)[0].model_dump(mode="json") == entry
        before_lookup = _rows(env.store)
        query = _value(env, LOOKUP)
        observed = await client.post(
            LOOKUP,
            content=_json_text(query).encode("utf-8"),
            headers={**headers("agent"), "Content-Type": "application/json"},
        )
        assert _rows(env.store) == before_lookup
        _status(observed, 200, LOOKUP)
        assert observed.json()["status"] == "recorded"
        assert observed.json()["entry"] == entry


@pytest.mark.parametrize("route", [ADMIT, LOOKUP], ids=["admit", "lookup"])
@pytest.mark.parametrize(
    "encoding,bom",
    [
        ("utf-16-le", b""),
        ("utf-16-be", b""),
        ("utf-16-le", b"\xff\xfe"),
        ("utf-16-be", b"\xfe\xff"),
        ("utf-32-le", b""),
        ("utf-32-be", b""),
        ("utf-32-le", b"\xff\xfe\x00\x00"),
        ("utf-32-be", b"\x00\x00\xfe\xff"),
    ],
    ids=["16le", "16be", "16le_bom", "16be_bom", "32le", "32be", "32le_bom", "32be_bom"],
)
async def test_otherwise_valid_utf16_utf32_native_json_refuses_before_mutation(
    env, route, encoding, bom
):
    value = _value(env, route)
    raw = bom + _json_text(value).encode(encoding)
    # A byte-autodetecting stdlib parser accepts the exact intended object.
    # Encoding is the only defect: fields, identities, scopes and digest agree.
    assert json.loads(raw) == value
    assert len(raw) < (65536 if route == ADMIT else 4096)
    async with api(env.store) as client:
        before = _rows(env.store)
        response = await client.post(
            route, content=raw, headers={**headers("agent"), "Content-Type": "application/json"}
        )
    assert _rows(env.store) == before
    _status(response, 422, route)


@pytest.mark.parametrize("route", [ADMIT, LOOKUP], ids=["admit", "lookup"])
@pytest.mark.parametrize("encoding", ["invalid_utf8", "lone_surrogate"])
async def test_known_string_unicode_defect_with_fresh_logical_digest_refuses_without_mutation(
    env, route, encoding
):
    changed = "Synthetic café" if encoding == "invalid_utf8" else "Synthetic \ud800 objective"
    value = _value(
        env,
        route,
        objective=changed,
        key=changed if route == LOOKUP else "synthetic-unicode-client-key",
    )
    if encoding == "invalid_utf8":
        raw = _json_text(value).encode("latin-1")
        # A hypothetical Latin1 repair sees the correct logical body and digest,
        # so a stale digest cannot independently account for refusal.
        assert json.loads(raw.decode("latin-1")) == value
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")
    else:
        raw = encoded(value)
        assert b"\\ud800" in raw
        assert json.loads(raw) == value
        with pytest.raises(UnicodeEncodeError):
            changed.encode("utf-8")
    if route == ADMIT:
        assert value["payload"]["payload_digest"] == _digest(value["payload"])
        assert value["payload"]["objective"] == changed
    assert len(raw) < (65536 if route == ADMIT else 4096)
    async with api(env.store) as client:
        before = _rows(env.store)
        response = await client.post(
            route, content=raw, headers={**headers("agent"), "Content-Type": "application/json"}
        )
    assert _rows(env.store) == before
    _status(response, 422, route)
