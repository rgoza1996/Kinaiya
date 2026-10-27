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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.policy import ApprovalError, PolicyDenied, ToolPolicy
from harnesses.seed import build_messages, observation_text, parse_action
from packages.contracts.interfaces import Journal, TextProvider
from packages.contracts.models import (
    AppendRequest,
    ApprovalResolution,
    Evidence,
    InterruptRequest,
    SteerRequest,
    Task,
    TaskRequest,
    TaskState,
)
from services.agent.mcp_tools import ModelScopedMCP, run_mcp_call, run_mcp_list
from services.agent.tools import (
    RunningProc,
    ToolFailure,
    _FailedWithEvidence,
    run_read,
    run_search,
    run_shell,
    run_write,
)


class TaskNotFound(KeyError):
    pass


class StaleRevision(ValueError):
    pass


class IdemConflict(ValueError):
    pass


class TaskTerminal(ValueError):
    pass


TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}


def _payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class _Record:
    task: Task
    worker: asyncio.Task[None] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    history: list[dict[str, str]] = field(default_factory=list)
    pending: dict[str, Any] | None = None
    pending_steer: dict[str, Any] | None = None
    last_tool_ok: bool | None = None
    # Set when a required effect (write/shell) fails in the current
    # revision; cleared by a later successful write/shell or by steering.
    # Reads/searches never clear it, so an unrelated read cannot hide a
    # failed effect from finish.
    failed_effect: bool = False
    cancel_requested: bool = False
    closing: bool = False
    proc_slot: dict[str, RunningProc | None] = field(default_factory=dict)
    journal_seq: int = 0
    steer_idem: dict[str, tuple[str, str]] = field(default_factory=dict)
    interrupt_idem: dict[str, tuple[str, str]] = field(default_factory=dict)
    source_revisions: dict[str, int] = field(default_factory=dict)
    explicit_skill: str | None = None
    used_skills: set[str] = field(default_factory=set)


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
    ) -> None:
        self.journal = journal
        self.provider = provider
        self.workspace = Path(workspace)
        self.policy = policy
        self.max_steps = max(1, int(max_steps))
        self.selector_registry = selector_registry
        # Optional broker for owner-configured MCP servers. None means the
        # mcp_list/mcp_call tools are unknown and honestly unavailable.
        self.mcp_host = mcp_host
        self.skill_service = skill_service
        self.resource_client = resource_client
        self._tasks: dict[str, _Record] = {}
        # Scoped (session_id, idempotency_key) so keys never collide across
        # sessions; restored by recover for terminal and paused tasks alike.
        self._submit_idem: dict[tuple[str, str], tuple[str, str]] = {}
        self._closed = False

    # -- journal ------------------------------------------------------
    async def _append(self, rec: _Record, kind: str, payload: dict[str, Any]) -> None:
        rec.journal_seq += 1
        # Every entry carries the task reference and top-level state so
        # observers (Converse grounding, Decisions routing) can find the
        # task without parsing nested blobs. Outcome entries additionally
        # carry the complete Task snapshot, built by the caller.
        payload = {
            "task_id": rec.task.task_id,
            "state": rec.task.state.value,
            **payload,
        }
        await self.journal.append(
            AppendRequest(
                session_id=rec.task.session_id,
                thread_id=rec.task.thread_id,
                epoch=rec.task.epoch,
                kind=kind,
                payload=payload,
                task_refs=[rec.task.task_id],
                idempotency_key=f"agent:{rec.task.task_id}:{kind}:{rec.journal_seq}",
            )
        )

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
    ) -> list[str]:
        """Resolve decisions-carried sources to exact authenticated user text.

        Every source must resolve through the journal to a finalized,
        authenticated-user ``conversation.user`` entry (or its authenticated
        revision) in this session/thread/epoch and at the current source
        revision. Classifier-invented, cross-session, stale, assistant, or
        unfinalized sources are rejected.
        """
        resolve = getattr(self.journal, "resolve", None)
        if resolve is None:
            raise PolicyDenied(f"decisions-routed {op} cannot be validated: journal has no resolve")
        texts: list[str] = []
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
            if resolved.kind != "conversation.user":
                raise PolicyDenied(
                    f"decisions-routed {op} source {source_id!r} is not a user message"
                )
            if resolved.origin != "authenticated-user":
                raise PolicyDenied(
                    f"decisions-routed {op} source {source_id!r} is not authenticated"
                )
            if resolved.payload.get("final") is not True:
                raise PolicyDenied(f"decisions-routed {op} source {source_id!r} is not finalized")
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
        return texts

    # -- interface: submit --------------------------------------------
    async def submit(
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
        if actor == "decisions":
            texts = await self._validated_source_texts(
                list(request.source_event_ids),
                session_id=request.session_id,
                thread_id=request.thread_id,
                epoch=request.epoch,
                source_revision=request.source_revision,
                op="submit",
            )
            expected = "\n".join(texts)
            if objective != expected:
                raise PolicyDenied(
                    "decisions-routed submit objective does not match "
                    "the resolved authenticated source text"
                )
            objective = expected
        payload = {
            "session_id": request.session_id,
            "thread_id": request.thread_id,
            "epoch": request.epoch,
            "objective": objective,
            "source_event_ids": list(request.source_event_ids),
            "source_revision": request.source_revision,
        }
        if explicit_skill is not None:
            payload["explicit_skill"] = explicit_skill
        digest = _payload_hash(payload)
        idem_key = (request.session_id, request.idempotency_key)
        if idem_key in self._submit_idem:
            old_digest, task_id = self._submit_idem[idem_key]
            if old_digest != digest:
                raise IdemConflict("idempotency key reuses a conflicting submit payload")
            return self._copy(task_id)
        task = Task(
            session_id=request.session_id,
            thread_id=request.thread_id,
            epoch=request.epoch,
            objective=objective,
            objective_revision=request.source_revision,
            state=TaskState.ACCEPTED,
            source_event_ids=list(request.source_event_ids),
            idempotency_key=request.idempotency_key,
            harness_version=self.selector_registry.active if self.selector_registry else "seed-v1",
        )
        rec = _Record(task=task, explicit_skill=explicit_skill)
        if actor == "decisions":
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
        instruction = request.instruction
        if actor == "decisions":
            texts = await self._validated_source_texts(
                list(request.source_event_ids),
                session_id=rec.task.session_id,
                thread_id=rec.task.thread_id,
                epoch=rec.task.epoch,
                source_revision=None,
                op="steer",
            )
            expected = "\n".join(texts)
            if instruction != expected:
                raise PolicyDenied(
                    "decisions-routed steer instruction does not match "
                    "the resolved authenticated source text"
                )
            instruction = expected
        payload = {
            "instruction": instruction,
            "expected_objective_revision": request.expected_objective_revision,
            "source_event_ids": list(request.source_event_ids),
        }
        digest = _payload_hash(payload)
        if request.idempotency_key in rec.steer_idem:
            old_digest, _ = rec.steer_idem[request.idempotency_key]
            if old_digest != digest:
                raise IdemConflict("idempotency key reuses a conflicting steer payload")
            return rec.task.model_copy(deep=True)
        if rec.task.state in TERMINAL:
            raise TaskTerminal(f"task {task_id} is already {rec.task.state}")
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
        rec.steer_idem[request.idempotency_key] = (digest, task_id)
        rec.pending_steer = {
            "instruction": instruction,
            "actor": actor,
            "source_event_ids": list(request.source_event_ids),
            "source_revisions": {
                source_id: (await self.journal.resolve(source_id)).payload.get("revision", 1)
                for source_id in request.source_event_ids
            },
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
        rec = self._get(task_id)
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
            return rec.task.model_copy(deep=True)
        if request.expected_objective_revision != rec.task.objective_revision:
            raise StaleRevision(
                f"stale revision {request.expected_objective_revision}; "
                f"current is {rec.task.objective_revision}"
            )
        rec.interrupt_idem[request.idempotency_key] = (digest, task_id)
        rec.cancel_requested = True
        rec.task.state = TaskState.CANCEL_REQUESTED
        # Never wait for the model: signal, kill the child group, return.
        proc = rec.proc_slot.get("proc")
        if proc is not None:
            await proc.kill()
        worker, rec.worker = rec.worker, None
        if worker is not None and not worker.done():
            worker.cancel()
        rec.resume.set()
        await self._append(
            rec, "agent.command", {"op": "interrupt", "actor": actor, "reason": request.reason}
        )
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
        if host is not None:
            await host.close()
        service, self.skill_service = self.skill_service, None
        if service is not None:
            service.close()
        client, self.resource_client = self.resource_client, None
        if client is not None:
            await client.close()

    # -- approvals ------------------------------------------------------
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
        outcomes: dict[str, dict[str, Any]] = {}
        after = 0
        while True:
            batch = await self.journal.read(after=after, limit=200)
            if not batch:
                break
            after = batch[-1].sequence
            for entry in batch:
                refs = entry.task_refs or []
                if entry.kind == "agent.command" and entry.payload.get("op") == "submit":
                    for ref in refs:
                        seen.setdefault(ref, entry.payload)
                elif entry.kind == "agent.outcome":
                    for ref in refs:
                        outcomes[ref] = entry.payload
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
                task = Task.model_validate(snapshot)
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
                    objective_revision=payload.get("objective_revision", 1),
                    state=TaskState.PAUSED,
                    summary=(
                        "recovered paused; uncertain writes not retried; resubmit to continue"
                    ),
                    source_event_ids=list(payload.get("source_event_ids", [])),
                    idempotency_key=payload.get("idempotency_key", task_id),
                    harness_version=payload.get("harness_version", "seed-v1"),
                )
            rec = _Record(task=task)
            rec.done.set()
            self._tasks[task_id] = rec
            recovered.append(task.model_copy(deep=True))
        return recovered

    # -- internals --------------------------------------------------------
    def _get(self, task_id: str) -> _Record:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise TaskNotFound(f"unknown task {task_id!r}") from None

    def _copy(self, task_id: str) -> Task:
        return self._get(task_id).task.model_copy(deep=True)

    def _grants(self) -> str:
        return (
            f"write={self.policy.effective_write} "
            f"shell={self.policy.effective_shell} "
            f"network={self.policy.network}"
        )

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

    async def _fail(self, rec: _Record, error: str) -> None:
        if rec.task.state in TERMINAL:
            return
        rec.task.state = TaskState.FAILED
        rec.task.error = error
        try:
            # A reset/deletion intentionally rejects old-epoch publication. Local
            # waiters must still learn that execution stopped without a stale write.
            await self._append(
                rec, "agent.outcome", self._outcome_payload(rec, "failed", {"error": error})
            )
        finally:
            rec.done.set()

    async def _succeed(self, rec: _Record, summary: str) -> None:
        rec.task.state = TaskState.VERIFYING
        if rec.cancel_requested:
            rec.task.state = TaskState.CANCELLED
            await self._append(rec, "agent.outcome", self._outcome_payload(rec, "cancelled", {}))
            rec.done.set()
            return
        rec.task.state = TaskState.SUCCEEDED
        rec.task.summary = summary
        await self._append(
            rec,
            "agent.outcome",
            self._outcome_payload(rec, "succeeded", {"summary": summary}),
        )
        rec.done.set()

    async def _cancel(self, rec: _Record) -> None:
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
                revision_before = rec.task.objective_revision
                messages = build_messages(
                    rec.task.objective,
                    rec.task.objective_revision,
                    list(rec.task.source_event_ids),
                    rec.history,
                    self._grants(),
                    self._mcp_server_ids(),
                    self.skill_service is not None,
                )
                try:
                    text = await self.provider.complete(messages)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # provider failure is terminal, honest
                    await self._fail(rec, f"provider error: {exc}")
                    return
                if rec.cancel_requested or rec.closing:
                    await self._cancel(rec)
                    return
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
                if action.tool == "finish":
                    await self._finish(rec, action.arguments, text)
                    return
                await self._execute(rec, action.tool, dict(action.arguments), text)
                if rec.task.state in TERMINAL:
                    return
            await self._fail(rec, f"step budget exhausted after {self.max_steps} steps")
        except asyncio.CancelledError:
            if rec.closing:
                if rec.task.state not in TERMINAL:
                    rec.task.state = TaskState.PAUSED
                rec.done.set()
            else:
                await self._cancel(rec)
        except Exception as exc:  # never leave waiters hanging
            if rec.closing:
                if rec.task.state not in TERMINAL:
                    rec.task.state = TaskState.PAUSED
                rec.done.set()
            else:
                await self._fail(rec, f"runtime error: {exc}")
        finally:
            if self.skill_service is not None:
                await self.skill_service.release_task(rec.task.task_id)
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

    async def _finish(self, rec: _Record, args: dict[str, Any], raw: str) -> None:
        _ = raw
        if rec.cancel_requested:
            await self._cancel(rec)
            return
        stale = await self._epoch_error(rec)
        if stale is not None:
            await self._fail(rec, stale)
            return
        revision = args.get("objective_revision")
        if revision != rec.task.objective_revision:
            await self._fail(
                rec,
                f"stale finish revision {revision!r}; current is {rec.task.objective_revision}",
            )
            return
        current = [
            e for e in rec.task.evidence if e.objective_revision == rec.task.objective_revision
        ]
        if not current:
            await self._fail(rec, "finish with no current-revision evidence")
            return
        if any(e.kind == "external" for e in current) and not any(
            e.kind in {"file", "command", "test"} or (e.kind == "read" and e.path and e.sha256)
            for e in current
        ):
            await self._fail(
                rec,
                "finish with only unverified external content receipts; "
                "independent native evidence is required to report success",
            )
            return
        if rec.failed_effect:
            await self._fail(
                rec,
                "finish while a required effect failed in this revision; "
                "a later unrelated read cannot hide it",
            )
            return
        if rec.last_tool_ok is not True:
            await self._fail(rec, "finish while a required tool has failed; not reporting success")
            return
        unverified = await self._verify_finish_files(rec)
        if unverified is not None:
            await self._fail(rec, unverified)
            return
        summary = str(args.get("summary", "") or "").strip()
        await self._succeed(rec, summary or "done")

    async def _execute(self, rec: _Record, tool: str, args: dict[str, Any], raw: str) -> None:
        if rec.cancel_requested:
            await self._cancel(rec)
            return
        stale = await self._epoch_error(rec)
        if stale is not None:
            await self._fail(rec, stale)
            return
        revision = rec.task.objective_revision
        digest = self.policy.digest_for(tool, args, revision)
        if tool == "mcp_call" and self.mcp_host is not None:
            self._model_mcp()._check(args.get("server_id", ""))
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

    async def _await_approval(
        self, rec: _Record, tool: str, args: dict[str, Any], digest: str, raw: str
    ) -> None:
        approval = self.policy.create_approval(digest, rec.task.objective_revision)
        rec.pending = {
            "approval_id": approval.approval_id,
            "digest": digest,
            "tool": tool,
            "arguments": args,
            "approved": False,
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
        revision = rec.task.objective_revision
        stale = await self._epoch_error(rec)
        if stale is not None:
            await self._fail(rec, stale)
            return
        if raw:
            rec.history.append({"role": "assistant", "content": raw[:2000]})
        if approval_id is not None:
            # One-use intent is now committed to this exact execution.
            rec.pending = None
        try:
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
                    str(args.get("path", "")),
                    args.get("content", ""),
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
            else:
                raise ToolFailure(f"unknown tool {tool!r}")
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
            if tool in ("write_file", "shell", "mcp_call"):
                rec.failed_effect = True
            rec.history.append({"role": "user", "content": observation_text(False, str(exc))})
            return
        except (ToolFailure, PolicyDenied, ApprovalError, OSError) as exc:
            rec.last_tool_ok = False
            if tool in ("write_file", "shell", "mcp_call"):
                rec.failed_effect = True
            rec.history.append({"role": "user", "content": observation_text(False, str(exc))})
            return
        rec.task.evidence.append(evidence)
        await self._append(
            rec,
            "agent.tool",
            {"tool": tool, "ok": True, "evidence": evidence.model_dump(mode="json")},
        )
        rec.last_tool_ok = True
        if tool in ("write_file", "shell", "mcp_call"):
            rec.failed_effect = False
        if tool in {"skills_list", "skill_load"}:
            rec.history.append(
                {
                    "role": "user",
                    "content": "[ok; untrusted skill content] " + out,
                    "context_kind": "skill",
                }
            )
        else:
            rec.history.append({"role": "user", "content": observation_text(True, out)})

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
