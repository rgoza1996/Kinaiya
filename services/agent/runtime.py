"""Seed Agent runtime: a bounded, supervised JSON tool-action loop.

The runtime owns task identity, lifecycle, evidence, approvals, and journal
persistence. The model proposes single JSON ``ToolAction`` objects; the
supervisor executes them, authors every ``Evidence`` record from observed
results, and decides completion. Model output can never supply evidence,
state, success claims, goals, or permission changes.

Boundaries enforced here (not in prompts):
- cancellation and objective revision are checked before every side effect
  and before completion; stale revisions are rejected;
- idempotency keys separate identical replay (same task back) from
  conflicting payload (rejected);
- steering applies at the safe loop boundary, invalidates old evidence,
  and requires re-verification under the new revision;
- interrupt cancels hung inference and kills the running child process
  group; ``status`` and ``interrupt`` never wait for the model;
- terminal tasks never re-execute; recovery marks uncertain work paused
  instead of blindly retrying writes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.extensions import HarnessError
from core.policy import ApprovalError, PolicyDenied, ToolPolicy
from harnesses.seed import ARTIFACT_RECEIPT_LIMIT, build_messages, observation_text, parse_action
from packages.contracts.agent_tools import (
    NativeToolSnapshot,
    agent_tool_scope,
    native_tool_snapshot,
)
from packages.contracts.interfaces import Journal, TextProvider
from packages.contracts.models import (
    AppendRequest,
    ApprovalResolution,
    Evidence,
    InterruptRequest,
    JournalEntry,
    PendingApproval,
    PendingApprovalPage,
    SpeechWindow,
    SteerRequest,
    Task,
    TaskRequest,
    TaskRetirement,
    TaskState,
)
from packages.providers.disclosure import combine_scopes, entry_allowed, scope_allowed, source_scope
from packages.providers.text import ActionDecodeError
from services.agent.artifacts import ArtifactConflict, ArtifactInvalid, ArtifactNotFound
from services.agent.completion import append_completion
from services.agent.mcp_tools import ModelScopedMCP, run_mcp_call, run_mcp_list
from services.agent.tools import (
    RunningProc,
    ToolFailure,
    _FailedWithEvidence,
    run_read,
    run_search,
    run_shell,
    run_write,
    validate_write_arguments,
)


class TaskNotFound(KeyError):
    pass


class StaleRevision(ValueError):
    pass


class IdemConflict(ValueError):
    pass


class TaskTerminal(ValueError):
    pass


class _PublicationUncertain(ToolFailure):
    """A possible persisted publication must never be blindly retried."""

    def __init__(self, message: str, evidence: Evidence | None = None):
        super().__init__(message)
        self.evidence = evidence


TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}


def _payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _ui_publication_receipt(envelope: dict[str, Any]) -> str:
    """Keep native pins and the complete manifest without replaying UI data."""
    receipt = {
        "history_kind": "ui_publication_receipt",
        "tool": "publish_ui",
        "ok": True,
        "artifact_id": envelope["artifact_id"],
        "revision": envelope["revision"],
        "objective_revision": envelope["objective_revision"],
        "content_sha256": envelope["content_sha256"],
        "components": [
            {"id": component["id"], "type": component["type"]}
            for component in envelope["components"]
        ],
        "title_excerpt": "",
        "summary_excerpt": "",
        "payload_omitted": True,
        "receipt_scope": "UI publication only; no filesystem or external effect proven",
        "next_action": (
            "Finish or update this artifact_id with expected_revision=revision; "
            "do not repeat the create unless required by the objective."
        ),
    }

    def encode() -> str:
        return json.dumps(receipt, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    def fits() -> bool:
        return len(encode().encode("utf-8")) <= ARTIFACT_RECEIPT_LIMIT

    def excerpt(text: str, length: int) -> str:
        return text[:length] + ("…" if 0 < length < len(text) else "")

    # The validated 24-component manifest and native pins fit independently
    # of optional excerpts. Never sacrifice a digest, revision or component ID.
    if not fits():
        raise ValueError("Native UI receipt manifest exceeds its history byte budget")
    for key, text, maximum in (
        ("title_excerpt", envelope["title"], 160),
        ("summary_excerpt", envelope["summary"], 512),
    ):
        maximum = min(maximum, len(text))
        receipt[key] = excerpt(text, maximum)
        if fits():
            continue
        # Measure the final escaped JSON bytes, not just UTF-8 source text.
        low, high = 0, maximum - 1
        while low < high:
            middle = (low + high + 1) // 2
            receipt[key] = excerpt(text, middle)
            if fits():
                low = middle
            else:
                high = middle - 1
        receipt[key] = excerpt(text, low)
    return encode()


# In-memory terminal summary for administratively retired recovered work.
# Durable receipts carry only hashes; this sentence is the whole effect
# accounting visible in the current session.
RETIRED_SUMMARY = (
    "Cancelled recovered work without replay; prior uncertain effects remain unreconciled."
)


def _authenticated_retirement(
    entry: JournalEntry, task: Task, submit_sequence: int
) -> TaskRetirement | None:
    """Return the receipt only for well-formed native retirements matching this task.

    Anything else — wrong origin/kind, cross-epoch causal refs, mismatched
    scope or revision, future-task references, malformed payloads — is
    ignored so it can never silently retire work.
    """
    if entry.kind != "agent.retirement" or entry.origin != "agent-supervisor":
        return None
    if submit_sequence < 1 or entry.sequence <= submit_sequence:
        return None
    if list(entry.causes) != []:
        return None
    try:
        receipt = TaskRetirement.model_validate(entry.payload)
    except ValueError:
        return None
    if receipt.task_id != task.task_id or entry.task_refs != [task.task_id]:
        return None
    if (
        receipt.task_session_id != task.session_id
        or receipt.task_thread_id != task.thread_id
        or receipt.task_epoch != task.epoch
        or receipt.objective_revision != task.objective_revision
    ):
        return None
    if entry.session_id != task.session_id or entry.thread_id != task.thread_id:
        return None
    if entry.epoch < task.epoch:
        return None
    return receipt


@dataclass
class _Record:
    task: Task
    worker: asyncio.Task[None] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    history: list[dict[str, str]] = field(default_factory=list)
    catalog_guards: dict[str, Any] = field(default_factory=dict)
    pending: dict[str, Any] | None = None
    pending_steer: dict[str, Any] | None = None
    last_tool_ok: bool | None = None
    # Aggregate failure of required work or UI publication in this revision.
    # Reads/searches never clear it, so an unrelated read cannot hide a
    # failed effect from finish.
    failed_effect: bool = False
    # Publication cannot recover a failed filesystem/external effect (or the
    # reverse). Keep each failure class until its own successful operation.
    failed_work_effect: bool = False
    failed_artifact: bool = False
    failed_plugin: bool = False
    cancel_requested: bool = False
    closing: bool = False
    proc_slot: dict[str, RunningProc | None] = field(default_factory=dict)
    journal_seq: int = 0
    steer_idem: dict[str, tuple[str, str]] = field(default_factory=dict)
    interrupt_idem: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Restored from durable retirement receipts: owner key hash -> request digest.
    retirement_keys: dict[str, str] = field(default_factory=dict)
    retire_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Set only by recover() for tasks reconstructed from the journal.
    recovered: bool = False
    submit_sequence: int = 0
    source_revisions: dict[str, int] = field(default_factory=dict)
    explicit_skill: str | None = None
    used_skills: set[str] = field(default_factory=set)
    plugin_versions: dict[str, str] = field(default_factory=dict)
    legacy_submit_scope: bool = False
    # Once a final success append may have committed, only its exact native
    # confirmation or restart recovery can settle it. Never append a competing
    # cancellation/failure or accept another revision while it is uncertain.
    completion_pending: bool = False


class AgentRuntime:
    """Functional standalone seed Agent bounded to its initial baseline."""

    def __init__(
        self,
        journal: Journal,
        provider: TextProvider,
        workspace: Path,
        policy: ToolPolicy,
        max_steps: int = 12,
        selector_registry=None,
        mcp_host: Any | None = None,
        skill_service: Any | None = None,
        resource_client: Any | None = None,
        plugin_service: Any | None = None,
        mcp_controller: Any | None = None,
        artifact_store: Any | None = None,
    ) -> None:
        self.journal = journal
        self.provider = provider
        self.workspace = Path(workspace)
        self.policy = policy
        self.max_steps = max(1, int(max_steps))
        self.selector_registry = selector_registry
        # Optional broker for owner-configured MCP servers. None means the
        # mcp_list/mcp_call tools are unknown and honestly unavailable.
        self.mcp_controller = mcp_controller
        if mcp_controller is not None:
            if mcp_host is not None and mcp_host is not mcp_controller.host:
                raise ValueError("Managed MCP must use its controller's host")
            mcp_host = mcp_controller.host
        self.mcp_host = mcp_host
        self.integration_lock = asyncio.Lock()
        self.skill_service = skill_service
        self.resource_client = resource_client
        self.plugin_service = plugin_service
        # Native presentation storage is optional for direct/test runtimes.
        # The actual launcher supplies a protected operator data path.
        self.artifact_store = artifact_store
        self._tasks: dict[str, _Record] = {}
        # Scoped (session_id, idempotency_key) so keys never collide across
        # sessions; restored by recover for terminal and paused tasks alike.
        self._submit_idem: dict[tuple[str, str], tuple[str, str]] = {}
        self._closed = False

    # -- journal ------------------------------------------------------
    def _append_request(self, rec: _Record, kind: str, payload: dict[str, Any]) -> AppendRequest:
        rec.journal_seq += 1
        # Every entry carries the task reference and top-level state so
        # observers (Converse grounding, Decisions routing) can find the
        # task without parsing nested blobs. Outcome entries additionally
        # carry the complete Task snapshot, built by the caller.
        payload = {
            "task_id": rec.task.task_id,
            "state": rec.task.state.value,
            **payload,
            "plugin_versions": dict(rec.plugin_versions),
            "content_scope": combine_scopes(
                rec.task.content_scope, payload.get("content_scope", "public_synthetic")
            ),
        }
        return AppendRequest(
            session_id=rec.task.session_id,
            thread_id=rec.task.thread_id,
            epoch=rec.task.epoch,
            kind=kind,
            payload=payload,
            task_refs=[rec.task.task_id],
            idempotency_key=f"agent:{rec.task.task_id}:{kind}:{rec.journal_seq}",
        )

    async def _append(self, rec: _Record, kind: str, payload: dict[str, Any]) -> None:
        entry = await self.journal.append(self._append_request(rec, kind, payload))
        if kind == "agent.command" and payload.get("op") == "submit":
            rec.submit_sequence = entry.sequence

    def _outcome_payload(self, rec: _Record, state: str, extra: dict[str, Any]) -> dict[str, Any]:
        """Complete task snapshot for a terminal/awaiting outcome entry."""
        return {
            "task_id": rec.task.task_id,
            "session_id": rec.task.session_id,
            "thread_id": rec.task.thread_id,
            "epoch": rec.task.epoch,
            "objective": rec.task.objective,
            "objective_revision": rec.task.objective_revision,
            "idempotency_key": rec.task.idempotency_key,
            "source_event_ids": list(rec.task.source_event_ids),
            "content_scope": rec.task.content_scope,
            "evidence": [e.model_dump(mode="json") for e in rec.task.evidence],
            "task": rec.task.model_dump(mode="json"),
            **extra,
            "state": state,
        }

    # -- actors -------------------------------------------------------
    @staticmethod
    def _check_actor(actor: str, source_event_ids: list[str], op: str) -> None:
        if actor == "owner":
            return
        if actor == "decisions":
            if not source_event_ids:
                raise PolicyDenied(
                    f"decisions-routed {op} must carry authentic user source event IDs"
                )
            return
        raise PolicyDenied(f"{op} requires owner or decisions actor, got {actor!r}")

    async def _require_session(self, session_id: str) -> Any:
        """Fail closed when the session is unknown to the journal."""
        try:
            return await self.journal.session(session_id)
        except Exception as exc:
            raise ValueError(f"unknown session {session_id!r}") from exc

    def _check_task_epoch(self, rec: _Record, session: Any) -> str | None:
        """Return an error when the task's thread/epoch is no longer current."""
        if (rec.task.thread_id, rec.task.epoch) != (session.thread_id, session.epoch):
            return (
                f"stale session epoch: task is thread {rec.task.thread_id} "
                f"epoch {rec.task.epoch}, session is now thread "
                f"{session.thread_id} epoch {session.epoch}; refusing to execute"
            )
        return None

    async def _epoch_error(self, rec: _Record) -> str | None:
        """Re-validate thread/epoch before a side effect or finish."""
        try:
            session = await self.journal.session(rec.task.session_id)
        except Exception as exc:
            return f"session unavailable before execution: {exc}"
        error = self._check_task_epoch(rec, session)
        if error:
            return error
        for event_id, revision in rec.source_revisions.items():
            try:
                source = await self.journal.resolve(event_id)
            except Exception:
                return "Source became unavailable before execution"
            if source.payload.get("revision", 1) != revision:
                return (
                    "Source was revised before execution; fresh authenticated steering is required"
                )
        return None

    async def _validated_source_texts(
        self,
        source_event_ids: list[str],
        *,
        session_id: str,
        thread_id: str,
        epoch: int,
        source_revision: int | None,
        op: str,
    ) -> tuple[list[str], str]:
        """Resolve decisions-carried sources to exact authenticated user text.

        Sources are finalized user messages or retained semantic speech windows,
        bound to this session/thread/epoch and their current revision. Window
        disclosure is checked again at the native execution boundary.
        """
        resolve = getattr(self.journal, "resolve", None)
        if resolve is None:
            raise PolicyDenied(f"decisions-routed {op} cannot be validated: journal has no resolve")
        texts: list[str] = []
        scopes: list[str] = []
        for source_id in source_event_ids:
            try:
                resolved = await resolve(source_id)
            except Exception as exc:
                raise PolicyDenied(
                    f"decisions-routed {op} carries an unresolvable source {source_id!r}"
                ) from exc
            if (
                resolved.session_id != session_id
                or resolved.thread_id != thread_id
                or resolved.epoch != epoch
            ):
                raise PolicyDenied(
                    f"decisions-routed {op} source {source_id!r} is outside "
                    "the current session/thread/epoch"
                )
            if resolved.kind not in {"conversation.user", "conversation.window"}:
                raise PolicyDenied(
                    f"decisions-routed {op} source {source_id!r} is not a user message"
                )
            if resolved.origin != "authenticated-user":
                raise PolicyDenied(
                    f"decisions-routed {op} source {source_id!r} is not authenticated"
                )
            if resolved.kind == "conversation.window":
                try:
                    window = SpeechWindow.model_validate(resolved.payload)
                    latest = await self.journal.latest_window(
                        session_id, epoch, window.utterance_id
                    )
                except Exception as exc:
                    raise PolicyDenied("Transcript-window source cannot be verified") from exc
                if latest is None or latest.payload.get("text") != window.text:
                    raise PolicyDenied("Transcript-window instruction was superseded")
                control = await self.journal.window_control(session_id, epoch, agent_interrupt=True)
                if control is not None and control.payload["source_sequence"] > resolved.sequence:
                    raise PolicyDenied(
                        "Later authenticated interruption supersedes this instruction"
                    )
            elif resolved.payload.get("final") is not True:
                raise PolicyDenied(f"decisions-routed {op} source {source_id!r} is not finalized")
            scopes.append(await self._source_scope(source_id, resolved))
            current_revision = resolved.payload.get("revision", 1)
            if source_revision is not None and source_revision != current_revision:
                raise PolicyDenied(
                    f"decisions-routed {op} source {source_id!r} is at revision "
                    f"{current_revision}, request carries {source_revision}"
                )
            text = resolved.payload.get("text", "")
            if not isinstance(text, str) or not text.strip():
                raise PolicyDenied(f"decisions-routed {op} source {source_id!r} has no usable text")
            texts.append(text)
        scope = combine_scopes(*scopes) if scopes else "public_synthetic"
        if not scope_allowed(self.provider.info, scope):
            raise PolicyDenied("Source disclosure does not permit the configured Agent provider")
        return texts, scope

    async def _source_scope(self, source_id, resolved):
        return await source_scope(self.journal, source_id, resolved)

    async def _task_scope(self, rec, *, persist=True, require_open=False):
        """Recheck source floors and the actual target provider at each continuation."""
        scopes = [rec.task.content_scope]
        for source_id in rec.task.source_event_ids:
            try:
                source = await self.journal.resolve(source_id)
                if (source.session_id, source.thread_id, source.epoch) != (
                    rec.task.session_id,
                    rec.task.thread_id,
                    rec.task.epoch,
                ):
                    raise ValueError("Wrong source scope")
                if source.kind not in {"conversation.user", "conversation.window"} or (
                    source.origin != "authenticated-user"
                ):
                    raise ValueError("Not an authenticated user source")
                scopes.append(await self._source_scope(source_id, source))
            except Exception as exc:
                # Missing provenance must never become a new export grant.
                if require_open:
                    self._check_completion_open(rec, require_active=True)
                rec.task.content_scope = "local_only"
                raise PolicyDenied("Task source disclosure cannot be verified") from exc
        if require_open:
            self._check_completion_open(rec, require_active=True)
        scope = combine_scopes(rec.task.content_scope, *scopes)
        if scope != rec.task.content_scope:
            rec.task.content_scope = scope
            if persist:
                await self._append(rec, "agent.command", {"op": "disclosure-narrowed"})
        # A steer or profile change can land while native reads above await.
        rec.task.content_scope = combine_scopes(rec.task.content_scope, scope)
        if not scope_allowed(self.provider.info, rec.task.content_scope):
            raise PolicyDenied("Task disclosure does not permit the configured Agent provider")

    # -- interface: submit --------------------------------------------
    async def submit(
        self, request: TaskRequest, actor: str = "owner", *, explicit_skill: str | None = None
    ) -> Task:
        # Management checks and task acceptance share one boundary. A task
        # cannot appear between the idle check and a server definition swap.
        async with self.integration_lock:
            return await self._submit(request, actor, explicit_skill=explicit_skill)

    def session_harness(self, pinned: str | None) -> str:
        """Native registry policy, never caller/model assertions, grants a pin."""
        registry = self.selector_registry
        if registry is None:
            if pinned not in {None, "seed-v1"}:
                raise ValueError("Pinned harness is unavailable")
            return "seed-v1"
        version = registry.active if pinned is None else pinned
        if registry.selection_eligibility(version) not in {
            "active",
            "approved_baseline",
            "owner_promoted",
        }:
            raise ValueError("Pinned harness has no current owner-approved eligibility")
        return version

    async def _submit(
        self, request: TaskRequest, actor: str = "owner", *, explicit_skill: str | None = None
    ) -> Task:
        self._check_actor(actor, list(request.source_event_ids), "submit")
        if explicit_skill is not None:
            if actor != "owner" or self.skill_service is None:
                raise PolicyDenied("Explicit skill invocation requires owner and configured skills")
            if not isinstance(explicit_skill, str) or not 1 <= len(explicit_skill) <= 256:
                raise ValueError("Invalid explicit skill id")
        session = await self._require_session(request.session_id)
        if (request.thread_id, request.epoch) != (session.thread_id, session.epoch):
            raise StaleRevision(
                f"submit targets thread {request.thread_id} epoch {request.epoch}; "
                f"session is now thread {session.thread_id} epoch {session.epoch}"
            )
        objective = request.objective
        source_scope = "public_synthetic"
        if request.source_event_ids:
            texts, source_scope = await self._validated_source_texts(
                list(request.source_event_ids),
                session_id=request.session_id,
                thread_id=request.thread_id,
                epoch=request.epoch,
                source_revision=request.source_revision,
                op="submit",
            )
            expected = "\n".join(texts)
            if actor == "decisions" and objective != expected:
                raise PolicyDenied(
                    "decisions-routed submit objective does not match "
                    "the resolved authenticated source text"
                )
            if actor == "decisions":
                objective = expected
        content_scope = combine_scopes(request.content_scope, source_scope)
        if not scope_allowed(self.provider.info, content_scope):
            raise PolicyDenied("Task disclosure does not permit the configured Agent provider")
        payload = {
            "session_id": request.session_id,
            "thread_id": request.thread_id,
            "epoch": request.epoch,
            "objective": objective,
            "source_event_ids": list(request.source_event_ids),
            "source_revision": request.source_revision,
            "content_scope": request.content_scope,
        }
        if explicit_skill is not None:
            payload["explicit_skill"] = explicit_skill
        digest = _payload_hash(payload)
        idem_key = (request.session_id, request.idempotency_key)
        if idem_key in self._submit_idem:
            old_digest, task_id = self._submit_idem[idem_key]
            if old_digest != digest:
                old_payload = {
                    key: value for key, value in payload.items() if key != "content_scope"
                }
                if not (
                    self._get(task_id).legacy_submit_scope
                    and request.content_scope == "local_only"
                    and old_digest == _payload_hash(old_payload)
                ):
                    raise IdemConflict("idempotency key reuses a conflicting submit payload")
            return self._copy(task_id)
        try:
            harness_version = self.session_harness(session.harness_version)
        except (HarnessError, OSError, ValueError, RuntimeError) as exc:
            raise PolicyDenied("The session's pinned harness is unavailable or unapproved") from exc
        task = Task(
            session_id=request.session_id,
            thread_id=request.thread_id,
            epoch=request.epoch,
            objective=objective,
            content_scope=content_scope,
            objective_revision=request.source_revision,
            state=TaskState.ACCEPTED,
            source_event_ids=list(request.source_event_ids),
            idempotency_key=request.idempotency_key,
            harness_version=harness_version,
        )
        rec = _Record(task=task, explicit_skill=explicit_skill)
        if request.source_event_ids:
            rec.source_revisions = dict.fromkeys(request.source_event_ids, request.source_revision)
        self._tasks[task.task_id] = rec
        self._submit_idem[idem_key] = (digest, task.task_id)
        await self._append(
            rec,
            "agent.command",
            {
                "op": "submit",
                "actor": actor,
                "session_id": request.session_id,
                "thread_id": request.thread_id,
                "epoch": request.epoch,
                "objective": objective,
                "objective_revision": task.objective_revision,
                "source_event_ids": list(request.source_event_ids),
                "source_revision": request.source_revision,
                "requested_content_scope": request.content_scope,
                "idempotency_key": request.idempotency_key,
                "payload_digest": digest,
                "harness_version": task.harness_version,
                "explicit_skill": explicit_skill,
            },
        )
        rec.worker = asyncio.create_task(self._run(rec))
        return task.model_copy(deep=True)

    # -- interface: steer ---------------------------------------------
    async def steer(self, task_id: str, request: SteerRequest, actor: str = "owner") -> Task:
        rec = self._get(task_id)
        self._check_actor(actor, list(request.source_event_ids), "steer")
        if request.idempotency_key not in rec.steer_idem:
            self._check_completion_open(rec, require_active=True)
        instruction = request.instruction
        source_scope = "public_synthetic"
        if request.source_event_ids:
            texts, source_scope = await self._validated_source_texts(
                list(request.source_event_ids),
                session_id=rec.task.session_id,
                thread_id=rec.task.thread_id,
                epoch=rec.task.epoch,
                source_revision=None,
                op="steer",
            )
            expected = "\n".join(texts)
            if actor == "decisions" and instruction != expected:
                raise PolicyDenied(
                    "decisions-routed steer instruction does not match "
                    "the resolved authenticated source text"
                )
            if actor == "decisions":
                instruction = expected
        if request.idempotency_key not in rec.steer_idem:
            self._check_completion_open(rec, require_active=True)
        payload = {
            "instruction": instruction,
            "expected_objective_revision": request.expected_objective_revision,
            "source_event_ids": list(request.source_event_ids),
            "content_scope": request.content_scope,
        }
        digest = _payload_hash(payload)
        if request.idempotency_key in rec.steer_idem:
            old_digest, _ = rec.steer_idem[request.idempotency_key]
            if old_digest != digest:
                raise IdemConflict("idempotency key reuses a conflicting steer payload")
            return rec.task.model_copy(deep=True)
        if rec.task.state in TERMINAL:
            raise TaskTerminal(f"task {task_id} is already {rec.task.state}")
        if rec.recovered and rec.worker is None:
            raise ValueError("Recovered work requires a new task; retire the paused task first")
        if rec.pending_steer is not None:
            raise StaleRevision(
                "Another steering revision is pending; wait for its applied acknowledgment"
            )
        if request.expected_objective_revision != rec.task.objective_revision:
            raise StaleRevision(
                f"stale revision {request.expected_objective_revision}; "
                f"current is {rec.task.objective_revision}"
            )
        stale = self._check_task_epoch(rec, await self._require_session(rec.task.session_id))
        if stale is not None:
            raise StaleRevision(stale)
        await self._task_scope(rec, require_open=True)
        source_revisions = {
            source_id: (await self.journal.resolve(source_id)).payload.get("revision", 1)
            for source_id in request.source_event_ids
        }
        # Source/session reads may have overlapped the final completion fence.
        # No record mutation or command append may follow a stale admission.
        self._check_completion_open(rec, require_active=True)
        if rec.pending_steer is not None:
            raise StaleRevision(
                "Another steering revision is pending; wait for its applied acknowledgment"
            )
        if request.expected_objective_revision != rec.task.objective_revision:
            raise StaleRevision("Task revision changed during steering disclosure validation")
        content_scope = combine_scopes(rec.task.content_scope, request.content_scope, source_scope)
        if not scope_allowed(self.provider.info, content_scope):
            raise PolicyDenied("Steering disclosure does not permit the configured Agent provider")
        rec.task.content_scope = content_scope
        rec.steer_idem[request.idempotency_key] = (digest, task_id)
        rec.pending_steer = {
            "instruction": instruction,
            "actor": actor,
            "content_scope": content_scope,
            "source_event_ids": list(request.source_event_ids),
            "source_revisions": source_revisions,
        }
        # Wake an approval wait so steering applies promptly at the boundary.
        rec.resume.set()
        await self._append(
            rec,
            "agent.command",
            {
                "op": "steer-requested",
                "actor": actor,
                "instruction": request.instruction,
                "expected_objective_revision": request.expected_objective_revision,
            },
        )
        return rec.task.model_copy(deep=True)

    async def _apply_steer(self, rec: _Record) -> None:
        pending = rec.pending_steer
        if not pending:
            return
        rec.pending_steer = None
        if pending["source_event_ids"]:
            try:
                texts, source_scope = await self._validated_source_texts(
                    pending["source_event_ids"],
                    session_id=rec.task.session_id,
                    thread_id=rec.task.thread_id,
                    epoch=rec.task.epoch,
                    source_revision=None,
                    op="steer",
                )
                revision_changed = False
                for source_id, revision in pending.get("source_revisions", {}).items():
                    resolved = await self.journal.resolve(source_id)
                    revision_changed |= resolved.payload.get("revision", 1) != revision
                if pending["actor"] == "decisions" and (
                    "\n".join(texts) != pending["instruction"] or revision_changed
                ):
                    raise PolicyDenied("Steering source changed before its execution boundary")
                rec.task.content_scope = combine_scopes(rec.task.content_scope, source_scope)
            except (PolicyDenied, ValueError, KeyError):
                await self._append(
                    rec,
                    "agent.command",
                    {
                        "op": "steer-rejected",
                        "actor": pending["actor"],
                        "reason": "source_changed_before_boundary",
                    },
                )
                return
        rec.task.content_scope = combine_scopes(rec.task.content_scope, pending["content_scope"])
        await self._task_scope(rec)
        rec.source_revisions.update(pending.get("source_revisions", {}))
        invalidated = [
            e.evidence_id
            for e in rec.task.evidence
            if e.objective_revision == rec.task.objective_revision
        ]
        new_revision = rec.task.objective_revision + 1
        # Keep the original goal and append the authenticated revision
        # constraint; never silently replace the whole task with an
        # isolated fragment. Any objective change invalidates prior evidence.
        rec.task.objective = (
            f"{rec.task.objective}\n\n[Revision {new_revision} constraint "
            f"from {pending['actor']}]: {pending['instruction']}"
        )
        for source_id in pending["source_event_ids"]:
            if source_id not in rec.task.source_event_ids:
                rec.task.source_event_ids.append(source_id)
        rec.task.objective_revision = new_revision
        rec.last_tool_ok = None
        rec.failed_effect = False
        rec.failed_work_effect = False
        rec.failed_artifact = False
        rec.failed_plugin = False
        rec.history.append(
            {
                "role": "user",
                "content": (
                    f"Steered to revision {rec.task.objective_revision}: "
                    f"{pending['instruction']} Prior evidence is invalid; "
                    "re-verify under the new revision before finish."
                ),
            }
        )
        # A steered revision supersedes any narrow approval bound to the old one.
        if rec.pending is not None:
            await self._append(
                rec,
                "approval.resolved",
                {
                    "approval_id": rec.pending["approval_id"],
                    "approved": False,
                    "reason": "superseded by steer",
                    "objective_revision": rec.task.objective_revision,
                },
            )
            rec.pending = None
        rec.task.state = TaskState.RUNNING
        await self._append(
            rec,
            "agent.command",
            {
                "op": "steer-applied",
                "objective_revision": rec.task.objective_revision,
                "invalidated_evidence": invalidated,
            },
        )

    # -- interface: interrupt -----------------------------------------
    async def interrupt(
        self, task_id: str, request: InterruptRequest, actor: str = "owner"
    ) -> Task:
        if self._closed:
            raise ValueError("Agent is closing")
        rec = self._get(task_id)
        self._check_completion_open(rec)
        if rec.recovered and rec.task.state == TaskState.PAUSED and rec.worker is None:
            if actor != "owner":
                raise PolicyDenied(f"retiring recovered work requires owner, got {actor!r}")
            if rec.proc_slot.get("proc") is not None:
                raise ValueError("cannot retire recovered work with a residual process handle")
            return await self._retire_recovered(rec, request)
        if actor not in ("owner", "decisions"):
            raise PolicyDenied(f"interrupt requires owner or decisions actor, got {actor!r}")
        if actor == "decisions":
            if not request.source_event_ids:
                raise PolicyDenied(
                    "decisions-routed interrupt must carry authentic user source event IDs"
                )
            await self._validated_source_texts(
                list(request.source_event_ids),
                session_id=rec.task.session_id,
                thread_id=rec.task.thread_id,
                epoch=rec.task.epoch,
                source_revision=request.source_revision,
                op="interrupt",
            )
        self._check_completion_open(rec)
        payload = {
            "expected_objective_revision": request.expected_objective_revision,
            "reason": request.reason,
            "source_event_ids": list(request.source_event_ids),
        }
        digest = _payload_hash(payload)
        if request.idempotency_key in rec.interrupt_idem:
            old_digest, _ = rec.interrupt_idem[request.idempotency_key]
            if old_digest != digest:
                raise IdemConflict("idempotency key reuses a conflicting interrupt payload")
            return rec.task.model_copy(deep=True)
        if rec.task.state in TERMINAL:
            key_sha = hashlib.sha256(request.idempotency_key.encode()).hexdigest()
            stored = rec.retirement_keys.get(key_sha)
            if stored is not None and stored != digest:
                raise IdemConflict("idempotency key reuses a conflicting interrupt payload")
            return rec.task.model_copy(deep=True)
        if request.expected_objective_revision != rec.task.objective_revision:
            raise StaleRevision(
                f"stale revision {request.expected_objective_revision}; "
                f"current is {rec.task.objective_revision}"
            )
        rec.interrupt_idem[request.idempotency_key] = (digest, task_id)
        already_requested = rec.cancel_requested
        recovered_paused = rec.task.state == TaskState.PAUSED and rec.worker is None
        rec.cancel_requested = True
        rec.task.state = TaskState.CANCEL_REQUESTED
        # Never wait for the model: signal, kill the child group, return.
        proc = rec.proc_slot.get("proc")
        if proc is not None:
            await proc.kill()
        worker = rec.worker
        if worker is not None and not worker.done() and not already_requested:
            worker.cancel()
        rec.resume.set()
        await self._append(
            rec,
            "agent.command",
            {
                "op": "interrupt",
                "actor": actor,
                "reason": request.reason,
                "content_scope": "local_only",
            },
        )
        if recovered_paused:
            rec.task.summary = "Cancelled recovered work without replay; prior uncertain effects remain unreconciled."
            await self._cancel(rec)
            if self.plugin_service is not None:
                await self.plugin_service.release_task(task_id)
        return rec.task.model_copy(deep=True)

    # -- administrative retirement (verified recovered paused work only) --
    @staticmethod
    def _stable_retirement_idem(task_id: str, key_sha: str) -> str:
        # Stable hashed identity: concurrent or retried stops for the same
        # owner key append the identical request, so the store dedupes
        # instead of duplicating receipts. Never the incrementing counter.
        return f"agent:{task_id}:retirement:{key_sha}"

    async def _committed_retirement(
        self, rec: _Record, key_sha: str, digest: str
    ) -> TaskRetirement | None:
        """Return a durably committed receipt matching this task, if any.

        Same owner key with a different payload stays a conflict; any other
        valid receipt for this scope settles the already-retired work.
        """
        matched: TaskRetirement | None = None
        after = 0
        while True:
            batch = await self.journal.read(
                after=after,
                limit=200,
                session_id=rec.task.session_id,
                task_id=rec.task.task_id,
                kinds=["agent.retirement"],
            )
            if not batch:
                break
            after = batch[-1].sequence
            for entry in batch:
                receipt = _authenticated_retirement(entry, rec.task, rec.submit_sequence)
                if receipt is not None:
                    if receipt.request_key_sha256 == key_sha and receipt.request_digest != digest:
                        raise IdemConflict("idempotency key reuses a conflicting interrupt payload")
                    if matched is None or receipt.request_key_sha256 == key_sha:
                        matched = receipt
        return matched

    async def _cleanup_retirement(self, rec: _Record) -> None:
        if rec.proc_slot.get("proc") is not None or rec.worker is not None:
            raise ValueError("cannot retire work with an unconfirmed worker or process handle")
        if self.plugin_service is not None:
            await self.plugin_service.release_task(rec.task.task_id)
        if self.skill_service is not None:
            await self.skill_service.release_task(rec.task.task_id)

    def _settle_retirement(
        self,
        rec: _Record,
        receipt: TaskRetirement,
        raw_key: str | None = None,
        raw_digest: str | None = None,
    ) -> None:
        """Publish terminal in-memory state only after verified cleanup."""
        rec.task.state = TaskState.CANCELLED
        rec.task.error = "cancelled by owner"
        rec.task.summary = RETIRED_SUMMARY
        rec.cancel_requested = True
        rec.retirement_keys[receipt.request_key_sha256] = receipt.request_digest
        if (
            raw_key is not None
            and raw_digest == receipt.request_digest
            and hashlib.sha256(raw_key.encode()).hexdigest() == receipt.request_key_sha256
        ):
            rec.interrupt_idem[raw_key] = (raw_digest, rec.task.task_id)
        rec.done.set()

    async def _retire_recovered(self, rec: _Record, request: InterruptRequest) -> Task:
        """Owner-only stop for verified recovered paused work.

        Publishes a minimal agent.retirement receipt in the current session
        epoch and only then settles terminal state. Never replays
        inference/tools and never emits an execution outcome for the old
        task. A failed append leaves retryable paused state with no
        poisoned owner idempotency mapping.
        """
        if request.expected_objective_revision != rec.task.objective_revision:
            raise StaleRevision(
                f"stale revision {request.expected_objective_revision}; "
                f"current is {rec.task.objective_revision}"
            )
        payload = {
            "expected_objective_revision": request.expected_objective_revision,
            "reason": request.reason,
            "source_event_ids": list(request.source_event_ids),
        }
        digest = _payload_hash(payload)
        key_sha = hashlib.sha256(request.idempotency_key.encode()).hexdigest()
        async with rec.retire_lock:
            if rec.task.state in TERMINAL:
                stored = rec.retirement_keys.get(key_sha)
                if stored is not None and stored != digest:
                    raise IdemConflict("idempotency key reuses a conflicting interrupt payload")
                return rec.task.model_copy(deep=True)
            if self._closed or rec.closing:
                raise ValueError("Agent is closing")
            if not rec.recovered or rec.task.state != TaskState.PAUSED or rec.submit_sequence < 1:
                raise ValueError("Retirement requires verified recovered paused work")
            if request.idempotency_key in rec.interrupt_idem:
                old_digest, _ = rec.interrupt_idem[request.idempotency_key]
                if old_digest != digest:
                    raise IdemConflict("idempotency key reuses a conflicting interrupt payload")
            committed = await self._committed_retirement(rec, key_sha, digest)
            if committed is not None:
                await self._cleanup_retirement(rec)
                self._settle_retirement(rec, committed, request.idempotency_key, digest)
                return rec.task.model_copy(deep=True)
            session = await self._require_session(rec.task.session_id)
            if session.thread_id != rec.task.thread_id:
                raise ValueError(
                    f"retiring recovered work requires the original thread {rec.task.thread_id!r}"
                )
            if session.epoch < rec.task.epoch:
                raise ValueError("retiring recovered work requires the current session epoch")
            receipt = TaskRetirement(
                task_id=rec.task.task_id,
                task_session_id=rec.task.session_id,
                task_thread_id=rec.task.thread_id,
                task_epoch=rec.task.epoch,
                objective_revision=rec.task.objective_revision,
                request_key_sha256=key_sha,
                request_digest=digest,
            )
            await self._cleanup_retirement(rec)
            await self.journal.append(
                AppendRequest(
                    session_id=rec.task.session_id,
                    thread_id=rec.task.thread_id,
                    epoch=session.epoch,
                    kind="agent.retirement",
                    payload=receipt.model_dump(mode="json"),
                    task_refs=[rec.task.task_id],
                    idempotency_key=self._stable_retirement_idem(rec.task.task_id, key_sha),
                )
            )
            self._settle_retirement(rec, receipt, request.idempotency_key, digest)
            return rec.task.model_copy(deep=True)

    # -- interface: status / wait / close ------------------------------
    # Async like every other AgentAdapter method, so the same adapter
    # serves Decisions and HTTP clients without a sync/async split.
    async def status(self, task_id: str | None = None) -> Task | list[Task]:
        if task_id is None:
            return [rec.task.model_copy(deep=True) for rec in self._tasks.values()]
        return self._copy(task_id)

    async def wait(self, task_id: str, timeout: float = 30) -> Task:
        rec = self._get(task_id)
        try:
            await asyncio.wait_for(rec.done.wait(), timeout=timeout)
        except TimeoutError:
            pass
        return rec.task.model_copy(deep=True)

    async def close(self) -> None:
        self._closed = True
        workers: list[tuple[_Record, asyncio.Task[None]]] = []
        for rec in self._tasks.values():
            if rec.task.state in TERMINAL:
                # Completion evidence precedes the resource-release callback.
                # Keep its client alive until bounded terminal cleanup finishes.
                if rec.worker is not None and not rec.worker.done():
                    workers.append((rec, rec.worker))
                continue
            rec.closing = True
            rec.cancel_requested = True
            proc = rec.proc_slot.get("proc")
            if proc is not None:
                await proc.kill()
            worker, rec.worker = rec.worker, None
            if worker is not None and not worker.done():
                worker.cancel()
                workers.append((rec, worker))
            rec.resume.set()
        for _rec, worker in workers:
            try:
                await asyncio.wait_for(worker, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                pass
        for rec in self._tasks.values():
            if rec.task.state not in TERMINAL:
                rec.task.state = TaskState.PAUSED
                rec.done.set()
        host, self.mcp_host = self.mcp_host, None
        controller, self.mcp_controller = self.mcp_controller, None
        if controller is not None:
            await controller.close()
        elif host is not None:
            await host.close()
        service, self.skill_service = self.skill_service, None
        if service is not None:
            service.close()
        plugins, self.plugin_service = self.plugin_service, None
        if plugins is not None:
            await plugins.close()
        client, self.resource_client = self.resource_client, None
        if client is not None:
            await client.close()
        artifact_store, self.artifact_store = self.artifact_store, None
        if artifact_store is not None:
            artifact_store.close()

    # -- approvals ------------------------------------------------------
    async def pending_approvals(
        self, *, after: str | None = None, limit: int = 100
    ) -> PendingApprovalPage:
        """Current native requests across task sessions, not a bounded history scan."""
        if not 1 <= limit <= 100:
            raise ValueError("Approval page limit must be 1..100")
        found = []
        records = sorted(list(self._tasks.values()), key=lambda r: r.task.task_id)
        for rec in records:
            if after is not None and rec.task.task_id <= after:
                continue
            pending = rec.pending
            if (
                rec.task.state != TaskState.AWAITING_APPROVAL
                or pending is None
                or pending.get("approved")
                or rec.pending_steer is not None
            ):
                continue
            session_id = rec.task.session_id
            if await self._epoch_error(rec) is not None:
                continue
            permit = self.policy.peek_approval(pending["approval_id"])
            if (
                rec.pending is not pending
                or rec.task.state != TaskState.AWAITING_APPROVAL
                or pending.get("approved")
                or rec.pending_steer is not None
                or permit is None
                or permit.used
                or permit.expires_at <= time.time()
                or permit.objective_revision != rec.task.objective_revision
            ):
                continue
            found.append(
                PendingApproval(
                    approval_id=permit.approval_id,
                    task_id=rec.task.task_id,
                    session_id=session_id,
                    thread_id=rec.task.thread_id,
                    epoch=rec.task.epoch,
                    tool=pending["tool"],
                    arguments=pending["arguments"],
                    action_digest=permit.action_digest,
                    objective_revision=permit.objective_revision,
                    expires_at=permit.expires_at,
                    reason=pending.get("reason", "tool_permission"),
                )
            )
            if len(found) > limit:
                return PendingApprovalPage(
                    approvals=found[:limit], next_after=found[limit - 1].task_id
                )
        return PendingApprovalPage(approvals=found)

    async def resolve_approval(
        self, approval_id: str, resolution: ApprovalResolution, actor: str = "owner"
    ) -> dict[str, Any]:
        if actor != "owner":
            raise PolicyDenied(f"approval resolve requires owner, got {actor!r}")
        for rec in self._tasks.values():
            pending = rec.pending
            if pending is not None and pending["approval_id"] == approval_id:
                break
        else:
            raise ApprovalError(f"unknown or superseded approval {approval_id!r}")
        if resolution.objective_revision != rec.task.objective_revision:
            raise StaleRevision("approval resolution targets a stale objective revision")
        if resolution.action_digest != pending["digest"]:
            raise ApprovalError("approval resolution digest does not match the pending action")
        if rec.task.state in TERMINAL:
            raise TaskTerminal("task already terminal; approval is moot")
        stale = await self._epoch_error(rec)
        if stale is not None:
            raise StaleRevision(stale)
        if resolution.approved:
            await self._task_scope(rec)
        await self._append(
            rec,
            "approval.resolved",
            {
                "approval_id": approval_id,
                "approved": resolution.approved,
                "objective_revision": resolution.objective_revision,
            },
        )
        if not resolution.approved:
            rec.pending = None
            await self._fail(rec, "owner denied the requested approval")
            return {"approval_id": approval_id, "approved": False}
        # Record intent; the one-use spend happens at execution under the
        # current revision, so a racing steer still invalidates it.
        pending["approved"] = True
        rec.resume.set()
        return {"approval_id": approval_id, "approved": True}

    def approval_for_task(self, task_id: str) -> str | None:
        rec = self._get(task_id)
        return rec.pending["approval_id"] if rec.pending else None

    # -- recovery ---------------------------------------------------------
    async def recover(self) -> list[Task]:
        async with self.integration_lock:
            return await self._recover()

    async def _recover(self) -> list[Task]:
        """Rebuild task state from the journal without re-executing anything.

        Reads the journal in bounded pages (<=200 entries, the journal
        maximum). Terminal tasks are restored from their complete outcome
        snapshots so Converse can ground on them, and submit idempotency is
        restored under scoped (session, idempotency) keys. Uncertain
        non-terminal work resumes as paused: the owner must explicitly
        resubmit. No worker is ever recreated, so an acknowledged write is
        never replayed after a restart.
        """
        seen: dict[str, dict[str, Any]] = {}
        submit_sequences: dict[str, int] = {}
        outcomes: dict[str, dict[str, Any]] = {}
        retirements: dict[str, list[JournalEntry]] = {}
        plugin_pins: dict[str, dict[str, str]] = {}
        journal_numbers: dict[str, int] = {}
        task_scopes: dict[str, str] = {}
        after = 0
        while True:
            batch = await self.journal.read(after=after, limit=200)
            if not batch:
                break
            after = batch[-1].sequence
            for entry in batch:
                refs = entry.task_refs or []
                if entry.origin == "agent-supervisor":
                    for ref in refs:
                        task_scopes[ref] = combine_scopes(
                            task_scopes.get(ref, "public_synthetic"),
                            entry.payload.get("content_scope", "local_only"),
                        )
                        prefix = f"agent:{ref}:{entry.kind}:"
                        key = entry.idempotency_key
                        if key.startswith(prefix):
                            suffix = key[len(prefix) :]
                            if suffix.isdecimal() and len(suffix) <= 19:
                                journal_numbers[ref] = max(journal_numbers.get(ref, 0), int(suffix))
                if entry.origin == "agent-supervisor" and isinstance(
                    entry.payload.get("plugin_versions"), dict
                ):
                    for ref in refs:
                        plugin_pins[ref] = dict(entry.payload["plugin_versions"])
                if entry.kind == "agent.command" and entry.payload.get("op") == "submit":
                    for ref in refs:
                        if ref not in seen and (
                            entry.origin == "agent-supervisor"
                            and refs == [ref]
                            and entry.payload.get("task_id") == ref
                            and entry.payload.get("session_id") == entry.session_id
                            and entry.payload.get("thread_id") == entry.thread_id
                            and entry.payload.get("epoch") == entry.epoch
                        ):
                            submit_sequences[ref] = entry.sequence
                        seen.setdefault(ref, entry.payload)
                elif entry.kind == "agent.outcome":
                    for ref in refs:
                        outcomes[ref] = entry.payload
                elif entry.kind == "agent.retirement":
                    for ref in refs:
                        retirements.setdefault(ref, []).append(entry)
        recovered: list[Task] = []
        for task_id, payload in seen.items():
            if task_id in self._tasks:
                continue
            digest = payload.get("payload_digest")
            if digest is not None and payload.get("session_id") and payload.get("idempotency_key"):
                self._submit_idem[(payload["session_id"], payload["idempotency_key"])] = (
                    digest,
                    task_id,
                )
            if task_id in outcomes:
                snapshot = outcomes[task_id].get("task")
                if snapshot is None:
                    continue  # outcome without a snapshot cannot be grounded
                task = Task.model_validate(
                    {
                        **snapshot,
                        "content_scope": combine_scopes(
                            snapshot.get("content_scope", "local_only"),
                            task_scopes.get(task_id, "local_only"),
                        ),
                    }
                )
                if task.state not in TERMINAL:
                    task.state = TaskState.PAUSED
                    task.summary = (
                        "recovered paused; outcome snapshot was non-terminal; "
                        "uncertain writes not retried; resubmit to continue"
                    )
            else:
                task = Task(
                    task_id=task_id,
                    session_id=payload.get("session_id", ""),
                    thread_id=payload.get("thread_id", ""),
                    epoch=payload.get("epoch", 1),
                    objective=payload.get("objective", ""),
                    content_scope=combine_scopes(
                        payload.get("content_scope", "local_only"),
                        task_scopes.get(task_id, "local_only"),
                    ),
                    objective_revision=payload.get("objective_revision", 1),
                    state=TaskState.PAUSED,
                    summary=(
                        "recovered paused; uncertain writes not retried; resubmit to continue"
                    ),
                    source_event_ids=list(payload.get("source_event_ids", [])),
                    idempotency_key=payload.get("idempotency_key", task_id),
                    harness_version=payload.get("harness_version", "seed-v1"),
                )
            rec = _Record(
                task=task,
                recovered=True,
                submit_sequence=submit_sequences.get(task_id, 0),
                legacy_submit_scope="requested_content_scope" not in payload,
            )
            if task.state not in TERMINAL:
                # A valid native receipt retires the work without replay;
                # malformed or mismatched receipts never settle anything, and
                # a native terminal execution outcome always wins.
                for entry in retirements.get(task_id, []):
                    receipt = _authenticated_retirement(entry, task, rec.submit_sequence)
                    if receipt is None:
                        continue
                    rec.retirement_keys[receipt.request_key_sha256] = receipt.request_digest
                if rec.retirement_keys:
                    task.state = TaskState.CANCELLED
                    task.error = "cancelled by owner"
                    task.summary = RETIRED_SUMMARY
            rec.journal_seq = journal_numbers.get(task_id, 0)
            try:
                await self._task_scope(rec, persist=False)
            except PolicyDenied:
                if task.state not in TERMINAL:
                    task.error = "Recovered task disclosure does not permit this Agent provider"
            pins = plugin_pins.get(task_id, {})
            if len(pins) > 16 or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in pins.items()
            ):
                raise ValueError("Recovered plugin pins are invalid")
            rec.plugin_versions = pins
            rec.done.set()
            self._tasks[task_id] = rec
            recovered.append(task.model_copy(deep=True))
        if self.plugin_service is not None:
            await self.plugin_service.recover_pins(
                {
                    task_id: rec.plugin_versions
                    for task_id, rec in self._tasks.items()
                    if rec.task.state not in TERMINAL
                }
            )
        return recovered

    # -- internals --------------------------------------------------------
    def _get(self, task_id: str) -> _Record:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise TaskNotFound(f"unknown task {task_id!r}") from None

    def _copy(self, task_id: str) -> Task:
        return self._get(task_id).task.model_copy(deep=True)

    @staticmethod
    def _check_completion_open(rec: _Record, *, require_active: bool = False) -> None:
        if rec.completion_pending:
            raise TaskTerminal(
                "Task completion confirmation is in progress or unconfirmed; "
                "no new task action accepted. Recover journal state before retrying."
            )
        if require_active and rec.task.state in TERMINAL:
            raise TaskTerminal(f"task {rec.task.task_id} is already {rec.task.state}")

    @staticmethod
    def _pause_completion(rec: _Record) -> None:
        rec.task.state = TaskState.PAUSED
        rec.task.summary = ""
        rec.task.error = (
            "Task completion journaling is unconfirmed; work stopped without retry. "
            "Recover journal state before starting further work."
        )
        rec.done.set()

    def plugin_dependents(self, version_id):
        return [
            rec.task.task_id
            for rec in self._tasks.values()
            if rec.task.state not in TERMINAL and version_id in rec.plugin_versions.values()
        ]

    def plugin_pins(self, task_id):
        return dict(self._get(task_id).plugin_versions)

    @staticmethod
    def _plugin_task(rec):
        return SimpleNamespace(task_id=rec.task.task_id, plugin_versions=rec.plugin_versions)

    def _grants(self) -> str:
        return (
            f"write={self.policy.effective_write} "
            f"shell={self.policy.effective_shell} "
            f"network={self.policy.network}"
        )

    def tool_snapshot(self, task: Task | None = None) -> NativeToolSnapshot:
        """Native gateways, independently of action approval/target readiness."""
        harness = (
            task.harness_version
            if task is not None
            else self.selector_registry.active
            if self.selector_registry is not None
            else "seed-v1"
        )
        return native_tool_snapshot(
            selector_available=(
                self.selector_registry is not None
                and isinstance(harness, str)
                and bool(harness)
                and harness != "seed-v1"
            ),
            mcp_available=self.mcp_host is not None,
            skills_available=self.skill_service is not None,
            plugins_available=self.plugin_service is not None,
            artifacts_available=self.artifact_store is not None,
            catalog_available=True,
        )

    async def inspect_tools(self, arguments: dict, *, task_id: str | None = None) -> dict:
        """Authenticated API projection; never advance a task or create pins."""
        from services.agent.tool_catalog import inspect_catalog

        rec = self._get(task_id) if task_id is not None else None
        return await inspect_catalog(self, arguments, rec=rec)

    @staticmethod
    def _catalog_export_current(rec: _Record, guards) -> bool:
        if all(guard.disclosure_current() for guard in guards):
            return True
        # Outcomes or later tools can contain metadata derived from the old
        # catalog. Preserve a sticky local floor when its disclosure is lost.
        rec.task.content_scope = "local_only"
        return False

    @staticmethod
    def _validate_catalog_history(rec: _Record) -> None:
        # Inspection can expose an unpinned package. Pin validation alone is
        # insufficient when an owner later withdraws its disclosure scope.
        if not AgentRuntime._catalog_export_current(rec, rec.catalog_guards.values()):
            # Later actions can quote earlier metadata. Deleting only the
            # original unit cannot remove those derived data dependencies.
            raise PolicyDenied("Tool catalog disclosure changed; task stopped")
        for turn in rec.history:
            if turn.get("context_kind") != "tool_catalog":
                continue
            key = hashlib.sha256(turn["content"].encode("utf-8")).hexdigest()
            guard = rec.catalog_guards.get(key)
            if guard is None:
                rec.task.content_scope = "local_only"
                raise PolicyDenied("Tool catalog history provenance is unavailable")

    async def _reject_unavailable_tool(self, rec: _Record, tool: str) -> None:
        rec.last_tool_ok = False
        self._effect_result(rec, tool, ok=False)
        if tool == "plugin_call":
            rec.failed_plugin = True
        rec.history.append(
            {
                "role": "user",
                "content": observation_text(False, f"Tool {tool!r} is unavailable for this task"),
            }
        )
        await self._append(
            rec,
            "agent.tool",
            {"tool": tool, "ok": False, "reason": "not_advertised", "effect_executed": False},
        )

    @staticmethod
    def _effect_result(rec: _Record, tool: str, *, ok: bool) -> None:
        if tool == "publish_ui":
            rec.failed_artifact = not ok
        elif tool in {"write_file", "shell", "mcp_call"}:
            rec.failed_work_effect = not ok
        rec.failed_effect = rec.failed_work_effect or rec.failed_artifact

    def _mcp_server_ids(self) -> list[str] | None:
        """Enabled owner-configured server ids for the prompt, or None.

        Only ids, never secrets, endpoints, or schemas. An unavailable
        host reports no servers rather than failing the whole turn.
        """
        host = self._model_mcp()
        if host is None:
            return None
        try:
            servers = host.list_servers()
        except Exception:
            return []
        return [
            str(entry.get("id", ""))
            for entry in servers
            if isinstance(entry, dict) and entry.get("enabled")
        ]

    def _model_mcp(self):
        return (
            ModelScopedMCP(self.mcp_host, self.provider.info) if self.mcp_host is not None else None
        )

    async def _fail(
        self, rec: _Record, error: str, *, decode_error: ActionDecodeError | None = None
    ) -> None:
        if rec.completion_pending:
            self._pause_completion(rec)
            return
        if rec.task.state in TERMINAL:
            return
        rec.task.state = TaskState.FAILED
        rec.task.error = error
        extra: dict[str, Any] = {"error": error}
        if decode_error is not None:
            # Copy only a freshly validated constant pair. Parser exceptions
            # can retain raw documents, keys and positions; none enter the
            # native outcome or become additional work/completion evidence.
            try:
                extra["provider_diagnostic"] = ActionDecodeError(
                    decode_error.stage, decode_error.reason
                ).diagnostic
            except Exception:
                pass  # Optional diagnostics cannot prevent the ordinary failure outcome.
        try:
            # A reset/deletion intentionally rejects old-epoch publication. Local
            # waiters must still learn that execution stopped without a stale write.
            await self._append(rec, "agent.outcome", self._outcome_payload(rec, "failed", extra))
        finally:
            rec.done.set()

    async def _succeed(self, rec: _Record, summary: str) -> None:
        rec.task.state = TaskState.VERIFYING
        # No await separates the final authority check in _finish from this
        # fence and frozen snapshot. The visible task is not successful yet.
        snapshot = rec.task.model_copy(
            deep=True, update={"state": TaskState.SUCCEEDED, "summary": summary, "error": None}
        )
        body = self._append_request(
            rec,
            "agent.outcome",
            self._outcome_payload(
                rec, "succeeded", {"summary": summary, "task": snapshot.model_dump(mode="json")}
            ),
        )
        rec.completion_pending = True
        try:
            confirmed = await append_completion(self.journal, body, after=rec.submit_sequence)
        except (Exception, asyncio.CancelledError):
            # The POST may have committed. A stop/shutdown or unavailable
            # read-back cannot authorize another, contradictory outcome.
            self._pause_completion(rec)
            return
        if not confirmed:
            self._pause_completion(rec)
            return
        rec.task = snapshot
        rec.completion_pending = False
        rec.done.set()

    async def _cancel(self, rec: _Record) -> None:
        if rec.completion_pending:
            self._pause_completion(rec)
            return
        if rec.task.state in TERMINAL:
            return
        rec.task.state = TaskState.CANCELLED
        rec.task.error = "cancelled by owner"
        await self._append(rec, "agent.outcome", self._outcome_payload(rec, "cancelled", {}))
        rec.done.set()

    async def _run(self, rec: _Record) -> None:
        if rec.task.state == TaskState.ACCEPTED:
            rec.task.state = TaskState.RUNNING
        try:
            if rec.explicit_skill:
                await self._dispatch(
                    rec, "skill_load", {"skill_id": rec.explicit_skill}, "", "", approval_id=None
                )
                if rec.last_tool_ok is not True:
                    await self._fail(rec, "Selected skill is unavailable under the current policy")
                    return
            for _ in range(self.max_steps):
                if rec.cancel_requested or rec.closing:
                    await self._cancel(rec)
                    return
                await self._apply_steer(rec)
                # Revocation applies before the next model request, including
                # content already retained in this task's bounded history.
                if self.skill_service is not None:
                    await self.skill_service.validate_task(
                        rec.task.task_id, self.provider.info, user_skill_id=rec.explicit_skill
                    )
                if self.plugin_service is not None:
                    await self.plugin_service.validate_task(
                        self._plugin_task(rec), self.provider.info
                    )
                await self._task_scope(rec)
                self._validate_catalog_history(rec)
                revision_before = rec.task.objective_revision
                tools = self.tool_snapshot(rec.task)
                messages = build_messages(
                    rec.task.objective,
                    rec.task.objective_revision,
                    list(rec.task.source_event_ids),
                    rec.history,
                    self._grants(),
                    self._mcp_server_ids(),
                    self.skill_service is not None,
                    self.plugin_service is not None,
                    self.artifact_store is not None,
                    tool_snapshot=tools,
                )
                export_guards = tuple(rec.catalog_guards.values())
                try:
                    with agent_tool_scope(
                        tools,
                        export_guard=lambda guards=export_guards: self._catalog_export_current(
                            rec, guards
                        ),
                    ):
                        text = await self.provider.complete(messages)
                except asyncio.CancelledError:
                    raise
                except ActionDecodeError as exc:
                    await self._fail(rec, f"provider error: {exc}", decode_error=exc)
                    return
                except Exception as exc:  # provider failure is terminal, honest
                    await self._fail(rec, f"provider error: {exc}")
                    return
                if rec.cancel_requested or rec.closing:
                    await self._cancel(rec)
                    return
                await self._task_scope(rec)
                self._validate_catalog_history(rec)
                # A steer that landed while inference was in flight applies
                # at this boundary, and the response inferred under the old
                # revision is discarded, never executed under the new one.
                if rec.pending_steer is not None or rec.task.objective_revision != revision_before:
                    await self._apply_steer(rec)
                    rec.history.append({"role": "assistant", "content": text[:2000]})
                    rec.history.append(
                        {
                            "role": "user",
                            "content": observation_text(
                                False,
                                "in-flight response discarded: the objective was "
                                "revised during inference; re-plan under the "
                                "current revision",
                            ),
                        }
                    )
                    continue
                try:
                    action = parse_action(text)
                except ValueError as exc:
                    rec.history.append({"role": "assistant", "content": text[:2000]})
                    rec.history.append(
                        {"role": "user", "content": observation_text(False, str(exc))}
                    )
                    continue
                if not tools.supports(action.tool):
                    await self._reject_unavailable_tool(rec, action.tool)
                    continue
                if action.tool == "finish":
                    if await self._finish(rec, action.arguments, text):
                        return
                    continue
                await self._execute(rec, action.tool, dict(action.arguments), text)
                if rec.task.state in TERMINAL:
                    return
            await self._fail(rec, f"step budget exhausted after {self.max_steps} steps")
        except asyncio.CancelledError:
            if rec.completion_pending:
                self._pause_completion(rec)
            elif rec.closing:
                if rec.task.state not in TERMINAL:
                    rec.task.state = TaskState.PAUSED
                rec.done.set()
            else:
                await self._cancel(rec)
        except Exception as exc:  # never leave waiters hanging
            if rec.completion_pending:
                self._pause_completion(rec)
            elif rec.closing:
                if rec.task.state not in TERMINAL:
                    rec.task.state = TaskState.PAUSED
                rec.done.set()
            else:
                await self._fail(rec, f"runtime error: {exc}")
        finally:
            if self.skill_service is not None:
                await self.skill_service.release_task(rec.task.task_id)
            if self.plugin_service is not None:
                await self.plugin_service.release_task(rec.task.task_id)
            if self.resource_client is not None and rec.task.state in TERMINAL and not rec.closing:
                try:
                    await self.resource_client.task_finished(rec.task.task_id)
                except Exception:
                    # Native task evidence remains authoritative. Restoration
                    # is a separate unconfirmed resource transition.
                    print(
                        json.dumps(
                            {
                                "service": "agent",
                                "resource_restore": "unconfirmed",
                                "task_id": rec.task.task_id,
                            }
                        ),
                        flush=True,
                    )

    async def _verify_finish_files(self, rec: _Record) -> str | None:
        """Re-read every file written in the current revision before accepting finish.

        A prior write followed by deletion or modification cannot claim a
        verified success. Only the latest evidence per path is checked, so
        an overwritten file is verified against its final content.
        """
        latest: dict[str, Any] = {}
        for e in rec.task.evidence:
            if e.objective_revision == rec.task.objective_revision and e.kind == "file" and e.path:
                latest[e.path] = e
        for path, evidence in latest.items():
            candidate = Path(path)
            try:
                if not candidate.is_file():
                    return f"finish verification failed: {path} is missing after the write"
                digest = hashlib.sha256(self.policy.read_bytes(candidate)).hexdigest()
            except (OSError, PolicyDenied) as exc:
                return f"finish verification failed: cannot re-read {path}: {exc}"
            if digest != evidence.sha256:
                return (
                    f"finish verification failed: {path} changed after the write "
                    f"(expected sha256 {evidence.sha256}, found {digest})"
                )
        return None

    def _verify_finish_artifacts(self, rec: _Record) -> str | None:
        """Verify each final publication receipt against protected storage."""
        latest: dict[str, Evidence] = {}
        for evidence in rec.task.evidence:
            if (
                evidence.objective_revision == rec.task.objective_revision
                and evidence.kind == "artifact"
            ):
                if not evidence.path or not evidence.path.startswith("artifact:"):
                    return "finish verification failed: invalid UI publication receipt"
                latest[evidence.path] = evidence
        for evidence in latest.values():
            try:
                if self.artifact_store is None or not self.artifact_store.verify(
                    evidence, rec.task
                ):
                    return (
                        "finish verification failed: UI publication no longer matches its receipt"
                    )
            except Exception:
                return "finish verification failed: UI artifact storage is unavailable"
        return None

    async def _finish(self, rec: _Record, args: dict[str, Any], raw: str) -> bool:
        await self._task_scope(rec)
        _ = raw
        if rec.cancel_requested:
            await self._cancel(rec)
            return True
        if rec.pending_steer is not None:
            await self._apply_steer(rec)
            return False
        stale = await self._epoch_error(rec)
        if rec.cancel_requested or rec.closing:
            await self._cancel(rec)
            return True
        if rec.pending_steer is not None:
            await self._apply_steer(rec)
            return False
        if stale is not None:
            await self._fail(rec, stale)
            return True
        revision = args.get("objective_revision")
        if revision != rec.task.objective_revision:
            await self._fail(
                rec,
                f"stale finish revision {revision!r}; current is {rec.task.objective_revision}",
            )
            return True
        current = [
            e
            for e in rec.task.evidence
            if e.objective_revision == rec.task.objective_revision and e.kind != "inspection"
        ]
        if not current:
            await self._fail(rec, "finish with no current-revision evidence")
            return True
        unverified_artifact = self._verify_finish_artifacts(rec)
        if unverified_artifact is not None:
            await self._fail(rec, unverified_artifact)
            return True
        if any(e.kind in {"external", "extension"} for e in current) and not any(
            e.kind in {"file", "command", "test", "artifact"}
            or (e.kind == "read" and e.path and e.sha256)
            for e in current
        ):
            await self._fail(
                rec,
                "finish with only unverified external content receipts; "
                "independent native evidence is required to report success",
            )
            return True
        if rec.failed_plugin:
            await self._fail(rec, "finish while a required plugin computation failed")
            return True
        if rec.failed_effect:
            await self._fail(
                rec,
                "finish while a required effect failed in this revision; "
                "a later unrelated read cannot hide it",
            )
            return True
        if rec.last_tool_ok is not True:
            await self._fail(rec, "finish while a required tool has failed; not reporting success")
            return True
        unverified = await self._verify_finish_files(rec)
        # A steer/cancel can land during source and epoch verification. It
        # wins if admitted before the final success append was reserved.
        if rec.cancel_requested or rec.closing:
            await self._cancel(rec)
            return True
        if rec.pending_steer is not None:
            await self._apply_steer(rec)
            return False
        if unverified is not None:
            await self._fail(rec, unverified)
            return True
        self._validate_catalog_history(rec)
        summary = str(args.get("summary", "") or "").strip()
        await self._succeed(rec, summary or "done")
        return True

    async def _uncommitted_speech_sources(self, rec: _Record) -> list[str]:
        """A tool grant is not authority to act on unfinished recognition (AT11)."""
        uncommitted = []
        for source_id in rec.task.source_event_ids:
            try:
                source = await self.journal.resolve(source_id)
                if source.kind != "conversation.window":
                    continue
                window = SpeechWindow.model_validate(source.payload)
                if source.origin != "authenticated-user" or (
                    source.session_id,
                    source.thread_id,
                    source.epoch,
                ) != (rec.task.session_id, rec.task.thread_id, rec.task.epoch):
                    raise ValueError("Wrong speech source scope")
                expected_revision = rec.source_revisions.get(source_id)
                if expected_revision is not None and window.revision != expected_revision:
                    raise ValueError("Speech source was revised during authority validation")
            except Exception as exc:
                raise PolicyDenied("Speech source authority cannot be verified") from exc
            if not window.capture_ended:
                uncommitted.append(source_id)
        return uncommitted

    async def _execute(self, rec: _Record, tool: str, args: dict[str, Any], raw: str) -> None:
        await self._task_scope(rec)
        self._validate_catalog_history(rec)
        revision = rec.task.objective_revision
        if rec.cancel_requested:
            await self._cancel(rec)
            return
        stale = await self._epoch_error(rec)
        if stale is not None:
            await self._fail(rec, stale)
            return
        if tool == "write_file":
            try:
                validate_write_arguments(args, revision)
            except ToolFailure as exc:
                rec.last_tool_ok = False
                self._effect_result(rec, tool, ok=False)
                if raw:
                    rec.history.append({"role": "assistant", "content": raw[:2000]})
                rec.history.append({"role": "user", "content": observation_text(False, str(exc))})
                return
        digest = self.policy.digest_for(tool, args, revision)
        if tool == "mcp_call" and self.mcp_host is not None:
            self._model_mcp()._check(args.get("server_id", ""))
        sources = (
            await self._uncommitted_speech_sources(rec)
            if tool in {"write_file", "shell", "mcp_call", "publish_ui"}
            else []
        )
        if await self._discard_revised_action(rec, revision, raw):
            return
        self._validate_catalog_history(rec)
        if sources:
            await self._await_approval(rec, tool, args, digest, raw, reason="uncommitted_speech")
            return
        if tool in ("write_file", "shell") and self.policy.needs_approval(tool):
            await self._await_approval(rec, tool, args, digest, raw)
            return
        if tool == "mcp_call" and self.mcp_host is not None and not self.policy.yolo:
            # Every mcp_call needs the existing digest/revision/expiry/one-use
            # owner approval. YOLO skips this per-action approval only; the
            # host still enforces enabled state, the exact allowlist, and
            # the shell/network transport grants on every dispatch.
            await self._await_approval(rec, tool, args, digest, raw)
            return
        await self._dispatch(rec, tool, args, digest, raw, approval_id=None)

    async def _discard_revised_action(self, rec: _Record, revision: int, raw: str) -> bool:
        """Journal validation can yield while an authenticated steer is accepted."""
        if rec.cancel_requested or rec.closing:
            await self._cancel(rec)
            return True
        if rec.pending_steer is None and rec.task.objective_revision == revision:
            return False
        await self._apply_steer(rec)
        rec.history.append({"role": "assistant", "content": raw[:2000]})
        rec.history.append(
            {
                "role": "user",
                "content": observation_text(
                    False,
                    "action discarded: the objective changed during source validation; re-plan",
                ),
            }
        )
        return True

    async def _await_approval(
        self,
        rec: _Record,
        tool: str,
        args: dict[str, Any],
        digest: str,
        raw: str,
        *,
        reason: str = "tool_permission",
    ) -> None:
        approval = self.policy.create_approval(digest, rec.task.objective_revision)
        rec.pending = {
            "approval_id": approval.approval_id,
            "digest": digest,
            "tool": tool,
            "arguments": args,
            "approved": False,
            "reason": reason,
        }
        # Clear before publishing: an owner can resolve the durable request while
        # the append HTTP response is still in flight. Never erase that signal.
        rec.resume.clear()
        rec.task.state = TaskState.AWAITING_APPROVAL
        await self._append(
            rec,
            "approval.requested",
            {
                "approval_id": approval.approval_id,
                "tool": tool,
                "arguments": args,
                "action_digest": digest,
                "objective_revision": rec.task.objective_revision,
                "expires_at": approval.expires_at,
                "reason": reason,
            },
        )
        while True:
            await rec.resume.wait()
            rec.resume.clear()
            if rec.cancel_requested or rec.closing:
                await self._cancel(rec)
                return
            # Steering at the boundary supersedes the narrow approval.
            if rec.pending_steer is not None:
                rec.pending = None
                rec.history.append({"role": "assistant", "content": raw[:2000]})
                rec.history.append(
                    {
                        "role": "user",
                        "content": observation_text(
                            False, "action superseded by steer; re-plan under the new revision"
                        ),
                    }
                )
                await self._apply_steer(rec)
                return
            pending = rec.pending
            if pending is None:  # denied path already failed the task
                return
            if not pending.get("approved"):
                continue
            if rec.task.objective_revision != approval.objective_revision:
                rec.pending = None
                rec.history.append({"role": "assistant", "content": raw[:2000]})
                rec.history.append(
                    {
                        "role": "user",
                        "content": observation_text(False, "approval went stale; re-plan"),
                    }
                )
                rec.task.state = TaskState.RUNNING
                return
            rec.task.state = TaskState.RUNNING
            await self._dispatch(rec, tool, args, digest, raw, approval_id=approval.approval_id)
            return

    async def _dispatch(
        self,
        rec: _Record,
        tool: str,
        args: dict[str, Any],
        digest: str,
        raw: str,
        *,
        approval_id: str | None,
    ) -> None:
        await self._task_scope(rec)
        revision = rec.task.objective_revision
        stale = await self._epoch_error(rec)
        if stale is not None:
            await self._fail(rec, stale)
            return
        sources = (
            await self._uncommitted_speech_sources(rec)
            if tool in {"write_file", "shell", "mcp_call", "publish_ui"}
            else []
        )
        if tool == "publish_ui":
            # Speech authority may have yielded after the earlier epoch/source
            # check. Re-read pinned revisions, then session identity after all
            # of those source awaits. The local discard below is the final
            # cancellation/steering fence before synchronous publication.
            stale = await self._epoch_error(rec)
            if stale is None:
                try:
                    session = await self.journal.session(rec.task.session_id)
                    stale = self._check_task_epoch(rec, session)
                except Exception:
                    stale = "Session authority unavailable before UI publication"
        if await self._discard_revised_action(rec, revision, raw):
            return
        if tool == "publish_ui" and stale is not None:
            await self._fail(rec, stale)
            return
        self._validate_catalog_history(rec)
        if tool == "write_file":
            try:
                validate_write_arguments(args, revision)
            except ToolFailure as exc:
                rec.last_tool_ok = False
                self._effect_result(rec, tool, ok=False)
                if raw:
                    rec.history.append({"role": "assistant", "content": raw[:2000]})
                rec.history.append({"role": "user", "content": observation_text(False, str(exc))})
                return
        if sources:
            pending = rec.pending
            if (
                approval_id is None
                or pending is None
                or not pending.get("approved")
                or (pending["approval_id"], pending["digest"], pending["tool"])
                != (approval_id, digest, tool)
            ):
                raise ApprovalError("Uncommitted speech requires owner approval for this action")
            # Broad file/shell grants skip the broker's approval spend;
            # spend here in that case. Otherwise the broker consumes this
            # same approval before effects. MCP always consumes a supplied
            # approval, including under YOLO. No derived permit is needed.
            # There is no await between this grant check and the broker's
            # matching synchronous permission check.
            if tool == "publish_ui" or (
                tool in {"write_file", "shell"} and not self.policy.needs_approval(tool)
            ):
                self.policy.consume_approval(approval_id, digest, revision)
        if raw and tool not in {"publish_ui", "tool_catalog"}:
            rec.history.append({"role": "assistant", "content": raw[:2000]})
        if approval_id is not None:
            # One-use intent is now committed to this exact execution.
            rec.pending = None
        try:
            if not self.tool_snapshot(rec.task).supports(tool):
                raise ToolFailure(f"Tool {tool!r} is unavailable for this task")
            if tool == "read_transcript":
                out, evidence = await self._read_transcript(rec, args)
            elif tool == "select_context":
                from services.agent.context import select_context

                out, evidence = await select_context(self, rec.task, args)
            elif tool == "read_file":
                out, evidence = run_read(
                    self.policy,
                    str(args.get("path", "")),
                    int(args.get("max_bytes", 65536)),
                    revision,
                )
            elif tool == "write_file":
                out, evidence = run_write(
                    self.policy,
                    args["path"],
                    args["content"],
                    revision,
                    approval_id=approval_id,
                    digest=digest,
                )
            elif tool == "search":
                out, evidence = run_search(
                    self.policy, str(args.get("pattern", "")), args.get("path"), revision
                )
            elif tool == "shell":
                out, evidence = await run_shell(
                    self.policy,
                    str(args.get("command", "")),
                    float(args.get("timeout_s", 60)),
                    revision,
                    approval_id=approval_id,
                    digest=digest,
                    proc_slot=rec.proc_slot,
                )
            elif tool == "mcp_list":
                out, evidence = await run_mcp_list(
                    self._model_mcp(),
                    args.get("server_id"),
                    revision,
                    after=args.get("after", 0),
                    tool=args.get("tool"),
                )
            elif tool == "mcp_call":
                out, evidence = await run_mcp_call(
                    self._model_mcp(),
                    self.policy,
                    args.get("server_id", ""),
                    args.get("tool", ""),
                    args.get("arguments", {}),
                    revision,
                    approval_id=approval_id,
                    digest=digest,
                )
            elif tool in {"skills_list", "skill_load"}:
                out, evidence = await self._skill_result(rec, tool, args)
            elif tool in {"plugins_list", "plugin_call"}:
                out, evidence = await self._plugin_result(rec, tool, args)
            elif tool == "publish_ui":
                out, evidence = await self._publish_ui(rec, args)
            elif tool == "tool_catalog":
                from packages.contracts.tool_catalog import CatalogError, encode_catalog
                from services.agent.tool_catalog import inspect_catalog

                guards = []
                try:
                    result = await inspect_catalog(self, args, rec=rec, guard_sink=guards)
                    out = encode_catalog(result)
                except CatalogError as exc:
                    raise ToolFailure("Tool catalog failed: " + exc.code) from exc
                # Metadata awaits may yield to a steer, interrupt or reset.
                # Recheck native authority before authoring an inspection receipt.
                stale = await self._epoch_error(rec)
                if await self._discard_revised_action(rec, revision, raw):
                    return
                if stale is not None:
                    await self._fail(rec, stale)
                    return
                if rec.task.state != TaskState.RUNNING or rec.recovered or self._closed:
                    raise ToolFailure("Tool catalog requires a current active task revision")
                catalog_guard = guards[0]
                if not catalog_guard.current():
                    raise ToolFailure("Tool catalog failed: invalid_metadata")
                evidence = Evidence(
                    kind="inspection",
                    summary="Tool catalog inspection only; no execution or permission proven",
                    sha256=hashlib.sha256(out.encode("ascii")).hexdigest(),
                    # Durable task outcomes can later be read by other roles.
                    # Keep the inspected result's digest, not foreign metadata
                    # whose disclosure may subsequently be withdrawn.
                    output=json.dumps({"inspection_only": True, "metadata_omitted": True}),
                    objective_revision=revision,
                )
            else:
                raise ToolFailure(f"unknown tool {tool!r}")
        except _PublicationUncertain as exc:
            rec.last_tool_ok = False
            self._effect_result(rec, tool, ok=False)
            if exc.evidence is not None:
                rec.task.evidence.append(exc.evidence)
            # Storage or journal confirmation may have failed after commit.
            # End this task instead of asking the model to create a duplicate.
            await self._fail(rec, str(exc))
            return
        except _FailedWithEvidence as exc:
            # Honest failure that still produced supervisor evidence
            # (e.g. a nonzero shell exit with its exit code recorded).
            rec.task.evidence.append(exc.evidence)
            await self._append(
                rec,
                "agent.tool",
                {"tool": tool, "ok": False, "evidence": exc.evidence.model_dump(mode="json")},
            )
            rec.last_tool_ok = False
            self._effect_result(rec, tool, ok=False)
            if tool == "plugin_call":
                rec.failed_plugin = True
            rec.history.append({"role": "user", "content": observation_text(False, str(exc))})
            return
        except (ToolFailure, PolicyDenied, ApprovalError, OSError) as exc:
            rec.last_tool_ok = False
            self._effect_result(rec, tool, ok=False)
            if tool == "plugin_call":
                rec.failed_plugin = True
            rec.history.append({"role": "user", "content": observation_text(False, str(exc))})
            return
        rec.task.evidence.append(evidence)
        try:
            await self._append(
                rec,
                "agent.tool",
                {"tool": tool, "ok": True, "evidence": evidence.model_dump(mode="json")},
            )
        except Exception:
            if tool != "publish_ui":
                raise
            rec.last_tool_ok = False
            self._effect_result(rec, tool, ok=False)
            await self._fail(
                rec,
                "UI publication was stored but its tool receipt could not be journaled; "
                "task stopped without retry",
            )
            return
        rec.last_tool_ok = True
        self._effect_result(rec, tool, ok=True)
        if tool == "plugin_call":
            rec.failed_plugin = False
        if tool == "tool_catalog":
            # Journal confirmation can also yield to disclosure changes. The
            # durable receipt remains honest inspection evidence; withdrawn
            # metadata must not become a later model-history unit.
            if catalog_guard.current():
                catalog_guard.commit()
                key = hashlib.sha256(out.encode("ascii")).hexdigest()
                rec.catalog_guards[key] = catalog_guard
                rec.history.append({"role": "user", "content": out, "context_kind": "tool_catalog"})
        elif tool == "publish_ui":
            # The admitted action may contain 48 KiB of UI data. Its verified
            # native receipt is the history unit; clipping/replaying the raw
            # assistant action would corrupt JSON or needlessly repeat data.
            rec.history.append({"role": "user", "content": out, "context_kind": "artifact_receipt"})
        elif tool in {"skills_list", "skill_load", "plugins_list", "plugin_call"}:
            context_kind = "plugin" if tool in {"plugins_list", "plugin_call"} else "skill"
            rec.history.append(
                {
                    "role": "user",
                    "content": f"[ok; untrusted {context_kind} content] " + out,
                    "context_kind": context_kind,
                }
            )
        else:
            rec.history.append({"role": "user", "content": observation_text(True, out)})

    async def _publish_ui(self, rec: _Record, args: dict[str, Any]) -> tuple[str, Evidence]:
        if self.artifact_store is None:
            raise ToolFailure("unknown tool 'publish_ui': UI artifacts are not configured")
        # All asynchronous authority reads completed before this point. No
        # await separates this final task check from the native transaction.
        if (
            self._closed
            or rec.recovered
            or rec.cancel_requested
            or rec.closing
            or rec.pending_steer is not None
            or rec.task.state != TaskState.RUNNING
        ):
            raise ToolFailure("UI publication requires the current active task revision")
        try:
            envelope = self.artifact_store.publish(rec.task, args)
        except ArtifactConflict as exc:
            raise ToolFailure("UI publication conflicts with its task or content revision") from exc
        except ArtifactInvalid as exc:
            raise ToolFailure("UI publication arguments failed bounded schema validation") from exc
        except ArtifactNotFound as exc:
            raise ToolFailure("UI publication target was not found") from exc
        except Exception as exc:
            raise _PublicationUncertain(
                "UI publication storage outcome is unconfirmed; task stopped without retry"
            ) from exc
        try:
            artifact_id = envelope["artifact_id"]
            content_revision = envelope["revision"]
            evidence = Evidence(
                kind="artifact",
                summary=f"Published UI artifact {artifact_id} revision {content_revision}; publication only",
                path=f"artifact:{artifact_id}",
                sha256=envelope["content_sha256"],
                objective_revision=rec.task.objective_revision,
            )
            if not self.artifact_store.verify(evidence, rec.task):
                raise ValueError("Unverified native receipt")
            out = _ui_publication_receipt(envelope)
        except Exception as exc:
            raise _PublicationUncertain(
                "UI publication was stored but its native receipt could not be verified; "
                "task stopped without retry"
            ) from exc
        try:
            await self._append(
                rec,
                "artifact.updated",
                {
                    "artifact_id": artifact_id,
                    "task_submit_sequence": rec.submit_sequence,
                    "session_id": rec.task.session_id,
                    "thread_id": rec.task.thread_id,
                    "epoch": rec.task.epoch,
                    "objective_revision": rec.task.objective_revision,
                    "revision": content_revision,
                    "content_sha256": envelope["content_sha256"],
                    "title": envelope["title"],
                    "summary": envelope["summary"],
                    "content_scope": envelope["content_scope"],
                },
            )
        except Exception as exc:
            raise _PublicationUncertain(
                "UI publication was stored but its update could not be journaled; "
                "task stopped without retry",
                evidence,
            ) from exc
        return out, evidence

    async def _plugin_result(self, rec, tool, args):
        service = self.plugin_service
        if service is None:
            raise ToolFailure("No compute-plugin host configured")
        try:
            if tool == "plugins_list":
                if set(args) - {"plugin_id", "after"}:
                    raise ValueError("plugins_list accepts only plugin_id and after")
                result = await service.catalog(self.provider.info, **args)
                evidence_kind = "external"
            else:
                if set(args) != {"plugin_id", "arguments"}:
                    raise ValueError("plugin_call requires only plugin_id and arguments")
                pinned_task = self._plugin_task(rec)
                version_id, changed = await service.pin_task(
                    pinned_task, args["plugin_id"], self.provider.info
                )
                if changed:
                    await self._append(
                        rec,
                        "plugin.pinned",
                        {"plugin_id": args["plugin_id"], "version_id": version_id},
                    )
                result = await service.call_task(
                    pinned_task, args["plugin_id"], args["arguments"], self.provider.info
                )
                stale = await self._epoch_error(rec)
                if stale is not None or rec.cancel_requested:
                    raise ToolFailure(stale or "Plugin result discarded after cancellation")
                evidence_kind = "extension"
            output = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ToolFailure(str(exc)) from exc
        return output, Evidence(
            kind=evidence_kind,
            summary=f"{tool}: compute-plugin receipt; not external-effect evidence",
            sha256=_payload_hash(result),
            output=output[:4000],
            objective_revision=rec.task.objective_revision,
        )

    async def _skill_result(self, rec, tool, args):
        if self.skill_service is None:
            raise ToolFailure("No skill controller configured")
        try:
            if tool == "skills_list":
                if set(args) - {"after"}:
                    raise ValueError("skills_list accepts only after")
                result = await self.skill_service.catalog(
                    self.provider.info, after=args.get("after", 0)
                )
            else:
                if set(args) - {"skill_id", "resource"}:
                    raise ValueError("skill_load accepts only skill_id and resource")
                skill_id = args.get("skill_id")
                result = await self.skill_service.load(
                    rec.task.task_id,
                    skill_id,
                    self.provider.info,
                    invocation="user" if skill_id == rec.explicit_skill else "model",
                    resource=args.get("resource"),
                )
                rec.used_skills.add(skill_id)
            output = json.dumps(result, ensure_ascii=False, sort_keys=True)
            if len(output) > 11500:
                raise ValueError("Skill result exceeds this model context profile")
        except Exception as exc:
            raise ToolFailure(str(exc)) from exc
        return output, Evidence(
            kind="external",
            summary=(
                f"{tool} {result.get('id', 'catalog')} {result.get('digest', '')}: "
                "untrusted skill content receipt; not task-effect evidence"
            ),
            sha256=_payload_hash(result),
            output=json.dumps(
                {
                    key: value
                    for key, value in result.items()
                    if key not in {"body", "resource_text"}
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            objective_revision=rec.task.objective_revision,
        )

    async def _read_transcript(self, rec, args):
        event_ids = args.get("event_ids", [])
        limit = int(args.get("limit", 8))
        if not isinstance(event_ids, list) or len(event_ids) > 20 or not 1 <= limit <= 20:
            raise ToolFailure("Transcript retrieval is limited to 20 entries per request")
        if event_ids:
            entries = [await self.journal.resolve(event_id) for event_id in event_ids]
        else:
            entries = await self.journal.read(
                after=max(0, int(args.get("after", 0))), session_id=rec.task.session_id, limit=limit
            )
        if any(e.session_id != rec.task.session_id or e.epoch != rec.task.epoch for e in entries):
            raise PolicyDenied("Transcript reference outside the task's current session epoch")
        selected, size = [], 0
        for entry in entries:
            scope = entry.payload.get("content_scope", "local_only")
            if entry.kind == "conversation.user":
                scope = await self._source_scope(entry.event_id, entry)
            elif entry.kind == "conversation.revision":
                target_id = entry.payload.get("target_event_id")
                scope = "local_only"
                if isinstance(target_id, str):
                    target = await self.journal.resolve(target_id)
                    if (
                        target.kind == "conversation.user"
                        and target.origin == "authenticated-user"
                        and (target.session_id, target.thread_id, target.epoch)
                        == (entry.session_id, entry.thread_id, entry.epoch)
                    ):
                        scope = combine_scopes(
                            entry.payload.get("content_scope", "local_only"),
                            await self._source_scope(target_id, target),
                        )
            if not entry_allowed(self.provider.info, entry) or not scope_allowed(
                self.provider.info, scope
            ):
                if event_ids:
                    raise PolicyDenied(
                        "Transcript window disclosure is not authorized for this model"
                    )
                continue
            rec.task.content_scope = combine_scopes(rec.task.content_scope, scope)
            text = entry.payload.get("text", "")[:2000]
            size += len(text)
            if size > 12000:
                break
            selected.append(
                {
                    "event_id": entry.event_id,
                    "kind": entry.kind,
                    "origin": entry.origin,
                    "sequence": entry.sequence,
                    "revision": entry.payload.get("revision", 1),
                    "text": text,
                }
            )
        evidence = Evidence(
            kind="read",
            summary=f"Retrieved {len(selected)} scoped journal entries",
            output=json.dumps({"event_ids": [e["event_id"] for e in selected]}),
            objective_revision=rec.task.objective_revision,
        )
        return json.dumps(selected), evidence
