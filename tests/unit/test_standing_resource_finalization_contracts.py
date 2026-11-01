"""Independent frozen-v1 Resources finalization grammar oracles.

Written from 228-resource-finalization-contract-v1.md before implementation read.
Pure packets prove neither native persistence nor physical cleanup. All vectors
are synthetic; hashes are independently calculated with the standard library.
"""

import copy
import hashlib
import importlib
import json
from pathlib import Path

import pytest
from pydantic import BaseModel
from test_standing_admission_contract import CREATED, EVENT
from test_standing_execution_contract import GOLDEN_INTENT, PREPARED, claim, initial_expected

from packages.contracts import agent_control, standing_work
from packages.contracts.standing_admission import StandingInitialProof
from packages.contracts.standing_closure import StandingAdmissionClosure
from packages.contracts.standing_execution import StandingExecutionClaim, StandingExecutionIntent
from packages.contracts.standing_resources import (
    StandingResourceObservation,
    StandingResourceReservation,
    TaskResourceContext,
)

MODULE = "packages.contracts.standing_resource_finalization"
ERROR = "Invalid standing resource finalization contract"
MARKER = "SYNTHETIC-UNTRUSTED-FINALIZATION-INPUT"
SAFE = 9_007_199_254_740_991
BACKEND = "b" * 32
INSTANCE = "00000000-0000-4000-8000-000000000991"
LEASE = "00000000-0000-4000-8000-000000000992"
OTHER_INSTANCE = "00000000-0000-4000-8000-000000000993"
ACTION_KEY = 'handoff: café "literal" 🧭'
CAPS = {
    "StandingResourceBindingRequest": ("MAX_STANDING_RESOURCE_BINDING_BYTES", 163840),
    "StandingResourceBindingReceipt": ("MAX_STANDING_RESOURCE_BINDING_RECEIPT_BYTES", 180224),
    "StandingResourceFinalizeRequest": ("MAX_STANDING_RESOURCE_FINALIZE_REQUEST_BYTES", 147456),
    "StandingResourceFinalizeContext": ("MAX_STANDING_RESOURCE_FINALIZE_CONTEXT_BYTES", 786432),
    "StandingResourceFinalization": ("MAX_STANDING_RESOURCE_FINALIZATION_BYTES", 802816),
    "StandingResourceFinalizationReceipt": (
        "MAX_STANDING_RESOURCE_FINALIZATION_RECEIPT_BYTES",
        819200,
    ),
}
DOMAINS = {
    "binding": "kinaiya.standing.resource-binding.v1",
    "context": "kinaiya.standing.resource-finalize-context.v1",
    "finalization": "kinaiya.standing.resource-finalization.v1",
}
# Frozen independently from the literal vectors and unchanged baseline schemas.
GOLDEN_HASHES = {
    "binding": "f1d94bb5db5484bfbccbcd250b48d54cd1ae99f52cb60513c3dfa90c69378685",
    "context": "627fdd3925068b3c19f990c7ef6b2d4873b1174eb9f0f97ee692783fae5c99f1",
    "finalization": "cad9088e0dd6c2a8c7dcff1f0893f586aa6932d3b3451b1ce5079cb2750bc80c",
}
OLD_SCHEMA_HASH = "21351c70f152823480441448d140c49cd561aa9be0a9a4e760c1d8035257328a"


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(domain, body):
    return hashlib.sha256(domain.encode("ascii") + b"\0" + encoded(body)).hexdigest()


def contract():
    try:
        return importlib.import_module(MODULE)
    except ModuleNotFoundError as error:
        if error.name != MODULE:
            raise
        pytest.fail("Frozen Resources finalization family is not implemented", pytrace=False)


def rehash(raw, field, domain):
    body = {key: value for key, value in raw.items() if key != field}
    raw[field] = digest(domain, body)
    return raw


def initial(record, *, event=EVENT, sequence=17, created=CREATED):
    native = {
        **initial_expected(record),
        "event_id": event,
        "sequence": sequence,
        "origin": "standing-supervisor",
        "created_at": created,
    }
    return {
        "schema_version": "1.0",
        "claim": copy.deepcopy(record),
        "event_id": event,
        "sequence": sequence,
        "created_at": created,
        "entry_digest": digest(
            "kinaiya.standing.initial.v1",
            {
                "claim_id": record["claim_id"],
                "claim_digest": record["claim_digest"],
                "entry": native,
            },
        ),
    }


def vectors(
    *,
    admitted=True,
    reserved=True,
    resource_disposition=None,
    restoration=None,
    original=None,
    prepared=PREPARED,
    event=EVENT,
    sequence=17,
    created=CREATED,
):
    assert admitted or not reserved
    original = copy.deepcopy(GOLDEN_INTENT if original is None else original)
    record = claim(original, prepared=prepared)
    accepted = (
        initial(record, event=event, sequence=sequence, created=created) if admitted else None
    )
    binding = {
        "schema_version": "1.0",
        "claim": copy.deepcopy(record),
        "resource_backend": BACKEND,
        "resource_instance": INSTANCE,
    }
    binding_receipt = {
        "schema_version": "1.0",
        "disposition": "created",
        "request": copy.deepcopy(binding),
        "request_digest": digest(DOMAINS["binding"], binding),
    }
    request = {
        "schema_version": "1.0",
        "idempotency_key": "finalize:synthetic-1",
        "original": copy.deepcopy(original),
        "expected_resource_backend": BACKEND,
        "expected_resource_instance": INSTANCE,
    }
    closure_body = {
        "schema_version": "1.0",
        "intent_digest": record["intent_digest"],
        "claim_id": record["claim_id"],
        "initial": copy.deepcopy(accepted),
    }
    closure = {
        **closure_body,
        "closure_digest": digest("kinaiya.standing.admission-closure.v1", closure_body),
    }
    reservation = None
    if reserved:
        task_context_body = {
            "schema_version": "1.0",
            "query": {"schema_version": "1.0", "task_id": original["task"]["task_id"]},
            "backend_id": original["expected_backend"],
            "family": "standing",
            "standing": copy.deepcopy(accepted),
            "ordinary": None,
        }
        task_context = {
            **task_context_body,
            "context_digest": digest("kinaiya.standing.resource-context.v1", task_context_body),
        }
        action = {
            "operation": "handoff",
            "idempotency_key": ACTION_KEY,
            "task_id": original["task"]["task_id"],
            "required_bytes": 134217728,
            "reason": "Synthetic café 🧭",
        }
        reservation = {
            "schema_version": "1.0",
            "context": task_context,
            "resource_backend": BACKEND,
            "resource_instance": INSTANCE,
            # The OLD Resources action grammar uses ensure_ascii=True.
            "action_digest": hashlib.sha256(
                json.dumps(action, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "lease_id": LEASE,
        }
    observation_body = {
        "schema_version": "1.0",
        "intent_digest": record["intent_digest"],
        "claim_id": record["claim_id"],
        "closure_digest": closure["closure_digest"],
        "coverage_sequence": 16,
        "accepted_sequence": sequence if admitted else None,
        "coverage": "accepted-after-bootstrap" if admitted else "legacy-or-unadmitted",
        "reservation": reservation,
    }
    observation = {
        **observation_body,
        "observation_digest": digest("kinaiya.standing.resource-observation.v1", observation_body),
    }
    context_body = {
        "schema_version": "1.0",
        "request": request,
        "binding": binding,
        "closure": closure,
        "observation": observation,
    }
    context = {**context_body, "context_digest": digest(DOMAINS["context"], context_body)}
    finalization_body = {
        "schema_version": "1.0",
        "context": context,
        "resource_disposition": resource_disposition
        or ("matching-lease-finalized" if reserved else "no-reservation"),
        "handoff_action_key": ACTION_KEY if reserved else None,
        "handoff_action_digest": reservation["action_digest"] if reserved else None,
        "reserved_lease_id": LEASE if reserved else None,
        "policy_revision": 5,
        "owner_intent_revision": 8,
        "restoration": restoration or ("restored" if reserved else "not-required"),
    }
    finalization = {
        **finalization_body,
        "finalization_digest": digest(DOMAINS["finalization"], finalization_body),
    }
    receipt = {
        "schema_version": "1.0",
        "disposition": "created",
        "finalization": finalization,
    }
    return dict(
        zip(CAPS, (binding, binding_receipt, request, context, finalization, receipt), strict=True)
    )


def parsed(name, **options):
    return getattr(contract(), name).model_validate(copy.deepcopy(vectors(**options)[name]))


def refuses(invoke, *args, normalized=False):
    with pytest.raises(ValueError) as caught:
        invoke(*args)
    message = str(caught.value)
    assert MARKER not in message
    assert "input_value=" not in message
    assert caught.value.__cause__ is None
    if normalized:
        assert message == ERROR
    else:
        assert ERROR in message


def new_context_hash(raw):
    return rehash(raw, "context_digest", DOMAINS["context"])


def new_finalization_hash(raw):
    return rehash(raw, "finalization_digest", DOMAINS["finalization"])


def observation_hash(raw):
    return rehash(raw, "observation_digest", "kinaiya.standing.resource-observation.v1")


def at_path(value, path):
    for field in path:
        value = value[field] if type(value) is dict else getattr(value, field)
    return value


def model_paths(value, path=()):
    if isinstance(value, BaseModel):
        yield path, value
        for field, child in value.__dict__.items():
            yield from model_paths(child, (*path, field))


def graph_depth(value):
    if type(value) is dict:
        return 1 + max((graph_depth(item) for pair in value.items() for item in pair), default=0)
    if type(value) is list:
        return 1 + max(map(graph_depth, value), default=0)
    return 1


def schema_pin():
    root = Path(__file__).resolve().parents[2] / "packages" / "contracts" / "schemas"
    # The original 184 stay exact; six finalization and two later #255 schemas
    # are additive families whose names are explicitly outside this old pin.
    additive = set(CAPS) | {"TranscriptScanPage", "TranscriptScanRequest"}
    paths = sorted(path for path in root.glob("*.json") if path.stem not in additive)
    body = b"".join(path.name.encode() + b"\0" + path.read_bytes() + b"\0" for path in paths)
    return len(paths), hashlib.sha256(body).hexdigest()


def test_independent_literal_vectors_are_valid_old_full_graphs_before_new_family_exists():
    for admitted, reserved in ((False, False), (True, False), (True, True)):
        raw = vectors(admitted=admitted, reserved=reserved)
        binding = raw["StandingResourceBindingRequest"]
        context = raw["StandingResourceFinalizeContext"]
        original = StandingExecutionIntent.model_validate(context["request"]["original"])
        assert StandingExecutionClaim.model_validate(binding["claim"]).validate_request(original)
        closure = StandingAdmissionClosure.model_validate(context["closure"])
        assert closure.validate_request(original)
        observation = StandingResourceObservation.model_validate(context["observation"])
        assert observation.validate_request(original)
        if reserved:
            reservation = StandingResourceReservation.model_validate(
                observation.model_dump()["reservation"]
            )
            assert reservation.context.standing.validate_request(original)
            assert StandingInitialProof.model_validate(context["closure"]["initial"])
    raw = vectors()
    assert raw["StandingResourceBindingReceipt"]["request_digest"] == GOLDEN_HASHES["binding"]
    assert raw["StandingResourceFinalizeContext"]["context_digest"] == GOLDEN_HASHES["context"]
    assert (
        raw["StandingResourceFinalization"]["finalization_digest"] == GOLDEN_HASHES["finalization"]
    )


def test_old_codecs_schema_bytes_and_depth_limits_are_preserved():
    assert schema_pin() == (184, OLD_SCHEMA_HASH)
    assert agent_control.MAX_AGENT_CONTROL_JSON_DEPTH == 12
    assert agent_control.MAX_AGENT_CONTROL_JSON_NODES == 2048
    raw = vectors()["StandingResourceFinalizationReceipt"]
    assert graph_depth(raw) == 13
    with pytest.raises(ValueError):
        standing_work._tree(raw)
    with pytest.raises(ValueError):
        standing_work.standing_json(encoded(raw), maximum=819200)
    context = vectors()["StandingResourceFinalizeContext"]
    for kind, child in (
        (StandingAdmissionClosure, context["closure"]),
        (StandingResourceObservation, context["observation"]),
        (TaskResourceContext, context["observation"]["reservation"]["context"]),
    ):
        assert kind.from_json(encoded(child)).canonical_bytes() == encoded(child)


@pytest.mark.parametrize("name", CAPS)
def test_six_packets_roundtrip_exact_caps_utf8_order_detachment_and_immutability(name):
    module, raw = contract(), vectors()[name]
    kind = getattr(module, name)
    value = kind.model_validate(copy.deepcopy(raw))
    constant, maximum = CAPS[name]
    assert getattr(module, constant) == kind.wire_bytes == maximum
    assert "wire_bytes" not in kind.model_fields
    assert kind.model_config["strict"] is True
    assert kind.model_config["frozen"] is True
    assert kind.model_config["extra"] == "forbid"
    assert kind.model_config["hide_input_in_errors"] is True
    assert value.canonical_bytes() == encoded(raw)
    assert module.finalization_bytes(value) == encoded(raw)
    assert type(module.checked_finalization(value, kind)) is kind
    assert module.checked_finalization(value, kind).canonical_bytes() == encoded(raw)
    assert kind.from_json(encoded(raw)).canonical_bytes() == encoded(raw)
    assert kind.model_validate(dict(reversed(list(raw.items())))).canonical_bytes() == encoded(raw)
    raw.clear()
    assert value.canonical_bytes() == encoded(vectors()[name])
    with pytest.raises(ValueError):
        value.schema_version = "1.1"
    with pytest.raises(ValueError):
        del value.schema_version


def test_public_hashes_are_independent_complete_domain_separated_bodies():
    module = contract()
    binding = parsed("StandingResourceBindingRequest")
    context = parsed("StandingResourceFinalizeContext")
    finalization = parsed("StandingResourceFinalization")
    assert module.resource_binding_digest(binding) == GOLDEN_HASHES["binding"]
    assert (
        module.resource_finalize_context_digest(
            context.request, context.binding, context.closure, context.observation
        )
        == GOLDEN_HASHES["context"]
    )
    assert (
        module.resource_finalization_digest(
            finalization.context,
            finalization.resource_disposition,
            finalization.handoff_action_key,
            finalization.handoff_action_digest,
            finalization.reserved_lease_id,
            finalization.policy_revision,
            finalization.owner_intent_revision,
            finalization.restoration,
        )
        == GOLDEN_HASHES["finalization"]
    )
    assert len(set(GOLDEN_HASHES.values())) == 3
    for key in ("policy_revision", "owner_intent_revision"):
        changed = vectors()["StandingResourceFinalization"]
        changed[key] += 1
        changed = new_finalization_hash(changed)
        assert changed["finalization_digest"] != GOLDEN_HASHES["finalization"]
        assert module.StandingResourceFinalization.model_validate(changed)
    # Coverage is retained context content, not merely a checksum shortcut.
    changed = vectors()["StandingResourceFinalizeContext"]
    changed["observation"]["coverage_sequence"] = 15
    observation_hash(changed["observation"])
    new_context_hash(changed)
    assert changed["context_digest"] != GOLDEN_HASHES["context"]
    assert module.StandingResourceFinalizeContext.model_validate(changed)


@pytest.mark.parametrize("name", CAPS)
def test_all_top_level_fields_are_required_closed_and_versioned(name):
    kind, raw = getattr(contract(), name), vectors(reserved=False)[name]
    assert set(raw) == set(kind.model_fields)
    assert all(field.is_required() for field in kind.model_fields.values())
    for field in raw:
        refuses(kind.model_validate, {key: item for key, item in raw.items() if key != field})
    refuses(kind.model_validate, {**raw, MARKER: True})
    for version in (None, True, 1, 1.0, "1.1", MARKER):
        refuses(kind.model_validate, {**raw, "schema_version": version})


def test_every_nested_required_field_remains_required_before_old_serializers_run():
    kind, raw = (
        contract().StandingResourceFinalizationReceipt,
        vectors()["StandingResourceFinalizationReceipt"],
    )
    pending = [((), raw)]
    while pending:
        path, nested = pending.pop()
        for field, child in nested.items():
            bad = copy.deepcopy(raw)
            at_path(bad, path).pop(field)
            refuses(kind.model_validate, bad)
            if type(child) is dict:
                pending.append(((*path, field), child))


@pytest.mark.parametrize("admitted,reserved", ((False, False), (True, False), (True, True)))
def test_full_admitted_unadmitted_and_reserved_contexts_bind_original(admitted, reserved):
    module = contract()
    raw = vectors(admitted=admitted, reserved=reserved)
    context = module.StandingResourceFinalizeContext.model_validate(
        raw["StandingResourceFinalizeContext"]
    )
    assert context.validate_request(context.request).canonical_bytes() == encoded(
        raw["StandingResourceFinalizeContext"]
    )
    assert context.observation.accepted_sequence == (17 if admitted else None)
    assert (context.closure.initial is not None) is admitted
    assert (context.observation.reservation is not None) is reserved
    if not admitted:
        assert context.observation.coverage == "legacy-or-unadmitted"


@pytest.mark.parametrize(
    "path,replacement",
    (
        (("task", "objective"), "A distinct synthetic objective."),
        (("task", "model_pin", "model"), "different-model"),
        (("task", "model_pin", "settings_revision"), 8),
        (("task", "model_pin", "catalog_binding", "configuration_sha256"), "e" * 64),
        (("task", "workspace_pin", "root_identity"), "d" * 64),
        (("task", "task_session", "epoch"), 4),
        (("task", "task_id"), OTHER_INSTANCE),
        (("producer", "request", "sequence"), 3),
        (("executor_id",), OTHER_INSTANCE),
        (("record_digest",), "d" * 64),
        (("idempotency_key",), "standing:different"),
    ),
)
def test_full_claim_original_identity_cannot_be_borrowed_despite_rehashed_context(
    path, replacement
):
    kind = contract().StandingResourceFinalizeContext
    changed = copy.deepcopy(GOLDEN_INTENT)
    at_path(changed, path[:-1])[path[-1]] = replacement
    other = vectors(original=changed)
    assert StandingExecutionClaim.model_validate(other["StandingResourceBindingRequest"]["claim"])
    for field in ("binding", "closure", "observation"):
        bad = vectors()["StandingResourceFinalizeContext"]
        bad[field] = copy.deepcopy(other["StandingResourceFinalizeContext"][field])
        refuses(kind.model_validate, new_context_hash(bad))


def test_context_joins_resource_controller_closure_digest_sequence_and_full_prepared_claim():
    kind = contract().StandingResourceFinalizeContext
    changes = [
        (("request", "expected_resource_backend"), "c" * 32),
        (("request", "expected_resource_instance"), OTHER_INSTANCE),
        (("binding", "resource_backend"), "c" * 32),
        (("binding", "resource_instance"), OTHER_INSTANCE),
    ]
    for path, replacement in changes:
        bad = vectors()["StandingResourceFinalizeContext"]
        at_path(bad, path[:-1])[path[-1]] = replacement
        refuses(kind.model_validate, new_context_hash(bad))
    bad = vectors()["StandingResourceFinalizeContext"]
    bad["binding"]["claim"] = claim(prepared="2026-11-01T08:30:02Z")
    assert StandingExecutionClaim.model_validate(bad["binding"]["claim"])
    refuses(kind.model_validate, new_context_hash(bad))
    for accepted in (None, 18):
        bad = vectors(reserved=False)["StandingResourceFinalizeContext"]
        bad["observation"]["accepted_sequence"] = accepted
        bad["observation"]["coverage"] = (
            "legacy-or-unadmitted" if accepted is None else "accepted-after-bootstrap"
        )
        observation_hash(bad["observation"])
        assert StandingResourceObservation.model_validate(bad["observation"])
        refuses(kind.model_validate, new_context_hash(bad))
    bad = vectors()["StandingResourceFinalizeContext"]
    bad["observation"]["closure_digest"] = "e" * 64
    observation_hash(bad["observation"])
    assert StandingResourceObservation.model_validate(bad["observation"])
    refuses(kind.model_validate, new_context_hash(bad))


def test_reservation_resource_identity_and_entire_initial_are_joined_not_only_claim_id():
    kind = contract().StandingResourceFinalizeContext
    for field, replacement in (
        ("resource_backend", "c" * 32),
        ("resource_instance", OTHER_INSTANCE),
    ):
        bad = vectors()["StandingResourceFinalizeContext"]
        bad["observation"]["reservation"][field] = replacement
        observation_hash(bad["observation"])
        assert StandingResourceObservation.model_validate(bad["observation"])
        refuses(kind.model_validate, new_context_hash(bad))
    for options in (
        {"prepared": "2026-11-01T08:30:02Z"},
        {"event": OTHER_INSTANCE},
        {"created": "2026-11-01T08:30:01.002000Z"},
    ):
        bad = vectors()["StandingResourceFinalizeContext"]
        foreign = vectors(**options)["StandingResourceFinalizeContext"]["observation"][
            "reservation"
        ]
        bad["observation"]["reservation"] = foreign
        observation_hash(bad["observation"])
        assert StandingResourceObservation.model_validate(bad["observation"])
        refuses(kind.model_validate, new_context_hash(bad))


def test_coverage_label_never_replaces_full_join_and_legacy_label_is_valid_metadata():
    kind = contract().StandingResourceFinalizeContext
    for admitted in (False, True):
        raw = vectors(admitted=admitted, reserved=False)["StandingResourceFinalizeContext"]
        raw["observation"]["coverage_sequence"] = 18
        raw["observation"]["coverage"] = "legacy-or-unadmitted"
        observation_hash(raw["observation"])
        assert (
            kind.model_validate(new_context_hash(raw)).observation.coverage
            == "legacy-or-unadmitted"
        )


@pytest.mark.parametrize(
    "admitted,reserved,disposition,restoration",
    (
        (False, False, "no-reservation", "not-required"),
        (True, False, "no-reservation", "not-required"),
        (True, True, "registered-no-lease", "not-required"),
        (True, True, "registered-no-lease", "restored"),
        (True, True, "registered-no-lease", "owner-off"),
        (True, True, "matching-lease-finalized", "restored"),
        (True, True, "matching-lease-finalized", "owner-off"),
    ),
)
@pytest.mark.parametrize("receipt_disposition", ("created", "retained"))
def test_only_three_exact_dispositions_and_permitted_restoration_histories(
    admitted, reserved, disposition, restoration, receipt_disposition
):
    module = contract()
    raw = vectors(
        admitted=admitted,
        reserved=reserved,
        resource_disposition=disposition,
        restoration=restoration,
    )
    raw["StandingResourceFinalizationReceipt"]["disposition"] = receipt_disposition
    result = module.StandingResourceFinalizationReceipt.model_validate(
        raw["StandingResourceFinalizationReceipt"]
    )
    assert result.validate_request(result.finalization.context.request) == result
    assert result.finalization.resource_disposition == disposition
    assert result.finalization.restoration == restoration
    assert result.disposition == receipt_disposition
    if not reserved:
        assert result.finalization.handoff_action_key is None
        assert result.finalization.handoff_action_digest is None
        assert result.finalization.reserved_lease_id is None


def test_discriminators_require_exact_reservation_and_explicit_action_nulls():
    kind = contract().StandingResourceFinalization
    for reserved in (False, True):
        source = vectors(reserved=reserved)["StandingResourceFinalization"]
        bad = copy.deepcopy(source)
        bad["resource_disposition"] = "no-reservation" if reserved else "registered-no-lease"
        refuses(kind.model_validate, new_finalization_hash(bad))
        for field in ("handoff_action_key", "handoff_action_digest", "reserved_lease_id"):
            bad = copy.deepcopy(source)
            bad[field] = (
                None
                if reserved
                else {
                    "handoff_action_key": ACTION_KEY,
                    "handoff_action_digest": "e" * 64,
                    "reserved_lease_id": LEASE,
                }[field]
            )
            refuses(kind.model_validate, new_finalization_hash(bad))
    for field, replacement in (
        ("handoff_action_digest", "e" * 64),
        ("reserved_lease_id", OTHER_INSTANCE),
    ):
        bad = vectors()["StandingResourceFinalization"]
        bad[field] = replacement
        refuses(kind.model_validate, new_finalization_hash(bad))
    for disposition, restoration, reserved in (
        ("matching-lease-finalized", "not-required", True),
        ("no-reservation", "restored", False),
        ("no-reservation", "owner-off", False),
        ("released", "restored", True),
        (MARKER, "restored", True),
        ("registered-no-lease", "released", True),
    ):
        bad = vectors(reserved=reserved)["StandingResourceFinalization"]
        bad.update(resource_disposition=disposition, restoration=restoration)
        refuses(kind.model_validate, new_finalization_hash(bad))


def test_old_arbitrary_unicode_action_key_character_bounds_are_preserved():
    kind = contract().StandingResourceFinalization
    for key in ("a", " ", "🧭" * 256, "literal\n\0 café é"):
        raw = vectors()["StandingResourceFinalization"]
        raw["handoff_action_key"] = key
        assert kind.model_validate(new_finalization_hash(raw)).handoff_action_key == key
    for key in ("", "🧭" * 257, True, 1, [MARKER], (MARKER,)):
        bad = vectors()["StandingResourceFinalization"]
        bad["handoff_action_key"] = key
        if type(key) is tuple:
            refuses(kind.model_validate, bad)
        else:
            refuses(kind.model_validate, new_finalization_hash(bad))


@pytest.mark.parametrize("field", ("policy_revision", "owner_intent_revision"))
def test_revision_safe_endpoints_and_no_boolean_float_or_string_coercions(field):
    kind = contract().StandingResourceFinalization
    for revision in (0, SAFE):
        raw = vectors()["StandingResourceFinalization"]
        raw[field] = revision
        assert getattr(kind.model_validate(new_finalization_hash(raw)), field) == revision
    for revision in (-1, SAFE + 1, True, False, 1.0, "1", None):
        raw = vectors()["StandingResourceFinalization"]
        raw[field] = revision
        refuses(kind.model_validate, new_finalization_hash(raw))


def test_validate_request_compares_complete_binding_and_finalize_original():
    module = contract()
    binding_receipt = parsed("StandingResourceBindingReceipt")
    assert binding_receipt.validate_request(binding_receipt.request) == binding_receipt
    for field, replacement in (
        ("resource_backend", "c" * 32),
        ("resource_instance", OTHER_INSTANCE),
    ):
        raw = vectors()["StandingResourceBindingRequest"]
        raw[field] = replacement
        refuses(
            binding_receipt.validate_request,
            module.StandingResourceBindingRequest.model_validate(raw),
            normalized=True,
        )
    raw = vectors()["StandingResourceBindingRequest"]
    raw["claim"] = claim(prepared="2026-11-01T08:30:02Z")
    refuses(
        binding_receipt.validate_request,
        module.StandingResourceBindingRequest.model_validate(raw),
        normalized=True,
    )
    for name in (
        "StandingResourceFinalizeContext",
        "StandingResourceFinalization",
        "StandingResourceFinalizationReceipt",
    ):
        value = parsed(name)
        for field, replacement in (
            ("idempotency_key", "finalize:another"),
            ("expected_resource_backend", "c" * 32),
            ("expected_resource_instance", OTHER_INSTANCE),
        ):
            raw = vectors()["StandingResourceFinalizeRequest"]
            raw[field] = replacement
            refuses(
                value.validate_request,
                module.StandingResourceFinalizeRequest.model_validate(raw),
                normalized=True,
            )
        raw = vectors()["StandingResourceFinalizeRequest"]
        raw["original"]["task"]["objective"] += " A different literal objective."
        refuses(
            value.validate_request,
            module.StandingResourceFinalizeRequest.model_validate(raw),
            normalized=True,
        )


def test_all_actual_nested_models_revalidate_without_losing_fields_metadata_or_extras():
    module = contract()
    value = parsed("StandingResourceFinalizationReceipt")
    for path, child in model_paths(value):
        assert module.checked_finalization(child, type(child)).model_dump(
            mode="json"
        ) == child.model_dump(mode="json")
        for forgery in ("extra", "missing", "fields-set", "fields-set-type", "extras"):
            bad = value.model_copy(deep=True)
            target = at_path(bad, path)
            if forgery == "extra":
                target.__dict__[MARKER] = True
            elif forgery == "missing":
                target.__dict__.pop(next(iter(target.__dict__)))
            elif forgery == "fields-set":
                target.__pydantic_fields_set__.add(MARKER)
            elif forgery == "fields-set-type":
                object.__setattr__(
                    target, "__pydantic_fields_set__", tuple(target.__pydantic_fields_set__)
                )
            else:
                object.__setattr__(target, "__pydantic_extra__", {MARKER: True})
            refuses(module.checked_finalization, bad, type(value), normalized=True)
            refuses(module.finalization_bytes, bad, normalized=True)
            refuses(bad.canonical_bytes, normalized=True)


def test_copied_actual_children_cannot_replace_typed_state_with_equivalent_raw_dicts():
    module = contract()
    value = parsed("StandingResourceFinalizationReceipt")
    for path, child in model_paths(value):
        if not path:
            continue
        bad = value.model_copy(deep=True)
        at_path(bad, path[:-1]).__dict__[path[-1]] = child.model_dump(mode="python")
        refuses(module.checked_finalization, bad, type(value), normalized=True)
        refuses(module.finalization_bytes, bad, normalized=True)


def test_subclass_and_unrelated_actual_model_identity_are_not_normalized_away():
    module = contract()
    value = parsed("StandingResourceFinalizationReceipt")
    for path, child in model_paths(value):
        subclass = type("SyntheticSubclass", (type(child),), {})
        copied = subclass.model_construct(**copy.deepcopy(child.__dict__))
        bad = value.model_copy(deep=True)
        if path:
            at_path(bad, path[:-1]).__dict__[path[-1]] = copied
        else:
            bad = copied
        refuses(module.checked_finalization, bad, type(value), normalized=True)
        refuses(module.finalization_bytes, bad, normalized=True)

    class Unrelated(BaseModel):
        schema_version: str

    refuses(module.finalization_bytes, Unrelated(schema_version="1.0"), normalized=True)
    for value, kind in (
        ({}, module.StandingResourceBindingRequest),
        (None, module.StandingResourceBindingRequest),
        (parsed("StandingResourceBindingRequest"), dict),
    ):
        refuses(module.checked_finalization, value, kind, normalized=True)


def test_true_deep_known_scalar_forgeries_and_hash_helpers_are_rechecked_before_dump():
    module = contract()
    value = parsed("StandingResourceFinalizationReceipt")
    for path, field, replacement in (
        (
            (
                "finalization",
                "context",
                "observation",
                "reservation",
                "context",
                "standing",
                "claim",
                "request",
                "task",
                "model_pin",
            ),
            "local",
            1,
        ),
        (
            ("finalization", "context", "binding", "claim", "request", "task", "workspace_pin"),
            "settings_revision",
            True,
        ),
        (
            (
                "finalization",
                "context",
                "request",
                "original",
                "task",
                "model_pin",
                "catalog_binding",
            ),
            "generation",
            "3",
        ),
        (("finalization", "context", "closure", "initial"), "sequence", 17.0),
        (("finalization", "context", "observation"), "accepted_sequence", True),
        (("finalization",), "policy_revision", SAFE + 1),
    ):
        bad = value.model_copy(deep=True)
        at_path(bad, path).__dict__[field] = replacement
        refuses(module.finalization_bytes, bad, normalized=True)
        refuses(module.checked_finalization, bad, type(value), normalized=True)
    binding = parsed("StandingResourceBindingRequest")
    binding.claim.request.task.model_pin.__dict__[MARKER] = True
    refuses(module.resource_binding_digest, binding, normalized=True)
    context = parsed("StandingResourceFinalizeContext")
    context.observation.reservation.context.standing.claim.request.task.workspace_pin.__dict__[
        MARKER
    ] = True
    refuses(
        module.resource_finalize_context_digest,
        context.request,
        context.binding,
        context.closure,
        context.observation,
        normalized=True,
    )


@pytest.mark.parametrize("name", CAPS)
def test_raw_caps_duplicate_aliases_encoding_scalars_and_malformed_inputs(name):
    kind, raw = getattr(contract(), name), vectors()[name]
    body = encoded(raw)
    maximum = CAPS[name][1]
    exact = b" " * (maximum - len(body)) + body
    assert kind.from_json(exact).canonical_bytes() == body
    invalid = (
        b" " + exact,
        b" " * 819201,
        b'{"schema_version":"1.0",' + body[1:],
        b'{"schema_\\u0076ersion":"1.0",' + body[1:],
        b"\xff" + body,
        body + b" {}",
        b"[]",
        b"null",
        b"true",
        b"NaN",
        b"1.0",
        body.decode("utf-8"),
        bytearray(body),
        memoryview(body),
    )
    for malformed in invalid:
        refuses(kind.from_json, malformed, normalized=True)
    for bad_scalar in (b"1.0", b"NaN", b"Infinity", b"-Infinity", str(SAFE + 1).encode()):
        malformed = body[:-1] + b',"' + MARKER.encode() + b'":' + bad_scalar + b"}"
        refuses(kind.from_json, malformed, normalized=True)
    surrogate = body[:-1] + b',"' + MARKER.encode() + b'":"\\ud800"}'
    refuses(kind.from_json, surrogate, normalized=True)


def test_depth13_full_graph_roundtrips_with_new_codec_only_and_refuses_unbounded_graphs():
    module = contract()
    raw = vectors()["StandingResourceFinalizationReceipt"]
    assert graph_depth(raw) == 13
    value = module.StandingResourceFinalizationReceipt.from_json(encoded(raw))
    assert module.finalization_bytes(value) == encoded(raw)
    assert (
        value.finalization.context.observation.reservation.context.standing.claim.request.task.model_pin.catalog_binding.id
        == "synthetic.profile"
    )
    for invalid in (object(), (1,), {1: MARKER}, "bad\ud800text", float("nan"), 1.5, SAFE + 1):
        bad = copy.deepcopy(raw)
        bad[MARKER] = invalid
        refuses(module.StandingResourceFinalizationReceipt.model_validate, bad)
    nested = MARKER
    for _ in range(17):
        nested = {"x": nested}
    for extra in (nested, [MARKER] * 16385):
        bad = {**raw, MARKER: extra}
        refuses(module.StandingResourceFinalizationReceipt.from_json, encoded(bad), normalized=True)
