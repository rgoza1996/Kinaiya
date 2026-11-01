"""Independent Codex oracles frozen from dispatch boundary v1, before source read.

These validate packet grammar and binding, never native liveness or admission.
All vectors are public synthetic literals; hashes use Python's stdlib only.
"""

import copy
import hashlib
import itertools
import json

import pytest
from packages.contracts.standing_dispatch import (
    MAX_STANDING_DISPATCH_EPOCH,
    MAX_STANDING_DISPATCH_REQUEST_BYTES,
    MAX_STANDING_DISPATCH_RESPONSE_BYTES,
    STANDING_PRODUCER_TTL_MS,
    StandingDispatchActivation,
    StandingDispatchActivationLookup,
    StandingDispatchActivationReceipt,
    StandingDispatchState,
    StandingDispatchStop,
    StandingDispatchStopProof,
    StandingProducerReceipt,
    StandingProducerRenewal,
    activation_digest,
)

BACKEND = "a" * 32
INSTANCE = "00000000-0000-0000-0000-000000000111"
OTHER = "00000000-0000-0000-0000-000000000222"
PRODUCER = "00000000-0000-0000-0000-000000000333"
FENCE = "00000000-0000-0000-0000-000000000444"
LEASE = "00000000-0000-0000-0000-000000000555"
SAFE = 9_007_199_254_740_991
TTL = 15000
EPOCHS = 10000


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def renewal():
    return {
        "schema_version": "1.0",
        "expected_backend": BACKEND,
        "expected_instance": INSTANCE,
        "producer_id": PRODUCER,
        "sequence": 1,
    }


def producer():
    return {
        "schema_version": "1.0",
        "request": renewal(),
        "lease_token": LEASE,
        "issued_monotonic_ms": 90000,
        "expires_monotonic_ms": 105000,
    }


def activation():
    return {
        "schema_version": "1.0",
        "mode_change": {
            "schema_version": "1.0",
            "idempotency_key": "synthetic-standing-activation:1",
            "expected_backend": BACKEND,
            "expected_instance": INSTANCE,
            "expected_revision": 0,
            "mode": "autonomous",
            "active_work_policy": "finish_already_accepted",
        },
        "expected_epoch": 0,
        "expected_fence_token": FENCE,
    }


def expected_digest(request, epoch):
    return hashlib.sha256(
        b"kinaiya.standing.dispatch.activation.v1\0"
        + canonical({"epoch": epoch, "request": request})
    ).hexdigest()


def receipt(request=None):
    request = copy.deepcopy(activation() if request is None else request)
    epoch = request["expected_epoch"] + 1
    return {
        "schema_version": "1.0",
        "request": request,
        "mode_receipt": {
            "schema_version": "1.0",
            "status": "mode_saved",
            "backend_id": request["mode_change"]["expected_backend"],
            "service_instance": request["mode_change"]["expected_instance"],
            "revision": request["mode_change"]["expected_revision"] + 1,
            "request": copy.deepcopy(request["mode_change"]),
        },
        "epoch": epoch,
        "activation_digest": expected_digest(request, epoch),
    }


def state(*, active=False):
    return {
        "schema_version": "1.0",
        "backend_id": BACKEND,
        "instance_id": INSTANCE,
        "control_revision": int(active),
        "mode": "autonomous" if active else "on_demand",
        "control_writable": True,
        "epoch": int(active),
        "stopped_through_epoch": 0,
        "fence_token": FENCE,
        "activation_digest": receipt()["activation_digest"] if active else None,
        "activation_control_revision": 1 if active else None,
        "producer": producer(),
        "dispatch_ready": active,
    }


def lookup(*, recorded=False):
    return {
        "schema_version": "1.0",
        "status": "recorded" if recorded else "unobserved",
        "request": activation(),
        "state": state(active=recorded),
        "receipt": receipt() if recorded else None,
        "mutation_fenced": recorded,
    }


def stop(*, epoch=1):
    return {
        "schema_version": "1.0",
        "expected_backend": BACKEND,
        "expected_instance": INSTANCE,
        "issued_epoch": epoch,
        "activation_digest": receipt()["activation_digest"] if epoch else None,
        "expected_fence_token": FENCE,
    }


def stop_proof():
    current = state(active=True)
    current.update(stopped_through_epoch=1, fence_token=OTHER, dispatch_ready=False)
    return {
        "schema_version": "1.0",
        "request": stop(),
        "state": current,
        "target_fenced": True,
    }


KINDS = (
    (StandingProducerRenewal, renewal, 4096),
    (StandingProducerReceipt, producer, 8192),
    (StandingDispatchState, state, 16384),
    (StandingDispatchActivation, activation, 4096),
    (StandingDispatchActivationReceipt, receipt, 16384),
    (StandingDispatchActivationLookup, lookup, 32768),
    (StandingDispatchStop, stop, 4096),
    (StandingDispatchStopProof, stop_proof, 32768),
)


def test_fixed_boundary_constants():
    assert MAX_STANDING_DISPATCH_REQUEST_BYTES == 4096
    assert MAX_STANDING_DISPATCH_RESPONSE_BYTES == 32768
    assert MAX_STANDING_DISPATCH_EPOCH == EPOCHS
    assert STANDING_PRODUCER_TTL_MS == TTL


@pytest.mark.parametrize("kind,vector,maximum", KINDS)
def test_positive_exact_roundtrip_and_detachment(kind, vector, maximum):
    raw = vector()
    expected = canonical(raw)
    model = kind.model_validate(raw)
    assert model.canonical_bytes() == expected
    assert kind.from_json(expected).canonical_bytes() == expected
    raw["schema_version"] = "tampered"
    assert model.canonical_bytes() == expected
    with pytest.raises(ValueError):
        model.schema_version = "tampered"


@pytest.mark.parametrize("kind,vector,maximum", KINDS)
def test_each_actual_raw_byte_boundary(kind, vector, maximum):
    body = canonical(vector())
    assert len(body) < maximum
    padded = b" " * (maximum - len(body)) + body
    assert kind.from_json(padded).canonical_bytes() == body
    with pytest.raises(ValueError):
        kind.from_json(b" " + padded)


@pytest.mark.parametrize("kind,vector,maximum", KINDS)
def test_required_schema_and_closed_unknown_fields(kind, vector, maximum):
    raw = vector()
    del raw["schema_version"]
    with pytest.raises(ValueError):
        kind.model_validate(raw)
    raw = vector()
    raw["unexpected_authority"] = True
    with pytest.raises(ValueError):
        kind.model_validate(raw)
    raw = vector()
    raw["schema_version"] = "2.0"
    with pytest.raises(ValueError):
        kind.model_validate(raw)


@pytest.mark.parametrize("kind,vector,maximum", KINDS)
def test_actual_copied_and_constructed_graph_closure(kind, vector, maximum):
    model = kind.model_validate(vector())
    for invalid in (
        model.model_copy(update={"unexpected_authority": True}),
        model.model_construct(**vector(), unexpected_authority=True),
    ):
        with pytest.raises(ValueError):
            invalid.canonical_bytes()
        with pytest.raises(ValueError):
            kind.model_validate(invalid)


@pytest.mark.parametrize("kind,vector,maximum", KINDS)
def test_raw_duplicate_float_surrogate_and_nonobject_refused(kind, vector, maximum):
    body = canonical(vector())
    for invalid in (
        b'{"schema_version":"1.0",' + body[1:],
        b'{"schema_version":1.0}',
        b'{"schema_version":"\\ud800"}',
        b"null",
        b"[]",
        body + b"x",
        body + b"\xff",
    ):
        with pytest.raises(ValueError):
            kind.from_json(invalid)


@pytest.mark.parametrize("value", [False, True, 0, -1, 1.0, "1", SAFE + 1])
def test_sequence_is_positive_safe_exact_integer(value):
    raw = renewal()
    raw["sequence"] = value
    with pytest.raises(ValueError):
        StandingProducerRenewal.model_validate(raw)


@pytest.mark.parametrize("value", [1, SAFE])
def test_sequence_supported_endpoints(value):
    raw = renewal()
    raw["sequence"] = value
    assert StandingProducerRenewal.model_validate(raw).sequence == value


@pytest.mark.parametrize("field", ["expected_instance", "producer_id"])
@pytest.mark.parametrize("invalid", [OTHER.upper().replace("222", "ABC"), "", "not-a-uuid", None])
def test_producer_uuid_fields_are_strict(field, invalid):
    raw = renewal()
    raw[field] = invalid
    with pytest.raises(ValueError):
        StandingProducerRenewal.model_validate(raw)


@pytest.mark.parametrize("field", ["issued_monotonic_ms", "expires_monotonic_ms"])
@pytest.mark.parametrize("invalid", [-1, True, 1.0, "90000", SAFE + 1])
def test_native_monotonic_ticks_are_bounded_exact_integers(field, invalid):
    raw = producer()
    raw[field] = invalid
    with pytest.raises(ValueError):
        StandingProducerReceipt.model_validate(raw)


@pytest.mark.parametrize("delta", [-1, 1, -15000])
def test_lease_ttl_is_exact_not_client_chosen(delta):
    raw = producer()
    raw["expires_monotonic_ms"] += delta
    with pytest.raises(ValueError):
        StandingProducerReceipt.model_validate(raw)


def test_maximum_monotonic_receipt_and_original_binding():
    raw = producer()
    raw.update(issued_monotonic_ms=SAFE - TTL, expires_monotonic_ms=SAFE)
    model = StandingProducerReceipt.model_validate(raw)
    original = StandingProducerRenewal.model_validate(renewal())
    assert model.validate_request(original).canonical_bytes() == canonical(raw)
    mismatch = renewal()
    mismatch["sequence"] = 2
    with pytest.raises(ValueError):
        model.validate_request(StandingProducerRenewal.model_validate(mismatch))


def test_activation_fixed_independent_hash_vector():
    request = activation()
    # This literal is computed/frozen independently below before source import.
    digest = expected_digest(request, 1)
    assert digest == "1b75a2788d6a09d1f76be42118eee4a76436735ca4ee7e334256d8e1ed773486"
    parsed = StandingDispatchActivation.model_validate(request)
    assert activation_digest(parsed, 1) == digest
    assert StandingDispatchActivationReceipt.model_validate(receipt()).activation_digest == digest
    for epoch in (0, 2, True, "1", 1.0, EPOCHS + 1):
        with pytest.raises(ValueError):
            activation_digest(parsed, epoch)


@pytest.mark.parametrize("mode", ["off", "on_demand", "owner", "decisions"])
def test_new_activation_never_impersonates_or_accepts_other_mode(mode):
    raw = activation()
    raw["mode_change"]["mode"] = mode
    with pytest.raises(ValueError):
        StandingDispatchActivation.model_validate(raw)


@pytest.mark.parametrize("epoch", [True, -1, 1.0, "1", EPOCHS, SAFE])
def test_activation_requires_capacity_for_next_epoch(epoch):
    raw = activation()
    raw["expected_epoch"] = epoch
    with pytest.raises(ValueError):
        StandingDispatchActivation.model_validate(raw)


def test_last_activation_epoch_and_nested_mode_identity():
    raw = activation()
    raw["expected_epoch"] = EPOCHS - 1
    raw["mode_change"]["expected_revision"] = SAFE - 1
    model = StandingDispatchActivationReceipt.model_validate(receipt(raw))
    assert model.epoch == EPOCHS
    assert model.mode_receipt.revision == SAFE
    assert model.validate_request(StandingDispatchActivation.model_validate(raw)) == model
    for path, replacement in (
        (("mode_receipt", "request", "idempotency_key"), "different"),
        (("mode_receipt", "service_instance"), OTHER),
        (("mode_receipt", "revision"), SAFE - 1),
        (("epoch",), EPOCHS - 1),
        (("activation_digest",), "0" * 64),
    ):
        invalid = receipt(raw)
        target = invalid
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = replacement
        with pytest.raises(ValueError):
            StandingDispatchActivationReceipt.model_validate(invalid)


def test_full_original_fence_and_nested_actual_model_graph_bound():
    original = StandingDispatchActivation.model_validate(activation())
    proof = StandingDispatchActivationReceipt.model_validate(receipt())
    altered = activation()
    altered["expected_fence_token"] = OTHER
    with pytest.raises(ValueError):
        proof.validate_request(StandingDispatchActivation.model_validate(altered))
    bad_mode = original.mode_change.model_copy(update={"permission_grant": "all"})
    malformed = original.model_copy(update={"mode_change": bad_mode})
    with pytest.raises(ValueError):
        activation_digest(malformed, 1)
    with pytest.raises(ValueError):
        proof.validate_request(malformed)
    raw = activation()
    raw["mode_change"]["idempotency_key"] = "bad\0key"
    with pytest.raises(ValueError):
        StandingDispatchActivation.model_validate(raw)


@pytest.mark.parametrize(
    "writable,autonomous,stopped,same_revision,live_producer",
    itertools.product((False, True), repeat=5),
)
def test_readiness_is_exact_conjunction_without_new_permission(
    writable, autonomous, stopped, same_revision, live_producer
):
    raw = state(active=True)
    expected = writable and autonomous and not stopped and same_revision and live_producer
    raw.update(
        control_writable=writable,
        mode="autonomous" if autonomous else "on_demand",
        stopped_through_epoch=int(stopped),
        control_revision=1 if same_revision else 2,
        producer=producer() if live_producer else None,
        dispatch_ready=expected,
    )
    assert StandingDispatchState.model_validate(raw).dispatch_ready == expected
    raw["dispatch_ready"] = not expected
    with pytest.raises(ValueError):
        StandingDispatchState.model_validate(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("epoch", True),
        ("epoch", EPOCHS + 1),
        ("stopped_through_epoch", 2),
        ("activation_digest", None),
        ("activation_control_revision", None),
        ("activation_control_revision", 0),
        ("activation_control_revision", 2),
        ("control_revision", -1),
        ("control_revision", True),
        ("control_writable", 1),
        ("dispatch_ready", 1),
    ],
)
def test_state_impossible_activation_or_coercion_rejected(field, value):
    raw = state(active=True)
    raw[field] = value
    with pytest.raises(ValueError):
        StandingDispatchState.model_validate(raw)


@pytest.mark.parametrize(
    "field,value", [("activation_digest", "f" * 64), ("activation_control_revision", 1)]
)
def test_zero_epoch_has_no_invented_activation(field, value):
    raw = state()
    raw["control_revision"] = 1
    raw[field] = value
    with pytest.raises(ValueError):
        StandingDispatchState.model_validate(raw)


@pytest.mark.parametrize(
    "field,value", [("expected_backend", "b" * 32), ("expected_instance", OTHER)]
)
def test_current_producer_must_belong_to_observed_native_incarnation(field, value):
    raw = state(active=True)
    raw["producer"]["request"][field] = value
    with pytest.raises(ValueError):
        StandingDispatchState.model_validate(raw)


@pytest.mark.parametrize("cause", ["instance", "control", "epoch", "fence"])
def test_unobserved_lookup_fence_is_precise(cause):
    raw = lookup()
    current = raw["state"]
    if cause == "instance":
        current["instance_id"] = OTHER
        current["producer"] = None
    elif cause == "control":
        current["control_revision"] = 1
    elif cause == "epoch":
        current.update(
            epoch=1,
            activation_digest="f" * 64,
            activation_control_revision=1,
            control_revision=1,
        )
    else:
        current["fence_token"] = OTHER
    raw["mutation_fenced"] = True
    original = StandingDispatchActivation.model_validate(activation())
    assert StandingDispatchActivationLookup.model_validate(raw).validate_request(original)
    raw["mutation_fenced"] = False
    with pytest.raises(ValueError):
        StandingDispatchActivationLookup.model_validate(raw)


def test_negative_observation_without_fence_is_not_failure_or_launch():
    raw = lookup()
    model = StandingDispatchActivationLookup.model_validate(raw)
    assert model.status == "unobserved" and model.receipt is None and not model.mutation_fenced
    raw["mutation_fenced"] = True
    with pytest.raises(ValueError):
        StandingDispatchActivationLookup.model_validate(raw)


@pytest.mark.parametrize(
    "change", ["missing", "unexpected", "past-epoch", "past-mode", "digest", "revision", "backend"]
)
def test_lookup_receipt_and_observation_binding(change):
    raw = lookup(recorded=change != "unexpected")
    if change == "missing":
        raw["receipt"] = None
    elif change == "unexpected":
        raw["receipt"] = receipt()
    elif change == "past-epoch":
        raw["state"] = state()
        raw["state"]["control_revision"] = 1
    elif change == "past-mode":
        raw["state"] = state()
    elif change == "digest":
        raw["state"]["activation_digest"] = "f" * 64
    elif change == "revision":
        raw["state"].update(control_revision=2, activation_control_revision=2)
    else:
        raw["state"]["backend_id"] = "b" * 32
        raw["state"]["producer"] = None
        raw["state"]["dispatch_ready"] = False
    with pytest.raises(ValueError):
        StandingDispatchActivationLookup.model_validate(raw)


def test_retired_controller_and_later_epoch_proof_are_history_only():
    raw = lookup(recorded=True)
    current = raw["state"]
    current.update(
        instance_id=OTHER,
        control_revision=3,
        epoch=2,
        stopped_through_epoch=2,
        activation_digest="f" * 64,
        activation_control_revision=3,
        producer=None,
        dispatch_ready=False,
    )
    model = StandingDispatchActivationLookup.model_validate(raw)
    assert model.mutation_fenced and model.receipt.epoch == 1 and model.state.epoch == 2
    assert model.validate_request(StandingDispatchActivation.model_validate(activation())) == model
    altered = activation()
    altered["expected_fence_token"] = OTHER
    with pytest.raises(ValueError):
        model.validate_request(StandingDispatchActivation.model_validate(altered))


@pytest.mark.parametrize("field", ["control_revision", "epoch"])
def test_lookup_observation_cannot_precede_original_expectation(field):
    raw = lookup()
    if field == "epoch":
        raw["request"]["expected_epoch"] = 1
    else:
        raw["request"]["mode_change"]["expected_revision"] = 1
    with pytest.raises(ValueError):
        StandingDispatchActivationLookup.model_validate(raw)


@pytest.mark.parametrize("epoch", [True, -1, 1.0, "1", EPOCHS + 1])
def test_stop_issued_epoch_boundaries(epoch):
    raw = stop()
    raw["issued_epoch"] = epoch
    with pytest.raises(ValueError):
        StandingDispatchStop.model_validate(raw)


def test_stop_zero_epoch_binding_and_last_epoch():
    assert StandingDispatchStop.model_validate(stop(epoch=0)).activation_digest is None
    assert StandingDispatchStop.model_validate(stop(epoch=EPOCHS)).issued_epoch == EPOCHS
    for epoch, digest in ((0, "f" * 64), (1, None)):
        raw = stop(epoch=epoch)
        raw["activation_digest"] = digest
        with pytest.raises(ValueError):
            StandingDispatchStop.model_validate(raw)


@pytest.mark.parametrize("invalid", [False, 1, 0, "true", None])
def test_stop_proof_target_fenced_is_literal_true_without_coercion(invalid):
    raw = stop_proof()
    raw["target_fenced"] = invalid
    with pytest.raises(ValueError):
        StandingDispatchStopProof.model_validate(raw)


def test_stop_proves_only_issued_target_and_never_newer_epoch():
    raw = stop_proof()
    original = StandingDispatchStop.model_validate(stop())
    assert StandingDispatchStopProof.model_validate(raw).validate_request(original)
    raw["state"].update(
        epoch=2,
        activation_digest="f" * 64,
        activation_control_revision=2,
        control_revision=2,
        dispatch_ready=True,
    )
    proof = StandingDispatchStopProof.model_validate(raw)
    assert proof.target_fenced and proof.state.dispatch_ready
    assert proof.state.epoch > proof.request.issued_epoch
    zero = stop_proof()
    zero["request"] = stop(epoch=0)
    zero["state"] = state()
    zero["state"]["fence_token"] = OTHER
    assert StandingDispatchStopProof.model_validate(zero).target_fenced


@pytest.mark.parametrize("change", ["not-fenced", "future-target", "wrong-digest", "backend"])
def test_stop_proof_impossible_authority_is_rejected(change):
    raw = stop_proof()
    if change == "not-fenced":
        raw["state"]["stopped_through_epoch"] = 0
        raw["state"]["dispatch_ready"] = True
    elif change == "future-target":
        raw["request"]["issued_epoch"] = 2
    elif change == "wrong-digest":
        raw["request"]["activation_digest"] = "f" * 64
    else:
        raw["request"]["expected_backend"] = "b" * 32
    with pytest.raises(ValueError):
        StandingDispatchStopProof.model_validate(raw)


def test_stop_validate_request_preserves_full_original_and_no_input_leak():
    model = StandingDispatchStopProof.model_validate(stop_proof())
    original = stop()
    original["expected_fence_token"] = OTHER
    with pytest.raises(ValueError):
        model.validate_request(StandingDispatchStop.model_validate(original))
    secret = "public-synthetic-input-not-for-error-echo"
    raw = activation()
    raw["mode_change"]["idempotency_key"] = secret + "\0"
    with pytest.raises(ValueError) as raised:
        StandingDispatchActivation.model_validate(raw)
    assert secret not in str(raised.value)
