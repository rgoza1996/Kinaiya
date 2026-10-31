"""Owner project labels and session associations; never filesystem grants."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProjectConflict(ValueError):
    pass


class ProjectChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:._-]+$")
    operation: Literal["create", "attach", "detach"]
    name: str | None = Field(default=None, min_length=1, max_length=120)
    project_id: str | None = Field(default=None, pattern=r"^[a-f0-9-]{36}$")
    expected_revision: int | None = Field(default=None, ge=1, le=9_007_199_254_740_990)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def shape(self):
        if self.operation == "create":
            if self.project_id is not None or self.expected_revision is not None or not self.name:
                raise ValueError("Project creation requires a name and no prior identity")
            if not self.name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in self.name):
                raise ValueError("Project name must be readable text")
        elif (
            not self.project_id
            or self.expected_revision is None
            or not self.session_id
            or self.name is not None
        ):
            raise ValueError("Session association requires a project revision and session")
        return self


class Projects:
    def __init__(self, path=None):
        if path is not None:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path) if path is not None else ":memory:")
        if path is not None:
            path.chmod(0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS projects "
            "(id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL, revision INTEGER NOT NULL);"
            "CREATE TABLE IF NOT EXISTS sessions "
            "(project TEXT NOT NULL, session TEXT NOT NULL, PRIMARY KEY(project,session));"
            "CREATE TABLE IF NOT EXISTS operations "
            "(key TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT NOT NULL);"
        )

    def get(self, project_id):
        row = self.db.execute(
            "SELECT id,name,created,revision FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise KeyError(project_id)
        sessions = self.db.execute(
            "SELECT session FROM sessions WHERE project=? ORDER BY session", (project_id,)
        ).fetchall()
        return {
            "schema_version": "1.0",
            "project_id": row[0],
            "name": row[1],
            "created_at": row[2],
            "revision": row[3],
            "session_ids": [r[0] for r in sessions],
        }

    def page(self, after=None, limit=20):
        rows = self.db.execute(
            "SELECT id FROM projects WHERE id>? ORDER BY id LIMIT ?", (after or "", limit + 1)
        ).fetchall()
        return {
            "schema_version": "1.0",
            "items": [self.get(row[0]) for row in rows[:limit]],
            "next_cursor": rows[limit - 1][0] if len(rows) > limit else None,
        }

    def receipt(self, key):
        row = self.db.execute("SELECT receipt FROM operations WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def change(self, request):
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute(
                "SELECT digest,receipt FROM operations WHERE key=?", (request.idempotency_key,)
            ).fetchone()
            if prior:
                if prior[0] != digest:
                    raise ProjectConflict("Request key belongs to another project change")
                self.db.rollback()
                return json.loads(prior[1])
            if self.db.execute("SELECT count(*) FROM operations").fetchone()[0] >= 10000:
                raise ValueError("Project receipt storage needs owner maintenance")
            project_id = request.project_id
            if request.operation == "create":
                if self.db.execute("SELECT count(*) FROM projects").fetchone()[0] >= 500:
                    raise ValueError("Project limit reached")
                project_id = str(uuid4())
                self.db.execute(
                    "INSERT INTO projects VALUES (?,?,?,1)",
                    (project_id, request.name.strip(), datetime.now(UTC).isoformat()),
                )
            else:
                project = self.get(project_id)
                if project["revision"] != request.expected_revision:
                    raise ProjectConflict("Project changed; refresh before editing")
                if project["revision"] >= 9_007_199_254_740_990:
                    raise ValueError("Project revision limit reached")
                self.db.execute("UPDATE projects SET revision=revision+1 WHERE id=?", (project_id,))
            if request.session_id is not None:
                if request.operation == "detach":
                    self.db.execute(
                        "DELETE FROM sessions WHERE project=? AND session=?",
                        (project_id, request.session_id),
                    )
                else:
                    if (
                        self.db.execute(
                            "SELECT count(*) FROM sessions WHERE project=?", (project_id,)
                        ).fetchone()[0]
                        >= 200
                    ):
                        raise ValueError("Project session limit reached")
                    self.db.execute(
                        "INSERT OR IGNORE INTO sessions VALUES (?,?)",
                        (project_id, request.session_id),
                    )
            receipt = {
                "schema_version": "1.0",
                "idempotency_key": request.idempotency_key,
                "operation": request.operation,
                "project": self.get(project_id),
            }
            self.db.execute(
                "INSERT INTO operations VALUES (?,?,?)",
                (request.idempotency_key, digest, json.dumps(receipt)),
            )
            self.db.commit()
            return receipt
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()
