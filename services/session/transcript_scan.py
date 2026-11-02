"""One native read snapshot for bounded, immutable transcript discovery."""

from packages.contracts.models import Session
from packages.contracts.transcript_scan import (
    MAX_TRANSCRIPT_SCAN_PAGE_BYTES,
    TranscriptScanConflict,
    TranscriptScanPage,
    TranscriptScanRequest,
    TranscriptScanUnavailable,
    checked_entry,
    checked_scan,
    transcript_json,
)


def transcript_scan(store, request):
    request = checked_scan(request, TranscriptScanRequest)
    started = False
    with store.lock:
        try:
            if getattr(store, "_agent_submit_unavailable", False) or store.db.in_transaction:
                raise TranscriptScanUnavailable
            store.db.execute("BEGIN")
            started = True
            row = store.db.execute(
                "SELECT length(cast(value AS BLOB)) FROM sessions WHERE session_id=?",
                (request.session_id,),
            ).fetchone()
            if row is None:
                raise TranscriptScanConflict
            if type(row[0]) is not int or not 1 <= row[0] <= 8192:
                raise TranscriptScanUnavailable
            raw_session = store.db.execute(
                "SELECT value FROM sessions WHERE session_id=?", (request.session_id,)
            ).fetchone()[0]
            if type(raw_session) is not str:
                raise TranscriptScanUnavailable
            session_fields = transcript_json(raw_session.encode("utf-8"), 8192)
            if not {"session_id", "thread_id", "epoch", "channel"} <= session_fields.keys():
                raise TranscriptScanUnavailable
            session = Session.model_validate(session_fields, strict=True)
            if (session.session_id, session.thread_id, session.epoch) != (
                request.session_id,
                request.thread_id,
                request.epoch,
            ):
                raise TranscriptScanConflict
            latest = store.db.execute("SELECT coalesce(max(sequence),0) FROM entries").fetchone()[0]
            through = latest if request.through_sequence is None else request.through_sequence
            if not request.after <= through <= latest:
                raise TranscriptScanConflict
            # Fetch only bounded row identities and byte counts first. A corrupt
            # or oversized value must not be materialized by the SQLite driver.
            rows = store.db.execute(
                "SELECT sequence,length(cast(event_id AS BLOB)),length(cast(value AS BLOB)) FROM entries "
                "WHERE session_id=? AND thread_id=? AND epoch=? AND sequence>? "
                "AND sequence<=? ORDER BY sequence LIMIT ?",
                (
                    request.session_id,
                    request.thread_id,
                    request.epoch,
                    request.after,
                    through,
                    request.limit + 1,
                ),
            ).fetchall()
            entries = []
            result = None
            for index, row in enumerate(rows[: request.limit]):
                if (
                    type(row[1]) is not int
                    or not 1 <= row[1] <= 512
                    or type(row[2]) is not int
                    or not 1 <= row[2] <= MAX_TRANSCRIPT_SCAN_PAGE_BYTES
                ):
                    if not entries:
                        raise TranscriptScanUnavailable
                    break
                native_row = store.db.execute(
                    "SELECT event_id,value FROM entries WHERE sequence=?", (row[0],)
                ).fetchone()
                native = native_row[1]
                if type(native) is not str:
                    raise TranscriptScanUnavailable
                entry = checked_entry(transcript_json(native.encode("utf-8")))
                if (entry.sequence, entry.event_id) != (row[0], native_row[0]):
                    raise TranscriptScanUnavailable
                candidate = [*entries, entry]
                exhausted = index + 1 == len(rows)
                try:
                    page = TranscriptScanPage(
                        schema_version="1.0",
                        request=request,
                        through_sequence=through,
                        entries=candidate,
                        next_after=None if exhausted else entry.sequence,
                        exhausted=exhausted,
                    )
                except ValueError:
                    if not entries:
                        raise TranscriptScanUnavailable from None
                    break
                entries, result = candidate, page
            if not entries:
                result = TranscriptScanPage(
                    schema_version="1.0",
                    request=request,
                    through_sequence=through,
                    entries=[],
                    next_after=None,
                    exhausted=True,
                )
            elif result.exhausted and len(entries) < len(rows):
                raise TranscriptScanUnavailable
            result = result.validate_request(request)
            store.db.rollback()
            if store.db.in_transaction:
                raise TranscriptScanUnavailable
            started = False
            return result
        except (TranscriptScanConflict, TranscriptScanUnavailable):
            raise
        except Exception:
            raise TranscriptScanUnavailable from None
        finally:
            if started:
                try:
                    store.db.rollback()
                    if store.db.in_transaction:
                        raise RuntimeError
                except Exception:
                    # Share the existing connection poison; later operations
                    # cannot treat uncertain snapshot cleanup as usable storage.
                    store._agent_submit_unavailable = True
                    raise TranscriptScanUnavailable from None
