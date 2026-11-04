"""Independent metadata oracles for frozen Session media DTOs.

Authored before the new contract/storage bodies were read. Synthetic NativeRefs,
cleanup flags and receipts here establish grammar only, never actual authority,
native observations, provider/media capability or physical cleanup.
"""

import base64
import copy
import hashlib
import json

import pytest

from packages.contracts import media

_MAX = 9_007_199_254_740_991
_ID = "1" * 32
_HASH = "2" * 64
_SOURCE = {"backend_id": "a" * 32, "receipt_id": "b" * 32, "sha256": "c" * 64}
_OWNER = {"backend_id": "a" * 32, "receipt_id": "d" * 32, "sha256": "e" * 64}
_SESSION = {"session_id": "synthetic-session", "thread_id": "synthetic-thread", "epoch": 1}
_FENCE = {"session": _SESSION, "source_revision": 1, "generation": 1, "lease_generation": 1}
_LOOKUP_KINDS = (
    "source",
    "upload_ack",
    "capture_ack",
    "seal",
    "capture_ready",
    "decode_ready",
    "decode_end",
    "decode_budget",
    "input_end",
    "task_proof",
    "anchor",
    "owner_arming",
    "arming_revocation",
    "utterance_lineage",
    "intent",
    "consumer",
    "range",
    "blob",
    "record",
    "manifest_page",
    "manifest_seal",
    "word_page",
    "alignment_page",
    "speaker",
    "derived_source",
    "reply",
    "native_duplex_timing",
    "decision",
    "evidence",
    "text_bridge",
    "admission_request",
    "action",
    "native_admission",
    "dependency_head",
    "delivery",
    "apply_head",
    "agent_owner",
    "dispatcher_owner",
    "agent_observation",
    "dispatcher_observation",
    "delivery_retirement",
    "admission_retirement",
    "supervisor",
    "asr",
    "asr_backend_state",
    "stream_entry",
    "producer_set_page",
    "publication_plan",
    "producer_watermark",
    "watermark_page",
    "stream_seal",
    "stream_snapshot",
    "local_exchange_owner",
    "local_close_observation",
    "remote_liability",
    "child_owner",
    "cleanup",
    "status",
    "delete",
    "check",
    "qualification_summary",
    "artifact",
)


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(name, value):
    return _sha(b"kinaiya.media.v1/" + name.encode("ascii") + b"\0" + _json(value))


def _kind(name):
    return getattr(media, "Media" + name)


def _valid(name, value):
    result = media.media_json(_json(value), _kind(name))
    assert media.media_bytes(result) == _json(value)
    assert media.media_digest(name, value) == _digest(name, value)
    assert media.media_digest(name, result) == _digest(name, value)
    return result


def _reject(name, value, code="bad_packet"):
    with pytest.raises(media.MediaContractError) as failure:
        media.media_json(_json(value), _kind(name))
    assert isinstance(failure.value, ValueError)
    assert failure.value.code == code
    assert str(failure.value) == "Invalid media contract"


def _reject_typed(value, code="bad_packet"):
    with pytest.raises(media.MediaContractError) as failure:
        media.media_bytes(value)
    assert failure.value.code == code


def _objective(kind="owner_text"):
    return (
        {"kind": "owner_text", "event_ref": "authenticated-owner-event"}
        if kind == "owner_text"
        else {"kind": "owner_media", "intent": copy.deepcopy(_OWNER)}
    )


def _task_bytes(size=None, **changes):
    # All fields/default behavior are the original TaskRequest's. Strings are
    # synthetic, and each source reference remains at most128UTF8bytes.
    task = {
        "schema_version": "1.0",
        **_SESSION,
        "objective": "Synthetic owner objective.",
        "content_scope": "local_only",
        "idempotency_key": "original-task-key",
        "source_event_ids": [f"{i:02d}-" + "r" * 125 for i in range(32)],
        "source_revision": 1,
        **changes,
    }
    if size is not None:
        task["objective"] = ""
        remaining = size - len(_json(task))
        assert remaining > 0
        task["objective"] = "😀" * (remaining // 4) + "x" * (remaining % 4)
        assert 0 < len(task["objective"]) <= 16000
        assert len(_json(task)) == size
    return _json(task)


def _legacy(raw=None):
    raw = _task_bytes() if raw is None else raw
    return {
        "contract": "TaskRequest",
        "canonical_utf8_b64": base64.b64encode(raw).decode("ascii"),
        "sha256": _sha(raw),
    }


def _anchor(*, inline=False, raw=None):
    return {
        "v": 1,
        "key": "anchor-key",
        "owner_objective": _objective(),
        "scope": "local_only",
        "submit": _legacy(raw) if inline else None,
        "task_admission": None if inline else copy.deepcopy(_OWNER),
    }


def _proof():
    return {
        "v": 1,
        "session": copy.deepcopy(_SESSION),
        "task_id": "native-task",
        "objective_revision": 1,
        "owner_objective": _objective(),
        "request_sha256": _HASH,
        "original_admission_id": "native-original-submit-event",
        "state": "accepted",
    }


def _analyze_anchor():
    return {
        "v": 1,
        "owner_objective": _objective(),
        "session": copy.deepcopy(_SESSION),
        "task_id": "native-task",
        "initial_objective_revision": 1,
        "task_admission": copy.deepcopy(_OWNER),
        "scope": "local_only",
        "allowed_operation": "evidence_only",
    }


def _peer(kind="local_process"):
    if kind == "local_process":
        return {
            "kind": kind,
            "identity": {
                "pid": 123,
                "start_tick": 7,
                "boot_id": "3" * 32,
                "nonce": "4" * 32,
                "pidfd_owner": "5" * 32,
            },
        }
    if kind == "native_task":
        return {
            "kind": kind,
            "backend_instance": "6" * 32,
            "task_id": "7" * 32,
            "request_sha256": _HASH,
        }
    if kind == "owned_worker":
        return {"kind": kind, "instance": "6" * 32, "request_sha256": _HASH}
    if kind == "client_playback":
        return {"kind": kind, "client_instance": "6" * 32, "generation_epoch": 1}
    return {"kind": "external_transport", "exchange_id": "6" * 32}


def _child_owner(*, index=0, kind="decoder", peer=None):
    return {
        "v": 1,
        "kind": kind,
        "instance": f"{index + 1:032x}",
        "operation_id": _ID,
        "source": copy.deepcopy(_SOURCE),
        "consumer": None,
        "binding_sha256": _HASH,
        "backend_id": _SOURCE["backend_id"],
        "peer": _peer() if peer is None else peer,
    }


def _child(*, index=0, state="running"):
    settled = state == "settled"
    return {
        "kind": "decoder",
        "instance": f"{index + 1:032x}",
        "operation_id": _ID,
        "owner": {**_OWNER, "receipt_id": f"{index + 1:032x}"},
        "binding_sha256": _HASH,
        "state": state,
        "outcome": "completed" if settled else None,
        "cleanup": copy.deepcopy(_OWNER) if settled else None,
    }


def _worker(*, scope="local_process", proof="pidfd_reap", state="settled"):
    return {
        "v": 1,
        "kind": "native_owner",
        "operation_id": _ID,
        "child_instance": "3" * 32,
        "binding_sha256": _HASH,
        "original_owner": copy.deepcopy(_OWNER),
        "state": state,
        "outcome": "completed" if state == "settled" else None,
        "proof": proof,
        "publication": "settled" if state == "settled" else "unknown",
        "scope": scope,
    }


def _exchange(*, state="settled"):
    settled = state == "settled"
    return {
        "v": 1,
        "kind": "local_exchange",
        "operation_id": _ID,
        "child_instance": "3" * 32,
        "binding_sha256": _HASH,
        "original_owner": copy.deepcopy(_OWNER),
        "exchange_owner": copy.deepcopy(_SOURCE),
        "state": state,
        "outcome": "completed" if settled else None,
        "local_task_joined": settled,
        "transport_sole_closed": settled,
        "body_fd_closed": settled,
        "all_readers_settled": settled,
        "publications_settled": settled,
        "observations": [
            {**_OWNER, "receipt_id": f"{i + 1:032x}"} for i in range(5 if settled else 1)
        ],
        "remote_liability": copy.deepcopy(_SOURCE),
    }


def _reservation():
    return {
        "reservation_id": _ID,
        "owner_spool_bytes": 6,
        "global_spool_bytes": 6,
        "metadata_bytes": 4096,
        "child_slots": 2,
        "policy_sha256": _HASH,
    }


def _status(*, children=None):
    return {
        "v": 1,
        "source": copy.deepcopy(_SOURCE),
        "fence": copy.deepcopy(_FENCE),
        "desired": "running",
        "local_actual": "running",
        "children": [_child()] if children is None else children,
        "remote_state": "none",
        "remote_liability_head": None,
        "reason": "none",
        "reservation": _reservation(),
        "raw_state": "buffered",
    }


def _control(command="stop"):
    return {
        "v": 1,
        "key": "control-key",
        "source": copy.deepcopy(_SOURCE),
        "fence": copy.deepcopy(_FENCE),
        "command": command,
        "position": {"num": 0, "den": 1} if command == "seek" else None,
    }


def _delete(*, deleted=True):
    return {
        "v": 1,
        "source": copy.deepcopy(_SOURCE),
        "fence": copy.deepcopy(_FENCE),
        "state": "local_deleted" if deleted else "local_delete_pending",
        "local_children_settled": deleted,
        "local_spool_unlinked": deleted,
        "local_processing_reservation_released": deleted,
        "remote_state": "none",
        "remote_liability_head": None,
    }


def _append(value):
    return {
        "v": 1,
        "key": "original-native-append",
        "consumer": None,
        "source": copy.deepcopy(_SOURCE),
        "fence": copy.deepcopy(_FENCE),
        "value": value,
    }


def _vectors():
    return {
        "OwnerObjective": _objective(),
        "TaskProofRequest": {
            "v": 1,
            "key": "proof-key",
            "session": copy.deepcopy(_SESSION),
            "original_admission_id": "original-submit-event",
        },
        "TaskProof": _proof(),
        "AnalyzeTaskAnchor": _analyze_anchor(),
        "LegacyTaskBody": _legacy(),
        "AnchorAdmission": _anchor(),
        "ProcessIdentity": _peer()["identity"],
        "Peer": _peer(),
        "ChildOwner": _child_owner(),
        "Child": _child(),
        "WorkerCleanup": _worker(),
        "LocalExchangeCleanup": _exchange(),
        "CleanupReceipt": _worker(),
        "Status": _status(),
        "Control": _control(),
        "DeleteRequest": {
            "v": 1,
            "key": "delete-key",
            "source": copy.deepcopy(_SOURCE),
            "fence": copy.deepcopy(_FENCE),
        },
        "DeleteReceipt": _delete(),
        "Lookup": {"v": 1, "kind": "source", "ref": copy.deepcopy(_SOURCE)},
        "KeyLookup": {"v": 1, "kind": "source", "key": "original-key", "request_sha256": _HASH},
        "ChildOwnerAppend": _append(_child_owner()),
        "WorkerCleanupAppend": _append(_worker()),
        "StatusAppend": _append(_status()),
    }


_NAMES = (
    "OwnerObjective",
    "TaskProofRequest",
    "TaskProof",
    "AnalyzeTaskAnchor",
    "LegacyTaskBody",
    "AnchorAdmission",
    "ProcessIdentity",
    "Peer",
    "ChildOwner",
    "Child",
    "WorkerCleanup",
    "LocalExchangeCleanup",
    "CleanupReceipt",
    "Status",
    "Control",
    "DeleteRequest",
    "DeleteReceipt",
    "Lookup",
    "KeyLookup",
    "ChildOwnerAppend",
    "WorkerCleanupAppend",
    "StatusAppend",
)


@pytest.mark.parametrize("name", _NAMES)
def test_manual_closed_vectors_roundtrip_and_exact_hash_domain(name):
    _valid(name, _vectors()[name])


@pytest.mark.parametrize("name", _NAMES)
def test_every_declared_outer_field_required_and_unknown_field_refused(name):
    raw = _vectors()[name]
    for field in raw:
        missing = {key: value for key, value in raw.items() if key != field}
        _reject(name, missing)
    _reject(name, {**raw, "untrusted_extra": "cannot grant authority"})


@pytest.mark.parametrize("kind", ["owner_text", "owner_media"])
def test_owner_objective_exact_variants(kind):
    raw = _objective(kind)
    _valid("OwnerObjective", raw)
    mixed = {**raw, **({"intent": _SOURCE} if kind == "owner_text" else {"event_ref": "a quote"})}
    _reject("OwnerObjective", mixed)
    _reject("OwnerObjective", {**raw, "kind": "transcript"})


@pytest.mark.parametrize("state", ["accepted", "unknown"])
def test_task_proof_states_are_metadata_not_execution(state):
    _valid("TaskProof", {**_proof(), "state": state})
    _reject("TaskProof", {**_proof(), "state": "completed"})


@pytest.mark.parametrize("field", ["submit", "task_admission"])
def test_anchor_exactly_one_original_submit_or_proof(field):
    _valid("AnchorAdmission", _anchor(inline=field == "submit"))
    neither = {**_anchor(), "submit": None, "task_admission": None}
    both = {**_anchor(), "submit": _legacy()}
    _reject("AnchorAdmission", neither)
    _reject("AnchorAdmission", both)


def test_inline_requires_text_objective_but_existing_media_proof_is_representable():
    _reject(
        "AnchorAdmission", {**_anchor(inline=True), "owner_objective": _objective("owner_media")}
    )
    _valid("AnchorAdmission", {**_anchor(), "owner_objective": _objective("owner_media")})
    _reject("AnalyzeTaskAnchor", {**_analyze_anchor(), "allowed_operation": "submit"})


def test_legacy_keeps_original_canonical_bytes_and_old_defaults_and_coercion():
    # TaskRequest already permits omitted defaults and int-string coercion.
    # New strict metadata must not rewrite the original legacy request bytes.
    minimal = {**_SESSION, "objective": "Synthetic.", "idempotency_key": "old-key"}
    for epoch in (1, "1"):
        raw = _json({**minimal, "epoch": epoch})
        legacy = _legacy(raw)
        parsed = _valid("LegacyTaskBody", legacy)
        assert parsed.canonical_utf8_b64 == legacy["canonical_utf8_b64"]
        assert parsed.sha256 == _sha(raw)


@pytest.mark.parametrize(
    "failure",
    [
        "hash",
        "contract",
        "base64",
        "json",
        "noncanonical",
        "duplicate",
        "extra_task",
        "objective_bound",
    ],
)
def test_legacy_exact_raw_hash_canonical_json_and_unchanged_task_validation(failure):
    raw = _task_bytes()
    if failure == "json":
        raw = b"not-json"
    elif failure == "noncanonical":
        raw = json.dumps(json.loads(raw), indent=1).encode()
    elif failure == "duplicate":
        raw = raw[:-1] + b',"epoch":1}'
    elif failure == "extra_task":
        raw = _json({**json.loads(raw), "owner_authority": True})
    elif failure == "objective_bound":
        raw = _task_bytes(objective="x" * 16001)
    legacy = _legacy(raw)
    if failure == "hash":
        legacy["sha256"] = "0" * 64
    elif failure == "contract":
        legacy["contract"] = "Task"
    elif failure == "base64":
        legacy["canonical_utf8_b64"] = "not@base64"
    _reject("LegacyTaskBody", legacy, "bound" if failure == "objective_bound" else "bad_packet")


def test_exact_decoded_64k_is_allowed_only_inside_inline_anchor_envelope():
    raw = _task_bytes(65536)
    assert len(raw) == 65536
    legacy = _legacy(raw)
    assert len(legacy["canonical_utf8_b64"]) == 87384
    anchor = _anchor(inline=True, raw=raw)
    assert 65536 < len(_json(anchor)) <= 262144
    parsed = _valid("AnchorAdmission", anchor)
    assert parsed.submit.canonical_utf8_b64 == legacy["canonical_utf8_b64"]
    _reject("LegacyTaskBody", legacy, "bound")
    _reject_typed(parsed.submit, "bound")
    over = _task_bytes(65537)
    assert len(_legacy(over)["canonical_utf8_b64"]) == 87384
    _reject("AnchorAdmission", _anchor(inline=True, raw=over), "bound")


def _receipt_raw(name, body):
    raw = {
        "v": 1,
        "id": _ID,
        "key": body.get("key", "original-receipt-key"),
        "request_sha256": _HASH,
        "body": copy.deepcopy(body),
    }
    raw["sha256"] = _digest("R<" + name + ">", raw)
    return raw


def test_exact_receipt_and_found_inline_anchor_keep_the_scoped_large_exception():
    anchor = _anchor(inline=True, raw=_task_bytes(65536))
    receipt = _receipt_raw("AnchorAdmission", anchor)
    kind = media.MediaReceipt[media.MediaAnchorAdmission]
    parsed = media.media_json(_json(receipt), kind)
    assert media.media_bytes(parsed) == _json(receipt)
    assert media.media_digest("R<AnchorAdmission>", receipt) == receipt["sha256"]
    found = {"v": 1, "found": True, "value": receipt}
    result = media.media_json(_json(found), media.MediaFound[media.MediaAnchorAdmission])
    assert media.media_bytes(result) == _json(found)
    assert 65536 < len(_json(found)) <= 262144


@pytest.mark.parametrize(
    "failure",
    [
        "outer_bytes",
        "metadata_bytes",
        "metadata_depth",
        "metadata_nodes",
        "outer_depth",
        "outer_nodes",
    ],
)
def test_inline_exception_never_removes_its_global_or_ordinary_metadata_bounds(failure):
    anchor = _anchor(inline=True, raw=_task_bytes(65536))
    if failure in {"outer_bytes", "metadata_bytes"}:
        anchor["untrusted_extra"] = "x" * (262144 if failure == "outer_bytes" else 65536)
    elif failure in {"metadata_depth", "outer_depth"}:
        nested = None
        for _ in range(25 if failure == "metadata_depth" else 33):
            nested = [nested]
        anchor["untrusted_extra"] = nested
    else:
        anchor["untrusted_extra"] = [0] * (8192 if failure == "metadata_nodes" else 32768)
    _reject("AnchorAdmission", anchor, "bound")


@pytest.mark.parametrize(
    "kind",
    ["local_process", "native_task", "owned_worker", "client_playback", "external_transport"],
)
def test_peer_discriminants_are_closed_and_do_not_accept_borrowed_identity(kind):
    raw = _peer(kind)
    _valid("Peer", raw)
    _reject("Peer", {**raw, "kind": "process"})
    _reject("Peer", {**raw, "unbound_socket": "/synthetic/not-authority"})
    for field in raw:
        _reject("Peer", {key: value for key, value in raw.items() if key != field})


@pytest.mark.parametrize(
    "kind",
    [
        "upload",
        "decoder",
        "converse_provider",
        "decisions_provider",
        "input_asr",
        "assistant_asr",
        "tts_provider",
        "playback",
        "agent_media",
        "agent_delivery",
    ],
)
def test_child_owner_kind_vocabulary_is_exact(kind):
    _valid("ChildOwner", _child_owner(kind=kind))


@pytest.mark.parametrize(
    "state", ["reserved", "running", "fenced", "closing", "settled", "unknown"]
)
def test_child_state_has_no_implicit_settlement(state):
    raw = _child(state=state)
    _valid("Child", raw)
    if state == "settled":
        _reject("Child", {**raw, "outcome": None})
        _reject("Child", {**raw, "cleanup": None})


@pytest.mark.parametrize(
    "scope,proof",
    [
        ("native_task", "local_join"),
        ("local_process", "pidfd_reap"),
        ("owned_worker", "owned_worker_receipt"),
        ("client_playback", "client_playback"),
    ],
)
def test_worker_settled_proof_matches_the_declared_local_scope(scope, proof):
    raw = _worker(scope=scope, proof=proof)
    _valid("WorkerCleanup", raw)
    _valid("CleanupReceipt", raw)
    _reject("WorkerCleanup", {**raw, "outcome": None})
    _reject("WorkerCleanup", {**raw, "publication": "unknown"})
    wrong = "pidfd_reap" if proof != "pidfd_reap" else "local_join"
    _reject("WorkerCleanup", {**raw, "proof": wrong})


@pytest.mark.parametrize("proof", ["none", "transport_only"])
def test_bare_transport_or_absent_proof_cannot_settle_a_worker(proof):
    _reject("WorkerCleanup", _worker(proof=proof))
    _reject("WorkerCleanup", _worker(scope="external_unknown", proof=proof))
    unknown = _worker(scope="external_unknown", proof=proof, state="unknown")
    _valid("WorkerCleanup", unknown)
    _reject("WorkerCleanup", {**unknown, "outcome": "completed"})


@pytest.mark.parametrize(
    "field",
    [
        "local_task_joined",
        "transport_sole_closed",
        "body_fd_closed",
        "all_readers_settled",
        "publications_settled",
    ],
)
def test_local_exchange_requires_every_local_settlement_component(field):
    raw = _exchange()
    _valid("LocalExchangeCleanup", raw)
    _valid("CleanupReceipt", raw)
    _reject("LocalExchangeCleanup", {**raw, field: False})
    _reject("LocalExchangeCleanup", {**raw, field: "true"})
    _reject("LocalExchangeCleanup", {**raw, "outcome": None})


def test_local_exchange_has_its_own_variant_and_bounded_observation_refs():
    _valid("LocalExchangeCleanup", _exchange(state="unknown"))
    raw = _exchange()
    raw["observations"] = [{**_OWNER, "receipt_id": f"{i + 1:032x}"} for i in range(64)]
    _valid("LocalExchangeCleanup", raw)
    _reject(
        "LocalExchangeCleanup", {**raw, "observations": raw["observations"] + [_SOURCE]}, "bound"
    )
    _reject("WorkerCleanup", _exchange())
    _reject("LocalExchangeCleanup", _worker())


def test_status_active_children_are_bounded_without_a_lifetime_history_cap():
    raw = _status(children=[_child(index=i) for i in range(16)])
    roles = ["input_asr"] * 8 + [
        "assistant_asr",
        "converse_provider",
        "decisions_provider",
        "playback",
        "agent_media",
        "agent_delivery",
        "upload",
        "decoder",
    ]
    for child, role in zip(raw["children"], roles, strict=True):
        child["kind"] = role
    raw["reservation"]["child_slots"] = 16
    _valid("Status", raw)
    _reject("Status", {**raw, "children": raw["children"] + [_child(index=16)]}, "bound")
    # Historical receipts are independent immutable records, not another
    # simultaneous Status child or proof that any actual child retired.
    for index in range(33):
        _valid("ChildOwnerAppend", _append(_child_owner(index=index)))
        _valid("Child", _child(index=index, state="settled"))


@pytest.mark.parametrize("command", ["start", "heartbeat", "stop", "seek", "reset", "resume"])
def test_control_position_is_exactly_seek_only(command):
    raw = _control(command)
    _valid("Control", raw)
    _reject("Control", {**raw, "position": None if command == "seek" else {"num": 0, "den": 1}})
    _reject("Control", {**raw, "command": "restart"})


@pytest.mark.parametrize(
    "field",
    ["local_children_settled", "local_spool_unlinked", "local_processing_reservation_released"],
)
def test_local_deleted_requires_all_local_boolean_observations(field):
    _reject("DeleteReceipt", {**_delete(), field: False})
    _reject("DeleteReceipt", {**_delete(), field: 1})
    _valid("DeleteReceipt", _delete(deleted=False))


@pytest.mark.parametrize("name", ["Status", "DeleteReceipt"])
def test_remote_unknown_does_not_claim_erasure_or_block_truthful_local_deletion(name):
    raw = _status() if name == "Status" else _delete()
    raw.update(remote_state="unknown", remote_liability_head=copy.deepcopy(_OWNER))
    _valid(name, raw)
    _reject(name, {**raw, "remote_liability_head": None})
    _reject(name, {**raw, "remote_state": "none"})


@pytest.mark.parametrize("kind", _LOOKUP_KINDS)
def test_complete_lookup_vocabulary_is_representable_without_enabling_routes(kind):
    _valid("Lookup", {"v": 1, "kind": kind, "ref": copy.deepcopy(_SOURCE)})
    _valid("KeyLookup", {"v": 1, "kind": kind, "key": "original-key", "request_sha256": _HASH})


@pytest.mark.parametrize(
    "kind",
    [
        "source_profile",
        "cleanup_any",
        "owner_objective",
        "native_owner",
        "Status",
        "",
        "source/../secret",
    ],
)
def test_lookup_rejects_unregistered_prefixes_aliases_and_paths(kind):
    _reject("Lookup", {"v": 1, "kind": kind, "ref": _SOURCE})
    _reject("KeyLookup", {"v": 1, "kind": kind, "key": "original-key", "request_sha256": _HASH})


@pytest.mark.parametrize(
    "name,other",
    [
        ("ChildOwnerAppend", "WorkerCleanupAppend"),
        ("WorkerCleanupAppend", "StatusAppend"),
        ("StatusAppend", "ChildOwnerAppend"),
    ],
)
def test_native_append_has_exact_envelope_and_fixed_value_type(name, other):
    raw = _vectors()[name]
    _valid(name, raw)
    _reject(name, {**raw, "kind": "caller-selects-authority"})
    _reject(name, {**raw, "value": _vectors()[other]["value"]})
    for field in ("consumer", "source", "fence"):
        _reject(name, {key: value for key, value in raw.items() if key != field})


@pytest.mark.parametrize(
    "name",
    [
        "TaskProof",
        "AnalyzeTaskAnchor",
        "Control",
        "Status",
        "DeleteRequest",
        "DeleteReceipt",
        "ChildOwnerAppend",
        "WorkerCleanupAppend",
        "StatusAppend",
        "OwnerObjective",
        "Peer",
        "CleanupReceipt",
    ],
)
def test_receipt_and_found_domains_keep_nested_hashes_and_exact_alias_types(name):
    body = _vectors()[name]
    if name == "OwnerObjective":
        body = _objective("owner_media")
    receipt = _receipt_raw(name, body)
    receipt_kind = media.MediaReceipt[_kind(name)]
    parsed = media.media_json(_json(receipt), receipt_kind)
    assert media.media_bytes(parsed) == _json(receipt)
    assert media.media_digest("R<" + name + ">", receipt) == receipt["sha256"]
    assert (
        media.media_digest(
            "R<" + name + ">", {key: value for key, value in receipt.items() if key != "sha256"}
        )
        == receipt["sha256"]
    )
    found = {"v": 1, "found": True, "value": receipt}
    decoded = media.media_json(_json(found), media.MediaFound[_kind(name)])
    assert media.media_bytes(decoded) == _json(found)
    broken = copy.deepcopy(receipt)
    broken["body"]["untrusted_extra"] = None
    with pytest.raises(media.MediaContractError):
        media.media_json(_json(broken), receipt_kind)
    if name == "OwnerObjective":
        changed = copy.deepcopy(receipt)
        changed["body"]["intent"]["sha256"] = "0" * 64
        with pytest.raises(media.MediaContractError):
            media.media_json(_json(changed), receipt_kind)


@pytest.mark.parametrize(
    "name",
    [
        "TaskProof",
        "AnalyzeTaskAnchor",
        "Control",
        "Status",
        "DeleteRequest",
        "DeleteReceipt",
        "ChildOwnerAppend",
        "WorkerCleanupAppend",
        "StatusAppend",
    ],
)
def test_typed_encoder_revalidates_extra_missing_and_hostile_wrong_fields(name):
    value = media.media_json(_json(_vectors()[name]), _kind(name))
    added = value.model_copy(update={"untrusted_extra": "not registered"}, deep=True)
    _reject_typed(added)
    missing = value.model_copy(deep=True)
    object.__delattr__(missing, "v")
    _reject_typed(missing)
    wrong = value.model_copy(update={"v": True}, deep=True)
    _reject_typed(wrong)


@pytest.mark.parametrize(
    "name,field",
    [
        ("TaskProof", "objective_revision"),
        ("AnalyzeTaskAnchor", "initial_objective_revision"),
        ("ProcessIdentity", "pid"),
    ],
)
def test_native_integer_fields_preserve_exact_bounds_and_reject_bool(name, field):
    raw = _vectors()[name]
    _valid(name, {**raw, field: _MAX})
    _reject(name, {**raw, field: _MAX + 1}, "bound")
    _reject(name, {**raw, field: 0}, "bound")
    _reject(name, {**raw, field: True})
    _reject(name, {**raw, field: "1"})


def test_metadata_utf8_caps_do_not_count_codepoints_only():
    _valid("TaskProof", {**_proof(), "task_id": "😀" * 32})
    _reject("TaskProof", {**_proof(), "task_id": "😀" * 33}, "bound")
    _valid("Control", {**_control(), "key": "😀" * 64})
    _reject("Control", {**_control(), "key": "😀" * 65}, "bound")


def test_typed_status_oversized_children_is_refused_before_touching_hostile_last_child():
    class Hostile:
        def __getattribute__(self, _name):
            raise AssertionError("An overbound declared child must not be inspected")

    status = media.media_json(_json(_status()), media.MediaStatus)
    corrupted = status.model_copy(update={"children": [0] * 8192 + [Hostile()]}, deep=False)
    _reject_typed(corrupted, "bound")
