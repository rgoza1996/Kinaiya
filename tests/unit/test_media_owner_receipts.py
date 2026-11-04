"""Source-informed parser-unit oracles for immutable native media receipt rows.

The old method/registry and public DTO fixtures were read; the replacement
method was not. All bodies/refs/cleanup flags are synthetic metadata. A minimal
owner realm exercises the actual method without SQLite, FDs or native authority.
"""

import copy
import hashlib
import json

import pytest
from test_media_contracts import _ack
from test_media_session_contracts import (
    _analyze_anchor,
    _child_owner,
    _delete,
    _exchange,
    _objective,
    _proof,
    _reservation,
    _status,
    _worker,
)

from packages.contracts import media
from services.session.media_clients import MediaSessionError
from services.session.media_owner import MediaSessionOwner

_REGISTERED = (
    ("source", "SourceAdmission"),
    ("upload_ack", "UploadAck"),
    ("seal", "Seal"),
    ("task_proof", "TaskProof"),
    ("anchor", "AnalyzeTaskAnchor"),
    ("status", "Status"),
    ("delete", "DeleteReceipt"),
    ("child_owner", "ChildOwner"),
    ("cleanup", "WorkerCleanup"),
)
_NAMES = dict(_REGISTERED)
_KINDS = tuple(_NAMES)
_REALM = "6" * 32
_ID = "9" * 32
_HASH = "5" * 64
_KEY = "synthetic-receipt:é"
_MARKER = "SYNTHETIC-UNTRUSTED-RECEIPT-FIELD"
_SESSION = {"session_id": "synthetic-session", "thread_id": "synthetic-thread", "epoch": 1}
_FENCE = {"session": _SESSION, "source_revision": 1, "generation": 1, "lease_generation": 1}
_REF = {"backend_id": "a" * 32, "receipt_id": "b" * 32, "sha256": "c" * 64}
_NESTED_PATHS = {
    "source": ("reservation",),
    "upload_ack": ("source",),
    "seal": ("fence", "session"),
    "task_proof": ("owner_objective",),
    "anchor": ("owner_objective",),
    "status": ("children", 0, "owner"),
    "delete": ("source",),
    "child_owner": ("peer", "identity"),
    "cleanup": ("original_owner",),
}


def _json(value, **changes):
    options = {
        "ensure_ascii": False,
        "sort_keys": True,
        "separators": (",", ":"),
        "allow_nan": False,
    }
    options.update(changes)
    return json.dumps(value, **options).encode("utf-8")


def _hash_receipt(kind, envelope):
    body = {name: value for name, value in envelope.items() if name != "sha256"}
    domain = b"kinaiya.media.v1/R<" + _NAMES[kind].encode("ascii") + b">\0"
    return hashlib.sha256(domain + _json(body)).hexdigest()


def _source():
    return {
        "v": 1,
        "admission_id": "1" * 32,
        "source_id": "2" * 32,
        "source_equivalence_id": None,
        "source_revision": 1,
        "session": copy.deepcopy(_SESSION),
        "kind": "recording",
        "purpose": "analyze",
        "anchor": copy.deepcopy(_REF),
        "replaces_source": None,
        "capture_instance": None,
        "client_instance": "3" * 32,
        "scope": "local_only",
        "reservation": _reservation(),
        "decoder_profile_sha256": _HASH,
        "fence": copy.deepcopy(_FENCE),
        "lease_expires_at_ms": 12345,
        "raw_retain_until_ms": None,
    }


def _seal():
    return {
        "v": 1,
        "source": copy.deepcopy(_REF),
        "source_equivalence_id": "4" * 32,
        "fence": copy.deepcopy(_FENCE),
        "reservation_id": "1" * 32,
        "bytes": 3,
        "source_sha256": hashlib.sha256(b"abc").hexdigest(),
        "decoder_profile_sha256": _HASH,
        "state": "sealed",
    }


def _body(kind):
    builders = {
        "source": _source,
        "upload_ack": _ack,
        "seal": _seal,
        "task_proof": _proof,
        "anchor": _analyze_anchor,
        "status": _status,
        "delete": _delete,
        "child_owner": _child_owner,
        "cleanup": _worker,
    }
    return builders[kind]()


def _at(value, path):
    for field in path:
        value = value[field] if type(value) in (dict, list) else getattr(value, field)
    return value


def _envelope(kind, body=None):
    value = {
        "v": 1,
        "id": _ID,
        "key": _KEY,
        "request_sha256": _HASH,
        "body": _body(kind) if body is None else copy.deepcopy(body),
    }
    value["sha256"] = _hash_receipt(kind, value)
    return value


def _row(kind, envelope=None, *, raw=None):
    value = _envelope(kind) if envelope is None else envelope
    return {
        "receipt_id": value["id"],
        "kind": kind,
        "key": value["key"],
        "request_sha256": value["request_sha256"],
        "sha256": value["sha256"],
        "canonical_receipt": _json(value) if raw is None else raw,
        "owner_realm_id": _REALM,
        **_SESSION,
        "original_role": "owner",
        "original_instance": "7" * 32,
        "original_operation_id": "8" * 32,
    }


@pytest.fixture
def owner():
    value = MediaSessionOwner.__new__(MediaSessionOwner)
    value.owner_realm_id = _REALM
    return value


def _refused(owner, row, *, error=media.MediaContractError, code="bad_packet"):
    with pytest.raises(error) as failure:
        owner._read_receipt(row)
    assert type(failure.value) is error
    assert failure.value.code == code
    assert str(failure.value) == (
        "Invalid media contract"
        if error is media.MediaContractError
        else "Invalid native media operation"
    )
    assert _MARKER not in str(failure.value)


def _accepted(owner, kind, envelope):
    row = _row(kind, envelope)
    result = owner._read_receipt(row)
    body_kind = getattr(media, "Media" + _NAMES[kind])
    assert type(result) is media.MediaReceipt[body_kind]
    assert type(result.body) is body_kind
    assert media.media_bytes(result) == row["canonical_receipt"] == _json(envelope)
    assert result.sha256 == _hash_receipt(kind, envelope)
    assert media.media_digest("R<" + _NAMES[kind] + ">", result) == envelope["sha256"]
    return result


@pytest.mark.parametrize("kind", _KINDS)
def test_all_nine_native_receipt_kinds_preserve_exact_canonical_body_and_hash(owner, kind):
    _accepted(owner, kind, _envelope(kind))


@pytest.mark.parametrize("kind", _KINDS)
def test_stored_realm_and_every_receipt_identity_field_must_match(owner, kind):
    row = _row(kind)
    for field, replacement in (
        ("owner_realm_id", "0" * 32),
        ("receipt_id", "0" * 32),
        ("key", "different-native-key"),
        ("request_sha256", "0" * 64),
        ("sha256", "0" * 64),
    ):
        _refused(owner, {**row, field: replacement}, error=MediaSessionError, code="unavailable")


@pytest.mark.parametrize("kind", ["unregistered", "capture_ack", "CleanupReceipt"])
def test_public_or_unknown_kind_names_do_not_expand_the_native_receipt_registry(owner, kind):
    _refused(owner, {**_row("source"), "kind": kind}, error=MediaSessionError, code="unavailable")


@pytest.mark.parametrize("kind", _KINDS)
def test_valid_receipt_cannot_be_decoded_under_a_different_registered_body_kind(owner, kind):
    other = _KINDS[(_KINDS.index(kind) + 1) % len(_KINDS)]
    _refused(owner, {**_row(kind), "kind": other})


@pytest.mark.parametrize("kind", _KINDS)
def test_matching_row_hash_cannot_bless_an_invalid_receipt_self_hash(owner, kind):
    envelope = _envelope(kind)
    envelope["key"] = "changed-key-with-stale-envelope-hash"
    # Every SQL identity field matches this malformed envelope; only its full
    # receipt self-hash is stale. No row equality check can make it valid.
    _refused(owner, _row(kind, envelope))


@pytest.mark.parametrize("kind", _KINDS)
def test_correctly_rehashed_extra_fields_at_outer_body_and_nested_levels_are_refused(owner, kind):
    for level in ("outer", "body", "nested"):
        envelope = _envelope(kind)
        target = envelope if level == "outer" else envelope["body"]
        if level == "nested":
            target = _at(envelope["body"], _NESTED_PATHS[kind])
        target[_MARKER] = True
        envelope["sha256"] = _hash_receipt(kind, envelope)
        _refused(owner, _row(kind, envelope))


@pytest.mark.parametrize("kind", _KINDS)
def test_rehashed_body_discriminators_and_cross_field_relations_remain_strict(owner, kind):
    body = _body(kind)
    if kind == "source":
        body["kind"] = "live"  # Missing the explicit live capture/equivalence.
    elif kind == "upload_ack":
        body["next_offset"] += 1
    elif kind == "seal":
        body["state"] = "ready"
    elif kind == "task_proof":
        body["owner_objective"]["kind"] = "assistant_text"
    elif kind == "anchor":
        body["allowed_operation"] = "submit_task"
    elif kind == "status":
        body["remote_state"] = "unknown"  # Retained liability ref is missing.
    elif kind == "delete":
        body["local_spool_unlinked"] = False  # local_deleted must preserve all three proofs.
    elif kind == "child_owner":
        body["peer"]["kind"] = "caller_selected_peer"
    else:
        body["scope"] = "native_task"  # pidfd_reap cannot settle a native_task scope.
    _refused(owner, _row(kind, _envelope(kind, body)))


@pytest.mark.parametrize("kind", _KINDS)
def test_each_read_is_fresh_after_the_previous_typed_return_was_mutated(owner, kind):
    row = _row(kind)
    original_bytes = row["canonical_receipt"]
    first = owner._read_receipt(row)
    previous = owner._read_receipt(row)
    assert previous is not first and previous.body is not first.body
    previous.__dict__["sha256"] = "0" * 64
    previous.body.__dict__[_MARKER] = True
    _at(previous.body, _NESTED_PATHS[kind]).__dict__[_MARKER] = True
    with pytest.raises(media.MediaContractError):
        media.media_bytes(previous)
    current = owner._read_receipt(row)
    assert current is not previous and current.body is not previous.body
    assert media.media_bytes(current) == media.media_bytes(first) == original_bytes
    assert row["canonical_receipt"] == original_bytes


@pytest.mark.parametrize(
    "form", ["leading-space", "trailing-newline", "key-order", "unicode-escape"]
)
def test_valid_but_noncanonical_stored_json_is_not_silently_rewritten(owner, form):
    envelope = _envelope("upload_ack")
    raw = _json(envelope)
    if form == "leading-space":
        raw = b" " + raw
    elif form == "trailing-newline":
        raw += b"\n"
    elif form == "key-order":
        raw = _json(dict(reversed(list(envelope.items()))), sort_keys=False)
    else:
        raw = _json(envelope, ensure_ascii=True)
    assert raw != _json(envelope)
    _refused(
        owner, _row("upload_ack", envelope, raw=raw), error=MediaSessionError, code="unavailable"
    )


@pytest.mark.parametrize(
    "form",
    [
        "truncated",
        "utf8",
        "trailing-json",
        "array",
        "null",
        "duplicate-outer",
        "duplicate-nested",
        "nan",
        "bool-version",
        "surrogate",
    ],
)
def test_stored_malformed_duplicate_and_coerced_json_is_refused(owner, form):
    envelope = _envelope("upload_ack")
    raw = _json(envelope)
    if form == "truncated":
        raw = raw[:-1]
    elif form == "utf8":
        raw = b"\xff" + raw
    elif form == "trailing-json":
        raw += b"{}"
    elif form == "array":
        raw = b"[]"
    elif form == "null":
        raw = b"null"
    elif form == "duplicate-outer":
        raw = b'{"id":"' + _ID.encode() + b'",' + raw[1:]
    elif form == "duplicate-nested":
        raw = raw.replace(b'"index":0', b'"index":0,"index":0', 1)
    elif form == "nan":
        raw = raw.replace(b'"index":0', b'"index":NaN', 1)
    elif form == "bool-version":
        envelope["v"] = True
        envelope["sha256"] = _hash_receipt("upload_ack", envelope)
        raw = _json(envelope)
    else:
        raw = raw.replace(_KEY.encode(), b"\\ud800", 1)
    _refused(owner, _row("upload_ack", envelope, raw=raw))


@pytest.mark.parametrize("bound", ["bytes", "nodes", "depth"])
def test_ordinary_receipt_raw_resource_bounds_are_preserved(owner, bound):
    row = _row("upload_ack")
    if bound == "bytes":
        raw = b" " * (65537 - len(row["canonical_receipt"])) + row["canonical_receipt"]
    elif bound == "nodes":
        raw = b'{"bomb":[' + b",".join([b"0"] * 8192) + b"]}"
    else:
        raw = b'{"bomb":' + b"[" * 24 + b"0" + b"]" * 24 + b"}"
    _refused(owner, {**row, "canonical_receipt": raw}, code="bound")


@pytest.mark.parametrize("kind", ["source", "child_owner", "status", "delete", "cleanup"])
def test_required_explicit_nulls_are_not_omitted_from_canonical_receipt_bodies(owner, kind):
    body = _body(kind)
    fields = {
        "source": (
            "source_equivalence_id",
            "replaces_source",
            "capture_instance",
            "raw_retain_until_ms",
        ),
        "child_owner": ("consumer",),
        "status": ("remote_liability_head",),
        "delete": ("remote_liability_head",),
        "cleanup": ("outcome",),
    }[kind]
    if kind == "cleanup":
        body.update(
            state="unknown",
            outcome=None,
            proof="none",
            publication="unknown",
            scope="external_unknown",
        )
    result = _accepted(owner, kind, _envelope(kind, body))
    decoded = json.loads(media.media_bytes(result))["body"]
    for field in fields:
        assert field in decoded and decoded[field] is None
        missing = copy.deepcopy(body)
        del missing[field]
        _refused(owner, _row(kind, _envelope(kind, missing)))


@pytest.mark.parametrize(
    "variant",
    [
        "live-owner",
        "media-proof",
        "media-anchor",
        "native-task-peer",
        "unknown-worker",
        "remote-status",
        "pending-delete",
    ],
)
def test_closed_discriminated_and_unknown_variants_preserve_their_exact_metadata(owner, variant):
    kind = {
        "live-owner": "source",
        "media-proof": "task_proof",
        "media-anchor": "anchor",
        "native-task-peer": "child_owner",
        "unknown-worker": "cleanup",
        "remote-status": "status",
        "pending-delete": "delete",
    }[variant]
    body = _body(kind)
    if variant == "live-owner":
        body.update(
            kind="live",
            purpose="owner_voice",
            anchor=None,
            source_equivalence_id="4" * 32,
            capture_instance="5" * 32,
        )
    elif variant in ("media-proof", "media-anchor"):
        body["owner_objective"] = _objective("owner_media")
        if variant == "media-proof":
            body["state"] = "unknown"
    elif variant == "native-task-peer":
        body["peer"] = {
            "kind": "native_task",
            "backend_instance": "1" * 32,
            "task_id": "2" * 32,
            "request_sha256": _HASH,
        }
    elif variant == "unknown-worker":
        body.update(
            state="unknown",
            outcome=None,
            proof="none",
            publication="unknown",
            scope="external_unknown",
        )
    else:
        body.update(remote_state="unknown", remote_liability_head=copy.deepcopy(_REF))
        if variant == "pending-delete":
            body.update(
                state="local_delete_pending",
                local_children_settled=False,
                local_spool_unlinked=False,
                local_processing_reservation_released=False,
            )
    _accepted(owner, kind, _envelope(kind, body))


def test_native_cleanup_kind_does_not_accept_the_wider_public_cleanup_alias(owner):
    exchange = _exchange()
    assert (
        type(media.media_json(_json(exchange), media.MediaLocalExchangeCleanup))
        is media.MediaLocalExchangeCleanup
    )
    _refused(owner, _row("cleanup", _envelope("cleanup", exchange)))
