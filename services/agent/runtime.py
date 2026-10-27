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
    InterruptRequest,
    SteerRequest,
    Task,
    TaskRequest,
    TaskState,
)
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
    cancel_requested: bool = False
    closing: bool = False
    proc_slot: dict[str, RunningProc | None] = field(default_factory=dict)
    journal_seq: int = 0
    steer_idem: dict[str, tuple[str, str]] = field(default_factory=dict)
    interrupt_idem: dict[str, tuple[str, str]] = field(default_factory=dict)


class AgentRuntime:
    """Functional standalone seed Agent bounded to its initial baseline."""

    def __init__(
        self,
        journal: Journal,
        provider: TextProvider,
        workspace: Path,
        policy: ToolPolicy,
        max_steps: int = 12,
    ) -> None:
        self.journal = journal
        self.provider = provider
        self.workspace = Path(workspace)
        self.policy = policy
        self.max_steps = max(1, int(max_steps))
        self._tasks: dict[str, _Record] = {}
        self._submit_idem: dict[str, tuple[str, str]] = {}
        self._closed = False

    # -- journal ------------------------------------------------------
    async def _append(self, rec: _Record, kind: str, payload: dict[str, Any]) -> None:
        rec.journal_seq += 1
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

    # -- interface: submit --------------------------------------------
    async def submit(self, request: TaskRequest, actor: str = "owner") -> Task:
        self._check_actor(actor, list(request.source_event_ids), "submit")
        payload = {
            "session_id": request.session_id,
            "thread_id": request.thread_id,
            "epoch": request.epoch,
            "objective": request.objective,
            "source_event_ids": list(request.source_event_ids),
            "source_revision": request.source_revision,
        }
        digest = _payload_hash(payload)
        if request.idempotency_key in self._submit_idem:
            old_digest, task_id = self._submit_idem[request.idempotency_key]
            if old_digest != digest:
                raise IdemConflict("idempotency key reuses a conflicting submit payload")
            return self._copy(task_id)
        task = Task(
            session_id=request.session_id,
            thread_id=request.thread_id,
            epoch=request.epoch,
            objective=request.objective,
            objective_revision=request.source_revision,
            state=TaskState.ACCEPTED,
            source_event_ids=list(request.source_event_ids),
            idempotency_key=request.idempotency_key,
        )
        rec = _Record(task=task)
        self._tasks[task.task_id] = rec
        self._submit_idem[request.idempotency_key] = (digest, task.task_id)
        await self._append(
            rec,
            "agent.command",
            {
                "op": "submit",
                "actor": actor,
                "objective": request.objective,
                "objective_revision": task.objective_revision,
                "source_event_ids": list(request.source_event_ids),
            },
        )
        rec.worker = asyncio.create_task(self._run(rec))
        return task.model_copy(deep=True)

    # -- interface: steer ---------------------------------------------
    async def steer(self, task_id: str, request: SteerRequest, actor: str = "owner") -> Task:
        rec = self._get(task_id)
        self._check_actor(actor, list(request.source_event_ids), "steer")
        payload = {
            "instruction": request.instruction,
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
        if request.expected_objective_revision != rec.task.objective_revision:
            raise StaleRevision(
                f"stale revision {request.expected_objective_revision}; "
                f"current is {rec.task.objective_revision}"
            )
        rec.steer_idem[request.idempotency_key] = (digest, task_id)
        rec.pending_steer = {
            "instruction": request.instruction,
            "actor": actor,
            "source_event_ids": list(request.source_event_ids),
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
        invalidated = [
            e.evidence_id
            for e in rec.task.evidence
            if e.objective_revision == rec.task.objective_revision
        ]
        rec.task.objective = pending["instruction"]
        rec.task.objective_revision += 1
        rec.last_tool_ok = None
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
        payload = {
            "expected_objective_revision": request.expected_objective_revision,
            "reason": request.reason,
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
    def status(self, task_id: str | None = None) -> Task | list[Task]:
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

        Uncertain non-terminal work resumes as paused: the owner must
        explicitly resubmit. Writes are never blindly retried.
        """
        entries = await self.journal.read(after=0, limit=10000)
        seen: dict[str, dict[str, Any]] = {}
        outcomes: set[str] = set()
        for entry in entries:
            refs = entry.task_refs or []
            if entry.kind == "agent.command" and entry.payload.get("op") == "submit":
                for ref in refs:
                    seen.setdefault(ref, entry.payload)
            elif entry.kind == "agent.outcome":
                outcomes.update(refs)
        recovered: list[Task] = []
        for task_id, payload in seen.items():
            if task_id in self._tasks or task_id in outcomes:
                continue
            task = Task(
                task_id=task_id,
                session_id=payload.get("session_id", ""),
                thread_id=payload.get("thread_id", ""),
                epoch=payload.get("epoch", 1),
                objective=payload.get("objective", ""),
                objective_revision=payload.get("objective_revision", 1),
                state=TaskState.PAUSED,
                summary="recovered paused; uncertain writes not retried; resubmit to continue",
                idempotency_key=payload.get("idempotency_key", task_id),
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

    async def _fail(self, rec: _Record, error: str) -> None:
        if rec.task.state in TERMINAL:
            return
        rec.task.state = TaskState.FAILED
        rec.task.error = error
        await self._append(rec, "agent.outcome", {"state": "failed", "error": error})
        rec.done.set()

    async def _succeed(self, rec: _Record, summary: str) -> None:
        rec.task.state = TaskState.VERIFYING
        if rec.cancel_requested:
            rec.task.state = TaskState.CANCELLED
            await self._append(rec, "agent.outcome", {"state": "cancelled"})
            rec.done.set()
            return
        rec.task.state = TaskState.SUCCEEDED
        rec.task.summary = summary
        await self._append(
            rec,
            "agent.outcome",
            {
                "state": "succeeded",
                "summary": summary,
                "evidence": [e.model_dump(mode="json") for e in rec.task.evidence],
            },
        )
        rec.done.set()

    async def _cancel(self, rec: _Record) -> None:
        if rec.task.state in TERMINAL:
            return
        rec.task.state = TaskState.CANCELLED
        rec.task.error = "cancelled by owner"
        await self._append(rec, "agent.outcome", {"state": "cancelled"})
        rec.done.set()

    async def _run(self, rec: _Record) -> None:
        if rec.task.state == TaskState.ACCEPTED:
            rec.task.state = TaskState.RUNNING
        try:
            for _ in range(self.max_steps):
                if rec.cancel_requested or rec.closing:
                    await self._cancel(rec)
                    return
                await self._apply_steer(rec)
                messages = build_messages(
                    rec.task.objective,
                    rec.task.objective_revision,
                    list(rec.task.source_event_ids),
                    rec.history,
                    self._grants(),
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
                # at this boundary, before the (possibly stale) response.
                await self._apply_steer(rec)
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

    async def _finish(self, rec: _Record, args: dict[str, Any], raw: str) -> None:
        _ = raw
        if rec.cancel_requested:
            await self._cancel(rec)
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
        if rec.last_tool_ok is not True:
            await self._fail(rec, "finish while a required tool has failed; not reporting success")
            return
        summary = str(args.get("summary", "") or "").strip()
        await self._succeed(rec, summary or "done")

    async def _execute(self, rec: _Record, tool: str, args: dict[str, Any], raw: str) -> None:
        if rec.cancel_requested:
            await self._cancel(rec)
            return
        revision = rec.task.objective_revision
        digest = self.policy.digest_for(tool, args, revision)
        if tool in ("write_file", "shell") and self.policy.needs_approval(tool):
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
            },
        )
        rec.resume.clear()
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
        rec.history.append({"role": "assistant", "content": raw[:2000]})
        if approval_id is not None:
            # One-use intent is now committed to this exact execution.
            rec.pending = None
        try:
            if tool == "read_file":
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
            rec.history.append({"role": "user", "content": observation_text(False, str(exc))})
            return
        except (ToolFailure, PolicyDenied, ApprovalError, OSError) as exc:
            rec.last_tool_ok = False
            rec.history.append({"role": "user", "content": observation_text(False, str(exc))})
            return
        rec.task.evidence.append(evidence)
        await self._append(
            rec, "agent.tool", {"tool": tool, "ok": True, "evidence": evidence.model_dump(mode="json")}
        )
        rec.last_tool_ok = True
        rec.history.append({"role": "user", "content": observation_text(True, out)})
