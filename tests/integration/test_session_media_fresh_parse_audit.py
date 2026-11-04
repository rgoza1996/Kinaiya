"""Independent observable preservation cases for native persisted-media audits.

Authored from baseline 2afc0d6 and its existing native Session fixture/source.
The candidate patch and new author output were not read. These are static-only
artifacts until root gates them; no baseline failure or timing claim is intended.
Only synthetic metadata and six-byte source reservations are needed. The fixture
owns actual Auth, ASGI, SQLite and spool custody. Corrupt-history cases explicitly
allow its existing unknown-shutdown path; disposable handle teardown cannot prove
native settlement. No decoder, provider, model or new authority double is used.
"""

import json

import pytest
from test_session_media_owner import _digest, _error, _json, _stack, _typed_receipt

from packages.contracts import media

_TABLES = (
    "entries",
    "media_meta",
    "media_owner_auth",
    "media_operations",
    "media_receipts",
    "media_catalog",
    "media_clients",
    "media_reservations",
    "media_sources",
    "media_dependencies",
    "media_uploads",
    "media_objective_anchors",
    "media_equivalences",
)
_NEXT_NONCE = "ef" * 32


def _rows(env):
    # Include exact persisted content, not just row counts or an empty cache.
    with env.store.lock:
        return {
            table: tuple(
                tuple(row) for row in env.store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")
            )
            for table in _TABLES
        }


def _read(env, statement, args):
    with env.store.lock:
        row = env.store.db.execute(statement, args).fetchone()
        assert row is not None
        return dict(row)


def _change(env, statement, args):
    # This is intentional corruption of a real, already admitted fixture row.
    with env.store.lock:
        changed = env.store.db.execute(statement, args)
        assert changed.rowcount == 1
        env.store.db.commit()


async def _refuse_before_new_admission(env):
    before = _rows(env)
    reservations = tuple(env.reserve_calls)
    reserved_bytes = env.spool.reserved_bytes
    offsets = {source: upload.next_offset for source, upload in env.uploads.items()}
    request = env.admit_request(key="fresh-after-audit", size=6)
    response = await env.post("/v1/media/sources/admit", request, nonce=_NEXT_NONCE)
    _error(response, 503, "unavailable")
    assert _rows(env) == before
    assert tuple(env.reserve_calls) == reservations
    assert env.spool.reserved_bytes == reserved_bytes
    assert {source: upload.next_offset for source, upload in env.uploads.items()} == offsets


async def test_canonical_native_history_catalog_and_new_admission_remain_usable(tmp_path):
    async with _stack(tmp_path) as env:
        source, _ = await env.admit()
        before = _rows(env)
        replay, _ = await env.admit()
        assert replay == source
        assert _rows(env) == before
        found = await env.lookup("source", source)
        assert found.status_code == 200
        assert found.content == _json({"v": 1, "found": True, "value": source})
        names = {
            "admit": "Admit",
            "task_proof": "TaskProofRequest",
            "anchor": "AnchorAdmission",
            "child_owner_append": "ChildOwnerAppend",
        }
        with env.store.lock:
            requests = list(env.store.db.execute("SELECT * FROM media_operations"))
            catalog = list(env.store.db.execute("SELECT * FROM media_catalog"))
        assert {row["kind"] for row in requests} == set(names)
        assert sum(row["kind"] == "child_owner_append" for row in requests) == 2
        for row in requests:
            request = json.loads(row["canonical_request"])
            assert _json(request) == bytes(row["canonical_request"])
            assert _digest(names[row["kind"]], request) == row["request_sha256"]
            assert request["key"] == row["key"]
            short = names[row["kind"]]
            kind = getattr(media, "Media" + short)
            typed = media.media_json(bytes(row["canonical_request"]), kind)
            assert type(typed) is kind
            assert media.media_bytes(typed) == bytes(row["canonical_request"])
            assert media.media_digest(short, typed) == row["request_sha256"]
            assert typed.key == row["key"]
        assert {row["kind"] for row in catalog} == {"limits", "decoder_profile"}
        for row in catalog:
            value = json.loads(row["canonical_value"])
            assert _json(value) == bytes(row["canonical_value"])
            short = "Limits" if row["kind"] == "limits" else "DecoderProfile"
            body = (
                value
                if short == "Limits"
                else {key: item for key, item in value.items() if key != "profile_sha256"}
            )
            expected = _digest(short, body)
            assert expected == row["sha256"]
            if short == "DecoderProfile":
                assert expected == value["profile_sha256"]
            typed = media.media_json(bytes(row["canonical_value"]), getattr(media, "Media" + short))
            assert media.media_bytes(typed) == bytes(row["canonical_value"])
            assert media.media_digest(short, typed) == expected
        request = env.admit_request(key="fresh-after-audit", size=6)
        response = await env.post("/v1/media/sources/admit", request, nonce=_NEXT_NONCE)
        _typed_receipt(response, "Admit", "SourceAdmission", request)
        assert len(env.reserve_calls) == 2


@pytest.mark.parametrize(
    "corruption",
    [
        "noncanonical",
        "nested-type",
        "nested-semantic",
        "request-hash",
        "wrong-request-kind",
        "request-row-key",
    ],
)
async def test_corrupt_native_request_history_refuses_before_new_effect(tmp_path, corruption):
    async with _stack(tmp_path, allow_unknown_shutdown=True) as env:
        source, _ = await env.admit()
        key = "task-proof" if corruption == "wrong-request-kind" else source["key"]
        row = _read(env, "SELECT * FROM media_operations WHERE key=?", (key,))
        request = json.loads(row["canonical_request"])
        raw, digest = bytes(row["canonical_request"]), row["request_sha256"]
        if corruption == "wrong-request-kind":
            # Native task-proof and seal charges are equal; this reaches exact
            # request-kind parsing rather than inventing a different byte charge.
            _change(env, "UPDATE media_operations SET kind='seal' WHERE key=?", (key,))
        else:
            if corruption == "noncanonical":
                raw = b" " + raw
            elif corruption == "request-hash":
                digest = "0" * 64
            else:
                if corruption == "nested-type":
                    request["session"]["epoch"] = True
                elif corruption == "nested-semantic":
                    request["purpose"] = "owner_voice"
                else:
                    request["key"] = "different-historical-key"
                raw, digest = _json(request), _digest("Admit", request)
            _change(
                env,
                "UPDATE media_operations SET canonical_request=?,request_sha256=? WHERE key=?",
                (raw, digest, key),
            )
        await _refuse_before_new_admission(env)


@pytest.mark.parametrize(
    "corruption",
    [
        "noncanonical-limits",
        "profile-nested-type",
        "profile-nested-semantic",
        "profile-self-hash",
        "catalog-row-hash",
    ],
)
async def test_corrupt_native_catalog_refuses_before_new_effect(tmp_path, corruption):
    async with _stack(tmp_path, allow_unknown_shutdown=True) as env:
        await env.admit()
        kind = "limits" if corruption == "noncanonical-limits" else "decoder_profile"
        row = _read(env, "SELECT * FROM media_catalog WHERE kind=?", (kind,))
        value = json.loads(row["canonical_value"])
        raw, digest = bytes(row["canonical_value"]), row["sha256"]
        if corruption == "noncanonical-limits":
            raw += b"\n"
        elif corruption == "catalog-row-hash":
            digest = "0" * 64
        else:
            if corruption == "profile-nested-type":
                value["limits"]["source"]["file_bytes"] = True
            elif corruption == "profile-nested-semantic":
                # Integers of the correct type still violate reduced-rational semantics.
                value["limits"]["decode"]["frame_rate"] = {"num": 2, "den": 2}
            else:
                value["generation"] += 1
            digest = _digest(
                "DecoderProfile",
                {key: item for key, item in value.items() if key != "profile_sha256"},
            )
            if corruption != "profile-self-hash":
                value["profile_sha256"] = digest
            raw = _json(value)
        _change(
            env,
            "UPDATE media_catalog SET canonical_value=?,sha256=? WHERE kind=? AND sha256=?",
            (raw, digest, kind, row["sha256"]),
        )
        await _refuse_before_new_admission(env)


@pytest.mark.parametrize(
    "corruption",
    [
        "noncanonical",
        "nested-semantic",
        "receipt-self-hash",
        "receipt-row-binding",
    ],
)
async def test_corrupt_native_receipt_refuses_before_new_effect(tmp_path, corruption):
    async with _stack(tmp_path, allow_unknown_shutdown=True) as env:
        source, _ = await env.admit()
        row = _read(env, "SELECT * FROM media_receipts WHERE receipt_id=?", (source["id"],))
        value = json.loads(row["canonical_receipt"])
        raw, digest = bytes(row["canonical_receipt"]), row["sha256"]
        request_digest = row["request_sha256"]
        if corruption == "noncanonical":
            raw += b" "
        elif corruption == "receipt-row-binding":
            request_digest = "0" * 64
        else:
            if corruption == "nested-semantic":
                value["body"]["fence"]["source_revision"] += 1
                value["sha256"] = _digest(
                    "R<SourceAdmission>",
                    {key: item for key, item in value.items() if key != "sha256"},
                )
                digest = value["sha256"]
            else:
                value["body"]["lease_expires_at_ms"] += 1
            raw = _json(value)
        _change(
            env,
            "UPDATE media_receipts SET canonical_receipt=?,sha256=?,request_sha256=? "
            "WHERE receipt_id=?",
            (raw, digest, request_digest, source["id"]),
        )
        await _refuse_before_new_admission(env)
