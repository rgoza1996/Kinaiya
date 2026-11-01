"""One deterministic registry for explicit, authenticated Agent input only."""

import asyncio
import difflib
import hashlib
import json
import re
import shlex
import sqlite3
from pathlib import Path

from packages.contracts.models import (
    TERMINAL_STATES,
    AppendRequest,
    InterruptRequest,
    ProviderInfo,
    TaskRequest,
)


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
    "voice": ("Open Voice or speech model options without starting capture", [], "[tts|stt|vad]"),
    "create-harness": ("Ask Agent to build a specialized harness candidate", [], "<request>"),
    "sessions": ("List Agent sessions", [], ""),
    "resume": ("Select an existing Agent session", [], "<session-id>"),
    "new": ("Create an Agent session without cancelling existing work", [], "[name]"),
    "history": ("Inspect the most recent 100 journal entries", [], ""),
    "clear": ("Clear the client view; preserve the journal and running tasks", [], ""),
    "resources": (
        "Inspect policy or explicitly hand local voice resources to work",
        [],
        "status|profile <name>|handoff <bytes> [task-id] [--gpu-bytes N]|restore|converse on|off",
    ),
    "skills": (
        "Inspect or manage installed skills without inference",
        [],
        "list|inspect|install|enable|disable|update|rollback|remove|scope ...",
    ),
    "skill": ("Start normal Agent work with one selected skill", [], "<qualified-id> [request]"),
    "mcp": (
        "Manage MCP server connections without inference",
        [],
        "list|inspect <id>|configure <profile.json>|test|enable|disable|reconnect|remove <id>|reconcile",
    ),
    "plugins": (
        "Inspect or manage compute plugins without inference",
        [],
        "list [after]|inspect <version>|control '<JSON>'",
    ),
}


class Registry:
    def __init__(self, runtime, owner_journal=None, *, path=None, profiles=None):
        self.runtime = runtime
        self.journal = owner_journal or runtime.journal
        self.profiles = profiles or {}
        self.lock = asyncio.Lock()
        self._skill_commands = []
        self._skill_targets = {}
        self._skills_truncated = False
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
                    "execution_type": (
                        "skill_invocation"
                        if name == "skill"
                        else "task_invocation"
                        if name == "create-harness"
                        else "runtime_control"
                    ),
                    "origin": "kinaiya-core",
                    "surfaces": ["agent-chat", "agent-cli"],
                    "permissions": ["owner"],
                    "target_scope": "agent",
                    "confirmation": "explicit-owner-submission",
                    "available": (
                        name not in {"skill", "skills"}
                        or getattr(self.runtime, "skill_service", None) is not None
                    )
                    and (
                        name != "resources"
                        or getattr(self.runtime, "resource_client", None) is not None
                    )
                    and (
                        name != "plugins"
                        or getattr(self.runtime, "plugin_service", None) is not None
                    )
                    and (
                        name != "mcp" or getattr(self.runtime, "mcp_controller", None) is not None
                    ),
                }
                for name, (description, aliases, syntax) in DEFINITIONS.items()
            ]
            + self._skill_commands,
            "skill_commands_truncated": self._skills_truncated,
        }

    async def refresh_skill_commands(self):
        """Bound owner command discovery; large catalogs use qualified /skill."""
        service = getattr(self.runtime, "skill_service", None)
        self._skill_commands, self._skill_targets = [], {}
        self._skills_truncated = False
        if service is None:
            return
        settings = getattr(self.runtime, "model_settings", None)
        configured = settings.default_info() if settings is not None else None
        info = (
            ProviderInfo.model_validate(configured)
            if configured is not None
            else self.runtime.provider.info
            if settings is None
            else None
        )
        rows, after = [], 0
        for _ in range(8):
            page = await service.list_owner(after=after, limit=8)
            rows.extend(page["skills"])
            after = page["next_after"]
            if after is None:
                break
        if after is not None:
            # A collision may exist beyond this bounded window. Do not pick
            # a publisher based on catalog ordering; use explicit qualified ids.
            self._skills_truncated = True
            return
        reserved = set(DEFINITIONS) | {
            alias for _, aliases, _ in DEFINITIONS.values() for alias in aliases
        }
        counts = {}
        for row in rows:
            counts[row["name"]] = counts.get(row["name"], 0) + 1
        for row in rows:
            name = row["name"]
            if re.fullmatch(r"[a-z][a-z0-9_-]*", name) is None or (
                name in reserved
                or counts[name] != 1
                or not row.get("user_invocable", True)
                or not row.get("bare_invocable", False)
            ):
                continue
            available = row.get("enabled", False) and row.get("status") == "ready"
            contributor = info is not None and (
                info.provider == "meta" or "contributor" in info.model.lower()
            )
            scope = row.get("scope", "local_only")
            available = (
                available
                and info is not None
                and (
                    info.local
                    or scope == "public_synthetic"
                    or (not contributor and scope == "private_cloud_authorized")
                )
            )
            self._skill_commands.append(
                {
                    "id": "skill:" + row["id"],
                    "name": name,
                    "aliases": [],
                    "description": row["description"][:80],
                    "syntax": "/" + name + " [request]",
                    "arguments_schema": {"type": "array", "items": {"type": "string"}},
                    "execution_type": "skill_invocation",
                    "origin": row["id"],
                    "surfaces": ["agent-chat", "agent-cli"],
                    "permissions": ["owner"],
                    "target_scope": "agent",
                    "confirmation": "explicit-owner-submission",
                    "available": available,
                }
            )
            if available:
                self._skill_targets[name] = row["id"]

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
                legacy_digest = hashlib.sha256(
                    request.model_dump_json(exclude={"content_scope"}).encode()
                ).hexdigest()
                if row[0] != digest and not (
                    request.content_scope == "local_only" and row[0] == legacy_digest
                ):
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
        # The durable intent reserves this key. Slow connection/IO work
        # must not hold the command registry lock and block status/stop.
        try:
            parsed = parse(request.input)
            if parsed is None:
                result = self._reply("error", None, error="literal_text", literal=request.input)
            else:
                await self.refresh_skill_commands()
                name, args = parsed
                name = next(
                    (n for n, (_, aliases, _) in DEFINITIONS.items() if name in aliases), name
                )
                if name in self._skill_targets:
                    result = self._reply(
                        "ok",
                        name,
                        result=await self._invoke_skill(self._skill_targets[name], args, request),
                    )
                elif name not in DEFINITIONS:
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
            journal_response = result
            target = result.get("result")
            if result.get("command") == "stop" and isinstance(target, dict):
                target_scope = (
                    target.get("session_id"),
                    target.get("thread_id"),
                    target.get("epoch"),
                )
                if target_scope != (session.session_id, session.thread_id, session.epoch):
                    # Owner inspection can return the old task directly, but
                    # its objective/evidence must not enter this epoch's receipt.
                    journal_response = {
                        **result,
                        "result": {
                            key: target[key]
                            for key in (
                                "task_id",
                                "session_id",
                                "thread_id",
                                "epoch",
                                "objective_revision",
                                "state",
                            )
                        },
                    }
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
                        "response": journal_response,
                        # Controls can inspect older/private task state. A new
                        # grant for the command text cannot relabel that state.
                        "content_scope": "local_only",
                    },
                )
            )
        except (ValueError, KeyError) as exc:
            result = self._reply("error", None, error=str(exc))
        async with self.lock:
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
        if name == "voice":
            if len(args) > 1 or (args and args[0] not in {"tts", "stt", "vad"}):
                raise ValueError("Usage: /voice [tts|stt|vad]")
            return {
                "open_options": "audio" if args and args[0] in {"tts", "stt"} else "voice",
                "focus": args[0] if args else "vad",
                "capture_changed": False,
            }
        if name == "create-harness":
            if not args or not " ".join(args).strip():
                raise ValueError("Usage: /create-harness <request>")
            objective = (
                "Create a specialized Kinaiya harness candidate for this owner request:\n\n"
                + " ".join(args)
                + "\n\nProduce the candidate source, manifest and usage notes in the granted "
                "workspace. Inspect the available native extension contracts before choosing "
                "an implementation. The current protected harness contract is context_selector.v1; "
                "do not claim unsupported runtime capabilities. Check the candidate within existing "
                "permissions. Creation does not authorize registration, evaluation-suite changes, "
                "promotion or activation. Report the produced paths and any remaining owner steps."
            )
            if len(objective) > 16000:
                raise ValueError("Harness request exceeds the supported task length")
            task = await runtime.submit(
                TaskRequest(
                    session_id=request.session_id,
                    thread_id=request.thread_id,
                    epoch=request.epoch,
                    objective=objective,
                    content_scope=request.content_scope,
                    idempotency_key="harness-command:"
                    + hashlib.sha256(request.idempotency_key.encode()).hexdigest(),
                ),
                actor="owner",
            )
            return {
                "accepted_task_id": task.task_id,
                "state": task.state.value,
                "workflow": "harness_candidate",
                "activation": "owner_review_required",
            }
        if name == "resources":
            return await self._resources_control(args, request)
        if name == "skill":
            if not args:
                raise ValueError("Usage: /skill <qualified-id> [request]")
            return await self._invoke_skill(args[0], args[1:], request)
        if name == "skills":
            return await self._skills_control(args)
        if name == "plugins":
            return await self._plugins_control(args, request)
        if name == "mcp":
            return await self._mcp_control(args, request)
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
                async with runtime.integration_lock:
                    if any(t.state not in TERMINAL_STATES for t in await runtime.status()):
                        raise ValueError(
                            "Finish or stop active work before selecting another profile"
                        )
                    settings = getattr(runtime, "model_settings", None)
                    if settings is None:
                        runtime.provider = self.profiles[args[0]]()
                    else:
                        from services.agent.model_settings import ModelSettingsChange

                        operation_key = (
                            "command:"
                            + hashlib.sha256(
                                (request.session_id + ":" + request.idempotency_key).encode()
                            ).hexdigest()
                        )
                        prior = settings.receipt(operation_key)
                        settings.change(
                            ModelSettingsChange(
                                schema_version="1.0",
                                expected_revision=prior["revision"] - 1
                                if prior is not None
                                else settings.snapshot()["revision"],
                                idempotency_key=operation_key,
                                profile_id=args[0],
                            )
                        )
                        runtime.provider = settings.selection(None)[0]
            settings = getattr(runtime, "model_settings", None)
            return {
                "active": settings.default_info()
                if settings is not None
                else runtime.provider.info.model_dump(),
                "profiles": sorted(self.profiles),
            }
        if name in {"permissions", "yolo"}:
            if name == "yolo":
                if args not in (["on"], ["off"]):
                    raise ValueError("Usage: /yolo on|off")

                async def apply_yolo():
                    runtime.policy.set_permissions(actor="owner", yolo=args == ["on"])
                    if (controller := getattr(runtime, "mcp_controller", None)) is not None:
                        await controller.retire_connections(actor="owner")
                    elif (host := getattr(runtime, "mcp_host", None)) is not None:
                        await host.reset_connections(actor="owner")

                if args == ["on"]:
                    async with runtime.integration_lock:
                        if any(t.state not in TERMINAL_STATES for t in await runtime.status()):
                            raise ValueError(
                                "Finish or stop active work before expanding YOLO grants"
                            )
                        await apply_yolo()
                else:
                    # Revocation is immediate, including during active work.
                    await apply_yolo()
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
                from core.extensions import HarnessError
                from packages.contracts.models import SessionRequest

                parent = await self.journal.session(request.session_id)
                if parent.channel != "agent" or (parent.thread_id, parent.epoch) != (
                    request.thread_id,
                    request.epoch,
                ):
                    raise ValueError("New session requires the current invoking Agent identity")
                if parent.harness_version is not None:
                    try:
                        self.runtime.session_harness(parent.harness_version)
                    except (HarnessError, OSError, ValueError) as exc:
                        raise ValueError(
                            "Invoking session harness is unavailable or unapproved"
                        ) from exc
                return (
                    await self.journal.create_session(
                        SessionRequest(
                            channel="agent",
                            name=" ".join(args),
                            harness_version=parent.harness_version,
                        )
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

    async def _invoke_skill(self, skill_id, args, request):
        task = await self.runtime.submit(
            TaskRequest(
                session_id=request.session_id,
                thread_id=request.thread_id,
                epoch=request.epoch,
                objective=" ".join(args)
                or "Follow the selected skill's procedure within current permissions.",
                content_scope=request.content_scope,
                idempotency_key="skill-command:"
                + hashlib.sha256(request.idempotency_key.encode()).hexdigest(),
            ),
            actor="owner",
            explicit_skill=skill_id,
        )
        return {"accepted_task_id": task.task_id, "state": task.state.value, "skill_id": skill_id}

    async def _mcp_control(self, args, request):
        from packages.contracts.models import MCPControl
        from services.agent.mcp_controls import (
            apply_mcp_control,
            require_mcp_controller,
        )

        service = require_mcp_controller(self.runtime)
        snapshot = service.snapshot()
        if not args or args == ["list"]:
            return snapshot
        if len(args) == 2 and args[0] == "inspect":
            selected = [row for row in snapshot["servers"] if row["id"] == args[1]]
            if not selected:
                raise ValueError("Unknown MCP server")
            return {"revision": snapshot["revision"], "server": selected[0]}
        fields = {
            "operation": args[0],
            "expected_revision": snapshot["revision"],
            "idempotency_key": "command:"
            + hashlib.sha256(
                (request.session_id + ":" + request.idempotency_key).encode()
            ).hexdigest(),
        }
        if args[0] == "configure" and len(args) == 2:
            raw = self.runtime.policy.read_bytes(args[1], maximum=262_145)
            try:
                fields["config"] = json.loads(raw)
            except (ValueError, RecursionError) as exc:
                raise ValueError("MCP profile must contain bounded valid JSON") from exc
            fields["reviewed"] = True
        elif args == ["reconcile"]:
            fields["reviewed"] = True
        elif len(args) == 2 and args[0] in {"enable", "disable", "remove", "test", "reconnect"}:
            fields["server_id"] = args[1]
            fields["reviewed"] = args[0] == "enable"
        else:
            raise ValueError(
                "Usage: /mcp list|inspect <id>|configure <profile.json>|test|enable|disable|reconnect|remove <id>|reconcile"
            )
        return await apply_mcp_control(self.runtime, MCPControl(**fields))

    async def _plugins_control(self, args, request):
        from services.agent.plugin_controls import (
            PluginControl,
            apply_plugin_control,
            require_plugins,
        )

        service = require_plugins(self.runtime)
        if not args or args[0] == "list":
            if len(args) > 2:
                raise ValueError("Usage: /plugins list [after]")
            return service.list_owner(after=int(args[1]) if len(args) == 2 else 0)
        if args[0] == "inspect" and len(args) == 2:
            fields = {"operation": "inspect", "version_id": args[1]}
        elif args[0] == "control" and len(args) == 2 and len(args[1]) <= 65_536:
            fields = json.loads(args[1])
            if not isinstance(fields, dict) or "idempotency_key" in fields:
                raise ValueError("Plugin control must be an object; command supplies its identity")
        else:
            raise ValueError("Usage: /plugins list [after]|inspect <version>|control '<JSON>'")
        fields["idempotency_key"] = (
            "command:"
            + hashlib.sha256(
                (request.session_id + ":" + request.idempotency_key).encode()
            ).hexdigest()
        )
        return await apply_plugin_control(self.runtime, PluginControl(**fields))

    async def _skills_control(self, args):
        from services.agent.skill_controls import SkillControl, apply_skill_control, require_skills

        if not args or args[0] == "list":
            if len(args) > 2:
                raise ValueError("Usage: /skills list [after]")
            return await require_skills(self.runtime).list_owner(
                after=int(args[1]) if len(args) == 2 else 0
            )
        operation, rest = args[0], args[1:]
        fields = {"operation": operation}
        if operation in {"install", "install_archive"} and len(rest) in {1, 2}:
            fields["source"] = rest[0]
            if len(rest) == 2:
                fields["content_scope"] = rest[1]
        elif operation in {"update", "update_archive"} and len(rest) in {2, 3}:
            fields.update(skill_id=rest[0], source=rest[1])
            if len(rest) == 3:
                fields["content_scope"] = rest[2]
        elif operation == "scope" and len(rest) == 2:
            fields.update(skill_id=rest[0], content_scope=rest[1])
        elif operation in {"inspect", "enable", "disable", "rollback", "remove"} and len(rest) == 1:
            fields["skill_id"] = rest[0]
        else:
            raise ValueError(
                "Usage: /skills list|inspect|install|install_archive|enable|disable|update|update_archive|rollback|remove|scope ..."
            )
        result = await apply_skill_control(self.runtime, SkillControl(**fields))
        await self.refresh_skill_commands()
        return result

    async def _resources_control(self, args, request):
        client = getattr(self.runtime, "resource_client", None)
        if client is None:
            raise ValueError("Resource coordination is not configured")
        try:
            if not args or args == ["status"]:
                return await client.status()
            fields = {
                "operation": args[0],
                "idempotency_key": "command:"
                + hashlib.sha256(
                    (request.session_id + request.idempotency_key).encode()
                ).hexdigest(),
            }
            if args[0] == "profile" and len(args) == 2:
                fields["profile"] = args[1]
            elif args[0] == "handoff":
                inputs = list(args[1:])
                if "--gpu-bytes" in inputs:
                    index = inputs.index("--gpu-bytes")
                    if index != len(inputs) - 2 or inputs.count("--gpu-bytes") != 1:
                        raise ValueError("Use one trailing --gpu-bytes N")
                    fields["required_gpu_bytes"] = int(inputs[-1])
                    inputs = inputs[:index]
                if len(inputs) not in {1, 2}:
                    raise ValueError("Handoff needs host bytes, optional task ID and --gpu-bytes N")
                fields["required_bytes"] = int(inputs[0])
                if len(inputs) == 2:
                    fields["task_id"] = inputs[1]
            elif args[0] == "converse" and len(args) == 2 and args[1] in {"on", "off"}:
                fields["enabled"] = args[1] == "on"
            elif args != ["restore"]:
                raise ValueError(
                    "Usage: /resources status|profile <name>|handoff <bytes> [task-id] [--gpu-bytes N]|restore|converse on|off"
                )
            result = await client.action(fields)
            if result.get("ok") is not True:
                raise ValueError(result.get("error", "Resource transition is unconfirmed"))
            return result
        except Exception as exc:
            raise ValueError(str(exc)) from exc

    def close(self):
        self.db.close()
