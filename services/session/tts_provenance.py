"""Native-only speech provenance bound to one retained Converse source."""

import hashlib
import math
import re

from packages.contracts.models import DeliveryRequest
from packages.contracts.tts_models import TTSModelPin, TTSSynthesisAdmission
from services.converse.tts_text import normalize_for_tts


def validate_tts(store, request, role):
    if role != "converse":
        raise ValueError("Only Converse can publish speech provenance")
    if store.session(request.session_id).channel != "converse":
        raise ValueError("Speech provenance requires a Converse session")
    delivery = None
    if request.kind == "converse.synthesis":
        admission = TTSSynthesisAdmission.model_validate(request.payload)
        source_id = admission.source_event_id
        generation = admission.generation_epoch
        if (
            request.causes != [source_id]
            or request.idempotency_key != f"tts-admission:{admission.operation_id}"
        ):
            raise ValueError("Speech admission source changed")
    else:
        extras = {"alignment", "tts_model", "tts_operation_id"}
        if set(request.payload) != set(DeliveryRequest.model_fields) | extras:
            raise ValueError("Speech delivery requires its exact native provenance")
        delivery = DeliveryRequest.model_validate(
            {key: value for key, value in request.payload.items() if key not in extras},
            strict=True,
        )
        if (
            request.payload["alignment"] != "conservative_estimate"
            or not math.isfinite(delivery.played_seconds)
            or (
                delivery.estimated_text_fraction is not None
                and not math.isfinite(delivery.estimated_text_fraction)
            )
            or not 1 <= len(delivery.chunk_id.encode("utf-8")) <= 128
        ):
            raise ValueError("Invalid native speech delivery projection")
        pin = TTSModelPin.model_validate(request.payload["tts_model"])
        source_id = delivery.event_id
        generation = delivery.generation_epoch
        operation = request.payload["tts_operation_id"]
        if (
            type(operation) is not str
            or re.fullmatch(r"[a-f0-9]{32}", operation) is None
            or request.causes != [source_id]
            or request.idempotency_key != f"delivery:{source_id}:{operation}:{delivery.status}"
        ):
            raise ValueError("Speech delivery source identity changed")
        row = store.db.execute(
            "SELECT CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB))<=65536 THEN value END FROM entries WHERE session_id=? AND writer='converse' AND idem=? LIMIT 1",
            (request.session_id, f"tts-admission:{operation}"),
        ).fetchone()
        if row is None or row[0] is None:
            raise ValueError("Speech admission is unavailable")
        from packages.contracts.models import JournalEntry

        original = JournalEntry.model_validate_json(row[0])
        admission = TTSSynthesisAdmission.model_validate(original.payload)
        if (
            original.kind != "converse.synthesis"
            or original.origin != "converse"
            or admission.operation_id != operation
            or admission.tts_model != pin
            or admission.source_event_id != source_id
            or admission.generation_epoch != generation
        ):
            raise ValueError("Speech delivery does not match its admission")
    source = store.entry(source_id)
    if (
        (source.session_id, source.thread_id, source.epoch)
        != (request.session_id, request.thread_id, request.epoch)
        or source.kind != "conversation.converse"
        or source.origin != "converse"
        or type(source.payload.get("generation_epoch")) is not int
        or source.payload.get("generation_epoch") != generation
        or (delivery is not None and source.payload.get("chunk_id") != delivery.chunk_id)
    ):
        raise ValueError("Speech provenance source is unavailable")
    if admission.content_scope != source.payload.get("content_scope", "local_only"):
        raise ValueError("Speech disclosure provenance changed")
    text = source.payload.get("text")
    if (
        type(text) is not str
        or admission.text_sha256
        != hashlib.sha256(normalize_for_tts(text).encode("utf-8")).hexdigest()
    ):
        raise ValueError("Speech text provenance changed")
