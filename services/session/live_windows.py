"""Sole native writer for an original transcript/capture pair and eligible revision."""

import sqlite3
from contextvars import ContextVar

from packages.contracts.live_windows import (
    CAPTURE_KIND,
    PROTOCOL,
    LiveWindowCandidate,
    LiveWindowCapture,
    LiveWindowPublication,
    LiveWindowPublicationProof,
    LiveWindowPublicationRef,
    LiveWindowPublicationState,
    entry_request,
    input_digest,
)
from packages.contracts.models import AppendRequest, JournalEntry, SpeechWindow
from packages.contracts.stt_settlement import digest, settlement_json
from services.session.store import JournalError

_CURRENT = ContextVar("native_live_window_publication", default=None)


class LiveWindowUnavailable(JournalError):
    pass


def _read(store, where, args):
    # Bound protected row columns in SQLite before allocating their Python values.
    row = store.db.execute(
        "SELECT CASE WHEN typeof(request)='text' AND length(CAST(request AS BLOB))<=65536 THEN request END, "
        "CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB))<=131072 THEN value END, "
        "CASE WHEN typeof(event_id)='text' AND length(CAST(event_id AS BLOB))<=128 THEN event_id END, "
        "CASE WHEN typeof(sequence)='integer' THEN sequence END, "
        "CASE WHEN typeof(session_id)='text' AND length(CAST(session_id AS BLOB))<=512 THEN session_id END, "
        "CASE WHEN typeof(thread_id)='text' AND length(CAST(thread_id AS BLOB))<=512 THEN thread_id END, "
        "CASE WHEN typeof(epoch)='integer' THEN epoch END, "
        "CASE WHEN typeof(kind)='text' AND length(CAST(kind AS BLOB))<=320 THEN kind END, "
        "CASE WHEN typeof(idem)='text' AND length(CAST(idem AS BLOB))<=1024 THEN idem END "
        "FROM entries WHERE writer='converse' AND " + where + " ORDER BY sequence DESC LIMIT 1",
        args,
    ).fetchone()
    if row is None:
        return None
    for text, bound in ((row[0], 65536), (row[1], 131072)):
        settlement_json(text.encode("utf-8") if type(text) is str else b"", bound)
    request = AppendRequest.model_validate_json(row[0], strict=True)
    entry = JournalEntry.model_validate_json(row[1], strict=True)
    if (
        type(row[2]) is not str
        or type(row[3]) is not int
        or (entry.event_id, entry.sequence) != (row[2], row[3])
        or any(type(row[index]) is not str for index in (4, 5, 7, 8))
        or type(row[6]) is not int
        or (entry.session_id, entry.thread_id, entry.epoch, entry.kind, entry.idempotency_key)
        != (row[4], row[5], row[6], row[7], row[8])
        or digest(entry_request(entry)) != digest(request.model_dump(mode="json"))
    ):
        raise LiveWindowUnavailable("Original live journal row is unavailable")
    return entry


def _capture(store, source_id):
    return _read(
        store,
        "kind=? AND json_extract(value,'$.payload.source_event_id')=?",
        (CAPTURE_KIND, source_id),
    )


def guard_legacy_decision(store, source_id):
    try:
        capture = _capture(store, source_id)
    except (ValueError, sqlite3.Error, KeyError, RecursionError):
        raise LiveWindowUnavailable("Original live source partition is unavailable") from None
    if capture is not None:
        raise LiveWindowUnavailable("Legacy routing cannot claim an Adaptive Live source")


def _latest_capture(store, session_id, epoch, utterance_id):
    return _read(
        store,
        "kind=? AND session_id=? AND epoch=? AND json_extract(value,'$.payload.original.utterance_id')=?",
        (CAPTURE_KIND, session_id, epoch, utterance_id),
    )


def _pair(store, original):
    source = _read(
        store, "session_id=? AND idem=?", (original.session_id, original.idempotency_key)
    )
    if source is None:
        return None
    capture = _capture(store, source.event_id)
    if capture is None:
        raise LiveWindowUnavailable("An existing transcript has no original live capture pair")
    return LiveWindowPublicationProof(
        schema_version="2.0", protocol=PROTOCOL, original=original, source=source, capture=capture
    )


def guard_window(store, request, window):
    """One utterance cannot silently switch between legacy and live publication."""
    current = _CURRENT.get()
    previous = _latest_capture(store, request.session_id, request.epoch, window.utterance_id)
    if previous is None:
        if (
            current is not None
            and store._window_row(request.session_id, request.epoch, window.utterance_id)
            is not None
        ):
            raise LiveWindowUnavailable("A legacy utterance cannot be converted to live capture")
        return
    marker = LiveWindowCapture.model_validate(previous.payload, strict=True)
    if current is None or (marker.original.owner_instance, marker.original.capture_id) != (
        current.owner_instance,
        current.capture_id,
    ):
        raise LiveWindowUnavailable("The original live capture owns this utterance")
    if current.request.model_dump(mode="json") != request.model_dump(mode="json"):
        raise LiveWindowUnavailable("Live publication changed its original request")


def validate_capture(store, request, role):
    current = _CURRENT.get()
    if current is None or role != "converse" or not store.db.in_transaction:
        raise LiveWindowUnavailable("Capture annotations require the original native publisher")
    marker = LiveWindowCapture.model_validate(request.payload, strict=True)
    if marker.original != current.reference():
        raise LiveWindowUnavailable("Capture annotation changed its original publication")
    source = _read(store, "event_id=?", (marker.source_event_id,))
    if source is None:
        raise LiveWindowUnavailable("Capture annotation has no authenticated source")
    # Use the same complete pair validator before inserting the native marker.
    candidate = JournalEntry(
        **request.model_dump(), sequence=source.sequence + 1, origin="converse"
    )
    LiveWindowPublicationProof(
        schema_version="2.0",
        protocol=PROTOCOL,
        original=marker.original,
        source=source,
        capture=candidate,
    )


def publish(store, original):
    # Detach mutable nested v1 models before entering an effect-producing scope.
    raw = (
        original.model_dump(mode="json")
        if isinstance(original, LiveWindowPublication)
        else original
    )
    original = LiveWindowPublication.model_validate(raw, strict=True)
    reference = original.reference()
    with store.lock:
        if store.db.in_transaction:
            raise LiveWindowUnavailable("An original journal transaction remains active")
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            existing = _pair(store, reference)
            if existing is not None:
                return existing
            token = _CURRENT.set(original)
            try:
                source = store._append_entry_locked(original.request, "converse")
                if (
                    digest(entry_request(source)) != reference.request_sha256
                    or source.origin != "authenticated-user"
                ):
                    raise LiveWindowUnavailable("Original live source identity is unavailable")
                prior = _latest_capture(
                    store, reference.session_id, reference.epoch, reference.utterance_id
                )
                window = SpeechWindow.model_validate(source.payload, strict=True)
                fingerprint = input_digest(window)
                previous = (
                    None
                    if prior is None
                    else LiveWindowCapture.model_validate(prior.payload, strict=True)
                )
                if previous is not None:
                    prior_pair = _pair(store, previous.original)
                    if prior_pair is None:
                        raise LiveWindowUnavailable(
                            "Previous original live publication is unavailable"
                        )
                duplicate = previous is not None and previous.input_sha256 == fingerprint
                revision = 1 if previous is None else previous.eligible_revision + (not duplicate)
                marker = LiveWindowCapture(
                    schema_version="2.0",
                    protocol=PROTOCOL,
                    capture_mode="adaptive_live",
                    original=reference,
                    source_event_id=source.event_id,
                    source_sequence=source.sequence,
                    source_revision=window.revision,
                    input_sha256=fingerprint,
                    eligible_revision=revision,
                    eligible_source_event_id=previous.eligible_source_event_id
                    if duplicate
                    else source.event_id,
                    eligible_source_sequence=previous.eligible_source_sequence
                    if duplicate
                    else source.sequence,
                    eligible_source_revision=previous.eligible_source_revision
                    if duplicate
                    else window.revision,
                )
                request = AppendRequest(
                    session_id=source.session_id,
                    thread_id=source.thread_id,
                    epoch=source.epoch,
                    kind=CAPTURE_KIND,
                    payload=marker.model_dump(mode="json"),
                    causes=[source.event_id],
                    idempotency_key="live-window-capture:" + source.event_id,
                )
                capture = store._append_entry_locked(request, "converse")
                return LiveWindowPublicationProof(
                    schema_version="2.0",
                    protocol=PROTOCOL,
                    original=reference,
                    source=source,
                    capture=capture,
                )
            finally:
                _CURRENT.reset(token)


def lookup(store, reference):
    reference = LiveWindowPublicationRef.model_validate(reference, strict=True)
    with store.lock:
        receipt = _pair(store, reference)
    return LiveWindowPublicationState(
        schema_version="2.0", protocol=PROTOCOL, original=reference, receipt=receipt
    )


def latest(store, session_id, thread_id, epoch, utterance_id):
    with store.lock:
        session = store.session(session_id)
        if (session.thread_id, session.epoch, session.channel) != (thread_id, epoch, "converse"):
            raise LiveWindowUnavailable("Original live source scope is not current")
        capture = _latest_capture(store, session_id, epoch, utterance_id)
        if capture is None:
            return None
        marker = LiveWindowCapture.model_validate(capture.payload, strict=True)
        current = _pair(store, marker.original)
        if current is None:
            raise LiveWindowUnavailable("Current original live publication is unavailable")
        eligible_capture = _capture(store, marker.eligible_source_event_id)
        if eligible_capture is None:
            raise LiveWindowUnavailable("Original eligible capture is unavailable")
        original = LiveWindowCapture.model_validate(eligible_capture.payload, strict=True).original
        receipt = _pair(store, original)
        if receipt is None:
            raise LiveWindowUnavailable("Original eligible publication is unavailable")
        return LiveWindowCandidate(
            schema_version="2.0", protocol=PROTOCOL, eligible=receipt, latest=current
        )
