"""Independent #228 initial proof oracles; pure packets never launch work."""

import copy
import hashlib
import importlib
from datetime import UTC, datetime, timedelta, timezone

import pytest
from test_standing_execution_contract import (
    GOLDEN_INITIAL,
    GOLDEN_INTENT,
    MARKER,
    OTHER,
    SAFE,
    claim,
    encoded,
    intent_at_initial_size,
)

from packages.contracts.agent_submit import submit_entry
from packages.contracts.models import JournalEntry
from packages.contracts.standing_execution import (
    StandingExecutionClaim,
    StandingExecutionIntent,
)

CREATED = "2026-11-01T08:30:01.001000Z"
EVENT = "00000000-0000-0000-0000-000000000777"
GOLDEN_DIGEST = "c3129fef6eff8b209d811fc1c1e0c79b4f196b58329a0afc74ca9af22b7b5f58"


def admission_module():
    try:
        return importlib.import_module("packages.contracts.standing_admission")
    except ModuleNotFoundError as exc:
        if exc.name != "packages.contracts.standing_admission":
            raise
        pytest.fail("Standing initial proof capability is absent", pytrace=False)


def entry(created=CREATED):
    return {
        **copy.deepcopy(GOLDEN_INITIAL),
        "event_id": EVENT,
        "sequence": 17,
        "origin": "standing-supervisor",
        "created_at": created,
    }


def stdlib_digest(record, native):
    return hashlib.sha256(
        b"kinaiya.standing.initial.v1\0"
        + encoded(
            {
                "claim_id": record["claim_id"],
                "claim_digest": record["claim_digest"],
                "entry": native,
            }
        )
    ).hexdigest()


def proof(created=CREATED):
    retained = claim()
    return {
        "schema_version": "1.0",
        "claim": retained,
        "event_id": EVENT,
        "sequence": 17,
        "created_at": created,
        "entry_digest": stdlib_digest(retained, entry(created)),
    }


def wrapper():
    return {"schema_version": "1.0", "disposition": "created", "proof": proof()}


def typed_claim():
    return StandingExecutionClaim.model_validate(claim())


def refuses(call, *args):
    with pytest.raises(ValueError) as exc:
        call(*args)
    assert MARKER not in str(exc.value)
    assert exc.value.__cause__ is None


def test_stdlib_literal_golden_fixture_and_old_grammar_positive_refusal_control():
    assert len(entry()) == 13
    assert stdlib_digest(claim(), entry()) == GOLDEN_DIGEST
    assert proof()["entry_digest"] == GOLDEN_DIGEST
    native = JournalEntry.model_validate(entry())
    assert native.model_dump(mode="json") == entry()
    refuses(submit_entry, native)


def test_exact_entry_projection_hash_and_native_metadata_are_literal_and_detached():
    module = admission_module()
    retained = typed_claim()
    original = entry()
    for supplied in (copy.deepcopy(original), JournalEntry.model_validate(original)):
        actual = module.standing_entry(supplied, retained)
        assert type(actual) is JournalEntry
        assert actual.model_dump(mode="json") == original
        assert module.standing_entry_digest(retained, EVENT, 17, CREATED) == GOLDEN_DIGEST
        parsed = module.standing_initial_proof(retained, supplied)
        assert parsed.model_dump(mode="json") == proof()
        assert parsed.journal_entry().model_dump(mode="json") == original
        actual.payload["objective"] = "local detached mutation"
        if isinstance(supplied, JournalEntry):
            assert supplied.model_dump(mode="json") == original
        else:
            assert supplied == original
        detached = parsed.journal_entry()
        detached.payload["standing"]["claim_digest"] = "0" * 64
        assert parsed.model_dump(mode="json") == proof()
        assert retained.model_dump(mode="json") == claim()


@pytest.mark.parametrize(
    "name,vector,maximum",
    [
        ("StandingInitialProof", proof, 163840),
        ("StandingTaskAdmission", wrapper, 180224),
    ],
)
def test_strict_complete_frozen_packets_raw_caps_and_full_original_binding(name, vector, maximum):
    kind = getattr(admission_module(), name)
    original = StandingExecutionIntent.model_validate(GOLDEN_INTENT)
    raw = vector()
    parsed = kind.model_validate(raw)
    assert parsed.canonical_bytes() == encoded(raw)
    assert parsed.validate_request(original).canonical_bytes() == encoded(raw)
    alternate = copy.deepcopy(GOLDEN_INTENT)
    alternate["executor_id"] = OTHER
    refuses(parsed.validate_request, StandingExecutionIntent.model_validate(alternate))
    for field in raw:
        bad = copy.deepcopy(raw)
        bad.pop(field)
        refuses(kind.model_validate, bad)
    refuses(kind.model_validate, {**raw, "extra": MARKER})
    for version in (None, "1.1", 1):
        refuses(kind.model_validate, {**raw, "schema_version": version})
    refuses(setattr, parsed, "schema_version", "1.1")
    body = encoded(raw)
    exact = b" " * (maximum - len(body)) + body
    assert kind.from_json(exact).canonical_bytes() == body
    for bad in (
        b" " + exact,
        b'{"schema_version":"1.0",' + body[1:],
        b"\xff" + body,
        body + b" {}",
        b"[]",
        b"null",
    ):
        refuses(kind.from_json, bad)
    for bad in (body.decode(), bytearray(body), memoryview(body)):
        refuses(kind.from_json, bad)
    raw.clear()
    assert parsed.canonical_bytes() == body


@pytest.mark.parametrize(
    "created",
    [
        "0001-01-01T00:00:00Z",
        "2000-02-29T23:59:59Z",
        "2026-11-01T08:30:01Z",
        "2026-11-01T08:30:01.000001Z",
        "2026-11-01T08:30:01.120000Z",
        "9999-12-31T23:59:59.999999Z",
    ],
)
def test_exact_native_utc_serializer_valid_endpoints(created):
    module = admission_module()
    native = module.standing_entry(entry(created), typed_claim())
    assert native.model_dump(mode="json") == entry(created)
    parsed = module.StandingInitialProof.model_validate(proof(created))
    assert parsed.journal_entry().model_dump(mode="json") == entry(created)
    assert module.standing_initial_proof(typed_claim(), native).canonical_bytes() == encoded(
        proof(created)
    )
    assert (
        module.standing_entry_digest(typed_claim(), EVENT, 17, created)
        == proof(created)["entry_digest"]
    )


@pytest.mark.parametrize(
    "created",
    [
        "1900-02-29T00:00:00Z",
        "2026-11-01T24:00:00Z",
        "2026-11-01T08:30:60Z",
        "2026-11-01T08:30:01+00:00",
        "2026-11-01T08:30:01.001Z",
        "2026-11-01T08:30:01.000000Z",
        "2026-11-01T08:30:01Z\n",
        "2026-11-01t08:30:01Z",
        "0000-01-01T00:00:00Z",
        None,
        datetime(2026, 11, 1, tzinfo=UTC),
    ],
)
def test_raw_timestamps_are_never_normalized_or_defaulted(created):
    module = admission_module()
    refuses(module.standing_entry, entry(created), typed_claim())
    refuses(module.standing_initial_proof, typed_claim(), entry(created))
    refuses(module.standing_entry_digest, typed_claim(), EVENT, 17, created)
    raw = proof()
    raw["created_at"] = created
    refuses(module.StandingInitialProof.model_validate, raw)


def test_typed_journal_timestamp_requires_an_actual_aware_utc_datetime():
    module = admission_module()
    for value in (
        datetime(2026, 11, 1),
        datetime(2026, 11, 1, tzinfo=timezone(timedelta(hours=1))),
        CREATED,
    ):
        native = JournalEntry.model_validate(entry())
        native.__dict__["created_at"] = value
        refuses(module.standing_entry, native, typed_claim())
    native = JournalEntry.model_validate(entry())
    assert module.standing_entry(native, typed_claim()).model_dump(mode="json") == entry()


def test_typed_native_defaults_are_existing_values_and_actual_missing_keys_are_refused():
    module = admission_module()
    native = JournalEntry(
        **copy.deepcopy(GOLDEN_INITIAL), sequence=17, origin="standing-supervisor"
    )
    assert {"event_id", "created_at"}.isdisjoint(native.__pydantic_fields_set__)
    before = native.model_dump(mode="json")
    parsed = module.standing_initial_proof(typed_claim(), native)
    assert parsed.journal_entry().model_dump(mode="json") == before
    assert parsed.event_id == native.event_id
    for field in ("event_id", "created_at", "schema_version", "payload"):
        bad = native.model_copy(deep=True)
        bad.__dict__.pop(field)
        refuses(module.standing_entry, bad, typed_claim())
    bad = native.model_copy(deep=True)
    bad.__pydantic_fields_set__.add(MARKER)
    refuses(module.standing_entry, bad, typed_claim())


def test_every_initial_request_field_and_nested_pin_is_bound_without_old_parser_reinterpretation():
    module = admission_module()
    good = entry()
    for field in good:
        missing = copy.deepcopy(good)
        missing.pop(field)
        refuses(module.standing_entry, missing, typed_claim())
    for key, value in (
        ("kind", "agent.command"),
        ("origin", "agent-supervisor"),
        ("schema_version", "1.1"),
        ("causes", ["synthetic-source"]),
        ("task_refs", [OTHER]),
        ("idempotency_key", "different"),
        ("epoch", 4),
        ("session_id", "another"),
    ):
        refuses(module.standing_entry, {**good, key: value}, typed_claim())
    for path, value in (
        (["actor"], "owner"),
        (["objective"], "changed"),
        (["model_pin", "model"], "changed"),
        (["workspace_pin", "root_identity"], "0" * 64),
        (["standing", "capability_sha256"], "0" * 64),
        (["auxiliary_models"], {"analysis": {}}),
        (["source_event_ids"], ["synthetic-source"]),
    ):
        bad = copy.deepcopy(good)
        nested = bad["payload"]
        for key in path[:-1]:
            nested = nested[key]
        nested[path[-1]] = value
        refuses(module.standing_entry, bad, typed_claim())
    for path in (
        [],
        ["payload"],
        ["payload", "standing"],
        ["payload", "model_pin"],
        ["payload", "workspace_pin"],
    ):
        bad = copy.deepcopy(good)
        nested = bad
        for key in path:
            nested = nested[key]
        nested["extra"] = MARKER
        refuses(module.standing_entry, bad, typed_claim())
    for bad in (
        [],
        None,
        True,
        JournalEntry.model_validate(good).model_copy(update={"extra": MARKER}),
    ):
        refuses(module.standing_entry, bad, typed_claim())


def test_scalar_safe_limits_and_actual_graph_coercions_are_checked_before_dump():
    module = admission_module()
    retained = typed_claim()
    for event_id in ("x", "é" * 128):
        native = module.standing_entry(
            {**entry(), "event_id": event_id, "sequence": SAFE}, retained
        )
        assert native.event_id == event_id
        assert native.sequence == SAFE
    for field, values in (
        ("event_id", ["", "x" * 129, "bad\0id", "bad\ud800id", True]),
        ("sequence", [0, -1, SAFE + 1, True, False, 17.0, "17"]),
    ):
        for value in values:
            raw = {**entry(), field: value}
            refuses(module.standing_entry, raw, retained)
            args = [retained, EVENT, 17, CREATED]
            args[1 if field == "event_id" else 2] = value
            refuses(module.standing_entry_digest, *args)
            native = JournalEntry.model_validate(entry())
            native.__dict__[field] = value
            refuses(module.standing_entry, native, retained)
    for value in ((1,), {"extra": object()}, "bad\ud800text"):
        native = JournalEntry.model_validate(entry())
        native.payload["objective"] = value
        refuses(module.standing_entry, native, retained)
    native = JournalEntry.model_validate(entry())
    native.__dict__["causes"] = ()
    refuses(module.standing_entry, native, retained)


def test_proof_hash_nested_closure_and_full_claim_helpers_cannot_be_bypassed():
    module = admission_module()
    parsed = module.StandingInitialProof.model_validate(proof())
    for field, value in (
        ("entry_digest", "0" * 64),
        ("event_id", OTHER),
        ("sequence", 18),
        ("created_at", "2026-11-01T08:30:02Z"),
    ):
        refuses(module.StandingInitialProof.model_validate, {**proof(), field: value})
    for path in ([], ["claim"], ["claim", "request"], ["claim", "request", "task", "model_pin"]):
        corrupted = parsed.model_copy(deep=True)
        nested = corrupted
        for field in path:
            nested = getattr(nested, field)
        nested.__dict__["extra"] = MARKER
        refuses(corrupted.canonical_bytes)
        refuses(corrupted.journal_entry)
        refuses(corrupted.validate_request, StandingExecutionIntent.model_validate(GOLDEN_INTENT))
    bad_claim = typed_claim().model_copy(update={"extra": MARKER})
    for supplied in (bad_claim, claim(), None):
        refuses(module.standing_entry, entry(), supplied)
        refuses(module.standing_initial_proof, supplied, entry())
        refuses(module.standing_entry_digest, supplied, EVENT, 17, CREATED)
    wrapper_model = module.StandingTaskAdmission.model_validate(wrapper())
    wrapper_model.proof.claim.request.task.model_pin.__dict__["local"] = 1
    refuses(wrapper_model.validate_request, StandingExecutionIntent.model_validate(GOLDEN_INTENT))
    for disposition in ("accepted", None, True):
        refuses(
            module.StandingTaskAdmission.model_validate, {**wrapper(), "disposition": disposition}
        )
    retained = module.StandingTaskAdmission.model_validate({**wrapper(), "disposition": "retained"})
    assert retained.proof.journal_entry().model_dump(mode="json") == entry()


def test_request_serializer_limit_is_preserved_before_initial_proof_can_be_built():
    module = admission_module()
    from test_standing_execution_contract import initial_expected

    exact = claim(intent_at_initial_size(65536))
    projected = {
        **initial_expected(exact),
        **{key: entry()[key] for key in ("event_id", "sequence", "origin", "created_at")},
    }
    parsed = module.standing_initial_proof(StandingExecutionClaim.model_validate(exact), projected)
    assert parsed.journal_entry().model_dump(mode="json") == projected
    assert len(parsed.journal_entry().model_dump_json().encode()) <= 69632
    over = claim(intent_at_initial_size(65537))
    projected = {
        **initial_expected(over),
        **{key: entry()[key] for key in ("event_id", "sequence", "origin", "created_at")},
    }
    refuses(module.standing_initial_proof, StandingExecutionClaim.model_validate(over), projected)
