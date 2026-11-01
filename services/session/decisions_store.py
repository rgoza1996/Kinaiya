"""Session-owned private capture. Generic transcript readers never see scripts."""

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from packages.contracts.decisions import DecisionsCallFinish, DecisionsCallStart, encoded

CONVERSATION = (
    "conversation.user",
    "conversation.revision",
    "conversation.window",
    "conversation.converse",
)
AGENT = ("agent.command", "agent.state", "agent.tool", "agent.outcome")
RECEIPTS = (
    "routing.decided",
    "routing.classification_refused",
    "routing.dispatch",
    "routing.window_decided",
    "routing.window_dispatch",
    "routing.window_status",
)
PAYLOAD_QUOTA = 256 * 1024 * 1024
PAGE_BYTES = 262144
DETAIL_CALL_BYTES = 3 * 1024 * 1024


def _digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _stamp():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _literal_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(str(k) + "\n" + _literal_text(v) for k, v in value.items())
    if isinstance(value, list):
        return "\n".join(_literal_text(v) for v in value)
    return str(value)


def _revision(source):
    value = source.payload.get("revision", 1)
    return value if type(value) is int and 1 <= value <= 9007199254740991 else None


def date_range(from_date=None, to_date=None, timezone="UTC"):
    if not isinstance(timezone, str) or len(timezone) > 128:
        raise ValueError("Invalid inspection timezone")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Unknown inspection timezone") from exc
    if (from_date is None) != (to_date is None):
        raise ValueError("Both source dates are required")
    if from_date is None:
        return None, None
    if any(
        not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v, re.ASCII)
        for v in (from_date, to_date)
    ):
        raise ValueError("Source dates require YYYY-MM-DD")
    start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
    if start > end:
        raise ValueError("Source date range is reversed")
    return tuple(
        datetime.combine(d, time(), zone).astimezone(UTC).isoformat()
        for d in (start, end + timedelta(days=1))
    )


class DecisionsInspectionStore:
    def __init__(self, store):
        self.store = store
        self.payload_quota = PAYLOAD_QUOTA
        with store.lock, store.db:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS decisions_calls(
                  call_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  call_id TEXT NOT NULL UNIQUE,
                  source_event_id TEXT NOT NULL REFERENCES entries(event_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                  start_digest TEXT NOT NULL, finish_digest TEXT,
                  value TEXT NOT NULL, payload_bytes INTEGER NOT NULL,
                  search TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS decisions_calls_source
                  ON decisions_calls(source_event_id, call_sequence);
            """)

    def _expire(self):
        db = self.store.db
        retained = db.execute(
            "SELECT coalesce(sum(payload_bytes),0) FROM decisions_calls"
        ).fetchone()[0]
        if retained <= self.payload_quota:
            return
        rows = db.execute(
            "SELECT call_sequence,value,payload_bytes FROM decisions_calls WHERE payload_bytes>0 ORDER BY call_sequence"
        )
        try:
            for row in rows:
                value = json.loads(row["value"])
                for key in ("request", "raw_response", "validated_output"):
                    value[key] = None
                value["capture_state"] = "expired"
                db.execute(
                    "UPDATE decisions_calls SET value=?,payload_bytes=0,search='' WHERE call_sequence=?",
                    (encoded(value).decode(), row["call_sequence"]),
                )
                retained -= row["payload_bytes"]
                if retained <= self.payload_quota:
                    break
        finally:
            rows.close()

    def _save(self, sequence, value):
        payload = {k: value.get(k) for k in ("request", "raw_response", "validated_output")}
        size = len(encoded(payload)) if value["capture_state"] == "retained" else 0
        self.store.db.execute(
            "UPDATE decisions_calls SET value=?,payload_bytes=?,search=? WHERE call_sequence=?",
            (
                encoded(value).decode(),
                size,
                _literal_text(payload).casefold() if size else "",
                sequence,
            ),
        )
        self._expire()

    def start(self, request):
        request = DecisionsCallStart.model_validate(request)
        body, db = request.model_dump(), self.store.db
        if request.model_pin is None:
            # Preserve historical request digests when the additive pin is absent.
            body.pop("model_pin")
        with self.store.lock, db:
            source = self.store.entry(request.source_event_id)
            if source.kind not in CONVERSATION:
                raise ValueError("Only native conversation sources can invoke Decisions")
            if source.origin != (
                "converse" if source.kind == "conversation.converse" else "authenticated-user"
            ):
                raise ValueError("Decisions source has no native provenance")
            if _revision(source) is None:
                raise ValueError("Decisions source revision is unavailable")
            if (
                source.session_id,
                source.thread_id,
                source.epoch,
                source.sequence,
                source.payload.get("revision", 1),
            ) != (
                request.session_id,
                request.thread_id,
                request.epoch,
                request.source_sequence,
                request.source_revision,
            ):
                raise ValueError("Decisions capture source binding mismatch")
            prior = db.execute(
                "SELECT * FROM decisions_calls WHERE call_id=?", (request.call_id,)
            ).fetchone()
            if prior:
                if prior["start_digest"] != _digest(body):
                    raise ValueError("Conflicting Decisions call replay")
                return json.loads(prior["value"])
            session = self.store.session(request.session_id)
            if (session.thread_id, session.epoch) != (source.thread_id, source.epoch):
                raise ValueError("Obsolete Decisions capture source")
            value = {
                **body,
                "created_at": _stamp(),
                "completed_at": None,
                "status": "started",
                "raw_response": None,
                "validated_output": None,
                "error_type": None,
                "capture_state": "retained",
            }
            if request.reused_from_call_id:
                original = db.execute(
                    "SELECT value FROM decisions_calls WHERE call_id=? AND session_id=?",
                    (request.reused_from_call_id, request.session_id),
                ).fetchone()
                if not original:
                    raise ValueError("Unknown original Decisions call")
                original = json.loads(original[0])
                if (original["thread_id"], original["epoch"]) != (request.thread_id, request.epoch):
                    raise ValueError("Reuse crossed a native scope")
                for key in ("provider", "model", "artifact_sha256"):
                    value[key] = original[key]
                if original.get("model_pin") is not None:
                    value["model_pin"] = original["model_pin"]
                else:
                    value.pop("model_pin", None)
                value["reused_from_source_event_id"] = original["source_event_id"]
            cursor = db.execute(
                "INSERT INTO decisions_calls(call_id,source_event_id,session_id,start_digest,value,payload_bytes,search) VALUES(?,?,?,?,?,0,'')",
                (request.call_id, source.event_id, source.session_id, _digest(body), "{}"),
            )
            value["call_sequence"] = cursor.lastrowid
            self._save(cursor.lastrowid, value)
            return json.loads(
                db.execute(
                    "SELECT value FROM decisions_calls WHERE call_id=?", (request.call_id,)
                ).fetchone()[0]
            )

    def finish(self, call_id, request):
        request = DecisionsCallFinish.model_validate(request)
        body, db = request.model_dump(), self.store.db
        with self.store.lock, db:
            row = db.execute("SELECT * FROM decisions_calls WHERE call_id=?", (call_id,)).fetchone()
            if not row:
                raise KeyError(call_id)
            if request.source_event_id != row["source_event_id"]:
                raise ValueError("Decisions completion source mismatch")
            if row["finish_digest"]:
                if row["finish_digest"] != _digest(body):
                    raise ValueError("Conflicting Decisions completion replay")
                return json.loads(row["value"])
            value = json.loads(row["value"])
            updates = dict(body)
            if updates.get("artifact_sha256") is None:
                updates.pop("artifact_sha256", None)
            value.update(updates)
            value["completed_at"] = _stamp()
            if value["capture_state"] == "expired":
                for key in ("request", "raw_response", "validated_output"):
                    value[key] = None
            db.execute(
                "UPDATE decisions_calls SET finish_digest=? WHERE call_id=?",
                (_digest(body), call_id),
            )
            self._save(row["call_sequence"], value)
            return json.loads(
                db.execute(
                    "SELECT value FROM decisions_calls WHERE call_id=?", (call_id,)
                ).fetchone()[0]
            )

    def _receipts(self, source):
        rows = self.store.db.execute(
            "SELECT value FROM entries WHERE session_id=? AND thread_id=? AND epoch=? AND kind IN ("
            + ",".join("?" for _ in RECEIPTS)
            + ") AND (EXISTS(SELECT 1 FROM json_each(entries.value,'$.causes') WHERE value=?) OR json_extract(value,'$.payload.source_event_id')=?) ORDER BY sequence DESC LIMIT 101",
            (
                source.session_id,
                source.thread_id,
                source.epoch,
                *RECEIPTS,
                source.event_id,
                source.event_id,
            ),
        ).fetchall()
        result, size = [], 0
        for row in rows:
            size += len(row[0].encode())
            if len(result) == 100 or size > PAGE_BYTES:
                break
            result.append(json.loads(row[0]))
        return result, len(result) < len(rows)

    def _summary(self, source):
        db = self.store.db
        calls = db.execute(
            "SELECT count(*),max(call_sequence) FROM decisions_calls WHERE source_event_id=?",
            (source.event_id,),
        ).fetchone()
        call = (
            json.loads(
                db.execute(
                    "SELECT value FROM decisions_calls WHERE call_sequence=?", (calls[1],)
                ).fetchone()[0]
            )
            if calls[1]
            else None
        )
        receipts, _ = self._receipts(source)
        decision = next(
            (
                r["payload"]
                for r in receipts
                if r["kind"] in {"routing.decided", "routing.window_decided"}
            ),
            {},
        )
        native_reason = decision.get("reason_code")
        if source.kind in AGENT:
            disposition, capture = "no_inference", "not_applicable"
        elif call:
            disposition = {
                "started": "pending",
                "completed": "classified",
                "refused": "refused",
                "error": "error",
                "cancelled": "error",
                "reused": "reused",
            }[call["status"]]
            capture = call["capture_state"]
        elif any(r["kind"] == "routing.classification_refused" for r in receipts):
            disposition, capture = "refused", "unavailable"
        elif native_reason in {
            "agent_disabled_no_replay",
            "assistant_has_no_user_authority",
            "no_transcript_text",
            "outcome_feedback_loop_prevented",
        }:
            disposition, capture = "no_inference", "not_applicable"
        elif decision.get("classification_reused") is True:
            disposition, capture = "reused", "unavailable"
        else:
            disposition, capture = "unavailable", "unavailable"
        text = source.payload.get("text", source.payload.get("objective", ""))
        if not isinstance(text, str) or not {"text", "objective"}.intersection(source.payload):
            text = encoded(source.payload).decode()
        confidence = decision.get("confidence")
        if (
            type(confidence) not in (int, float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            confidence = None
        return {
            "source_event_id": source.event_id,
            "source_sequence": source.sequence,
            "session_id": source.session_id,
            "thread_id": source.thread_id,
            "epoch": source.epoch,
            "source_kind": source.kind,
            "source_origin": source.origin,
            "source_revision": _revision(source),
            "created_at": source.created_at.isoformat(),
            "preview": text[:1000],
            "disposition": disposition,
            "outcome": decision.get("route", decision.get("agent_route")),
            "decision_id": decision.get("decision_id"),
            "provider": call["provider"] if call else None,
            "model": call["model"] if call else None,
            "confidence": confidence,
            "call_count": calls[0],
            "capture_state": capture,
        }

    def page(
        self,
        *,
        session_id=None,
        source="all",
        outcome=None,
        search=None,
        from_date=None,
        to_date=None,
        timezone="UTC",
        through_sequence=None,
        before_sequence=None,
        limit=20,
    ):
        if source not in {"all", "conversation", "agent"} or not 1 <= limit <= 20:
            raise ValueError("Invalid inspection filter")
        if search is not None and (not isinstance(search, str) or len(search) > 200):
            raise ValueError("Inspection search exceeds its bound")
        start, end = date_range(from_date, to_date, timezone)
        if (through_sequence is None) != (before_sequence is None):
            raise ValueError("Continuation requires both inspection counters")
        with self.store.lock:
            maximum = self.store.db.execute(
                "SELECT coalesce((SELECT seq FROM sqlite_sequence WHERE name='entries'),0)"
            ).fetchone()[0]
            through = maximum if through_sequence is None else through_sequence
            before = through + 1 if before_sequence is None else before_sequence
            if not 0 <= through <= maximum or not 1 <= before <= through + 1:
                raise ValueError("Invalid inspection continuation")
            kinds = (
                CONVERSATION
                if source == "conversation"
                else AGENT
                if source == "agent"
                else CONVERSATION + AGENT
            )
            clauses = [
                "e.sequence<=?",
                "e.sequence<?",
                "e.kind IN (" + ",".join("?" for _ in kinds) + ")",
            ]
            params = [through, before, *kinds]
            if session_id is not None:
                clauses.append("e.session_id=?")
                params.append(session_id)
            if start:
                clauses.extend(
                    [
                        "julianday(json_extract(e.value,'$.created_at'))>=julianday(?)",
                        "julianday(json_extract(e.value,'$.created_at'))<julianday(?)",
                    ]
                )
                params.extend([start, end])
            if search:
                clauses.append(
                    "(instr(lower(coalesce(json_extract(e.value,'$.payload.text'),json_extract(e.value,'$.payload.objective'),e.value)),lower(?))>0 OR EXISTS(SELECT 1 FROM decisions_calls c WHERE c.source_event_id=e.event_id AND instr(c.search,?)>0))"
                )
                params.extend([search, search.casefold()])
            # Native outcome matching is literal and parameterized. A call
            # error/refusal is separate from the eventual routing disposition.
            if outcome:
                binding = "r.session_id=e.session_id AND r.thread_id=e.thread_id AND r.epoch=e.epoch AND (json_extract(r.value,'$.payload.source_event_id')=e.event_id OR EXISTS(SELECT 1 FROM json_each(r.value,'$.causes') WHERE value=e.event_id))"
                status = "(SELECT json_extract(c.value,'$.status') FROM decisions_calls c WHERE c.source_event_id=e.event_id ORDER BY call_sequence DESC LIMIT 1)"
                native_refusal = f"EXISTS(SELECT 1 FROM entries r WHERE {binding} AND r.kind='routing.classification_refused')"
                native_none = f"EXISTS(SELECT 1 FROM entries r WHERE {binding} AND r.kind IN ('routing.decided','routing.window_decided') AND json_extract(r.value,'$.payload.reason_code') IN ('agent_disabled_no_replay','assistant_has_no_user_authority','no_transcript_text','outcome_feedback_loop_prevented'))"
                native_reuse = f"EXISTS(SELECT 1 FROM entries r WHERE {binding} AND r.kind='routing.window_decided' AND json_extract(r.value,'$.payload.classification_reused')=1)"
                disposition = (
                    "CASE WHEN e.kind IN ('agent.command','agent.state','agent.tool','agent.outcome') THEN 'no_inference' "
                    + " ".join(
                        f"WHEN {status}='{raw}' THEN '{label}'"
                        for raw, label in (
                            ("started", "pending"),
                            ("completed", "classified"),
                            ("refused", "refused"),
                            ("error", "error"),
                            ("cancelled", "error"),
                            ("reused", "reused"),
                        )
                    )
                    + f" WHEN {native_refusal} THEN 'refused' WHEN {native_none} THEN 'no_inference' WHEN {native_reuse} THEN 'reused' ELSE 'unavailable' END"
                )
                clauses.append(
                    "(EXISTS(SELECT 1 FROM decisions_calls c WHERE c.source_event_id=e.event_id AND json_extract(c.value,'$.status')=?) OR EXISTS(SELECT 1 FROM entries r WHERE r.session_id=e.session_id AND r.thread_id=e.thread_id AND r.epoch=e.epoch AND r.kind IN ('routing.decided','routing.window_decided','routing.window_status') AND (json_extract(r.value,'$.payload.source_event_id')=e.event_id OR EXISTS(SELECT 1 FROM json_each(r.value,'$.causes') WHERE value=e.event_id)) AND (? IN (json_extract(r.value,'$.payload.route'),json_extract(r.value,'$.payload.agent_route'),json_extract(r.value,'$.payload.conversation'),json_extract(r.value,'$.payload.phase')))) OR ("
                    + disposition
                    + ")=?)"
                )
                params.extend([outcome, outcome, outcome])
            rows = self.store.db.execute(
                "SELECT e.event_id FROM entries e WHERE "
                + " AND ".join(clauses)
                + " ORDER BY e.sequence DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
            output, size = [], 0
            for row in rows[:limit]:
                item = self._summary(self.store.entry(row[0]))
                size += len(encoded(item))
                if size > PAGE_BYTES:
                    break
                output.append(item)
            exhausted = len(rows) <= len(output)
            return {
                "contract_version": 1,
                "through_sequence": through,
                "next_before_sequence": None if exhausted else output[-1]["source_sequence"],
                "exhausted": exhausted,
                "timezone": timezone,
                "range_start_utc": start,
                "range_end_utc": end,
                "rows": output,
            }

    def detail(
        self, source_event_id, *, through_call_sequence=None, before_call_sequence=None, limit=5
    ):
        if not 1 <= limit <= 5 or (through_call_sequence is None) != (before_call_sequence is None):
            raise ValueError("Invalid call continuation")
        with self.store.lock:
            source = self.store.entry(source_event_id)
            if source.kind not in CONVERSATION + AGENT:
                raise KeyError(source_event_id)
            receipts, truncated = self._receipts(source)
            maximum = self.store.db.execute(
                "SELECT coalesce(max(call_sequence),0) FROM decisions_calls WHERE source_event_id=?",
                (source_event_id,),
            ).fetchone()[0]
            through = maximum if through_call_sequence is None else through_call_sequence
            before = through + 1 if before_call_sequence is None else before_call_sequence
            if not 0 <= through <= maximum or not 1 <= before <= through + 1:
                raise ValueError("Invalid call continuation")
            rows = self.store.db.execute(
                "SELECT value FROM decisions_calls WHERE source_event_id=? AND call_sequence<=? AND call_sequence<? ORDER BY call_sequence DESC LIMIT ?",
                (source_event_id, through, before, limit + 1),
            ).fetchall()
            calls, size = [], 0
            for row in rows[:limit]:
                size += len(row[0].encode())
                if size > DETAIL_CALL_BYTES:
                    break
                calls.append(json.loads(row[0]))
            exhausted = len(rows) <= len(calls)
            return {
                "contract_version": 1,
                "source": source.model_dump(mode="json"),
                "summary": self._summary(source),
                "receipts": receipts,
                "receipts_truncated": truncated,
                "through_call_sequence": through,
                "next_before_call_sequence": None if exhausted else calls[-1]["call_sequence"],
                "exhausted": exhausted,
                "calls": calls,
            }
