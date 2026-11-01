"""Codex-authored admission tombstones, composed with the native Task owner.

All helpers below are transaction-neutral. Public calls use the Task owner's
same Store lock/BEGIN IMMEDIATE; neither a read nor a constructor migrates.
"""

import hmac
import re

from packages.contracts.standing_closure import (
    MAX_STANDING_ADMISSION_CLOSURE_BYTES,
    StandingAdmissionClosure,
    _original,
    standing_closure_digest,
)
from packages.contracts.standing_execution import capability_digest, claim_id, intent_digest
from packages.contracts.standing_work import StandingConflict, StandingUnavailable, canonical_bytes

_META = "standing_admission_closure_meta"
_ROWS = "standing_admission_closures"
_TABLES = {
    _META: (
        f"CREATE TABLE {_META}(id INTEGER PRIMARY KEY CHECK(id=1),"
        "version INTEGER NOT NULL,backend_id TEXT NOT NULL)",
        (
            ("id", "INTEGER", 0, None, 1, 0),
            ("version", "INTEGER", 1, None, 0, 0),
            ("backend_id", "TEXT", 1, None, 0, 0),
        ),
    ),
    _ROWS: (
        f"CREATE TABLE {_ROWS}(claim_id TEXT PRIMARY KEY,closure TEXT NOT NULL)",
        (("claim_id", "TEXT", 0, None, 1, 0), ("closure", "TEXT", 1, None, 0, 0)),
    ),
}


def _sql_identity(value):
    return re.sub(r"\s+", "", value).rstrip(";").lower()


class StandingAdmissionClosures:
    """Private passive composition; registration belongs to StandingTaskOwner."""

    def __init__(self, owner):
        self.owner = owner

    def blocks_admission(self):
        # The caller has just validated the full observation in this same
        # transaction. No separate lookup can open the first-admission gate.
        return self.owner._one(f"SELECT 1 FROM {_ROWS} LIMIT 1") is not None

    def register(self, meta, claim, initial):
        owner = self.owner
        present = owner._all(
            "SELECT name,type FROM sqlite_schema WHERE name IN (?,?)", (_META, _ROWS)
        )
        created = False
        if not present:
            try:
                owner._writer(meta, registering=True)
            except StandingConflict:
                raise StandingUnavailable from None
            for sql, _ in _TABLES.values():
                owner._sql(sql)
            owner._sql(f"INSERT INTO {_META} VALUES(1,1,?)", (meta["backend_id"],))
            created = True
        elif len(present) != 2 or any(row["type"] != "table" for row in present):
            raise StandingUnavailable
        self.observe(meta, claim, initial)
        return created

    def observe(self, meta, claim, initial):
        owner = self.owner
        for name, (sql, expected) in _TABLES.items():
            definition = owner._one("SELECT type,sql FROM sqlite_schema WHERE name=?", (name,))
            if (
                definition is None
                or definition["type"] != "table"
                or type(definition["sql"]) is not str
                or _sql_identity(definition["sql"]) != _sql_identity(sql)
                or owner._all(f"PRAGMA foreign_key_list({name})")
                or owner._one(
                    "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name=?", (name,)
                )
            ):
                raise StandingUnavailable
            columns = tuple(
                (
                    row["name"],
                    row["type"],
                    row["notnull"],
                    row["dflt_value"],
                    row["pk"],
                    row["hidden"],
                )
                for row in owner._all(f"PRAGMA table_xinfo({name})")
            )
            if columns != expected:
                raise StandingUnavailable
            indexes = owner._all(f"PRAGMA index_list({name})")
            if name == _META:
                if indexes:
                    raise StandingUnavailable
            else:
                if len(indexes) != 1:
                    raise StandingUnavailable
                index = indexes[0]
                if index["unique"] != 1 or index["origin"] != "pk" or index["partial"] != 0:
                    raise StandingUnavailable
                escaped = index["name"].replace('"', '""')
                keys = tuple(
                    (row["name"], row["desc"], row["coll"])
                    for row in owner._all(f'PRAGMA index_xinfo("{escaped}")')
                    if row["key"] == 1
                )
                if keys != (("claim_id", 0, "BINARY"),):
                    raise StandingUnavailable
        if owner._one(f"SELECT count(*) FROM {_META}")[0] != 1 or owner._one(
            f"SELECT 1 FROM {_META} WHERE typeof(id)!='integer' OR id!=1 "
            "OR typeof(version)!='integer' OR version!=1 OR typeof(backend_id)!='text' "
            "OR length(CAST(backend_id AS BLOB))!=32 LIMIT 1"
        ):
            raise StandingUnavailable
        if owner._one(f"SELECT backend_id FROM {_META}")[0] != meta["backend_id"]:
            raise StandingUnavailable
        count = owner._one(f"SELECT count(*) FROM {_ROWS}")[0]
        if type(count) is not int or not 0 <= count <= 1:
            raise StandingUnavailable
        if owner._one(
            f"SELECT 1 FROM {_ROWS} WHERE typeof(claim_id)!='text' "
            "OR length(CAST(claim_id AS BLOB))!=64 OR typeof(closure)!='text' "
            "OR length(CAST(closure AS BLOB)) NOT BETWEEN 1 AND ? LIMIT 1",
            (MAX_STANDING_ADMISSION_CLOSURE_BYTES,),
        ):
            raise StandingUnavailable
        if not count:
            return None
        if claim is None:
            raise StandingUnavailable
        row = owner._one(f"SELECT claim_id,closure FROM {_ROWS}")
        closure = StandingAdmissionClosure.from_json(row["closure"].encode("utf-8"))
        if (
            row["claim_id"] != claim.claim_id
            or closure.claim_id != claim.claim_id
            or canonical_bytes(closure).decode("utf-8") != row["closure"]
            or (closure.initial is None) != (initial is None)
            or initial is not None
            and canonical_bytes(closure.initial) != canonical_bytes(initial)
        ):
            raise StandingUnavailable
        return closure.validate_request(claim.request)

    def close_admission(self, original, *, capability, deadline=None, cancelled=None):
        owner = self.owner
        deadline = owner.claims._options(deadline, cancelled)
        original = _original(original)
        if (
            type(capability) is not bytes
            or len(capability) != 32
            or not hmac.compare_digest(
                capability_digest(
                    original.expected_backend,
                    original.idempotency_key,
                    original.executor_id,
                    capability,
                ),
                original.capability_sha256,
            )
        ):
            raise StandingConflict
        owner._guard(deadline, cancelled)
        created = False

        def commit_guard():
            meta, _, _, _, _, current, proof = owner._observation()
            if current is None or canonical_bytes(current.request) != canonical_bytes(original):
                raise StandingUnavailable
            if created:
                owner._writer(meta)
            self.observe(meta, current, proof)
            # This is last: every SQL/history join counts toward the deadline.
            owner._guard(deadline, cancelled)

        with owner._transaction(commit_guard):
            meta, generations, _, _, _, claim, initial = owner._observation()
            if original.expected_backend != meta["backend_id"]:
                raise StandingConflict
            owner.dispatch._known(original.expected_instance, generations)
            if claim is None or canonical_bytes(claim.request) != canonical_bytes(original):
                raise StandingConflict
            previous = self.observe(meta, claim, initial)
            if previous is not None:
                return previous.validate_request(original)
            owner._writer(meta)
            closure = StandingAdmissionClosure(
                schema_version="1.0",
                intent_digest=intent_digest(original),
                claim_id=claim_id(original),
                initial=initial,
                closure_digest=standing_closure_digest(original, initial),
            ).validate_request(original)
            owner._guard(deadline, cancelled)
            owner._sql(
                f"INSERT INTO {_ROWS} VALUES(?,?)",
                (claim.claim_id, canonical_bytes(closure).decode("utf-8")),
            )
            created = True
            self.observe(meta, claim, initial)
        owner._guard(deadline, cancelled)
        return closure

    def lookup(self, original):
        owner = self.owner
        original = _original(original)
        with owner._transaction():
            meta, generations, _, _, _, claim, initial = owner._observation()
            if original.expected_backend != meta["backend_id"]:
                raise StandingConflict
            owner.dispatch._known(original.expected_instance, generations)
            if claim is None or claim.request.idempotency_key != original.idempotency_key:
                return None
            if canonical_bytes(claim.request) != canonical_bytes(original):
                raise StandingConflict
            result = self.observe(meta, claim, initial)
            return None if result is None else result.validate_request(original)
