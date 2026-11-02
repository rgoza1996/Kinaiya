"""Consumer equivalence of fresh rows; parsing grants no native/physical proof.

Complete synthetic packets and golden hashes predate this optimization. Expected
row bytes use those literal dictionaries and stdlib JSON, never the consumer's
serializer or a reference copy of its implementation.
"""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from test_standing_resource_finalization_contracts import (
    BACKEND,
    GOLDEN_HASHES,
    OTHER_INSTANCE,
    vectors,
)

from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingReceipt,
    StandingResourceBindingRequest,
    StandingResourceFinalizeContext,
    StandingResourceFinalizeRequest,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from services.resources.standing_barrier import StandingBarrier

ERROR = "Invalid standing resource finalization contract"
OLD_ERROR = "Invalid standing execution contract"
PRIVATE = "PUBLIC-SYNTHETIC-PRIVATE-ROW-INPUT"
COLUMNS = ("binding", "receipt", "seal", "context")
KINDS = (
    StandingResourceBindingRequest,
    StandingResourceBindingReceipt,
    StandingResourceFinalizeRequest,
    StandingResourceFinalizeContext,
)
PACKETS = (
    "StandingResourceBindingRequest",
    "StandingResourceBindingReceipt",
    "StandingResourceFinalizeRequest",
    "StandingResourceFinalizeContext",
)


def encoded(raw):
    return json.dumps(
        raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(domain, raw):
    return hashlib.sha256(domain.encode("ascii") + b"\0" + encoded(raw)).hexdigest()


def rehash(raw, field, domain):
    raw[field] = digest(domain, {key: value for key, value in raw.items() if key != field})


def fixture_row(phase="drained", **options):
    bundle = vectors(**options)
    binding, receipt, seal, context = (copy.deepcopy(bundle[name]) for name in PACKETS)
    record = binding["claim"]
    row = {
        "claim_id": record["claim_id"],
        "task_id": record["request"]["task"]["task_id"],
        "session_backend": record["request"]["expected_backend"],
        "resource_backend": binding["resource_backend"],
        "instance_id": binding["resource_instance"],
        "claim": encoded(record).decode(),
        "binding": encoded(binding).decode(),
        "receipt": None if phase == "pending" else encoded(receipt).decode(),
        "seal": encoded(seal).decode() if phase in {"sealed", "drained"} else None,
        "context": encoded(context).decode() if phase in {"sealed", "drained"} else None,
        "phase": phase,
    }
    return row


@pytest.fixture
def barrier():
    # This is only the trusted private row-consumer boundary. No database,
    # owner operation, admission or restoration is simulated by these packets.
    owner = SimpleNamespace(standing_handoff=SimpleNamespace(backend=BACKEND))
    return StandingBarrier(owner)


def assert_row(barrier, row):
    before = copy.deepcopy(row)
    result = barrier._checked_row(row)
    assert type(result) is tuple and len(result) == 4
    for column, kind, value in zip(COLUMNS, KINDS, result, strict=True):
        if row[column] is None:
            assert value is None
        else:
            assert type(value) is kind
            assert value.model_dump(mode="json") == json.loads(row[column])
            assert value.canonical_bytes() == row[column].encode("utf-8")
    assert row == before
    return result


def refuses(barrier, row, kind=StandingUnavailable, message=None):
    before = copy.deepcopy(row)
    with pytest.raises(kind) as caught:
        barrier._checked_row(row)
    assert type(caught.value) is kind
    expected = (
        message
        or {
            StandingUnavailable: "Standing work authority is unavailable",
            StandingConflict: "Standing work conflicts with retained authority",
        }[kind]
    )
    assert str(caught.value) == expected
    assert PRIVATE not in str(caught.value) and caught.value.__cause__ is None
    if kind is ValueError:
        assert caught.value.__suppress_context__
    assert row == before


@pytest.mark.parametrize("phase", ("pending", "bound", "sealed", "drained"))
def test_legal_phases_keep_exact_complete_unicode_packets(barrier, phase):
    row = fixture_row(phase)
    binding, receipt, seal, context = assert_row(barrier, row)
    assert binding.claim.request.task.model_pin.reasoning is None
    assert "Inspect café.txt and é.txt literally; preserve 🧭." in row["claim"]
    assert "\\u00e9" not in row["claim"] and "\\ud83e" not in row["claim"]
    assert (
        digest("kinaiya.standing.resource-binding.v1", json.loads(row["binding"]))
        == (GOLDEN_HASHES["binding"])
    )
    if receipt is not None:
        assert receipt.request_digest == GOLDEN_HASHES["binding"]
    if context is not None:
        assert context.context_digest == GOLDEN_HASHES["context"]
        assert seal is not None and context.observation.reservation is not None


@pytest.mark.parametrize("admitted", (False, True))
def test_admitted_and_unadmitted_no_reservation_contexts_keep_required_nulls(barrier, admitted):
    row = fixture_row(admitted=admitted, reserved=False)
    _, _, _, context = assert_row(barrier, row)
    assert context.observation.reservation is None
    assert (context.closure.initial is not None) is admitted
    assert (context.observation.accepted_sequence is not None) is admitted
    assert context.observation.coverage == (
        "accepted-after-bootstrap" if admitted else "legacy-or-unadmitted"
    )


def test_retained_receipt_and_nullable_model_catalog_are_legal_metadata(barrier):
    original = copy.deepcopy(vectors()["StandingResourceFinalizeRequest"]["original"])
    original["task"]["model_pin"]["catalog_binding"] = None
    original["task"]["model_pin"]["catalog_revision"] = None
    row = fixture_row(original=original)
    receipt = json.loads(row["receipt"])
    receipt["disposition"] = "retained"
    row["receipt"] = encoded(receipt).decode()
    binding, receipt, _, _ = assert_row(barrier, row)
    assert receipt.disposition == "retained"
    assert binding.claim.request.task.model_pin.catalog_binding is None
    assert binding.claim.request.task.model_pin.catalog_revision is None


@pytest.mark.parametrize(
    "column,replacement",
    (
        ("claim_id", None),
        ("task_id", 7),
        ("session_backend", b"a" * 32),
        ("resource_backend", False),
        ("instance_id", []),
        ("claim", {}),
        ("binding", b"{}"),
        ("phase", True),
        ("receipt", 0),
        ("seal", []),
        ("context", {}),
    ),
)
def test_sql_columns_require_exact_strings_or_explicit_optional_nulls(barrier, column, replacement):
    row = fixture_row()
    row[column] = replacement
    refuses(barrier, row)


@pytest.mark.parametrize("column", ("claim", "binding", "phase"))
def test_string_subclasses_do_not_cross_the_exact_sql_scalar_boundary(barrier, column):
    class Text(str):
        pass

    row = fixture_row()
    row[column] = Text(row[column])
    refuses(barrier, row)


@pytest.mark.parametrize(
    "column,replacement",
    (
        ("claim_id", "f" * 64),
        ("task_id", OTHER_INSTANCE),
        ("session_backend", "c" * 32),
        ("resource_backend", "c" * 32),
        ("instance_id", OTHER_INSTANCE),
        ("phase", "restored"),
    ),
)
def test_scalar_row_identity_and_phase_cannot_replace_complete_packet_joins(
    barrier, column, replacement
):
    row = fixture_row()
    row[column] = replacement
    refuses(barrier, row)


def test_configured_backend_must_match_but_row_parsing_grants_no_controller_adoption(barrier):
    row = fixture_row()
    barrier.owner.controller_id = OTHER_INSTANCE
    # Historical metadata can be parsed; this method does not launch or adopt.
    assert_row(barrier, row)
    barrier.gate.backend = "c" * 32
    refuses(barrier, row)


def test_valid_claim_with_same_claim_id_but_different_complete_record_cannot_replace_binding(
    barrier,
):
    row = fixture_row()
    other = fixture_row(prepared="2026-11-01T08:30:00Z")
    assert_row(barrier, other)
    assert row["claim_id"] == other["claim_id"] and row["claim"] != other["claim"]
    row["claim"] = other["claim"]
    refuses(barrier, row)


@pytest.mark.parametrize(
    "phase,column",
    (
        ("bound", "receipt"),
        ("sealed", "receipt"),
        ("sealed", "seal"),
        ("drained", "context"),
        ("pending", "receipt"),
        ("bound", "seal"),
    ),
)
def test_phase_and_nullable_packet_relationships_remain_exact(barrier, phase, column):
    row = fixture_row(phase)
    complete = fixture_row()
    if row[column] is None:
        row[column] = complete[column]
        if column == "seal":
            row["context"] = complete["context"]
    else:
        row[column] = None
    refuses(barrier, row)


@pytest.mark.parametrize("column", ("claim", *COLUMNS))
def test_duplicate_raw_members_are_rejected_before_canonical_reencoding(barrier, column):
    row = fixture_row()
    row[column] = '{"schema_version":"' + PRIVATE + '",' + row[column][1:]
    refuses(barrier, row, ValueError, OLD_ERROR if column == "claim" else ERROR)


@pytest.mark.parametrize("column", ("claim", "context"))
def test_malformed_json_keeps_its_original_fixed_parser_error(barrier, column):
    row = fixture_row()
    row[column] = '{"' + PRIVATE
    refuses(barrier, row, ValueError, OLD_ERROR if column == "claim" else ERROR)


def pin(raw, column):
    for name in {
        "claim": ("request",),
        "binding": ("claim", "request"),
        "receipt": ("request", "claim", "request"),
        "seal": ("original",),
        "context": ("binding", "claim", "request"),
    }[column]:
        raw = raw[name]
    return raw["task"]["model_pin"]


@pytest.mark.parametrize("column", ("claim", *COLUMNS))
@pytest.mark.parametrize("fault", ("missing-null", "strict-bool", "metadata-extra"))
def test_full_raw_closure_is_not_normalized_or_repaired(barrier, column, fault):
    row = fixture_row()
    raw = json.loads(row[column])
    if fault == "missing-null":
        assert pin(raw, column).pop("reasoning") is None
    elif fault == "strict-bool":
        raw["schema_version"] = True
    else:
        pin(raw, column)["__pydantic_extra__"] = {PRIVATE: True}
    row[column] = encoded(raw).decode()
    refuses(barrier, row, ValueError, OLD_ERROR if column == "claim" else ERROR)


@pytest.mark.parametrize("column", ("claim", "binding", "receipt", "context"))
def test_own_packet_digest_corruption_remains_a_parser_failure(barrier, column):
    row = fixture_row()
    raw = json.loads(row[column])
    target, field = {
        "claim": (raw, "claim_digest"),
        "binding": (raw.get("claim"), "claim_digest"),
        "receipt": (raw, "request_digest"),
        "context": (raw, "context_digest"),
    }[column]
    target[field] = "0" * 64
    row[column] = encoded(raw).decode()
    refuses(barrier, row, ValueError, OLD_ERROR if column == "claim" else ERROR)


@pytest.mark.parametrize("column", ("claim", *COLUMNS))
def test_semantically_valid_noncanonical_raw_rows_are_not_silently_normalized(barrier, column):
    row = fixture_row()
    raw = json.loads(row[column])
    if column in {"claim", "seal"}:
        row[column] += "\n"
    elif column == "binding":
        row[column] = json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    elif column == "receipt":
        # The independent canonical encoder sorts keys; retain reversed order
        # explicitly here to create a genuinely noncanonical, valid object.
        row[column] = json.dumps(
            dict(reversed(list(raw.items()))), ensure_ascii=False, separators=(",", ":")
        )
    else:
        row[column] = json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=1)
    assert json.loads(row[column]) == raw and row[column] != encoded(raw).decode()
    refuses(barrier, row)


def foreign_receipt(kind):
    options = {"prepared": "2026-11-01T08:30:00Z"} if kind == "claim-record" else {}
    raw = vectors(**options)["StandingResourceBindingReceipt"]
    if kind == "backend":
        raw["request"]["resource_backend"] = "c" * 32
    elif kind == "controller":
        raw["request"]["resource_instance"] = OTHER_INSTANCE
    if kind != "claim-record":
        rehash(raw, "request_digest", "kinaiya.standing.resource-binding.v1")
    return raw


@pytest.mark.parametrize("kind", ("claim-record", "backend", "controller"))
def test_complete_valid_foreign_receipt_uses_fixed_full_request_mismatch_error(barrier, kind):
    row = fixture_row()
    raw = foreign_receipt(kind)
    valid = StandingResourceBindingReceipt.from_json(encoded(raw))
    assert valid.canonical_bytes() == encoded(raw)
    assert valid.request.claim.claim_id == row["claim_id"]
    assert encoded(raw["request"]).decode() != row["binding"]
    row["receipt"] = encoded(raw).decode()
    refuses(barrier, row, ValueError, ERROR)


def foreign_seal(kind):
    raw = vectors()["StandingResourceFinalizeRequest"]
    if kind == "original":
        raw["original"]["task"]["objective"] += " A different literal objective."
    elif kind == "backend":
        raw["expected_resource_backend"] = "c" * 32
    else:
        raw["expected_resource_instance"] = OTHER_INSTANCE
    return raw


@pytest.mark.parametrize("kind", ("original", "backend", "controller"))
def test_complete_valid_foreign_seal_remains_an_authority_conflict(barrier, kind):
    row = fixture_row()
    raw = foreign_seal(kind)
    assert StandingResourceFinalizeRequest.from_json(encoded(raw)).canonical_bytes() == encoded(raw)
    row["seal"] = encoded(raw).decode()
    refuses(barrier, row, StandingConflict)


def foreign_context(kind):
    raw = vectors(prepared="2026-11-01T08:30:00Z")["StandingResourceFinalizeContext"]
    if kind == "request":
        raw["request"]["idempotency_key"] += ":other"
        rehash(raw, "context_digest", "kinaiya.standing.resource-finalize-context.v1")
    return raw


@pytest.mark.parametrize("kind", ("request", "binding"))
def test_complete_valid_foreign_context_preserves_request_then_binding_error_order(barrier, kind):
    row = fixture_row()
    raw = foreign_context(kind)
    assert StandingResourceFinalizeContext.from_json(encoded(raw)).canonical_bytes() == encoded(raw)
    assert raw["binding"]["claim"]["claim_id"] == row["claim_id"]
    row["context"] = encoded(raw).decode()
    if kind == "request":
        refuses(barrier, row, ValueError, ERROR)
    else:
        assert encoded(raw["request"]).decode() == row["seal"]
        refuses(barrier, row)


@pytest.mark.parametrize("first", ("raw-row", "receipt", "seal"))
def test_earlier_refusal_precedes_later_independently_invalid_optional_packet(barrier, first):
    row = fixture_row()
    if first == "raw-row":
        row["binding"] += " "
        row["receipt"] = PRIVATE
        refuses(barrier, row)
    elif first == "receipt":
        row["receipt"] = encoded(foreign_receipt("claim-record")).decode()
        row["seal"] = encoded(foreign_seal("controller")).decode()
        refuses(barrier, row, ValueError, ERROR)
    else:
        row["seal"] = encoded(foreign_seal("controller")).decode()
        row["context"] = PRIVATE
        refuses(barrier, row, StandingConflict)


def model_graph(value):
    if isinstance(value, BaseModel):
        yield value
        for child in value.__dict__.values():
            yield from model_graph(child)
    elif type(value) in {tuple, list}:
        for child in value:
            yield from model_graph(child)


@pytest.mark.parametrize("fault", ("scalar", "fields-set", "extra"))
def test_independent_calls_rebuild_detached_complete_actual_models(barrier, fault):
    row = fixture_row()
    before = copy.deepcopy(row)
    first = assert_row(barrier, row)
    target = first[0].claim.request.task.model_pin
    if fault == "scalar":
        target.__dict__["model"] = PRIVATE
    elif fault == "fields-set":
        object.__setattr__(target, "__pydantic_fields_set__", {"model"})
    else:
        object.__setattr__(target, "__pydantic_extra__", {PRIVATE: True})
    second = assert_row(barrier, row)
    assert row == before
    assert {id(model) for model in model_graph(first)}.isdisjoint(
        id(model) for model in model_graph(second)
    )
    assert {id(model.__pydantic_fields_set__) for model in model_graph(first)}.isdisjoint(
        id(model.__pydantic_fields_set__) for model in model_graph(second)
    )
    row["receipt"] = encoded(foreign_receipt("claim-record")).decode()
    refuses(barrier, row, ValueError, ERROR)
    row.update(before)
    assert_row(barrier, row)


@pytest.mark.parametrize("excess", (0, 1))
def test_raw_claim_cap_and_noncanonical_padding_do_not_become_stored_authority(barrier, excess):
    row = fixture_row()
    raw = row["claim"]
    row["claim"] += " " * (147456 + excess - len(raw.encode("utf-8")))
    assert len(row["claim"].encode("utf-8")) == 147456 + excess
    refuses(barrier, row)


@pytest.mark.parametrize(
    "column,maximum",
    (("binding", 163840), ("receipt", 180224), ("seal", 147456), ("context", 786432)),
)
def test_complete_new_packet_raw_byte_caps_precede_canonical_row_comparison(
    barrier, column, maximum
):
    row = fixture_row()
    row[column] += " " * (maximum + 1 - len(row[column].encode("utf-8")))
    assert len(row[column].encode("utf-8")) == maximum + 1
    refuses(barrier, row, ValueError, ERROR)
