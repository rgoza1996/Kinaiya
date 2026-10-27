"""One deterministic registry for explicit, authenticated Agent input only."""

import asyncio
import difflib
import hashlib
import json
import re
import shlex
import sqlite3
from pathlib import Path

from packages.contracts.models import TERMINAL_STATES, AppendRequest, InterruptRequest


def parse(text):
    """A single-line leading command; paths, quotations and escaped slashes stay literal."""
    if "\n" in text or "\r" in text or not re.match(r"^/[a-z][a-z0-9_-]*(?:\s|$)", text):
        return None
    words = shlex.split(text)
    return words[0][1:], words[1:]


DEFINITIONS = {
    "help": ("List commands or inspect one", ["commands"], "[command]"),
    "status": ("Inspect factual task state", ["tasks"], "[task-id]"),
    "stop": ("Request cancellation of a selected task", [], "[task-id]"),
    "model": ("Inspect or select a configured Agent profile", [], "[profile]"),
    "permissions": ("Inspect current worker permissions", [], ""),
    "yolo": ("Change workspace grants until this service restarts", [], "on|off"),
    "sessions": ("List Agent sessions", [], ""),
    "resume": ("Select an existing Agent session", [], "<session-id>"),
    "new": ("Create an Agent session without cancelling existing work", [], "[name]"),
    "history": ("Inspect the most recent 100 journal entries", [], ""),
    "clear": ("Clear the client view; preserve the journal and running tasks", [], ""),
}


class Registry:
    def __init__(self, runtime, owner_journal=None, *, path=None, profiles=None):
        self.runtime = runtime
        self.journal = owner_journal or runtime.journal
        self.profiles = profiles or {}
        self.lock = asyncio.Lock()
        if path:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path) if path else ":memory:")
        if path:
            path.chmod(0o600)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS commands (session TEXT, key TEXT, digest TEXT, result TEXT, "
            "PRIMARY KEY(session,key))"
        )
        self.db.commit()

    def definitions(self):
        return {
            "schema_version": "1.0",
            "commands": [
                {
                    "id": "kinaiya:" + name,
                    "name": name,
                    "aliases": aliases,
                    "description": description,
                    "syntax": "/" + name + (" " + syntax if syntax else ""),
                    "arguments_schema": {"type": "array", "items": {"type": "string"}},
                    "execution_type": "runtime_control",
                    "origin": "kinaiya-core",
                    "surfaces": ["agent-chat", "agent-cli"],
                    "permissions": ["owner"],
                    "target_scope": "agent",
                    "confirmation": "explicit-owner-submission",
                    "available": True,
                }
                for name, (description, aliases, syntax) in DEFINITIONS.items()
            ],
        }

    async def execute(self, request, actor="owner"):
        if actor != "owner":
            raise PermissionError("Only explicit authenticated owner input can dispatch commands")
        session = await self.journal.session(request.session_id)
        if session.channel != "agent" or (session.thread_id, session.epoch) != (
            request.thread_id,
            request.epoch,
        ):
            raise ValueError("Commands require the current Agent session and epoch")
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        async with self.lock:
            row = self.db.execute(
                "SELECT digest,result FROM commands WHERE session=? AND key=?",
                (request.session_id, request.idempotency_key),
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("Command idempotency key conflicts with earlier input")
                return (
                    json.loads(row[1])
                    if row[1]
                    else self._reply(
                        "error",
                        None,
                        error="Interrupted dispatch; inspect state before a new request",
                    )
                )
            with self.db:
                self.db.execute(
                    "INSERT INTO commands VALUES (?,?,?,NULL)",
                    (request.session_id, request.idempotency_key, digest),
                )
            try:
                parsed = parse(request.input)
                if parsed is None:
                    result = self._reply("error", None, error="literal_text", literal=request.input)
                else:
                    name, args = parsed
                    name = next(
                        (n for n, (_, aliases, _) in DEFINITIONS.items() if name in aliases), name
                    )
                    if name not in DEFINITIONS:
                        result = self._reply(
                            "error",
                            name,
                            error="Unknown command",
                            suggestions=difflib.get_close_matches(name, DEFINITIONS, n=3),
                        )
                    else:
                        result = self._reply(
                            "ok", name, result=await self._dispatch(name, args, request)
                        )
                await self.runtime.journal.append(
                    AppendRequest(
                        session_id=session.session_id,
                        thread_id=session.thread_id,
                        epoch=session.epoch,
                        kind="agent.command",
                        idempotency_key="command:" + request.idempotency_key,
                        payload={
                            "op": "control",
                            "actor": "owner",
                            "input": request.input,
                            "response": result,
                        },
                    )
                )
            except (ValueError, KeyError) as exc:
                result = self._reply("error", None, error=str(exc))
            with self.db:
                self.db.execute(
                    "UPDATE commands SET result=? WHERE session=? AND key=?",
                    (json.dumps(result), request.session_id, request.idempotency_key),
                )
            return result

    @staticmethod
    def _reply(status, command, **fields):
        return {"schema_version": "1.0", "status": status, "command": command, **fields}

    async def _dispatch(self, name, args, request):
        runtime = self.runtime
        if name == "help":
            if len(args) > 1:
                raise ValueError("Usage: /help [command]")
            commands = self.definitions()["commands"]
            return (
                commands if not args else [d for d in commands if d["name"] == args[0].lstrip("/")]
            )
        if name in {"status", "stop"}:
            if len(args) > 1:
                raise ValueError("Expected at most one task id")
            tasks = await runtime.status()
            if name == "status":
                return [
                    t.model_dump(mode="json") for t in tasks if not args or t.task_id == args[0]
                ]
            choices = [t for t in tasks if t.state not in TERMINAL_STATES]
            if args:
                choices = [t for t in choices if t.task_id == args[0]]
            elif len(choices) > 1:
                raise ValueError(
                    "Ambiguous target; select a task id: " + ", ".join(t.task_id for t in choices)
                )
            if not choices:
                raise ValueError("No matching active task")
            task = choices[0]
            return (
                await runtime.interrupt(
                    task.task_id,
                    InterruptRequest(
                        expected_objective_revision=task.objective_revision,
                        idempotency_key="command:" + request.idempotency_key,
                    ),
                )
            ).model_dump(mode="json")
        if name == "model":
            if len(args) > 1:
                raise ValueError("Usage: /model [configured-profile]")
            if args:
                if args[0] not in self.profiles:
                    raise ValueError("Unknown configured profile")
                if any(t.state not in TERMINAL_STATES for t in await runtime.status()):
                    raise ValueError("Finish or stop active work before selecting another profile")
                runtime.provider = self.profiles[args[0]]()
            return {"active": runtime.provider.info.model_dump(), "profiles": sorted(self.profiles)}
        if name in {"permissions", "yolo"}:
            if name == "yolo":
                if args not in (["on"], ["off"]):
                    raise ValueError("Usage: /yolo on|off")
                runtime.policy.set_permissions(actor="owner", yolo=args == ["on"])
            elif args:
                raise ValueError("Usage: /permissions")
            return {
                k: getattr(runtime.policy, k)
                for k in ("allow_write", "allow_shell", "network", "yolo")
            }
        if name in {"sessions", "new", "resume"}:
            if name == "sessions":
                if args:
                    raise ValueError("Usage: /sessions")
                return [
                    s.model_dump() for s in await self.journal.sessions() if s.channel == "agent"
                ]
            if name == "new":
                from packages.contracts.models import SessionRequest

                return (
                    await self.journal.create_session(
                        SessionRequest(channel="agent", name=" ".join(args))
                    )
                ).model_dump()
            if len(args) != 1:
                raise ValueError("Usage: /resume <session-id>")
            session = await self.journal.session(args[0])
            if session.channel != "agent":
                raise ValueError("Select an Agent session")
            return session.model_dump()
        if args:
            raise ValueError("Unexpected arguments")
        if name == "history":
            return [
                e.model_dump(mode="json")
                for e in await self.runtime.journal.recent(session_id=request.session_id, limit=100)
            ]
        if name == "clear":
            return {"clear_view": True, "journal_preserved": True}

    def close(self):
        self.db.close()
