"""Versioned exchange wire records, unchanged legacy pin grammars and schemas."""

import base64
import hashlib
import json

import pytest

from packages.contracts import media as m
from packages.contracts.converse_models import RoleModelPin
from packages.contracts.decisions_models import DecisionsModelPin, DecisionsModelSelection


def _pin(role):
    if role == "converse":
        return RoleModelPin(
            source="host",
            settings_revision=0,
            catalog_revision=None,
            catalog_binding=None,
            provider="native_llama_media",
            model="synthetic-local",
            local=True,
            reasoning=None,
        )
    return DecisionsModelPin(
        backend_id="a" * 32,
        instance_id="b" * 32,
        settings_revision=0,
        selection=DecisionsModelSelection(source="host", catalog_binding=None),
        catalog_revision=None,
        provider="native_llama_media",
        model="synthetic-local",
        local=True,
        protocol="host_configured",
    )


def _envelope(role, raw):
    return m.MediaCanonicalModelPinV2(
        v=1,
        role=role,
        canonical_utf8_b64=base64.b64encode(raw).decode("ascii"),
        pin_sha256=hashlib.sha256(raw).hexdigest(),
    )


@pytest.mark.parametrize("role", ["converse", "decisions"])
def test_canonical_pin_round_trip_keeps_the_exact_legacy_schema(role):
    pin = _pin(role)
    before = type(pin).model_json_schema()
    envelope = m.canonical_media_model_pin_v2(pin)
    restored = m.materialize_media_model_pin_v2(envelope)
    assert restored == pin and restored is not pin
    assert type(pin).model_json_schema() == before
    assert m.media_json(m.media_bytes(envelope), type(envelope)) == envelope
    raw = base64.b64decode(envelope.canonical_utf8_b64, validate=True)
    assert json.loads(raw).keys() == type(pin).model_fields.keys()
    assert hashlib.sha256(raw).hexdigest() == envelope.pin_sha256


@pytest.mark.parametrize("role", ["converse", "decisions"])
@pytest.mark.parametrize(
    "fault", ["spacing", "missing_default", "extra", "duplicate", "wrong_role", "nan"]
)
def test_byte_envelope_refuses_ambiguous_or_noncanonical_legacy_pin(role, fault):
    raw = base64.b64decode(m.canonical_media_model_pin_v2(_pin(role)).canonical_utf8_b64)
    value = json.loads(raw)
    if fault == "spacing":
        raw = json.dumps(value).encode()
    elif fault == "duplicate":
        raw = b'{"role":"' + role.encode() + b'",' + raw[1:]
    elif fault == "nan":
        raw = raw.replace(b'"settings_revision":0', b'"settings_revision":NaN')
    else:
        if fault == "missing_default":
            del value["role"]
        elif fault == "extra":
            value["credential"] = "synthetic-only"
        else:
            value["role"] = "decisions" if role == "converse" else "converse"
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(m.MediaContractError, match="^Invalid media contract$"):
        _envelope(role, raw)


def test_unchecked_instances_and_changed_envelope_digest_are_revalidated():
    envelope = m.canonical_media_model_pin_v2(_pin("converse"))
    forged = envelope.model_copy(update={"pin_sha256": "0" * 64})
    with pytest.raises(m.MediaContractError):
        m.materialize_media_model_pin_v2(forged)
    pin = _pin("converse").model_copy(update={"settings_revision": True})
    with pytest.raises(m.MediaContractError):
        m.canonical_media_model_pin_v2(pin)


def test_v2_body_storage_has_no_fd_or_remote_authority_variant():
    for raw in (
        b'{"kind":"wire_buffer","fd":7}',
        b'{"kind":"owned_fd","fd_owner_id":"' + b"a" * 32 + b'"}',
    ):
        with pytest.raises(m.MediaContractError):
            m.media_json(raw, m.MediaWireBodyStorageV2)
    with pytest.raises(m.MediaContractError):
        m.media_json(b'{"kind":"disclosed_remote"}', m.MediaLocalNoRemoteV2)


@pytest.mark.parametrize(
    "component,proof",
    [
        ("local_task", "sole_transport_close"),
        ("publication", "task_join"),
        ("reader", "publication_settlement"),
        ("transport", "reader_join"),
    ],
)
def test_close_observation_cannot_substitute_another_component_proof(component, proof):
    ref = m.MediaNativeRef(backend_id="a" * 32, receipt_id="b" * 32, sha256="c" * 64)
    with pytest.raises(m.MediaContractError):
        m.MediaLocalCloseObservationV2(
            v=1,
            exchange_owner=ref,
            component=component,
            owner_id="d" * 32,
            state="settled",
            proof=proof,
            request_sha256="e" * 64,
            binding_sha256="f" * 64,
        )


def test_legacy_cleanup_union_stays_closed_to_v2():
    schema = m.MEDIA_SCHEMA_ALIASES["MediaCleanupReceipt"]
    from pydantic import TypeAdapter

    legacy = TypeAdapter(schema).json_schema()
    assert set(legacy["discriminator"]["mapping"]) == {"native_owner", "local_exchange"}
