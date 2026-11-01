"""Independent #199 oracles for the bounded pure-contract revision.

The original contract40 and client fixtures remain frozen. Production imports
are lazy so a missing implementation is a capability failure during execution.
"""

import copy
import hashlib
import importlib
import json

import pytest

from packages.contracts.models import AppendRequest, JournalEntry

REQUEST_LIMIT = 65536
RESPONSE_LIMIT = 69632
TASK = "supplement-proposal"
WINNER = "supplement-winner"


def _contract():
    try:
        return importlib.import_module("packages.contracts.agent_submit")
    except ModuleNotFoundError as exc:
        if exc.name != "packages.contracts.agent_submit":
            raise
        pytest.fail("Pure Agent submit contract is absent (capability baseline)")


def _wire(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )


def _logical(payload):
    value = {
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
    value["content_scope"] = payload["requested_content_scope"]
    if payload["explicit_skill"] is not None:
        value["explicit_skill"] = payload["explicit_skill"]
    return value


def _digest(payload):
    return hashlib.sha256(
        json.dumps(_logical(payload), sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _proposal_data(*, task_id=TASK, objective="Synthetic supplemental submit.", plugin_count=0):
    payload = {
        "task_id": task_id,
        "state": "accepted",
        "op": "submit",
        "actor": "owner",
        "session_id": "supplement-session",
        "thread_id": "supplement-thread",
        "epoch": 1,
        "objective": objective,
        "objective_revision": 1,
        "source_event_ids": ["supplement-source"],
        "source_revision": 1,
        "requested_content_scope": "local_only",
        "idempotency_key": "supplement-client-key",
        "harness_version": "seed-v1",
        "model_pin": {
            "schema_version": "1.0",
            "profile_id": "supplement-profile",
            "provider": "synthetic",
            "model": "supplement-model",
            "local": True,
            "reasoning": None,
            "settings_revision": 0,
        },
        "explicit_skill": None,
        "plugin_versions": {f"plugin-{index:03}": "v" * 128 for index in range(plugin_count)},
        "content_scope": "local_only",
    }
    payload["payload_digest"] = _digest(payload)
    return {
        "schema_version": "1.0",
        "session_id": payload["session_id"],
        "thread_id": payload["thread_id"],
        "epoch": payload["epoch"],
        "kind": "agent.command",
        "payload": payload,
        "causes": ["supplement-cause"],
        "task_refs": [task_id],
        "idempotency_key": f"agent:{task_id}:agent.command:1",
    }


def _proposal(**kwargs):
    return AppendRequest.model_validate(_proposal_data(**kwargs))


def _entry_data(body):
    return {
        **body.model_dump(mode="json"),
        "event_id": "supplement-native-event",
        "sequence": 1,
        "origin": "agent-supervisor",
        "created_at": "2026-10-31T00:00:00Z",
    }


def _receipt_data(body, disposition="created"):
    return {"schema_version": "1.0", "disposition": disposition, "entry": _entry_data(body)}


def _lookup_data(body):
    return {
        "schema_version": "1.0",
        "session_id": body.session_id,
        "thread_id": body.thread_id,
        "epoch": body.epoch,
        "idempotency_key": body.payload["idempotency_key"],
    }


def test_objective_nul_is_valid_utf8_data_with_the_canonical_escaped_digest():
    raw = _proposal_data(objective="Synthetic café\x00after ☃.")
    assert raw["payload"]["payload_digest"] == (
        "3dd6d3466975ccb43b211c755b352101b1ccfee70d9ac648ed57b324673e00ce"
    )
    assert json.loads(_wire(raw))["payload"]["objective"] == raw["payload"]["objective"]
    before = copy.deepcopy(raw)
    module = _contract()
    decoded = module.agent_submit_json(_wire(raw))
    validated = module.submit_request(decoded)
    assert validated.model_dump(mode="json") == before
    receipt_data = _receipt_data(validated)
    receipt = module.AgentSubmitReceipt.model_validate(receipt_data).validate_request(validated)
    assert receipt.entry.model_dump(mode="json") == receipt_data["entry"]
    assert raw == before


@pytest.mark.parametrize(
    "field",
    ["session_id", "thread_id", "task_id", "source_event_ids", "causes", "event_id", "logical_key"],
)
def test_identifier_nul_still_refuses_after_objective_nul_is_allowed(field):
    good = _proposal()
    raw = good.model_dump(mode="json")
    bad_id = "supplement\x00identifier"
    if field in {"session_id", "thread_id"}:
        raw[field] = raw["payload"][field] = bad_id
    elif field == "task_id":
        raw["payload"][field] = bad_id
        raw["task_refs"] = [bad_id]
        raw["idempotency_key"] = f"agent:{bad_id}:agent.command:1"
    elif field == "source_event_ids":
        raw["payload"][field] = [bad_id]
    elif field == "causes":
        raw[field] = [bad_id]
    elif field == "logical_key":
        raw["payload"]["idempotency_key"] = bad_id
    raw["payload"]["payload_digest"] = _digest(raw["payload"])
    entry = _entry_data(AppendRequest.model_validate(raw))
    if field == "event_id":
        entry["event_id"] = bad_id
    assert entry["payload"]["payload_digest"] == _digest(entry["payload"])
    module = _contract()
    assert module.submit_request(good).model_dump(mode="json") == good.model_dump(mode="json")
    assert module.submit_entry(_entry_data(good)).model_dump(mode="json") == _entry_data(good)
    with pytest.raises(ValueError):
        module.submit_entry(entry)
    if field != "event_id":
        with pytest.raises(ValueError):
            module.submit_request(raw)


def test_logical_key_may_equal_the_formatted_native_key_in_its_separate_namespace():
    raw = _proposal_data()
    original_digest = raw["payload"]["payload_digest"]
    raw["payload"]["idempotency_key"] = raw["idempotency_key"]
    assert _digest(raw["payload"]) == original_digest
    before = copy.deepcopy(raw)
    module = _contract()
    body = module.submit_request(raw)
    assert body.model_dump(mode="json") == before
    receipt_data = _receipt_data(body)
    receipt = module.AgentSubmitReceipt.model_validate(receipt_data).validate_request(body)
    assert receipt.entry.model_dump(mode="json") == receipt_data["entry"]
    query = module.AgentSubmitLookupRequest.model_validate(_lookup_data(body))
    lookup_data = {"schema_version": "1.0", "status": "recorded", "entry": receipt_data["entry"]}
    result = module.AgentSubmitLookupResult.model_validate(lookup_data).validate_request(query)
    assert result.model_dump(mode="json") == lookup_data
    assert raw == before


@pytest.mark.parametrize("disposition", ["created", "retained"])
def test_receipt_binding_cannot_bypass_the_original_typed_proposal_request_cap(disposition):
    bounded = _proposal(objective="s" * 16000, plugin_count=300)
    oversized = _proposal(objective="s" * 16000, plugin_count=350)
    winner = _proposal(task_id=WINNER, objective="s" * 16000)
    receipt_data = _receipt_data(oversized if disposition == "created" else winner, disposition)
    before = oversized.model_dump(mode="json")
    assert len(_wire(bounded.model_dump(mode="json"))) < REQUEST_LIMIT
    assert REQUEST_LIMIT < len(_wire(before)) < RESPONSE_LIMIT
    assert len(_wire(receipt_data)) < RESPONSE_LIMIT
    assert len(json.dumps(receipt_data).encode()) < RESPONSE_LIMIT
    assert _logical(bounded.payload) == _logical(oversized.payload) == _logical(winner.payload)
    assert bounded.payload["payload_digest"] == oversized.payload["payload_digest"]
    assert all(
        type(key) is str and 1 <= len(key) <= 128 and type(value) is str and len(value) == 128
        for key, value in oversized.payload["plugin_versions"].items()
    )
    module = _contract()
    validated = module.submit_request(bounded)
    control_entry = bounded if disposition == "created" else winner
    control = module.AgentSubmitReceipt.model_validate(_receipt_data(control_entry, disposition))
    assert control.validate_request(validated).entry.model_dump(mode="json") == _entry_data(
        control_entry
    )
    # A response within its separate cap must be usable as a receipt. Its
    # existence cannot relax the smaller cap on the original typed proposal.
    assert (
        module.submit_entry(receipt_data["entry"]).model_dump(mode="json") == receipt_data["entry"]
    )
    receipt = module.AgentSubmitReceipt.model_validate(receipt_data)
    assert receipt.entry.model_dump(mode="json") == receipt_data["entry"]
    with pytest.raises(ValueError):
        module.submit_request(oversized)
    with pytest.raises(ValueError):
        receipt.validate_request(oversized)
    assert oversized.model_dump(mode="json") == before


@pytest.mark.parametrize("boundary", ["receipt", "lookup"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("epoch", True),
        ("epoch", "1"),
        ("sequence", True),
        ("sequence", "1"),
        ("event_id", None),
        ("created_at", None),
    ],
    ids=[
        "epoch_bool",
        "epoch_string",
        "sequence_bool",
        "sequence_string",
        "event_missing",
        "time_missing",
    ],
)
def test_nested_raw_entries_refuse_before_journal_coercion_or_identity_defaults(
    boundary, field, value
):
    body = _proposal()
    entry = _entry_data(body)
    valid = (
        _receipt_data(body)
        if boundary == "receipt"
        else {"schema_version": "1.0", "status": "recorded", "entry": entry}
    )
    malformed = copy.deepcopy(valid)
    if field in {"event_id", "created_at"}:
        del malformed["entry"][field]
    else:
        malformed["entry"][field] = value
    before = copy.deepcopy(malformed)
    # These precise defects disappear if the boundary constructs the permissive
    # base model first. Keep the original mapping for the contract invocation.
    permissive = JournalEntry.model_validate(malformed["entry"])
    if field in {"epoch", "sequence"}:
        assert type(getattr(permissive, field)) is int
        assert permissive.model_dump(mode="json") == entry
    else:
        assert field not in malformed["entry"]
        assert getattr(permissive, field) is not None
    assert len(_wire(malformed)) < RESPONSE_LIMIT
    module = _contract()
    model = module.AgentSubmitReceipt if boundary == "receipt" else module.AgentSubmitLookupResult
    control = model.model_validate(valid)
    request = (
        body
        if boundary == "receipt"
        else module.AgentSubmitLookupRequest.model_validate(_lookup_data(body))
    )
    assert control.validate_request(request).entry.model_dump(mode="json") == entry
    with pytest.raises(ValueError):
        model.model_validate(malformed)
    assert malformed == before
