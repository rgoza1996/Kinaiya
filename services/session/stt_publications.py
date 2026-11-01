"""Positive-only proof of one original native STT append, without content recall."""

from packages.contracts.models import AppendRequest, JournalEntry, SpeechWindow
from packages.contracts.stt_settlement import STTPublication, digest, encode, settlement_json


def lookup_publication(store, original):
    original = STTPublication.model_validate(original)
    if original.state != "required":
        raise ValueError("An exact admitted STT publication is required")
    with store.lock:
        row = store.db.execute(
            "SELECT CASE WHEN typeof(request)='text' AND length(CAST(request AS BLOB))<=65536 THEN request END, CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB))<=131072 THEN value END, CASE WHEN typeof(event_id)='text' AND length(CAST(event_id AS BLOB))<=128 THEN event_id END, CASE WHEN typeof(sequence)='integer' THEN sequence END FROM entries WHERE session_id=? AND writer='converse' AND idem=?",
            (original.session_id, original.idempotency_key),
        ).fetchone()
        event_id, sequence = None, None
        if row is not None:
            settlement_json(row[0].encode("utf-8") if type(row[0]) is str else b"", 65536)
            settlement_json(row[1].encode("utf-8") if type(row[1]) is str else b"", 131072)
            request = AppendRequest.model_validate_json(row[0], strict=True)
            entry = JournalEntry.model_validate_json(row[1], strict=True)
            window = SpeechWindow.model_validate(request.payload, strict=True)
            if (
                (request.session_id, request.thread_id, request.epoch, request.idempotency_key)
                != (
                    original.session_id,
                    original.thread_id,
                    original.epoch,
                    original.idempotency_key,
                )
                or request.kind != "conversation.window"
                or window.stt_model is None
                or window.audio_sha256 != original.audio_sha256
                or digest(window.stt_model.model_dump(mode="json")) != original.stt_identity_sha256
                or encode(window.model_dump(mode="json")) != encode(request.payload)
                or digest(request.model_dump(mode="json")) != original.request_sha256
                or entry.origin != "authenticated-user"
                or type(row[3]) is not int
                or not 1 <= row[3] <= 9_007_199_254_740_990
                or type(row[2]) is not str
                or not row[2]
                or (entry.event_id, entry.sequence) != (row[2], row[3])
                or encode(
                    entry.model_dump(
                        mode="json", exclude={"event_id", "sequence", "origin", "created_at"}
                    )
                )
                != encode(request.model_dump(mode="json"))
            ):
                raise ValueError("Original STT publication proof is unavailable")
            event_id, sequence = row[2], row[3]
    return {
        "schema_version": "1.0",
        "publication": original.model_dump(),
        "recorded": row is not None,
        "event_id": event_id,
        "sequence": sequence,
        "origin": None if row is None else "authenticated-user",
    }
