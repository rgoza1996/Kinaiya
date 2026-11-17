"""Protected original capture permission, independent of transcript retention."""

import secrets
import sqlite3
from contextlib import contextmanager

from packages.contracts.live_captures import (
    MAX_ACTIVE,
    MAX_RECORDS,
    MAX_REQUEST_BYTES,
    MAX_RESULT_BYTES,
    PROTOCOL,
    LiveCaptureBackend,
    LiveCaptureFence,
    LiveCaptureFenceReceipt,
    LiveCaptureReceipt,
    LiveCaptureRecoveryQuery,
    LiveCaptureRecoveryState,
    LiveCaptureRegistration,
    LiveCaptureSourceState,
    LiveCaptureState,
    capture_json,
)
from packages.contracts.live_windows import LiveWindowPublicationRef
from packages.contracts.stt_settlement import digest, encode
from services.session.store import JournalError


class LiveCaptureUnavailable(JournalError):
    pass


def _strict(model, value):
    if isinstance(value, model):
        value = value.model_dump(mode="json")
    result = model.model_validate(value, strict=True)
    if len(encode(result.model_dump(mode="json")).encode("utf-8")) > MAX_REQUEST_BYTES:
        raise LiveCaptureUnavailable("Native capture request exceeds its bound")
    return result


class LiveCaptureStore:
    def __init__(self, store):
        self.store = store
        self.controller_id = secrets.token_hex(16)
        self.closed = False
        self.failed = False
        self.activated = False

    def activate(self):
        """Trusted Session startup only; passive readers never claim ownership."""
        if self.activated:
            try:
                self.backend()
                return True
            except LiveCaptureUnavailable:
                return False
        try:
            with self._transaction(activate=True) as db:
                present = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('live_capture_meta','live_captures')"
                    )
                }
                if not present:
                    db.execute(
                        "CREATE TABLE live_capture_meta(id INTEGER PRIMARY KEY CHECK(id=1), backend_id TEXT NOT NULL, controller_id TEXT NOT NULL)"
                    )
                    db.execute(
                        "CREATE TABLE live_captures(owner_instance TEXT NOT NULL, capture_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE, thread_id TEXT NOT NULL, epoch INTEGER NOT NULL, controller_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, request TEXT NOT NULL, receipt TEXT, fence TEXT, PRIMARY KEY(owner_instance,capture_id))"
                    )
                    db.execute(
                        "INSERT INTO live_capture_meta VALUES(1,?,?)",
                        (secrets.token_hex(16), self.controller_id),
                    )
                elif present != {"live_capture_meta", "live_captures"}:
                    raise LiveCaptureUnavailable("Original native capture tables are unavailable")
                else:
                    self._backend(require_original=False)
                    db.execute(
                        "UPDATE live_capture_meta SET controller_id=? WHERE id=1",
                        (self.controller_id,),
                    )
                if self._backend(require_original=False).controller_id != self.controller_id:
                    raise LiveCaptureUnavailable("Native capture startup ownership changed")
            self.activated = True
            return True
        except (ValueError, sqlite3.Error, TypeError, KeyError, RecursionError):
            # Optional live capture becomes unavailable; corruption must not be
            # repaired by resetting its ledger or disable unrelated v1 services.
            self.failed = True
            return False

    @contextmanager
    def _transaction(self, *, activate=False):
        with self.store.lock:
            if self.closed or self.failed or (not activate and not self.activated):
                raise LiveCaptureUnavailable("Original native capture owner is unavailable")
            db = self.store.db
            # Refusal must not enter a context that could commit/roll back
            # somebody else's existing transaction on exit.
            if db.in_transaction:
                raise LiveCaptureUnavailable("Original journal transaction remains active")
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db

    def _backend(self, *, require_original=True):
        if self.closed or self.failed or (require_original and not self.activated):
            raise LiveCaptureUnavailable("Original native capture owner is unavailable")
        row = self.store.db.execute(
            "SELECT CASE WHEN typeof(backend_id)='text' AND length(CAST(backend_id AS BLOB))=32 THEN backend_id END, CASE WHEN typeof(controller_id)='text' AND length(CAST(controller_id AS BLOB))=32 THEN controller_id END FROM live_capture_meta WHERE id=1"
        ).fetchone()
        if row is None:
            raise LiveCaptureUnavailable("Original native capture identity is unavailable")
        backend = LiveCaptureBackend(
            schema_version="2.0", protocol=PROTOCOL, backend_id=row[0], controller_id=row[1]
        )
        if require_original and backend.controller_id != self.controller_id:
            raise LiveCaptureUnavailable("Original native capture controller was replaced")
        return backend

    def backend(self):
        with self.store.lock:
            try:
                return self._backend()
            except (ValueError, sqlite3.Error, TypeError):
                raise LiveCaptureUnavailable(
                    "Original native capture owner is unavailable"
                ) from None

    def _row(self, owner_instance, capture_id):
        row = self.store.db.execute(
            "SELECT CASE WHEN typeof(request)='text' AND length(CAST(request AS BLOB))<=4096 THEN request END, "
            "CASE WHEN typeof(receipt)='text' AND length(CAST(receipt AS BLOB))<=16384 THEN receipt END, "
            "CASE WHEN typeof(fence)='text' AND length(CAST(fence AS BLOB))<=16384 THEN fence END, "
            "CASE WHEN typeof(session_id)='text' AND length(CAST(session_id AS BLOB))<=512 THEN session_id END, "
            "CASE WHEN typeof(thread_id)='text' AND length(CAST(thread_id AS BLOB))<=512 THEN thread_id END, "
            "CASE WHEN typeof(epoch)='integer' THEN epoch END, "
            "CASE WHEN typeof(controller_id)='text' AND length(CAST(controller_id AS BLOB))=32 THEN controller_id END, "
            "CASE WHEN typeof(request_sha256)='text' AND length(CAST(request_sha256 AS BLOB))=64 THEN request_sha256 END, "
            "receipt IS NULL, fence IS NULL FROM live_captures WHERE owner_instance=? AND capture_id=?",
            (owner_instance, capture_id),
        ).fetchone()
        if row is None:
            return None
        request = LiveCaptureRegistration.model_validate(
            capture_json(row[0].encode() if type(row[0]) is str else b""), strict=True
        )
        values = []
        for index, model, absent in (
            (1, LiveCaptureReceipt, row[8]),
            (2, LiveCaptureFenceReceipt, row[9]),
        ):
            values.append(
                None
                if absent
                else model.model_validate(
                    capture_json(
                        row[index].encode() if type(row[index]) is str else b"", MAX_RESULT_BYTES
                    ),
                    strict=True,
                )
            )
        receipt, fence = values
        if (
            (
                request.owner_instance,
                request.capture_id,
                request.session_id,
                request.thread_id,
                request.epoch,
                request.backend.controller_id,
                digest(request.model_dump(mode="json")),
            )
            != (owner_instance, capture_id, row[3], row[4], row[5], row[6], row[7])
            or type(row[5]) is not int
            or (receipt is None and fence is None)
            or (receipt is not None and receipt.original != request)
            or (fence is not None and fence.request.original != request)
        ):
            raise LiveCaptureUnavailable("Original native capture metadata is inconsistent")
        return request, receipt, fence

    def _scope(self, original):
        try:
            current = self.store.session(original.session_id)
            return (current.thread_id, current.epoch, current.channel) == (
                original.thread_id,
                original.epoch,
                "converse",
            )
        except KeyError:
            return False

    def _state(self, original, row=None):
        backend = self._backend()
        row = self._row(original.owner_instance, original.capture_id) if row is None else row
        if row is not None and row[0] != original:
            raise LiveCaptureUnavailable("Native capture identity was reused with another request")
        receipt, fence = (None, None) if row is None else row[1:]
        scope = self._scope(original)
        return LiveCaptureState(
            schema_version="2.0",
            protocol=PROTOCOL,
            original=original,
            backend=backend,
            receipt=receipt,
            fence=fence,
            scope_current=scope,
            capture_open=receipt is not None
            and fence is None
            and scope
            and original.backend == backend,
        )

    def lookup(self, original):
        original = _strict(LiveCaptureRegistration, original)
        with self.store.lock:
            try:
                return self._state(original)
            except (ValueError, sqlite3.Error, TypeError, RecursionError):
                raise LiveCaptureUnavailable(
                    "Original native capture state is unavailable"
                ) from None

    def recover(self, query):
        query = _strict(LiveCaptureRecoveryQuery, query)
        with self.store.lock:
            try:
                self._backend()
                row = self._row(query.owner_instance, query.capture_id)
                return LiveCaptureRecoveryState(
                    schema_version="2.0",
                    protocol=PROTOCOL,
                    original=query,
                    capture=None if row is None else self._state(row[0], row),
                )
            except (ValueError, sqlite3.Error, TypeError, RecursionError):
                raise LiveCaptureUnavailable("Original native recovery is unavailable") from None

    def _new_row(self, original, receipt, fence):
        if self.store.db.execute("SELECT count(*) FROM live_captures").fetchone()[0] >= MAX_RECORDS:
            raise LiveCaptureUnavailable("Native capture receipt capacity is retained")
        self.store.db.execute(
            "INSERT INTO live_captures VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                original.owner_instance,
                original.capture_id,
                original.session_id,
                original.thread_id,
                original.epoch,
                original.backend.controller_id,
                digest(original.model_dump(mode="json")),
                encode(original.model_dump(mode="json")),
                None if receipt is None else encode(receipt.model_dump(mode="json")),
                None if fence is None else encode(fence.model_dump(mode="json")),
            ),
        )

    def register(self, original):
        original = _strict(LiveCaptureRegistration, original)
        with self._transaction() as db:
            backend = self._backend()
            row = self._row(original.owner_instance, original.capture_id)
            if row is not None:
                return self._state(original, row)
            if original.backend != backend or not self._scope(original):
                raise LiveCaptureUnavailable("Native capture registration scope is not current")
            active = db.execute(
                "SELECT count(*) FROM live_captures c JOIN sessions s ON s.session_id=c.session_id WHERE c.controller_id=? AND c.fence IS NULL AND c.receipt IS NOT NULL AND c.epoch=json_extract(s.value,'$.epoch') AND c.thread_id=json_extract(s.value,'$.thread_id') AND json_extract(s.value,'$.channel')='converse'",
                (self.controller_id,),
            ).fetchone()[0]
            if active >= MAX_ACTIVE:
                raise LiveCaptureUnavailable("Native capture permission capacity is retained")
            sequence = db.execute("SELECT coalesce(max(sequence),0) FROM entries").fetchone()[0]
            receipt = LiveCaptureReceipt(
                schema_version="2.0",
                protocol=PROTOCOL,
                original=original,
                registration_id=secrets.token_hex(16),
                registered_sequence=sequence,
            )
            self._new_row(original, receipt, None)
            return self._state(original)

    def fence(self, request):
        request = _strict(LiveCaptureFence, request)
        original = request.original
        with self._transaction() as db:
            backend = self._backend()
            row = self._row(original.owner_instance, original.capture_id)
            if row is not None and row[0] != original:
                raise LiveCaptureUnavailable("Native capture fence lost its original request")
            if row is not None and row[2] is not None:
                if row[2].request != request:
                    raise LiveCaptureUnavailable(
                        "Another original capture fence is already retained"
                    )
                return self._state(original, row)
            if original.backend != backend or (row is None and not self._scope(original)):
                raise LiveCaptureUnavailable("Original native capture permission is unavailable")
            fence = LiveCaptureFenceReceipt(
                schema_version="2.0",
                protocol=PROTOCOL,
                request=request,
                fence_id=secrets.token_hex(16),
            )
            if row is None:
                # A negative fence may precede a delayed registration ACK.
                # Its tombstone cannot be upgraded into positive permission.
                self._new_row(original, None, fence)
            else:
                db.execute(
                    "UPDATE live_captures SET fence=? WHERE owner_instance=? AND capture_id=?",
                    (
                        encode(fence.model_dump(mode="json")),
                        original.owner_instance,
                        original.capture_id,
                    ),
                )
            return self._state(original)

    def source_state(self, reference):
        from services.session.live_windows import _pair

        reference = LiveWindowPublicationRef.model_validate(reference, strict=True)
        with self.store.lock:
            self._backend()
            pair = _pair(self.store, reference)
            row = self._row(reference.owner_instance, reference.capture_id)
            capture = None if row is None else self._state(row[0], row)
            sequence = None if pair is None else pair.source.sequence
            permitted = bool(
                capture is not None
                and capture.capture_open
                and capture.receipt is not None
                and sequence is not None
                and sequence > capture.receipt.registered_sequence
            )
            return LiveCaptureSourceState(
                schema_version="2.0",
                protocol=PROTOCOL,
                original=reference,
                source_sequence=sequence,
                capture=capture,
                capture_permits_source=permitted,
            )

    def close(self):
        # Revoking local permission is not a physical resource-stop receipt.
        self.closed = True
