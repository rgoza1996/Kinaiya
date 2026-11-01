"""Positive-only exact native revocation event proof, with no content recall."""

from packages.contracts.agent_submit import agent_submit_json
from packages.contracts.authorizations import (
    AuthorizationJournalProof,
    AuthorizationRevoke,
    encode,
    revocation_append,
    validate_revocation_entry,
)
from packages.contracts.models import AppendRequest, JournalEntry


def lookup_revocation(store, original):
    original = AuthorizationRevoke.model_validate(original)
    expected = revocation_append(original)
    with store.lock:
        row = store.db.execute(
            "SELECT CASE WHEN typeof(request)='text' AND length(CAST(request AS BLOB))<=8192 THEN request END, CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB))<=16384 THEN value END, CASE WHEN typeof(event_id)='text' AND length(CAST(event_id AS BLOB))<=128 THEN event_id END, CASE WHEN typeof(sequence)='integer' THEN sequence END FROM entries WHERE session_id=? AND writer='agent' AND idem=?",
            (original.identity.session_id, expected.idempotency_key),
        ).fetchone()
        if row is None:
            return AuthorizationJournalProof(
                schema_version="1.0",
                request=original,
                recorded=False,
                event_id=None,
                sequence=None,
                origin=None,
            )
        if type(row[0]) is not str or type(row[1]) is not str:
            raise ValueError("Original authorization event is unavailable")
        request = AppendRequest.model_validate(
            agent_submit_json(row[0].encode(), maximum=8192), strict=True
        )
        agent_submit_json(row[1].encode(), maximum=16384)
        entry = JournalEntry.model_validate_json(row[1], strict=True)
        if encode(request.model_dump(mode="json")) != encode(expected.model_dump(mode="json")):
            raise ValueError("Original authorization event differs")
        validate_revocation_entry(entry, original)
        if type(row[3]) is not int or (entry.event_id, entry.sequence) != (row[2], row[3]):
            raise ValueError("Original authorization event identity differs")
        return AuthorizationJournalProof(
            schema_version="1.0",
            request=original,
            recorded=True,
            event_id=entry.event_id,
            sequence=entry.sequence,
            origin=entry.origin,
        )
