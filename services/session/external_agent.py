"""Native owner grants and scope-checked transcript custody for external Agents."""

import secrets
import sqlite3
from contextlib import contextmanager

from packages.contracts.external_agent import (
    MAX_EXTERNAL_NATIVE_REQUEST_BYTES,
    MAX_EXTERNAL_TRANSCRIPT_PAGE_BYTES,
    ExternalAgentConnect,
    ExternalAgentGrant,
    ExternalAgentTranscriptEntry,
    ExternalAgentTranscriptPage,
    ExternalAgentTranscriptPosition,
)
from packages.contracts.transcript_scan import checked_entry, transcript_json

_HISTORY_ORIGINS = {
    "conversation.user": {"authenticated-user"},
    "conversation.converse": {"converse"},
    "conversation.window": {"authenticated-user"},
    "conversation.revision": {"authenticated-user", "converse"},
}
_GRANT_COLUMNS = (
    "attachment_id,request_id,backend_session_id,revoked,"
    "CASE WHEN typeof(original)='text' AND length(cast(original AS BLOB)) "
    f"BETWEEN 1 AND {MAX_EXTERNAL_NATIVE_REQUEST_BYTES} THEN original END AS original"
)
_MAX_CHUNK_BYTES = 32768
_MAX_SCAN_ROWS = 20


class ExternalAgentConflict(ValueError):
    pass


class ExternalAgentScopeChanged(ExternalAgentConflict):
    pass


class ExternalAgentUnavailable(RuntimeError):
    pass


class ExternalAgentAccess:
    def __init__(self, store):
        self.store = store

    @contextmanager
    def _transaction(self, *, write=False):
        with self.store.lock:
            database = self.store.db
            if self.store._agent_submit_unavailable or database.in_transaction:
                raise ExternalAgentUnavailable
            try:
                database.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield database
                if write:
                    database.commit()
            except (ExternalAgentConflict, ExternalAgentUnavailable):
                raise
            except (sqlite3.Error, KeyError, ValueError, TypeError, RecursionError):
                raise ExternalAgentUnavailable from None
            finally:
                if database.in_transaction:
                    try:
                        database.rollback()
                        if database.in_transaction:
                            raise sqlite3.OperationalError
                    except sqlite3.Error:
                        self.store._agent_submit_unavailable = True
                        raise ExternalAgentUnavailable from None

    def _current(self, scope):
        try:
            session = self.store.session(scope.session_id)
        except KeyError:
            raise ExternalAgentScopeChanged from None
        if session.channel != "converse" or (
            session.session_id,
            session.thread_id,
            session.epoch,
        ) != (scope.session_id, scope.thread_id, scope.epoch):
            raise ExternalAgentScopeChanged

    def _grant(self, row, request_id):
        if row is None:
            raise ExternalAgentConflict
        if type(row["original"]) is not str or row["revoked"] not in (0, 1):
            raise ExternalAgentUnavailable
        raw = row["original"].encode("utf-8")
        transcript_json(raw, MAX_EXTERNAL_NATIVE_REQUEST_BYTES)
        original = ExternalAgentConnect.model_validate_json(raw, strict=True)
        if original.request_id != row["request_id"]:
            raise ExternalAgentUnavailable
        phase = "authorized" if row["backend_session_id"] is None else "bound"
        if row["revoked"]:
            phase = "revoked"
        return ExternalAgentGrant(
            contract_version=1,
            kind="grant",
            request_id=request_id,
            attachment_id=row["attachment_id"],
            original=original,
            phase=phase,
            backend_session_id=row["backend_session_id"],
        )

    def _lookup(self, database, attachment_id, request_id):
        return self._grant(
            database.execute(
                f"SELECT {_GRANT_COLUMNS} FROM external_agent_grants WHERE attachment_id=?",
                (attachment_id,),
            ).fetchone(),
            request_id,
        )

    def authorize(self, request):
        """Durably admit the exact owner request before a peer may be bound."""
        with self._transaction(write=True) as database:
            row = database.execute(
                f"SELECT {_GRANT_COLUMNS} FROM external_agent_grants WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
            if row is not None:
                retained = self._grant(row, request.request_id)
                if retained.original != request:
                    raise ExternalAgentConflict
                if retained.phase != "revoked":
                    self._current(request.scope)
                return retained
            self._current(request.scope)
            attachment_id = secrets.token_hex(16)
            database.execute(
                "INSERT INTO external_agent_grants "
                "(attachment_id,request_id,original,backend_session_id,revoked) VALUES (?,?,?,NULL,0)",
                (attachment_id, request.request_id, request.model_dump_json()),
            )
            return self._lookup(database, attachment_id, request.request_id)

    def bind(self, request):
        with self._transaction(write=True) as database:
            target = request.target
            grant = self._lookup(database, target.attachment_id, request.request_id)
            self._current(grant.original.scope)
            if (
                grant.phase == "revoked"
                or grant.backend_session_id not in (None, target.backend_session_id)
                or grant.original.expected_backend_session_id
                not in (None, target.backend_session_id)
            ):
                raise ExternalAgentConflict
            if grant.phase == "bound":
                return grant
            previous = database.execute(
                "SELECT 1 FROM external_agent_grants WHERE backend_session_id=?",
                (target.backend_session_id,),
            ).fetchone()
            if previous is not None:
                # A resumed Pi session still contains data from its original scope.
                raise ExternalAgentConflict
            database.execute(
                "UPDATE external_agent_grants SET backend_session_id=? WHERE attachment_id=?",
                (target.backend_session_id, grant.attachment_id),
            )
            return self._lookup(database, target.attachment_id, request.request_id)

    def revoke(self, request):
        with self._transaction(write=True) as database:
            grant = self._lookup(database, request.attachment_id, request.request_id)
            if grant.phase != "revoked":
                database.execute(
                    "UPDATE external_agent_grants SET revoked=1 WHERE attachment_id=?",
                    (request.attachment_id,),
                )
            return self._lookup(database, request.attachment_id, request.request_id)

    def transcript(self, request):
        """Observe current scope and qualified native entries in one read snapshot."""
        with self._transaction() as database:
            grant = self._lookup(database, request.target.attachment_id, request.request_id)
            scope = grant.original.scope
            self._current(scope)
            if (
                grant.phase != "bound"
                or grant.backend_session_id != request.target.backend_session_id
            ):
                raise ExternalAgentConflict
            latest = database.execute(
                "SELECT coalesce(max(sequence),0) FROM entries "
                "WHERE session_id=? AND thread_id=? AND epoch=? AND kind IN (?,?,?,?)",
                (scope.session_id, scope.thread_id, scope.epoch, *_HISTORY_ORIGINS),
            ).fetchone()[0]
            through = latest if request.through_sequence is None else request.through_sequence
            after = request.after
            if not after.sequence <= through <= latest:
                raise ExternalAgentConflict
            return self._transcript_page(database, request, grant, through)

    def _transcript_page(self, database, request, grant, through):
        scope = grant.original.scope
        after = request.after
        limit = min(request.limit, _MAX_SCAN_ROWS)
        start = after.sequence if after.byte_offset else after.sequence + 1
        rows = database.execute(
            "SELECT sequence,event_id,kind,length(cast(value AS BLOB)) AS bytes FROM entries "
            "WHERE session_id=? AND thread_id=? AND epoch=? AND sequence BETWEEN ? AND ? "
            "AND kind IN (?,?,?,?) ORDER BY sequence LIMIT ?",
            (
                scope.session_id,
                scope.thread_id,
                scope.epoch,
                start,
                through,
                *_HISTORY_ORIGINS,
                limit + 1,
            ),
        ).fetchall()
        if after.byte_offset and (not rows or rows[0]["sequence"] != after.sequence):
            raise ExternalAgentConflict
        entries, position = [], after

        def envelope(cursor, has_more, items):
            return ExternalAgentTranscriptPage(
                contract_version=1,
                request_id=request.request_id,
                kind="transcript",
                target=request.target,
                scope=scope,
                after=after,
                through_sequence=through,
                next_after=cursor,
                has_more=has_more,
                entries=tuple(items),
            )

        for row in rows[:limit]:
            offset = after.byte_offset if row["sequence"] == after.sequence else 0
            received = self._transcript_entry(database, row, grant, offset)
            if received is None:
                if offset:
                    raise ExternalAgentScopeChanged
                position = ExternalAgentTranscriptPosition(sequence=row["sequence"], byte_offset=0)
                continue
            entry, next_position = received
            candidate = envelope(next_position, True, [*entries, entry])
            if (
                len(candidate.model_dump_json().encode("utf-8"))
                > MAX_EXTERNAL_TRANSCRIPT_PAGE_BYTES - 64
            ):
                # Leave room for the final cursor when it advances over filtered rows.
                if position == after:
                    raise ExternalAgentUnavailable
                return envelope(position, True, entries)
            entries.append(entry)
            position = next_position
            if position.byte_offset:
                return candidate
        if len(rows) > limit:
            return envelope(position, True, entries)
        return envelope(
            ExternalAgentTranscriptPosition(sequence=through, byte_offset=0), False, entries
        )

    def _transcript_entry(self, database, row, grant, offset):
        size = row["bytes"]
        if type(size) is not int or not 1 <= size <= MAX_EXTERNAL_TRANSCRIPT_PAGE_BYTES:
            raise ExternalAgentUnavailable
        raw = database.execute(
            "SELECT value FROM entries WHERE sequence=?", (row["sequence"],)
        ).fetchone()[0]
        entry = checked_entry(transcript_json(raw.encode("utf-8")))
        scope = grant.original.scope
        if (
            (entry.session_id, entry.thread_id, entry.epoch)
            != (scope.session_id, scope.thread_id, scope.epoch)
            or (entry.sequence, entry.event_id, entry.kind)
            != (row["sequence"], row["event_id"], row["kind"])
            or entry.origin not in _HISTORY_ORIGINS[row["kind"]]
        ):
            raise ExternalAgentUnavailable
        content_scope = self.store._agent_context_scope(entry.event_id, scope)
        if content_scope not in grant.original.allowed_content:
            return None
        payload = entry.payload
        text = payload.get("text")
        if type(text) is not str:
            raise ExternalAgentUnavailable
        encoded = text.encode("utf-8")
        if offset and (offset >= len(encoded) or encoded[offset] & 0xC0 == 0x80):
            raise ExternalAgentConflict
        end = min(offset + _MAX_CHUNK_BYTES, len(encoded))
        while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
            end -= 1
        final = end == len(encoded)
        item = ExternalAgentTranscriptEntry(
            sequence=entry.sequence,
            event_id=entry.event_id,
            kind=entry.kind,
            origin=entry.origin,
            revision=payload.get("revision", 1),
            content_scope=content_scope,
            revises_event_id=payload.get("target_event_id")
            if entry.kind == "conversation.revision"
            else None,
            utterance_id=payload.get("utterance_id")
            if entry.kind == "conversation.window"
            else None,
            window_index=payload.get("window_index")
            if entry.kind == "conversation.window"
            else None,
            byte_offset=offset,
            end_of_entry=final,
            text=encoded[offset:end].decode("utf-8"),
        )
        return item, ExternalAgentTranscriptPosition(
            sequence=entry.sequence, byte_offset=0 if final else end
        )
