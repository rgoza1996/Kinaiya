"""Scoped companion grants and invitation state, owned by the Session SQLite lock.

No journal writes, models, media, resource dispatch or general service tokens.
All authority snapshots and mutations are synchronous under the Store RLock.
"""

import hashlib
import json
import math
import secrets
import time
from contextlib import contextmanager
from uuid import uuid4

from packages.contracts.companion import (
    CallAction,
    CallEnd,
    CallNote,
    CallRequest,
    CompanionCall,
    CompanionDevice,
    DeviceIssued,
    PairingIssued,
    PairingRedeem,
    PairingRequest,
)
from packages.contracts.models import MAX_PLAYBACK_GENERATION

OPEN = ("ringing", "accepted_waiting_media")


class CompanionError(ValueError):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class CompanionStore:
    """Additive module schema; one connection and lock, no second DB owner."""

    def __init__(self, store, *, clock=time.time):
        self.store, self.db, self.clock = store, store.db, clock
        with store.lock:
            if self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='companion_schema'"
            ).fetchone():
                if [r[0] for r in self.db.execute("SELECT version FROM companion_schema")] != [1]:
                    raise CompanionError(409, "Unsupported companion schema; preserve database")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS companion_schema(version INTEGER NOT NULL);
                INSERT INTO companion_schema SELECT 1
                    WHERE NOT EXISTS(SELECT 1 FROM companion_schema);
                CREATE TABLE IF NOT EXISTS companion_pairings(
                    code_hash TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    thread_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                    label TEXT NOT NULL, expires_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS companion_devices(
                    device_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    thread_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                    device_name TEXT NOT NULL, label TEXT NOT NULL,
                    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER,
                    expired_at INTEGER);
                CREATE TABLE IF NOT EXISTS companion_calls(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL UNIQUE,
                    device_id TEXT NOT NULL REFERENCES companion_devices(device_id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    thread_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                    title TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, ringing_deadline INTEGER NOT NULL,
                    media_deadline INTEGER, end_reason TEXT, note TEXT NOT NULL DEFAULT '',
                    creation_key TEXT NOT NULL, creation_digest TEXT NOT NULL,
                    UNIQUE(device_id,creation_key));
                CREATE INDEX IF NOT EXISTS companion_call_page
                    ON companion_calls(device_id,sequence);
                CREATE UNIQUE INDEX IF NOT EXISTS companion_one_open
                    ON companion_calls(device_id)
                    WHERE state IN ('ringing','accepted_waiting_media');
                CREATE TABLE IF NOT EXISTS companion_actions(
                    call_id TEXT NOT NULL REFERENCES companion_calls(call_id) ON DELETE CASCADE,
                    actor TEXT NOT NULL, idem TEXT NOT NULL, digest TEXT NOT NULL,
                    result TEXT NOT NULL, PRIMARY KEY(call_id,actor,idem));
            """)
            versions = [r[0] for r in self.db.execute("SELECT version FROM companion_schema")]
            if versions != [1]:
                raise CompanionError(409, "Unsupported companion schema; preserve database")
            self.db.commit()

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise CompanionError(503, "Companion clock unavailable")
        now = math.floor(value)
        if not 0 <= now <= MAX_PLAYBACK_GENERATION - 86400:
            raise CompanionError(503, "Companion clock outside supported range")
        return now

    def _binding_current(self, row):
        try:
            session = self.store.session(row["session_id"])
        except KeyError:
            return False
        return session.channel == "converse" and (session.thread_id, session.epoch) == (
            row["thread_id"],
            row["epoch"],
        )

    def _device_status(self, row, now):
        if row["revoked_at"] is not None:
            return "revoked"
        if not self._binding_current(row):
            return "stale"
        if row["expired_at"] is not None or row["expires_at"] <= now:
            return "expired"
        return "active"

    def _end(self, call_id, state, reason):
        row = self._call_row(call_id)
        if row["revision"] >= MAX_PLAYBACK_GENERATION:
            raise CompanionError(409, "Call revision exhausted")
        self.db.execute(
            "UPDATE companion_calls SET state=?,end_reason=?,revision=revision+1 WHERE call_id=?",
            (state, reason, call_id),
        )

    def _reconcile(self, now):
        # Fixed retained caps bound this scan:32devices and <=3200calls.
        self.db.execute("DELETE FROM companion_pairings WHERE expires_at<=?", (now,))
        self.db.execute(
            """DELETE FROM companion_pairings WHERE NOT EXISTS(
                SELECT 1 FROM sessions s WHERE s.session_id=companion_pairings.session_id
                AND json_extract(s.value,'$.channel')='converse'
                AND json_extract(s.value,'$.thread_id')=companion_pairings.thread_id
                AND json_extract(s.value,'$.epoch')=companion_pairings.epoch)"""
        )
        for device in self.db.execute("SELECT * FROM companion_devices").fetchall():
            # Once observed, expiry remains terminal across a clock rollback/restart.
            if device["expired_at"] is None and device["expires_at"] <= now:
                self.db.execute(
                    "UPDATE companion_devices SET expired_at=? WHERE device_id=?",
                    (now, device["device_id"]),
                )
            status = self._device_status(device, now)
            for call in self.db.execute(
                "SELECT * FROM companion_calls WHERE device_id=? AND state IN (?,?)",
                (device["device_id"], *OPEN),
            ).fetchall():
                if status != "active":
                    reason = {
                        "revoked": "device_revoked",
                        "expired": "device_expired",
                        "stale": "session_invalidated",
                    }[status]
                    self._end(call["call_id"], "ended", reason)
                elif call["state"] == "ringing" and call["ringing_deadline"] <= now:
                    self._end(call["call_id"], "missed", "unanswered")
                elif call["state"] == "accepted_waiting_media" and call["media_deadline"] <= now:
                    self._end(call["call_id"], "ended", "media_unavailable")

    @contextmanager
    def _locked(self):
        with self.store.lock:
            now = self._now()
            # Publish deadline/invalidation facts even if the subsequent request
            # refuses; the same lock still covers every authorization snapshot.
            with self.db:
                self._reconcile(now)
            with self.db:
                yield now

    def _device_row(self, device_id):
        row = self.db.execute(
            "SELECT * FROM companion_devices WHERE device_id=?", (device_id,)
        ).fetchone()
        if row is None:
            raise CompanionError(404, "Unknown companion device")
        return row

    def _active(self, row, now):
        if self._device_status(row, now) != "active":
            raise CompanionError(403, "Companion grant unavailable")
        return row

    def _authenticate(self, token, now):
        if not isinstance(token, str) or len(token) != 43:
            raise CompanionError(401, "Invalid companion bearer")
        row = self.db.execute(
            "SELECT * FROM companion_devices WHERE token_hash=?", (_digest(token),)
        ).fetchone()
        if row is None:
            raise CompanionError(401, "Invalid companion bearer")
        return self._active(row, now)

    def _device(self, row, now):
        return CompanionDevice(
            **{
                k: row[k]
                for k in (
                    "device_id",
                    "device_name",
                    "label",
                    "session_id",
                    "thread_id",
                    "epoch",
                    "created_at",
                    "expires_at",
                    "revoked_at",
                )
            },
            status=self._device_status(row, now),
        )

    def _call_row(self, call_id):
        row = self.db.execute(
            "SELECT * FROM companion_calls WHERE call_id=?", (call_id,)
        ).fetchone()
        if row is None:
            raise CompanionError(404, "Unknown companion call")
        return row

    def _call(self, row):
        return CompanionCall(
            **{k: row[k] for k in CompanionCall.model_fields if k != "schema_version"}
        )

    def pair(self, request: PairingRequest):
        request = PairingRequest.model_validate(request.model_dump())
        with self._locked() as now:
            if not self._binding_current(request.model_dump()):
                raise CompanionError(409, "Pairing requires the current Converse binding")
            if self.db.execute("SELECT count(*) FROM companion_pairings").fetchone()[0] >= 8:
                raise CompanionError(409, "Pending pairing capacity reached")
            code = secrets.token_urlsafe(32)
            self.db.execute(
                "INSERT INTO companion_pairings VALUES(?,?,?,?,?,?)",
                (
                    _digest(code),
                    request.session_id,
                    request.thread_id,
                    request.epoch,
                    request.label,
                    now + 120,
                ),
            )
            return PairingIssued(**request.model_dump(), pairing_code=code, expires_at=now + 120)

    def redeem(self, request: PairingRedeem):
        request = PairingRedeem.model_validate(request.model_dump())
        with self._locked() as now:
            row = self.db.execute(
                "SELECT * FROM companion_pairings WHERE code_hash=?", (_digest(request.code),)
            ).fetchone()
            if row is None or row["expires_at"] <= now or not self._binding_current(row):
                raise CompanionError(403, "Pairing code unavailable")
            if self.db.execute("SELECT count(*) FROM companion_devices").fetchone()[0] >= 32:
                raise CompanionError(409, "Retained device capacity reached")
            token, device_id = secrets.token_urlsafe(32), str(uuid4())
            self.db.execute(
                "INSERT INTO companion_devices VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)",
                (
                    device_id,
                    _digest(token),
                    row["session_id"],
                    row["thread_id"],
                    row["epoch"],
                    request.device_name,
                    row["label"],
                    now,
                    now + 86400,
                ),
            )
            self.db.execute(
                "DELETE FROM companion_pairings WHERE code_hash=?", (_digest(request.code),)
            )
            return DeviceIssued(
                device_token=token, device=self._device(self._device_row(device_id), now)
            )

    def self_device(self, token):
        with self._locked() as now:
            return self._device(self._authenticate(token, now), now)

    def devices(self):
        with self._locked() as now:
            return [
                self._device(r, now)
                for r in self.db.execute(
                    "SELECT * FROM companion_devices ORDER BY created_at,device_id"
                )
            ]

    def revoke(self, device_id):
        with self._locked() as now:
            self._device_row(device_id)
            self.db.execute(
                "UPDATE companion_devices SET revoked_at=COALESCE(revoked_at,?) WHERE device_id=?",
                (now, device_id),
            )
            self._reconcile(now)
            return self._device(self._device_row(device_id), now)

    def invite(self, request: CallRequest):
        request = CallRequest.model_validate(request.model_dump())
        with self._locked() as now:
            device = self._active(self._device_row(request.device_id), now)
            digest = _digest(_encoded(request.model_dump()))
            prior = self.db.execute(
                "SELECT * FROM companion_calls WHERE device_id=? AND creation_key=?",
                (request.device_id, request.idempotency_key),
            ).fetchone()
            if prior:
                if prior["creation_digest"] != digest:
                    raise CompanionError(409, "Call idempotency conflict")
                return self._call(prior)
            if (
                self.db.execute(
                    "SELECT count(*) FROM companion_calls WHERE device_id=?", (request.device_id,)
                ).fetchone()[0]
                >= 100
            ):
                raise CompanionError(409, "Retained call capacity reached")
            if self.db.execute(
                "SELECT 1 FROM companion_calls WHERE device_id=? AND state IN (?,?)",
                (request.device_id, *OPEN),
            ).fetchone():
                raise CompanionError(409, "Device already has an open invitation")
            call_id = str(uuid4())
            sequence = self.db.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='companion_calls'"
            ).fetchone()
            if sequence is not None and sequence[0] >= MAX_PLAYBACK_GENERATION:
                raise CompanionError(409, "Call sequence exhausted")
            self.db.execute(
                """INSERT INTO companion_calls(
                    call_id,device_id,session_id,thread_id,epoch,title,state,revision,
                    created_at,ringing_deadline,creation_key,creation_digest)
                    VALUES(?,?,?,?,?,?,'ringing',1,?,?,?,?)""",
                (
                    call_id,
                    request.device_id,
                    device["session_id"],
                    device["thread_id"],
                    device["epoch"],
                    request.title,
                    now,
                    now + 30,
                    request.idempotency_key,
                    digest,
                ),
            )
            return self._call(self._call_row(call_id))

    def get_call(self, call_id, *, token=None):
        with self._locked() as now:
            device = self._authenticate(token, now) if token is not None else None
            call = self._call_row(call_id)
            if device is not None and call["device_id"] != device["device_id"]:
                raise CompanionError(404, "Unknown companion call")
            return self._call(call)

    def calls(self, *, token=None, session_id=None, device_id=None, after=0, limit=20):
        if type(after) is not int or not 0 <= after <= MAX_PLAYBACK_GENERATION:
            raise CompanionError(422, "Invalid page cursor")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise CompanionError(422, "Page limit must be1..20")
        with self._locked() as now:
            if token is not None:
                device_id = self._authenticate(token, now)["device_id"]
            elif session_id is None:
                raise CompanionError(422, "Owner call reads require session_id")
            clauses, args = ["sequence>?"], [after]
            for key, value in (("session_id", session_id), ("device_id", device_id)):
                if value is not None:
                    clauses.append(key + "=?")
                    args.append(value)
            rows = self.db.execute(
                "SELECT * FROM companion_calls WHERE "
                + " AND ".join(clauses)
                + " ORDER BY sequence LIMIT ?",
                [*args, limit + 1],
            ).fetchall()
            return {
                "calls": [self._call(r) for r in rows[:limit]],
                "next_after": rows[limit - 1]["sequence"] if len(rows) > limit else None,
            }

    def action(self, call_id, request: CallAction | CallEnd, *, token=None):
        request = type(request).model_validate(request.model_dump())
        with self._locked() as now:
            device = self._authenticate(token, now) if token is not None else None
            call = self._call_row(call_id)
            if device is not None and call["device_id"] != device["device_id"]:
                raise CompanionError(404, "Unknown companion call")
            self._active(self._device_row(call["device_id"]), now)
            if not self._binding_current(call):
                raise CompanionError(403, "Call binding unavailable")
            actor = "owner" if device is None else device["device_id"]
            if device is None and not isinstance(request, CallEnd):
                raise CompanionError(403, "Owner may only end an invitation")
            action = "end" if isinstance(request, CallEnd) else request.action
            digest = _digest(_encoded(request.model_dump()))
            old = self.db.execute(
                "SELECT digest,result FROM companion_actions WHERE call_id=? AND actor=? AND idem=?",
                (call_id, actor, request.idempotency_key),
            ).fetchone()
            if old is not None:
                if old["digest"] != digest:
                    raise CompanionError(409, "Action idempotency conflict")
                result = CompanionCall.model_validate_json(old["result"])
                if result.revision != call["revision"]:
                    raise CompanionError(409, "Action receipt superseded; read current call")
                return result
            if request.expected_revision != call["revision"]:
                raise CompanionError(409, "Stale call revision")
            if call["state"] not in OPEN:
                raise CompanionError(409, "Call is terminal")
            if call["revision"] >= MAX_PLAYBACK_GENERATION - 1:
                raise CompanionError(409, "Call revision exhausted")
            if action == "answer" and call["state"] == "ringing":
                self.db.execute(
                    "UPDATE companion_calls SET state='accepted_waiting_media',media_deadline=?,revision=revision+1 WHERE call_id=?",
                    (now + 15, call_id),
                )
            elif action == "decline" and call["state"] == "ringing":
                self._end(call_id, "declined", "declined")
            elif action == "end":
                self._end(call_id, "ended", "owner_ended" if device is None else "device_ended")
            else:
                raise CompanionError(409, "Action is invalid in this state")
            result = self._call(self._call_row(call_id))
            self.db.execute(
                "INSERT INTO companion_actions VALUES(?,?,?,?,?)",
                (call_id, actor, request.idempotency_key, digest, result.model_dump_json()),
            )
            return result

    def note(self, call_id, request: CallNote):
        request = CallNote.model_validate(request.model_dump())
        with self._locked() as now:
            call = self._call_row(call_id)
            self._active(self._device_row(call["device_id"]), now)
            if not self._binding_current(call):
                raise CompanionError(403, "Call binding unavailable")
            if request.expected_revision != call["revision"]:
                raise CompanionError(409, "Stale call revision")
            if call["revision"] >= MAX_PLAYBACK_GENERATION - (1 if call["state"] in OPEN else 0):
                raise CompanionError(409, "Call revision exhausted")
            self.db.execute(
                "UPDATE companion_calls SET note=?,revision=revision+1 WHERE call_id=?",
                (request.note, call_id),
            )
            return self._call(self._call_row(call_id))
