"""Immutable credential generations inside a protected native catalog database.

Known references are copied only on an explicit owner registration/rotation.
Inspection does not read the environment or credential files. A captured adapter
owns its resolved key; later catalog mutations cannot retarget that adapter.
"""

import os
import re
import uuid
from types import MappingProxyType


def valid_key(value):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 16384
        or not value.isascii()
        or value != value.strip()
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Credential must be a bounded single token")
    try:
        if len(value.encode("utf-8")) > 16384:
            raise ValueError("Credential exceeds its UTF-8 bound")
    except UnicodeError:
        raise ValueError("Credential is not valid UTF-8") from None
    return value


class CredentialVault:
    """Use the catalog's connection/transaction; never commit independently."""

    def __init__(self, db, *, env_refs=None):
        self.db = db
        self.env_refs = MappingProxyType(dict(env_refs or {}))
        if len(self.env_refs) > 32 or any(
            type(alias) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", alias) is None
            or type(name) is not str
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) is None
            for alias, name in self.env_refs.items()
        ):
            raise ValueError("Host credential aliases must be explicitly allowlisted")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS credentials (id TEXT PRIMARY KEY, "
            "generation TEXT NOT NULL, removed INTEGER NOT NULL CHECK(removed IN (0,1)))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS credential_versions (id TEXT NOT NULL, "
            "generation TEXT NOT NULL, source TEXT NOT NULL, reference TEXT, key BLOB NOT NULL, "
            "PRIMARY KEY(id, generation))"
        )

    def reference_key(self, source, reference):
        # The request cannot supply a path or an arbitrary environment name.
        try:
            if source == "muse" and reference is None:
                from packages.providers.text import _meta_credential

                return valid_key(_meta_credential())
            if source == "openrouter" and reference is None:
                from packages.providers.openrouter_speech import _openrouter_credential

                return valid_key(_openrouter_credential())
            if source == "host_env" and reference in self.env_refs:
                return valid_key(os.environ.get(self.env_refs[reference]))
        except Exception:
            # File names, loader exceptions and supplied keys are private.
            raise ValueError("Known credential reference is unavailable") from None
        raise ValueError("Known credential reference is unavailable")

    def store(self, credential_id, key, *, source, reference=None):
        key = valid_key(key)
        exists = self.db.execute(
            "SELECT 1 FROM credentials WHERE id=?", (credential_id,)
        ).fetchone()
        if not exists and self.db.execute("SELECT count(*) FROM credentials").fetchone()[0] >= 32:
            raise ValueError("Credential count requires owner maintenance")
        if (
            self.db.execute(
                "SELECT count(*) FROM credential_versions WHERE id=?", (credential_id,)
            ).fetchone()[0]
            >= 32
        ):
            raise ValueError("Credential generations require owner maintenance")
        generation = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO credential_versions VALUES (?, ?, ?, ?, ?)",
            (credential_id, generation, source, reference, key.encode("utf-8")),
        )
        self.db.execute(
            "INSERT INTO credentials VALUES (?, ?, 0) ON CONFLICT(id) DO UPDATE SET "
            "generation=excluded.generation, removed=0",
            (credential_id, generation),
        )
        return {"id": credential_id, "generation": generation, "removed": False}

    def remove(self, credential_id):
        row = self.db.execute(
            "SELECT CASE WHEN typeof(generation)='text' AND length(CAST(generation AS BLOB))=32 "
            "THEN generation END, CASE WHEN typeof(removed)='integer' THEN removed END "
            "FROM credentials WHERE id=?",
            (credential_id,),
        ).fetchone()
        if row is not None and (
            type(row[0]) is not str
            or re.fullmatch(r"[0-9a-f]{32}", row[0]) is None
            or type(row[1]) is not int
            or row[1] not in {0, 1}
        ):
            raise ValueError("Stored credential metadata is invalid")
        if row is None or row[1]:
            raise ValueError("Credential is absent or already removed")
        self.db.execute("UPDATE credentials SET removed=1 WHERE id=?", (credential_id,))
        return {"id": credential_id, "generation": row[0], "removed": True}

    def binding_exists(self, credential_id, generation, *, for_new=True):
        if (
            type(credential_id) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", credential_id) is None
            or type(generation) is not str
            or re.fullmatch(r"[0-9a-f]{32}", generation) is None
        ):
            return False
        row = self.db.execute(
            "SELECT CASE WHEN typeof(c.removed)='integer' THEN c.removed END, "
            "CASE WHEN typeof(c.generation)='text' AND length(CAST(c.generation AS BLOB))=32 "
            "THEN c.generation END FROM credentials c JOIN credential_versions v ON c.id=v.id "
            "WHERE v.id=? AND v.generation=?",
            (credential_id, generation),
        ).fetchone()
        return (
            row is not None
            and type(row[0]) is int
            and row[0] in {0, 1}
            and type(row[1]) is str
            and re.fullmatch(r"[0-9a-f]{32}", row[1]) is not None
            and (not for_new or (row[0] == 0 and row[1] == generation))
        )

    def resolve(self, credential_id, generation, *, for_new=True):
        if not self.binding_exists(credential_id, generation, for_new=for_new):
            raise ValueError("Credential generation is unavailable for this selection")
        row = self.db.execute(
            "SELECT CASE WHEN typeof(key)='blob' AND length(key) BETWEEN 1 AND 16384 "
            "THEN key END FROM credential_versions WHERE id=? AND generation=?",
            (credential_id, generation),
        ).fetchone()
        try:
            return valid_key(row[0].decode("utf-8"))
        except Exception:
            raise ValueError("Stored credential generation is unavailable") from None

    def snapshot(self):
        # Deliberately exclude key material, key digests and filesystem paths.
        rows = self.db.execute(
            """SELECT
                CASE WHEN typeof(c.id)='text' AND length(CAST(c.id AS BLOB)) BETWEEN 1 AND 64
                    THEN c.id END,
                CASE WHEN typeof(c.generation)='text' AND length(CAST(c.generation AS BLOB))=32
                    THEN c.generation END,
                CASE WHEN typeof(c.removed)='integer' THEN c.removed END,
                CASE WHEN typeof(v.source)='text' AND length(CAST(v.source AS BLOB))<=16
                    THEN v.source END,
                CASE WHEN typeof(v.reference)='text' AND length(CAST(v.reference AS BLOB))<=64
                    THEN v.reference END,
                CASE WHEN v.reference IS NULL OR (typeof(v.reference)='text'
                    AND length(CAST(v.reference AS BLOB))<=64) THEN 0 ELSE 1 END
                FROM credentials c """
            "LEFT JOIN credential_versions v ON c.id=v.id AND c.generation=v.generation ORDER BY c.id LIMIT 33"
        ).fetchall()
        if len(rows) > 32:
            raise ValueError("Stored credential count exceeds its bound")
        for credential_id, generation, removed, source, reference, invalid_reference in rows:
            if (
                invalid_reference
                or type(credential_id) is not str
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", credential_id) is None
                or type(generation) is not str
                or re.fullmatch(r"[0-9a-f]{32}", generation) is None
                or type(removed) is not int
                or removed not in {0, 1}
                or source not in {"imported", "muse", "openrouter", "host_env"}
                or (
                    source == "host_env"
                    and (
                        type(reference) is not str
                        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", reference) is None
                    )
                )
                or (source != "host_env" and reference is not None)
            ):
                raise ValueError("Stored credential metadata is invalid")
        return [
            {
                "id": row[0],
                "generation": row[1],
                "removed": bool(row[2]),
                "source": row[3],
                "reference": row[4],
                "available": not bool(row[2]),
                "availability_scope": "stored_key_only",
                "readiness": "unknown",
            }
            for row in rows
        ]
