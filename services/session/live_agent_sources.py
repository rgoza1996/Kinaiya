"""Actual captured revisions qualify unchanged user text independently of the mic."""

from packages.contracts.decisions_attempts import DecisionsSource
from packages.contracts.live_agent_sources import LiveAgentSourceQuery, LiveAgentSourceState
from packages.contracts.live_windows import CAPTURE_KIND, LiveWindowCapture, input_digest
from services.session.live_windows import _capture, _pair, _read
from services.session.store import JournalError


def unchanged_capture_history(store, expected, current, *, same_slot=True):
    # Native capture digests already normalize the text/scope/STT identity.
    # Permit the three legal speaking/end-hint combinations, then check every row
    # in SQLite without allocating an unbounded revision history in Python.
    allowed = [
        input_digest(expected.payload | {"user_speaking": speaking, "capture_ended": ended})
        for speaking, ended in ((False, False), (True, False), (False, True))
    ]
    slot = "AND json_extract(e.value,'$.payload.window_index')=? " if same_slot else ""
    args = [
        CAPTURE_KIND,
        expected.session_id,
        expected.thread_id,
        expected.epoch,
        expected.payload["utterance_id"],
        expected.sequence,
        current.sequence,
    ]
    if same_slot:
        args.append(expected.payload["window_index"])
    args.extend(allowed)
    changed = store.db.execute(
        "SELECT 1 FROM entries e LEFT JOIN entries c ON c.kind=? AND c.writer='converse' "
        "AND c.session_id=e.session_id AND c.thread_id=e.thread_id AND c.epoch=e.epoch "
        "AND json_extract(c.value,'$.payload.source_event_id')=e.event_id "
        "WHERE e.kind='conversation.window' AND e.writer='converse' AND e.session_id=? AND e.thread_id=? AND e.epoch=? "
        "AND json_extract(e.value,'$.payload.utterance_id')=? AND e.sequence>=? AND e.sequence<=? "
        + slot
        + "AND (c.event_id IS NULL OR json_extract(c.value,'$.payload.input_sha256') IS NULL "
        "OR json_extract(c.value,'$.payload.input_sha256') NOT IN (?,?,?)) LIMIT 1",
        args,
    ).fetchone()
    return changed is None


def authority(store, request):
    request = LiveAgentSourceQuery.model_validate(request, strict=True)
    with store.lock:
        row = store.db.execute(
            "SELECT CASE WHEN writer IN ('converse','owner') THEN writer END FROM entries WHERE event_id=?",
            (request.source.source_event_id,),
        ).fetchone()
        if row is None or row[0] is None:
            raise JournalError("Original speech source writer is unavailable")
        source = _read(store, "event_id=?", (request.source.source_event_id,), writer=row[0])
        if (
            source is None
            or source.kind != "conversation.window"
            or source.origin != "authenticated-user"
            or DecisionsSource.of(source) != request.source
        ):
            raise JournalError("Original native Agent speech source is unavailable")
        marker = _capture(store, source.event_id)
        live = marker is not None
        equivalent = False
        if live:
            original = LiveWindowCapture.model_validate(marker.payload, strict=True)
            pair = _pair(store, original.original)
            if pair is None or pair.source != source:
                raise JournalError("Original Agent speech capture binding changed")
            expected = _read(
                store,
                "kind='conversation.window' AND writer='converse' AND session_id=? AND thread_id=? AND epoch=? "
                "AND json_extract(value,'$.payload.utterance_id')=? "
                "AND json_extract(value,'$.payload.window_index')=? "
                "AND json_extract(value,'$.payload.revision')=?",
                (
                    source.session_id,
                    source.thread_id,
                    source.epoch,
                    source.payload["utterance_id"],
                    source.payload["window_index"],
                    request.expected_revision,
                ),
            )
            if expected is not None:
                captured = _capture(store, expected.event_id)
                if captured is None:
                    raise JournalError("Expected native Agent revision has no capture proof")
                observed = LiveWindowCapture.model_validate(captured.payload, strict=True)
                expected_pair = _pair(store, observed.original)
                if expected_pair is None or expected_pair.source != expected:
                    raise JournalError("Expected Agent revision changed its capture proof")
                resolved = store.resolve(source.event_id)
                current = _read(
                    store,
                    "kind='conversation.window' AND session_id=? AND thread_id=? AND epoch=? "
                    "AND json_extract(value,'$.payload.utterance_id')=? AND json_extract(value,'$.payload.window_index')=? "
                    "AND json_extract(value,'$.payload.revision')=?",
                    (
                        source.session_id,
                        source.thread_id,
                        source.epoch,
                        source.payload["utterance_id"],
                        source.payload["window_index"],
                        resolved.payload.get("revision", 1),
                    ),
                )
                equivalent = (
                    resolved.session_id,
                    resolved.thread_id,
                    resolved.epoch,
                    resolved.kind,
                    resolved.origin,
                ) == (
                    source.session_id,
                    source.thread_id,
                    source.epoch,
                    source.kind,
                    source.origin,
                ) and all(
                    expected.payload.get(key) == resolved.payload.get(key)
                    for key in ("text", "content_scope", "stt_model")
                )
                equivalent = (
                    equivalent
                    and current is not None
                    and unchanged_capture_history(store, expected, current)
                )
        return LiveAgentSourceState(
            schema_version="2.0",
            protocol="native_live_agent_source_v2",
            original=request,
            live=live,
            revision_equivalent=equivalent,
        )
