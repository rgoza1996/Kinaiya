"""Pure #199 wire identity grammar, not SQL/authentication/execution authority."""

import copy
import importlib

import pytest
from test_agent_submit_client import (
    MARKER,
    WINNER,
    digest,
    native_entry,
    proposal,
    query_dict,
    response,
)
from test_agent_submit_client_supplement import floor_proposal

from packages.contracts.models import JournalEntry


def contract():
    try:
        return importlib.import_module("packages.contracts.agent_submit")
    except ModuleNotFoundError as exc:
        if exc.name != "packages.contracts.agent_submit":
            raise
        pytest.fail("Pure Agent submit contract is absent (capability baseline)")


def safe_custom_error(error):
    value = str(error)
    assert MARKER not in value
    assert "received-marker" not in value
    assert " column " not in value
    assert len(value) <= 160


@pytest.mark.parametrize("raw", [False, True], ids=["model_instances", "raw_mappings"])
def test_current_request_entry_and_bound_models_return_detached_copies(raw):
    module = contract()
    body = proposal()
    before = body.model_dump(mode="json")
    supplied = copy.deepcopy(before) if raw else body
    validated = module.submit_request(supplied)
    assert validated.model_dump(mode="json") == before
    assert validated is not supplied and validated.payload is not body.payload
    validated.payload["model_pin"]["model"] = "detached-change"
    assert body.model_dump(mode="json") == before
    if raw:
        assert supplied == before
    entry_data = native_entry(body)
    supplied_entry = entry_data if raw else JournalEntry.model_validate(entry_data)
    entry = module.submit_entry(supplied_entry)
    assert entry.model_dump(mode="json") == entry_data
    assert entry is not supplied_entry
    receipt = module.AgentSubmitReceipt(disposition="created", entry=entry)
    bound = receipt.validate_request(body)
    assert bound is not receipt and bound.entry is not receipt.entry
    bound.entry.payload["model_pin"]["model"] = "another-detached-change"
    assert receipt.entry.model_dump(mode="json") == entry_data
    query = module.AgentSubmitLookupRequest(**query_dict())
    lookup = module.AgentSubmitLookupResult(status="recorded", entry=entry)
    bound_lookup = lookup.validate_request(query)
    assert bound_lookup is not lookup and bound_lookup.entry is not lookup.entry
    assert body.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "public_floor", [False, True], ids=["local_request", "requested_public_local_floor"]
)
def test_different_canonical_retained_winner_preserves_identity_and_declared_floor(public_floor):
    module = contract()
    body = floor_proposal() if public_floor else proposal()
    winning = floor_proposal(winner=True) if public_floor else proposal(task_id=WINNER, winner=True)
    entry = native_entry(winning)
    receipt = module.AgentSubmitReceipt(disposition="retained", entry=entry).validate_request(body)
    assert receipt.entry.model_dump(mode="json") == entry
    assert receipt.entry.payload["task_id"] != body.payload["task_id"]
    assert receipt.entry.payload["model_pin"] == winning.payload["model_pin"]
    assert receipt.entry.payload["harness_version"] == winning.payload["harness_version"]
    assert receipt.entry.payload["content_scope"] == "local_only"


@pytest.mark.parametrize(
    "bad", ["duplicate", "nonfinite", "overflow", "utf8", "surrogate", "oversize"]
)
def test_strict_json_refusal_uses_one_fixed_unchained_diagnostic(bad):
    module = contract()
    raw = {
        "duplicate": b'{"received-marker":1,"received-marker":2}',
        "nonfinite": b'{"received-marker":NaN}',
        "overflow": b'{"received-marker":1e999}',
        "utf8": b'{"received-marker":"\xff"}',
        "surrogate": b'{"received-marker":"\\ud800"}',
        "oversize": b" " * 65537,
    }[bad]
    with pytest.raises(ValueError) as expected:
        module.agent_submit_json(b"{")
    with pytest.raises(ValueError) as caught:
        module.agent_submit_json(raw)
    assert str(caught.value) == str(expected.value)
    safe_custom_error(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("maximum", [True, 0, 1.0, 69633])
def test_json_maximum_is_exact_positive_bounded_integer(maximum):
    module = contract()
    with pytest.raises(ValueError):
        module.agent_submit_json(b"{}", maximum=maximum)
    assert module.agent_submit_json(b"{}", maximum=2) == {}
    with pytest.raises(ValueError):
        module.agent_submit_json(b"{} ", maximum=2)


@pytest.mark.parametrize(
    "invalid",
    [
        "epoch_bool",
        "revision_string",
        "local_int",
        "settings_bool",
        "payload_extra",
        "pin_extra",
        "scope_widening",
        "digest",
    ],
)
def test_raw_proposal_fields_are_strict_before_permissive_base_models(invalid):
    module = contract()
    body = proposal().model_dump(mode="json")
    if invalid == "epoch_bool":
        body["epoch"] = True
        body["payload"]["epoch"] = True
    elif invalid == "revision_string":
        body["payload"]["source_revision"] = "1"
    elif invalid == "local_int":
        body["payload"]["model_pin"]["local"] = 1
    elif invalid == "settings_bool":
        body["payload"]["model_pin"]["settings_revision"] = True
    elif invalid == "payload_extra":
        body["payload"]["received-marker"] = "not-a-current-field"
    elif invalid == "pin_extra":
        body["payload"]["model_pin"]["received-marker"] = "not-a-pin-field"
    elif invalid == "scope_widening":
        body["payload"]["content_scope"] = "public_synthetic"
    else:
        body["payload"]["payload_digest"] = "0" * 64
    before = copy.deepcopy(body)
    with pytest.raises(ValueError) as caught:
        module.submit_request(body)
    safe_custom_error(caught.value)
    assert body == before


@pytest.mark.parametrize("missing", ["event_id", "sequence", "origin", "created_at"])
def test_native_receipt_identity_is_required_not_generated_by_defaults(missing):
    module = contract()
    entry = native_entry(proposal())
    del entry[missing]
    before = copy.deepcopy(entry)
    with pytest.raises(ValueError) as caught:
        module.submit_entry(entry)
    safe_custom_error(caught.value)
    assert entry == before


@pytest.mark.parametrize("changed", ["causes", "model_pin", "harness"])
def test_created_binding_checks_complete_request_not_only_logical_digest(changed):
    module = contract()
    body = proposal()
    payload = response("admit")
    if changed == "causes":
        payload["entry"]["causes"] = ["another-safe-source"]
    elif changed == "model_pin":
        payload["entry"]["payload"]["model_pin"]["model"] = "another-native-model"
    else:
        payload["entry"]["payload"]["harness_version"] = "v-" + "b" * 24
    assert payload["entry"]["payload"]["payload_digest"] == body.payload["payload_digest"]
    receipt = module.AgentSubmitReceipt.model_validate(payload)
    with pytest.raises(ValueError) as caught:
        receipt.validate_request(body)
    safe_custom_error(caught.value)


@pytest.mark.parametrize("changed", ["original_key", "effective_scope"])
def test_retained_binding_refuses_key_or_scope_change_without_digest_change(changed):
    module = contract()
    body = floor_proposal()
    payload = {"disposition": "retained", "entry": native_entry(floor_proposal(winner=True))}
    original_digest = payload["entry"]["payload"]["payload_digest"]
    if changed == "original_key":
        payload["entry"]["payload"]["idempotency_key"] = "another-original-client-key"
    else:
        payload["entry"]["payload"]["content_scope"] = "public_synthetic"
    assert payload["entry"]["payload"]["payload_digest"] == original_digest
    receipt = module.AgentSubmitReceipt.model_validate(payload)
    with pytest.raises(ValueError) as caught:
        receipt.validate_request(body)
    safe_custom_error(caught.value)


@pytest.mark.parametrize(
    "history", ["missing_pin", "null_pin", "missing_optionals", "pre_scope", "pre_scope_public"]
)
def test_only_declared_historical_omissions_and_old_local_digest_are_reusable(history):
    module = contract()
    body = proposal()
    entry = native_entry(proposal(task_id=WINNER, winner=True))
    payload = entry["payload"]
    if history == "null_pin":
        payload["model_pin"] = None
    elif history == "missing_optionals":
        payload.pop("model_pin")
        payload.pop("explicit_skill")
        payload.pop("plugin_versions")
    elif history.startswith("pre_scope"):
        payload.pop("model_pin")
        payload.pop("requested_content_scope")
        old_logical = {
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
        payload["payload_digest"] = digest(old_logical)
    else:
        payload.pop("model_pin")
    original = copy.deepcopy(entry)
    validated = module.submit_entry(entry)
    assert validated.model_dump(mode="json") == original
    receipt = module.AgentSubmitReceipt(disposition="retained", entry=validated)
    if history == "pre_scope_public":
        requested = dict(body.payload)
        requested["requested_content_scope"] = "public_synthetic"
        logical = {
            key: requested[key]
            for key in (
                "session_id",
                "thread_id",
                "epoch",
                "objective",
                "source_event_ids",
                "source_revision",
            )
        }
        logical["content_scope"] = "public_synthetic"
        requested["payload_digest"] = digest(logical)
        body = body.model_copy(update={"payload": requested}, deep=True)
        with pytest.raises(ValueError):
            receipt.validate_request(body)
    else:
        assert receipt.validate_request(body).entry.model_dump(mode="json") == original
    assert entry == original


@pytest.mark.parametrize("invalid", ["strict_epoch", "recorded_scope", "unobserved_entry"])
def test_lookup_revalidates_shape_and_scope_without_inventing_negative_observation(invalid):
    module = contract()
    query = module.AgentSubmitLookupRequest(**query_dict())
    if invalid == "strict_epoch":
        with pytest.raises(ValueError):
            module.AgentSubmitLookupRequest(**{**query_dict(), "epoch": True})
        forged = query.model_copy(update={"epoch": True})
        negative = module.AgentSubmitLookupResult(status="unobserved", entry=None)
        with pytest.raises(ValueError):
            negative.validate_request(forged)
    elif invalid == "recorded_scope":
        result = module.AgentSubmitLookupResult(status="recorded", entry=native_entry(proposal()))
        with pytest.raises(ValueError):
            result.validate_request(query.model_copy(update={"thread_id": "another-safe-thread"}))
    else:
        with pytest.raises(ValueError):
            module.AgentSubmitLookupResult(status="unobserved", entry=native_entry(proposal()))


def test_shared_exception_diagnostics_are_fixed_and_do_not_accept_payload_authority():
    module = contract()
    for name, message in (
        ("AgentSubmitUnavailable", "Agent submission admission is unavailable"),
        ("AgentSubmitConflict", "Agent submission key conflicts with retained scope or content"),
        ("AgentSubmitUnconfirmed", "Agent submission persistence is unconfirmed"),
    ):
        error = getattr(module, name)()
        assert isinstance(error, ValueError)
        assert str(error) == message
        assert error.args == (message,)
