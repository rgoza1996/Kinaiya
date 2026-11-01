"""Pure-contract integration oracles for main PR202 auxiliary model metadata.

The C5-only contract may lack this integration capability. No provider factory,
selection, execution, storage, or network is part of these tests.
"""

import copy
import hashlib
import importlib
import json

import pytest

from packages.contracts.models import AgentModelPin, AppendRequest

TASK = "auxiliary-proposal-task"
WINNER = "auxiliary-winner-task"
ROLES = ("analysis", "compression", "title_generation", "delegation", "harness_design")
ABSENT = object()


def _contract():
    try:
        return importlib.import_module("packages.contracts.agent_submit")
    except ModuleNotFoundError as exc:
        if exc.name != "packages.contracts.agent_submit":
            raise
        pytest.fail("Pure Agent submit contract is absent (capability baseline)")


def _pin(label):
    value = {
        "schema_version": "1.0",
        "profile_id": "synthetic-profile-" + label,
        "provider": "synthetic",
        "model": "synthetic-model-" + label,
        "local": True,
        "reasoning": None,
        "settings_revision": 0,
    }
    assert AgentModelPin.model_validate(value).model_dump(mode="json") == value
    return value


def _proposal(*, task_id=TASK, auxiliary_models=ABSENT):
    logical = {
        "session_id": "auxiliary-synthetic-session",
        "thread_id": "auxiliary-synthetic-thread",
        "epoch": 1,
        "objective": "Observe public synthetic auxiliary model metadata.",
        "source_event_ids": [],
        "source_revision": 1,
        "content_scope": "local_only",
    }
    digest = hashlib.sha256(
        json.dumps(logical, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    payload = {
        **logical,
        "task_id": task_id,
        "state": "accepted",
        "op": "submit",
        "actor": "owner",
        "objective_revision": 1,
        "requested_content_scope": "local_only",
        "idempotency_key": "auxiliary-synthetic-client-key",
        "payload_digest": digest,
        "harness_version": "synthetic-auxiliary-harness",
        "model_pin": _pin("primary"),
        "explicit_skill": None,
        "plugin_versions": {},
    }
    if auxiliary_models is not ABSENT:
        payload["auxiliary_models"] = copy.deepcopy(auxiliary_models)
    return AppendRequest(
        session_id=logical["session_id"],
        thread_id=logical["thread_id"],
        epoch=1,
        kind="agent.command",
        payload=payload,
        causes=[],
        task_refs=[task_id],
        idempotency_key=f"agent:{task_id}:agent.command:1",
    )


def _entry(body):
    return {
        **body.model_dump(mode="json"),
        "event_id": "auxiliary-synthetic-native-event",
        "sequence": 1,
        "origin": "agent-supervisor",
        "created_at": "2026-10-31T00:00:00Z",
    }


def _query(module, body):
    return module.AgentSubmitLookupRequest(
        session_id=body.session_id,
        thread_id=body.thread_id,
        epoch=body.epoch,
        idempotency_key=body.payload["idempotency_key"],
    )


def _require_auxiliary_support(module, body):
    raw = body.model_dump(mode="json")
    assert "auxiliary_models" in raw["payload"]
    c5 = copy.deepcopy(raw)
    c5["payload"].pop("auxiliary_models")
    assert module.submit_request(c5).model_dump(mode="json") == c5
    try:
        validated = module.submit_request(raw)
    except ValueError:
        pytest.fail(
            "Auxiliary submit metadata is unavailable (main PR202 integration capability baseline)",
            pytrace=False,
        )
    assert validated.model_dump(mode="json") == raw
    return validated


def test_c5_proposal_without_auxiliary_metadata_remains_exactly_unchanged():
    body = _proposal()
    before = body.model_dump(mode="json")
    module = _contract()
    validated = module.submit_request(body)
    assert validated.model_dump(mode="json") == before
    receipt = module.AgentSubmitReceipt(disposition="created", entry=_entry(body))
    assert receipt.validate_request(body).entry.model_dump(mode="json") == _entry(body)
    assert "auxiliary_models" not in validated.payload
    assert "auxiliary_models" not in receipt.entry.payload
    assert body.model_dump(mode="json") == before


def test_legacy_entry_without_auxiliary_metadata_does_not_gain_default_pins():
    body = _proposal()
    raw = _entry(_proposal(task_id=WINNER))
    for field in ("model_pin", "explicit_skill", "plugin_versions"):
        raw["payload"].pop(field)
    before = copy.deepcopy(raw)
    module = _contract()
    entry = module.submit_entry(raw)
    assert entry.model_dump(mode="json") == before
    receipt = module.AgentSubmitReceipt(disposition="retained", entry=entry).validate_request(body)
    assert receipt.entry.model_dump(mode="json") == before
    assert "auxiliary_models" not in receipt.entry.payload
    assert "model_pin" not in receipt.entry.payload
    assert raw == before


def test_explicit_empty_auxiliary_object_is_valid_and_preserved():
    body = _proposal(auxiliary_models={})
    before = body.model_dump(mode="json")
    module = _contract()
    validated = _require_auxiliary_support(module, body)
    entry = module.submit_entry(_entry(validated))
    assert entry.payload["auxiliary_models"] == {}
    receipt = module.AgentSubmitReceipt(disposition="created", entry=entry).validate_request(body)
    assert receipt.entry.model_dump(mode="json") == _entry(body)
    assert body.model_dump(mode="json") == before


def test_all_five_auxiliary_roles_are_preserved_and_excluded_from_the_logical_digest():
    auxiliary = {role: _pin(role) for role in ROLES}
    body = _proposal(auxiliary_models=auxiliary)
    baseline = _proposal()
    assert body.payload["payload_digest"] == baseline.payload["payload_digest"]
    assert len(auxiliary) == 5
    before = body.model_dump(mode="json")
    module = _contract()
    validated = _require_auxiliary_support(module, body)
    raw_entry = _entry(validated)
    assert module.submit_entry(raw_entry).model_dump(mode="json") == raw_entry
    receipt = module.AgentSubmitReceipt(disposition="created", entry=raw_entry)
    assert receipt.validate_request(body).entry.payload["auxiliary_models"] == auxiliary
    result = module.AgentSubmitLookupResult(status="recorded", entry=raw_entry)
    assert result.validate_request(_query(module, body)).entry.model_dump(mode="json") == raw_entry
    validated.payload["auxiliary_models"]["analysis"]["model"] = "synthetic-detached-change"
    assert body.model_dump(mode="json") == before


def test_created_receipt_compares_auxiliary_metadata_despite_equal_logical_digest():
    body = _proposal(auxiliary_models={"analysis": _pin("original-analysis")})
    module = _contract()
    _require_auxiliary_support(module, body)
    control = module.AgentSubmitReceipt(disposition="created", entry=_entry(body))
    assert control.validate_request(body).entry.model_dump(mode="json") == _entry(body)
    changed = _entry(body)
    changed["payload"]["auxiliary_models"]["analysis"] = _pin("different-analysis")
    assert changed["payload"]["payload_digest"] == body.payload["payload_digest"]
    assert module.submit_entry(changed).model_dump(mode="json") == changed
    receipt = module.AgentSubmitReceipt(disposition="created", entry=changed)
    with pytest.raises(ValueError):
        receipt.validate_request(body)


def test_retained_winner_keeps_its_different_auxiliary_map_and_pins_detached():
    loser = _proposal(auxiliary_models={"analysis": _pin("losing-analysis")})
    winner = _proposal(
        task_id=WINNER,
        auxiliary_models={
            "analysis": _pin("winning-analysis"),
            "title_generation": _pin("winning-title"),
        },
    )
    assert loser.payload["payload_digest"] == winner.payload["payload_digest"]
    module = _contract()
    _require_auxiliary_support(module, loser)
    _require_auxiliary_support(module, winner)
    before = _entry(winner)
    receipt = module.AgentSubmitReceipt(disposition="retained", entry=before)
    retained = receipt.validate_request(loser)
    assert retained.entry.model_dump(mode="json") == before
    assert retained.entry.payload["auxiliary_models"] == winner.payload["auxiliary_models"]
    retained.entry.payload["auxiliary_models"]["analysis"]["model"] = "synthetic-detached-change"
    assert receipt.entry.model_dump(mode="json") == before
    assert _entry(winner) == before


@pytest.mark.parametrize(
    "invalid",
    [
        "map_array",
        "unknown_role",
        "six_roles",
        "null_pin",
        "local_int",
        "settings_bool",
        "settings_string",
        "pin_extra",
    ],
)
def test_auxiliary_map_is_closed_and_each_nonnull_pin_is_strict_after_valid_map_control(invalid):
    auxiliary = {role: _pin(role) for role in ROLES}
    body = _proposal(auxiliary_models=auxiliary)
    module = _contract()
    validated = _require_auxiliary_support(module, body)
    assert module.submit_entry(_entry(validated)).model_dump(mode="json") == _entry(body)
    raw = body.model_dump(mode="json")
    pins = raw["payload"]["auxiliary_models"]
    if invalid == "map_array":
        raw["payload"]["auxiliary_models"] = [pins["analysis"]]
    elif invalid == "unknown_role":
        raw["payload"]["auxiliary_models"] = {"unknown_auxiliary_role": pins["analysis"]}
    elif invalid == "six_roles":
        pins["unknown_sixth_role"] = _pin("sixth")
        assert len(pins) == 6
    elif invalid == "null_pin":
        pins["analysis"] = None
    elif invalid == "local_int":
        pins["analysis"]["local"] = 1
    elif invalid == "settings_bool":
        pins["analysis"]["settings_revision"] = True
    elif invalid == "settings_string":
        pins["analysis"]["settings_revision"] = "0"
    else:
        pins["analysis"]["unexpected_pin_field"] = "synthetic-extra"
    before = copy.deepcopy(raw)
    with pytest.raises(ValueError):
        module.submit_request(raw)
    typed = AppendRequest.model_validate(raw)
    assert typed.model_dump(mode="json") == before
    with pytest.raises(ValueError):
        module.submit_request(typed)
    entry = _entry(typed)
    with pytest.raises(ValueError):
        module.submit_entry(entry)
    with pytest.raises(ValueError):
        module.AgentSubmitReceipt(disposition="created", entry=entry)
    with pytest.raises(ValueError):
        module.AgentSubmitLookupResult(status="recorded", entry=entry)
    assert raw == before
