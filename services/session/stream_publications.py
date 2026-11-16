"""Positive exact-original lookup; absence grants no permission to retry an append."""

from packages.contracts.models import AppendRequest, JournalEntry
from packages.contracts.stt_settlement import digest, settlement_json
from packages.contracts.whistle_stream_owner import (
    PUBLICATION_PROTOCOL,
    StreamPublication,
    StreamPublicationProof,
)


def lookup_publication(store, original):
    original = StreamPublication.model_validate(original)
    with store.lock:
        row = store.db.execute(
            "SELECT CASE WHEN typeof(request)='text' AND length(CAST(request AS BLOB))<=65536 THEN request END, CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB))<=131072 THEN value END, CASE WHEN typeof(event_id)='text' AND length(CAST(event_id AS BLOB))<=128 THEN event_id END, CASE WHEN typeof(sequence)='integer' THEN sequence END FROM entries WHERE session_id=? AND writer='converse' AND idem=?",
            (original.session_id, original.idempotency_key),
        ).fetchone()
        entry = None
        if row is not None:
            settlement_json(row[0].encode("utf-8") if type(row[0]) is str else b"", 65536)
            settlement_json(row[1].encode("utf-8") if type(row[1]) is str else b"", 131072)
            request = AppendRequest.model_validate_json(row[0], strict=True)
            entry = JournalEntry.model_validate_json(row[1], strict=True)
            if (
                digest(request.model_dump(mode="json")) != original.request_sha256
                or type(row[2]) is not str
                or type(row[3]) is not int
                or (entry.event_id, entry.sequence) != (row[2], row[3])
            ):
                raise ValueError("Stream journal history does not match its original append")
    return StreamPublicationProof(
        schema_version="1.0", protocol=PUBLICATION_PROTOCOL, original=original, entry=entry
    ).model_dump(mode="json")
