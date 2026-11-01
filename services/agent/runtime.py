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
import copy
import hashlib
import json
import sqlite3
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.extensions import HarnessError
from core.policy import ApprovalError, ApprovalReceiptUnavailable, PolicyDenied, ToolPolicy
from core.policy.tool_admission import SCOPED_BUILTINS, MCPToolAdmission, native_tool_admission
from harnesses.seed import (
    ARTIFACT_RECEIPT_LIMIT,
    build_messages,
    history_exchanges,
    observation_text,
    parse_action,
)
from packages.contracts.agent_control import (
    AgentSteerAdmissionRequest,
    AgentSteerChange,
    AgentSteerLookupResult,
    AgentSteerReceipt,
    AgentSteerSource,
    AgentTaskIdentity,
)
from packages.contracts.agent_submit import (
    AGENT_SUBMIT_ADMISSION_TIMEOUT_S,
    AGENT_SUBMIT_LOOKUP_TIMEOUT_S,
    AgentSubmitConflict,
    AgentSubmitLookupRequest,
    AgentSubmitLookupResult,
    AgentSubmitReceipt,
    AgentSubmitUnavailable,
    AgentSubmitUnconfirmed,
    submit_entry,
    submit_request,
)
from packages.contracts.agent_tools import (
    NativeToolSnapshot,
    agent_tool_scope,
    native_tool_snapshot,
)
from packages.contracts.interfaces import (
    AgentControlConflict,
    AgentControlUnavailable,
    AgentModeOff,
    Journal,
    TextProvider,
)
from packages.contracts.models import (
    AppendRequest,
    ApprovalReissue,
    ApprovalReissueReceipt,
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
    new_id,
)
from packages.contracts.standing_admission import StandingInitialProof, StandingTaskAdmission
from packages.contracts.standing_execution import StandingExecutionIntent
from packages.providers.disclosure import (
    ContextScopeClosure,
    combine_scopes,
    context_scope,
    scope_allowed,
    source_scope,
)
from packages.providers.prompt_budget import PromptCountUnavailable
from packages.providers.text import ActionDecodeError, ProviderCleanupError, ProviderRefusal
from services.agent.artifacts import ArtifactConflict, ArtifactInvalid, ArtifactNotFound
from services.agent.completion import append_completion
from services.agent.history_budget import select_history
from services.agent.mcp_tools import ModelScopedMCP, run_mcp_call, run_mcp_list
from services.agent.resource_completion import ResourceCompletion, start_resource_retirement
from services.agent.tools import (
    ToolFailure,
    _FailedWithEvidence,
    run_read,
    run_search,
    run_shell,
    run_write,
    shell_directory,
    validate_write_arguments,
)
from services.context_usage import ContextObservations, captured_model


class TaskNotFound(KeyError):
    pass


class StaleRevision(ValueError):
    pass


class IdemConflict(ValueError):
    pass


class TaskTerminal(ValueError):
    pass


class _InferenceRevised(Exception):
    """An authenticated steer invalidated a request before action admission."""

    def __init__(self, response: str | None = None):
        self.response = response
        super().__init__("Agent inference superseded by steering")


class _PublicationUncertain(ToolFailure):
    """A possible persisted publication must never be blindly retried."""

    def __init__(self, message: str, evidence: Evidence | None = None):
        super().__init__(message)
        self.evidence = evidence


TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
MAX_PENDING_SUBMITS = 128
MAX_SUBMIT_OPERATIONS = 128


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


_STEER_SOURCE_GUARD = ContextVar("kinaiya_steer_source_guard", default=None)


@dataclass
class _Record:
    task: Task
    provider: TextProvider | None = None
    worker: asyncio.Task[None] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    history: list[dict[str, str]] = field(default_factory=list)
    catalog_guards: dict[str, Any] = field(default_factory=dict)
    pending: dict[str, Any] | None = None
    approval_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    approval_wait_id: str | None = None
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
    proc_slot: dict[str, Any] = field(default_factory=dict)
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
    context_event_ids: set[str] = field(default_factory=set)
    explicit_skill: str | None = None
    auxiliary_providers: dict[str, TextProvider] = field(default_factory=dict)
    model_lease: Any | None = None
    foreign_context: bool = False
    used_skills: set[str] = field(default_factory=set)
    plugin_versions: dict[str, str] = field(default_factory=dict)
    legacy_submit_scope: bool = False
    # Once a final success append may have committed, only its exact native
    # confirmation or restart recovery can settle it. Never append a competing
    # cancellation/failure or accept another revision while it is uncertain.
    completion_pending: bool = False
    submit_confirmed: bool = False
    # Immutable native initial-entry identity, independent of later steering.
    submit_identity: str | None = None
    native_task_identity: AgentTaskIdentity | None = None
    steer_attempt: _SteerAttempt | None = None
    workspace_context: Any | None = None
    standing_initial: StandingInitialProof | None = None
    standing_cleanup_uncertain: bool = False
    resource_completion: Any | None = None
    # A later adapter close cannot confirm an earlier failed request cleanup.
    _cleanup_uncertain: bool = False


@dataclass
class _SteerAttempt:
    record: _Record
    original: AgentSteerChange
    actor: str
    digest: str
    deadline: float
    active: bool = True
    sent: bool = False
    installed: bool = False
    proposal: AgentSteerAdmissionRequest | None = None
    receipt: AgentSteerReceipt | None = None
    owner: asyncio.Task | None = None

    def suppress(self):
        self.active = False


@dataclass
class _PendingSubmit:
    request: TaskRequest
    actor: str
    digest: str
    record: _Record
    proposal: AppendRequest
    suppressed: asyncio.Future[None]
    launch_open: bool = True
    admission: asyncio.Task | None = None
    lookup: asyncio.Task | None = None
    receipt: AgentSubmitReceipt | None = None
    controls: dict[tuple[str, str], str] = field(default_factory=dict)
    authority_deadline: float | None = None
    launched: bool = False

    def suppress(self) -> None:
        # This latch closes synchronously, before cancellation or any readback.
        self.launch_open = False
        if not self.suppressed.done():
            self.suppressed.set_result(None)
        self.record.done.set()
        # Native submission/lookup uses the journal client, never these model
        # adapters. A suppressed launch can close its unused captured keys now;
        # late receipt knowledge cannot revive them or schedule inference.
        if not self.launched and self.record.model_lease is not None:
            self.record.model_lease.start_close()


@dataclass(eq=False)
class _SubmitPreflight:
    deadline: float
    open: bool = True
    pending: _PendingSubmit | None = None

    def retire(self, *, suppress=True) -> None:
        self.open = False
        if suppress and self.pending is not None:
            self.pending.suppress()


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
        standing_resource_client: Any | None = None,
        plugin_service: Any | None = None,
        mcp_controller: Any | None = None,
        artifact_store: Any | None = None,
    ) -> None:
        self.journal = journal
        self.provider = provider
        self.model_settings = None
        self.context_observations = ContextObservations("agent")
        self.runtime_settings = None
        self.capability_settings = None
        self._capabilities_configured = False
        self.tool_permissions = None
        self.workspace_settings = None
        self.projects = None
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
        self.standing_resource_client = standing_resource_client
        self.plugin_service = plugin_service
        # Native presentation storage is optional for direct/test runtimes.
        # The actual launcher supplies a protected operator data path.
        self.artifact_store = artifact_store
        self._tasks: dict[str, _Record] = {}
        # Scoped (session_id, idempotency_key) so keys never collide across
        # sessions; restored by recover for terminal and paused tasks alike.
        self._submit_idem: dict[tuple[str, str], tuple[str, str]] = {}
        self._pending_submits: dict[tuple[str, str], _PendingSubmit] = {}
        self._standing_pending = {}
        self._submit_operations: set[asyncio.Task] = set()
        self._submit_waiters: set[asyncio.Future] = set()
        self._submit_preflights: set[_SubmitPreflight] = set()
        self._closed = False
        self._workspace_requests = set()
        self._workspace_operations = set()
        self._workspace_workers = set()
        self._workspace_watched = set()
        self._workspace_cleanup_failed = False
        self._resource_completions = set()
        self._resource_retirement_started = False
        self._resource_retirement = None
        self._resource_retirement_workers = set()
        from services.agent.authorizations import AgentAuthorizations

        self.authorizations = AgentAuthorizations(self)

    def workspace_operation_state(self):
        """Actual owners, including cancelled callers whose native work remains."""
        records = tuple(self._tasks.values())
        exchanges = (
            *tuple(getattr(self.journal, "_submit_exchanges", ())),
            *tuple(getattr(self.standing_resource_client, "_submit_exchanges", ())),
        )
        standing_close = getattr(self.standing_resource_client, "_close_task", None)
        uncertain_hosts = tuple(getattr(self.mcp_controller, "_uncertain_hosts", ()))
        actors = tuple(getattr(self.mcp_host, "_actors", {}).values()) + tuple(
            actor for host in uncertain_hosts for actor in getattr(host, "_actors", {}).values()
        )
        models = self.model_settings
        leases = tuple(getattr(models, "catalog_leases", ())) if models is not None else ()

        def failed_lease(lease):
            return lease.failed or (
                lease.cleanup is not None and lease.cleanup.done() and not lease.settled
            )

        def failed_operation(operation):
            return (
                operation is not None
                and operation.done()
                and (operation.cancelled() or operation.exception() is not None)
            )

        return {
            "closed": self._closed,
            "tasks": sum(not self._closed and rec.task.state not in TERMINAL for rec in records),
            "workers": len(
                {
                    worker
                    for worker in [*(rec.worker for rec in records), *self._workspace_workers]
                    if worker is not None and not worker.done()
                }
            ),
            "processes": sum(rec.proc_slot.get("proc") is not None for rec in records),
            "retirements": sum(rec.retire_lock.locked() for rec in records),
            "submit_preflights": len(self._submit_preflights),
            "submit_operations": sum(not op.done() for op in self._submit_operations)
            + int(standing_close is not None and not standing_close.done())
            + sum(
                not ex.task.done() or ex.close_task is not None and not ex.close_task.done()
                for ex in exchanges
            ),
            "pending_submits": 0 if self._closed else len(self._pending_submits),
            "ecosystem_operations": len(self._workspace_requests)
            + sum(not op.done() for op in self._workspace_operations)
            + sum(
                getattr(actor, "_current", None) is not None or not actor._queue.empty()
                for actor in actors
            ),
            "cleanup_pending": sum(
                not lease.settled and not failed_lease(lease) for lease in leases
            )
            + sum(not owner.operation.done() for owner in self._resource_completions)
            + int(self._resource_retirement is not None and not self._resource_retirement.done()),
            "cleanup_failed": int(
                self._workspace_cleanup_failed
                or getattr(self.journal, "_submit_cleanup_failed", False)
                or getattr(self.standing_resource_client, "_submit_cleanup_failed", False)
            )
            + sum(bool(failed_lease(lease)) for lease in leases)
            + sum(failed_operation(owner.operation) for owner in self._resource_completions)
            + int(failed_operation(self._resource_retirement))
            + len(uncertain_hosts),
        }

    def _workspace_settled(self):
        if self.workspace_settings is not None:

            def watch(operation, *, cleanup=False):
                if operation is None:
                    return
                if operation.done():
                    if cleanup and (operation.cancelled() or operation.exception() is not None):
                        self._workspace_cleanup_failed = True
                    return
                if operation in self._workspace_watched:
                    return
                self._workspace_watched.add(operation)

                def settled(task):
                    self._workspace_watched.discard(task)
                    if cleanup and (task.cancelled() or task.exception() is not None):
                        self._workspace_cleanup_failed = True
                    elif not task.cancelled():
                        task.exception()
                    self._workspace_settled()

                operation.add_done_callback(settled)

            for exchange in (
                *tuple(getattr(self.journal, "_submit_exchanges", ())),
                *tuple(getattr(self.standing_resource_client, "_submit_exchanges", ())),
            ):
                watch(exchange.task)
                watch(exchange.close_task, cleanup=True)
            watch(getattr(self.standing_resource_client, "_close_task", None), cleanup=True)
            for owner in tuple(self._resource_completions):
                watch(owner.operation, cleanup=True)
            watch(self._resource_retirement, cleanup=True)
            for lease in tuple(getattr(self.model_settings, "catalog_leases", ())):
                watch(lease.cleanup)
                for operation in tuple(lease.retiring):
                    watch(operation)
            self.workspace_settings.release_if_settled()

    def _retain_workspace_worker(self, worker, *, transport=False):
        if worker is None:
            return
        self._workspace_workers.add(worker)

        def settled(task):
            failed = task.cancelled()
            if not failed:
                failed = task.exception() is not None
            if transport and failed:
                self._workspace_cleanup_failed = True
            self._workspace_workers.discard(task)
            self._workspace_settled()

        worker.add_done_callback(settled)

    def _workspace_current(self, rec=None):
        if self.workspace_settings is None:
            return None
        try:
            context = self.workspace_settings.active_context()
            if rec is not None and (
                rec.recovered
                or rec.workspace_context is not context
                or rec.task.workspace_pin != context.pin
            ):
                raise ValueError("Task has no live captured workspace")
            return context
        except (OSError, ValueError, RuntimeError):
            raise PolicyDenied("Native workspace authority is unavailable") from None

    async def owned_workspace_operation(self, invoke, *, require_root=True, retain=None):
        """A caller timeout never releases a still-running native operation."""
        from services.agent.workspace_ownership import MAX_WORKSPACE_OPERATIONS, captured_context

        if self._closed or len(self._workspace_operations) >= MAX_WORKSPACE_OPERATIONS:
            raise PolicyDenied("Workspace operation capacity is unavailable")
        context = captured_context(self, required=require_root)
        if context is not None:
            self._workspace_current()
        operation = asyncio.create_task(invoke())
        self._workspace_operations.add(operation)
        if retain is not None:
            retain(operation)

        def settled(task):
            if not task.cancelled():
                task.exception()
            self._workspace_operations.discard(task)
            if self.workspace_settings is not None:
                self.workspace_settings.release_if_settled()

        operation.add_done_callback(settled)
        return await asyncio.shield(operation)

    async def retire_workspace(self):
        """Retire actual MCP actors, retaining their workers beyond timeout."""
        controller, host = self.mcp_controller, self.mcp_host
        actors = tuple(getattr(host, "_actors", {}).values())
        if controller is not None:
            await controller.retire_connections(actor="owner")
        elif host is not None:
            await host.reset_connections(actor="owner")
        workers = [actor._task for actor in actors if actor._task is not None]
        # Some transport stop methods return before resistant cancellation has
        # settled. The workspace owner's shielded retirement retains this join.
        if workers:
            results = await asyncio.gather(*workers, return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results):
                self._workspace_cleanup_failed = True
                raise RuntimeError("Actual workspace transport cleanup is unconfirmed")

    def prepare_workspace_apply(self, context):
        from services.agent.workspace_ownership import PreparedWorkspaceInstall

        owners = (self.policy, self.mcp_controller, self.skill_service, self.plugin_service)
        services = owners[1:]
        prepared = tuple(
            service.prepare_workspace(context.root) if service is not None else None
            for service in services
        )
        previous = (
            self.workspace,
            self.policy.workspace,
            self.policy.protected_roots,
            self.policy.readonly_roots,
            self.policy._workspace_current,
            self.policy._workspace_root_fd,
            tuple(
                getattr(service, "_workspace", getattr(service, "workspace", None))
                for service in services
            ),
        )

        def current():
            return (
                context.current()
                and owners
                == (self.policy, self.mcp_controller, self.skill_service, self.plugin_service)
                and previous
                == (
                    self.workspace,
                    self.policy.workspace,
                    self.policy.protected_roots,
                    self.policy.readonly_roots,
                    self.policy._workspace_current,
                    self.policy._workspace_root_fd,
                    tuple(
                        getattr(service, "_workspace", getattr(service, "workspace", None))
                        for service in services
                    ),
                )
            )

        def apply():
            self.policy.install_workspace_context(
                context,
                actor="owner",
                available=lambda: self.workspace_settings.active_context() is context,
            )
            self.workspace = context.root
            for service, value in zip(services, prepared, strict=True):
                if service is not None:
                    service.install_workspace(value)

        return PreparedWorkspaceInstall(current, apply)

    def provider_for(self, rec=None):
        """A task retains its native provider; owner defaults affect future work."""
        return rec.provider if rec is not None and rec.provider is not None else self.provider

    def _owner_provider_selection(self):
        """Owner metadata only; never construct a catalog adapter or resolve keys."""
        try:
            if self.model_settings is not None:
                return self.model_settings.selection_info(None)
            return self.provider.info, None
        except (ValueError, RuntimeError, OSError, sqlite3.Error, SystemExit):
            raise PolicyDenied("Owner model selection is unavailable") from None

    def provider_info_for(self, rec=None):
        if rec is not None:
            return self.provider_for(rec).info
        return self._owner_provider_selection()[0]

    def owner_provider_context(self):
        """Immutable metadata stamp for one owner inspection, never authority."""
        info, pin = self._owner_provider_selection()
        return json.dumps(
            {
                "provider": info.model_dump(mode="json"),
                "pin": pin.model_dump(mode="json") if pin is not None else None,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    # -- journal ------------------------------------------------------
    def _append_request(self, rec: _Record, kind: str, payload: dict[str, Any]) -> AppendRequest:
        rec.journal_seq += 1
        if kind == "agent.tool" and rec.workspace_context is not None:
            payload = {
                **payload,
                "workspace_pin": rec.workspace_context.pin.model_dump(mode="json"),
            }
            evidence = payload.get("evidence")
            if isinstance(evidence, dict) and evidence.get("kind") == "file":
                try:
                    payload["workspace_relative_path"] = str(
                        Path(evidence["path"]).relative_to(rec.workspace_context.root)
                    )
                except (ValueError, KeyError, TypeError):
                    pass
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

    async def _source_authority_texts(
        self,
        source_event_ids: list[str],
        *,
        session_id: str,
        thread_id: str,
        epoch: int,
        source_revision: int | None,
        op: str,
        guard=None,
    ) -> tuple[list[str], str]:
        """Resolve authenticated source authority without provider disclosure.

        Sources are finalized user messages or retained semantic speech windows,
        bound to this session/thread/epoch and their current revision. Window
        supersession and interruption are checked here; export disclosure is
        enforced separately by the caller.
        """
        if guard is not None:
            guard()
        resolve = getattr(self.journal, "resolve", None)
        if resolve is None:
            raise PolicyDenied(f"decisions-routed {op} cannot be validated: journal has no resolve")
        texts: list[str] = []
        scopes: list[str] = []
        for source_id in source_event_ids:
            if guard is not None:
                guard()
            try:
                resolved = await resolve(source_id)
            except Exception as exc:
                if guard is not None:
                    guard()
                raise PolicyDenied(
                    f"decisions-routed {op} carries an unresolvable source {source_id!r}"
                ) from exc
            if guard is not None:
                guard()
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
                if guard is not None:
                    guard()
                try:
                    window = SpeechWindow.model_validate(resolved.payload)
                    latest = await self.journal.latest_window(
                        session_id, epoch, window.utterance_id
                    )
                except Exception as exc:
                    if guard is not None:
                        guard()
                    raise PolicyDenied("Transcript-window source cannot be verified") from exc
                if guard is not None:
                    guard()
                if latest is None or latest.payload.get("text") != window.text:
                    raise PolicyDenied("Transcript-window instruction was superseded")
                control = await self.journal.window_control(session_id, epoch, agent_interrupt=True)
                if guard is not None:
                    guard()
                if control is not None and control.payload["source_sequence"] > resolved.sequence:
                    raise PolicyDenied(
                        "Later authenticated interruption supersedes this instruction"
                    )
            elif resolved.payload.get("final") is not True:
                raise PolicyDenied(f"decisions-routed {op} source {source_id!r} is not finalized")
            scopes.append(
                await self._source_scope(source_id, resolved, guard=guard)
                if guard is not None and _STEER_SOURCE_GUARD.get() is None
                else await self._source_scope(source_id, resolved)
            )
            if guard is not None:
                guard()
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
        return texts, scope

    async def _validated_source_texts(
        self,
        source_event_ids: list[str],
        *,
        session_id: str,
        thread_id: str,
        epoch: int,
        source_revision: int | None,
        op: str,
        provider_info=None,
        guard=None,
    ) -> tuple[list[str], str]:
        """Resolve decisions-carried sources to exact authenticated user text.

        Sources are finalized user messages or retained semantic speech windows,
        bound to this session/thread/epoch and their current revision. Window
        disclosure is checked again at the native execution boundary.
        """
        texts, scope = await self._source_authority_texts(
            source_event_ids,
            session_id=session_id,
            thread_id=thread_id,
            epoch=epoch,
            source_revision=source_revision,
            op=op,
            **({"guard": guard} if guard is not None else {}),
        )
        if not scope_allowed(
            provider_info if provider_info is not None else self.provider.info, scope
        ):
            raise PolicyDenied("Source disclosure does not permit the configured Agent provider")
        return texts, scope

    async def _source_scope(self, source_id, resolved, *, guard=None):
        if guard is None:
            guard = _STEER_SOURCE_GUARD.get()
        if guard is None:
            return await source_scope(self.journal, source_id, resolved)

        def read(method):
            async def guarded(*args, **kwargs):
                guard()
                try:
                    return await getattr(self.journal, method)(*args, **kwargs)
                finally:
                    guard()

            return guarded

        # Only the existing source-chain reads are delegated. The guard cannot
        # supply native admission, provenance, defaults or a successful receipt.
        journal = SimpleNamespace(entry=read("entry"), resolve=read("resolve"), read=read("read"))
        return await source_scope(journal, source_id, resolved)

    async def _task_scope(self, rec, *, persist=True, require_open=False):
        """Recheck source floors and the actual target provider at each continuation."""
        self._workspace_current(rec)
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
        context_closure = ContextScopeClosure(
            rec.task.session_id, rec.task.thread_id, rec.task.epoch
        )
        for event_id in sorted(rec.context_event_ids):
            try:
                entry = await self.journal.entry(event_id)
                if (entry.session_id, entry.thread_id, entry.epoch) != (
                    rec.task.session_id,
                    rec.task.thread_id,
                    rec.task.epoch,
                ):
                    raise ValueError("Wrong context scope")
                scopes.append(await context_scope(self.journal, entry, closure=context_closure))
            except Exception as exc:
                if require_open:
                    self._check_completion_open(rec, require_active=True)
                rec.task.content_scope = "local_only"
                raise PolicyDenied("Retained context disclosure cannot be verified") from exc
        if require_open:
            self._check_completion_open(rec, require_active=True)
        scope = combine_scopes(rec.task.content_scope, *scopes)
        if scope != rec.task.content_scope:
            rec.task.content_scope = scope
            if persist:
                await self._append(rec, "agent.command", {"op": "disclosure-narrowed"})
        # A steer or profile change can land while native reads above await.
        rec.task.content_scope = combine_scopes(rec.task.content_scope, scope)
        self._workspace_current(rec)
        if rec.recovered:
            # Recovery restores evidence and sticky disclosure floors, never a
            # live adapter. Do not consult/rebind the current owner default for
            # a historical task, including when that default is unavailable.
            raise PolicyDenied("Recovered work has no live model; resubmit as new work")
        if not scope_allowed(self.provider_for(rec).info, rec.task.content_scope):
            raise PolicyDenied("Task disclosure does not permit the configured Agent provider")

    # -- interface: submit --------------------------------------------
    async def submit(
        self, request: TaskRequest, actor: str = "owner", *, explicit_skill: str | None = None
    ) -> Task:
        return await self._submit(
            request.model_copy(deep=True), actor, explicit_skill=explicit_skill
        )

    async def reconcile_submit(
        self, request: TaskRequest, actor: str = "owner", *, explicit_skill: str | None = None
    ) -> Task:
        """Observe an exact prior submission without opening a new launch opportunity."""
        return await self._submit(
            request.model_copy(deep=True),
            actor,
            explicit_skill=explicit_skill,
            reconcile_only=True,
        )

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
        self,
        request: TaskRequest,
        actor: str = "owner",
        *,
        explicit_skill: str | None = None,
        reconcile_only: bool = False,
    ) -> Task:
        self._check_actor(actor, list(request.source_event_ids), "submit")
        async with self.integration_lock:
            preflight = _SubmitPreflight(time.monotonic() + AGENT_SUBMIT_LOOKUP_TIMEOUT_S)
            self._submit_preflights.add(preflight)
            try:
                operation = self._start_submit_operation(
                    lambda: self._prepare_submit(
                        request,
                        actor,
                        explicit_skill=explicit_skill,
                        preflight=preflight,
                        reconcile_only=reconcile_only,
                    )
                )
                prepared = await self._await_submit_operation(
                    operation, preflight.deadline - time.monotonic()
                )
                self._submit_open(preflight=preflight)
            except BaseException:
                preflight.retire()
                raise
            finally:
                self._submit_preflights.discard(preflight)
            preflight.retire(suppress=False)
        if isinstance(prepared, Task):
            return prepared
        return await self._admit_submit(prepared)

    def _submit_open(self, *, preflight=None, pending=None, deadline=None) -> None:
        current = asyncio.current_task()
        expired = deadline is not None and time.monotonic() >= deadline
        if preflight is not None:
            expired = expired or not preflight.open or time.monotonic() >= preflight.deadline
        if (
            self._closed
            or expired
            or (current is not None and current.cancelling())
            or (
                pending is not None
                and (
                    not pending.launch_open
                    or pending.record.closing
                    or pending.record.cancel_requested
                )
            )
        ):
            if preflight is not None:
                preflight.retire()
            if pending is not None:
                pending.suppress()
            raise AgentSubmitUnavailable

    async def _prepare_submit(
        self,
        request: TaskRequest,
        actor: str,
        *,
        explicit_skill: str | None,
        preflight: _SubmitPreflight,
        reconcile_only: bool = False,
    ) -> Task | _PendingSubmit:
        def guard():
            self._submit_open(preflight=preflight)

        guard()
        self._check_actor(actor, list(request.source_event_ids), "submit")
        if explicit_skill is not None:
            if actor != "owner":
                raise PolicyDenied("Explicit skill invocation requires owner and configured skills")
            if not isinstance(explicit_skill, str) or not 1 <= len(explicit_skill) <= 256:
                raise ValueError("Invalid explicit skill id")
        session = await self._require_session(request.session_id)
        guard()
        if (request.thread_id, request.epoch) != (session.thread_id, session.epoch):
            raise StaleRevision(
                f"submit targets thread {request.thread_id} epoch {request.epoch}; "
                f"session is now thread {session.thread_id} epoch {session.epoch}"
            )
        objective = request.objective
        source_scope = "public_synthetic"
        if request.source_event_ids:
            texts, source_scope = await self._source_authority_texts(
                list(request.source_event_ids),
                session_id=request.session_id,
                thread_id=request.thread_id,
                epoch=request.epoch,
                source_revision=request.source_revision,
                op="submit",
                guard=guard,
            )
            expected = "\n".join(texts)
            if actor == "decisions" and objective != expected:
                raise PolicyDenied(
                    "decisions-routed submit objective does not match "
                    "the resolved authenticated source text"
                )
            if actor == "decisions":
                objective = expected
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
        guard()
        confirmed = self._submit_idem.get(idem_key)
        if confirmed is not None:
            old_digest, task_id = confirmed
            rec = self._get(task_id)
            if rec.submit_confirmed:
                self._check_submit_digest(rec, payload, digest, old_digest)
                return self._copy(task_id)
        pending = self._pending_submits.get(idem_key)
        if pending is not None:
            if pending.digest != digest:
                raise IdemConflict("idempotency key reuses a conflicting submit payload")
            if pending.launch_open:
                raise AgentSubmitUnconfirmed
            deadline = preflight.deadline
            try:
                result = await self._lookup_submit(request, deadline, pending)
                if result.status != "recorded":
                    raise AgentSubmitUnconfirmed
                AgentSubmitReceipt(disposition="retained", entry=result.entry).validate_request(
                    pending.proposal
                )
                rec = await self._hydrate_submit(result.entry, deadline, guard=guard)
            except (AgentSubmitConflict, asyncio.CancelledError):
                raise
            except Exception:
                raise AgentSubmitUnconfirmed from None
            self._release_submit(pending, rec)
            return rec.task.model_copy(deep=True)

        # A native winner precedes every current provider, harness and skill check.
        deadline = preflight.deadline
        result = await self._lookup_submit(request, deadline)
        guard()
        if result.status == "recorded":
            winner = result.entry
            self._check_submit_digest(
                None, payload, digest, winner.payload["payload_digest"], entry=winner
            )
            rec = await self._hydrate_submit(winner, deadline, guard=guard)
            return rec.task.model_copy(deep=True)

        if reconcile_only:
            # Unobserved is not failure evidence: an older admission may still
            # commit. Receipt checks never construct models or reserve new work.
            raise AgentSubmitUnconfirmed

        # Management remains excluded until selection and the frozen reservation.
        guard()
        confirmed = self._submit_idem.get(idem_key)
        if confirmed is not None:
            old_digest, task_id = confirmed
            rec = self._get(task_id)
            if rec.submit_confirmed:
                self._check_submit_digest(rec, payload, digest, old_digest)
                return self._copy(task_id)
        pending = self._pending_submits.get(idem_key)
        if pending is not None:
            if pending.digest != digest:
                raise IdemConflict("idempotency key reuses a conflicting submit payload")
            raise AgentSubmitUnconfirmed
        if (
            len(self._pending_submits) >= MAX_PENDING_SUBMITS
            or len(self._submit_operations) >= MAX_SUBMIT_OPERATIONS
        ):
            raise AgentSubmitUnavailable
        if self.model_settings is not None and self.model_settings.catalog_cleanup_busy:
            raise PolicyDenied("Catalog provider cleanup is unconfirmed; no new work accepted")
        if self._capabilities_configured or self.capability_settings is not None:
            # Historical submission reconciliation returned above. A genuinely
            # new task cannot capture models under an unreadable tool owner.
            self.tool_snapshot()
        if explicit_skill is not None and self.skill_service is None:
            raise PolicyDenied("Explicit skill invocation requires owner and configured skills")
        if explicit_skill is not None and not self.tool_snapshot().supports("skill_load"):
            raise PolicyDenied("Explicit skill invocation is disabled by native availability")
        from services.agent.workspace_ownership import captured_context

        workspace_context = captured_context(self)
        model_lease = None
        content_scope = combine_scopes(request.content_scope, source_scope)
        try:
            harness_version = self.session_harness(session.harness_version)
            if self.model_settings is not None:
                task_provider, model_pin, auxiliary_providers, auxiliary_pins, model_lease = (
                    self.model_settings.task_selection(harness_version, content_scope)
                )
            else:
                task_provider, model_pin = self.provider, None
                auxiliary_providers, auxiliary_pins = {}, {}
        except (HarnessError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            raise PolicyDenied(
                "The session's pinned harness or model selection is unavailable"
            ) from exc
        try:
            if request.source_event_ids and not scope_allowed(task_provider.info, source_scope):
                raise PolicyDenied(
                    "Source disclosure does not permit the configured Agent provider"
                )
            content_scope = combine_scopes(request.content_scope, source_scope)
            if not scope_allowed(task_provider.info, content_scope):
                raise PolicyDenied("Task disclosure does not permit the configured Agent provider")
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
                model_pin=model_pin,
                auxiliary_models=auxiliary_pins,
                **(
                    {"workspace_pin": workspace_context.pin}
                    if workspace_context is not None
                    else {}
                ),
            )
            rec = _Record(
                task=task,
                provider=task_provider,
                explicit_skill=explicit_skill,
                auxiliary_providers=dict(auxiliary_providers),
                model_lease=model_lease,
                workspace_context=workspace_context,
                proc_slot={"on_settled": self._workspace_settled},
            )
            if request.source_event_ids:
                rec.source_revisions = dict.fromkeys(
                    request.source_event_ids, request.source_revision
                )
            proposal = submit_request(
                self._append_request(
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
                        "model_pin": model_pin.model_dump(mode="json")
                        if model_pin is not None
                        else None,
                        "auxiliary_models": {
                            key: value.model_dump(mode="json")
                            for key, value in auxiliary_pins.items()
                        },
                        "explicit_skill": explicit_skill,
                        **(
                            {"workspace_pin": workspace_context.pin.model_dump(mode="json")}
                            if workspace_context is not None
                            else {}
                        ),
                    },
                )
            )
            pending = _PendingSubmit(
                request=request,
                actor=actor,
                digest=digest,
                record=rec,
                proposal=proposal,
                suppressed=asyncio.get_running_loop().create_future(),
            )
            # Public uncertainty never mutates the accepted native proposal.
            task.state = TaskState.PAUSED
            task.summary = ""
            task.error = str(AgentSubmitUnconfirmed())
            guard()
            self._workspace_current(rec)
            if task.task_id in self._tasks:
                raise AgentSubmitUnavailable
            preflight.pending = pending
            self._pending_submits[idem_key] = pending
            self._tasks[task.task_id] = rec

            return pending
        except BaseException:
            if model_lease is not None:
                model_lease.start_close()
            raise

    async def _admit_submit(self, pending: _PendingSubmit) -> Task:
        rec, proposal = pending.record, pending.proposal
        request, digest = pending.request, pending.digest
        idem_key = (request.session_id, request.idempotency_key)
        try:
            admit = getattr(self.journal, "admit_submit", None)
            if not callable(admit):
                raise AgentSubmitUnavailable

            async def send():
                self._submit_open(pending=pending)
                return await admit(proposal.model_copy(deep=True))

            pending.admission = self._start_submit_operation(send, pending)
            raw = await self._await_submit_operation(
                pending.admission, AGENT_SUBMIT_ADMISSION_TIMEOUT_S, pending
            )
            receipt = self._submit_receipt(raw, proposal)
            pending.receipt = receipt
        except asyncio.CancelledError:
            pending.suppress()
            raise
        except AgentSubmitConflict:
            pending.suppress()
            raise
        except AgentModeOff:
            pending.suppress()
            # This direct, fully validated refusal applies only to this new
            # attempt. Earlier uncertainty never reaches a fresh admission.
            self._release_submit(pending, None)
            raise
        except Exception:
            pending.suppress()
            raise AgentSubmitUnconfirmed from None
        if self._closed:
            pending.suppress()
            raise AgentSubmitUnconfirmed
        if receipt.disposition == "retained":
            pending.suppress()
            rec = await self._hydrate_submit(
                receipt.entry, time.monotonic() + AGENT_SUBMIT_LOOKUP_TIMEOUT_S
            )
            self._release_submit(pending, rec)
            return rec.task.model_copy(deep=True)

        rec.submit_sequence = receipt.entry.sequence
        rec.native_task_identity = self._native_task_identity(receipt.entry)
        rec.submit_identity = _payload_hash(receipt.entry.model_dump(mode="json"))
        rec.submit_confirmed = True
        rec.task.error = None
        self._submit_idem[idem_key] = (digest, rec.task.task_id)
        try:
            # Only this direct created response has a launch opportunity.
            pending.authority_deadline = time.monotonic() + AGENT_SUBMIT_LOOKUP_TIMEOUT_S
            authority = self._start_submit_operation(lambda: self._fresh_submit_authority(pending))
            await self._await_submit_operation(authority, AGENT_SUBMIT_LOOKUP_TIMEOUT_S, pending)
            if not pending.launch_open or self._closed or rec.closing or rec.cancel_requested:
                pending.suppress()
                return rec.task.model_copy(deep=True)
            pending.launch_open = False
            rec.task.state = TaskState.ACCEPTED
            rec.worker = asyncio.create_task(self._run(rec))
            pending.launched = True
            return rec.task.model_copy(deep=True)
        except BaseException:
            pending.suppress()
            if rec.task.state not in TERMINAL:
                rec.task.error = "Submission confirmed; local execution was not started"
            raise
        finally:
            self._release_submit(pending, rec)

    @staticmethod
    def _native_task_identity(entry):
        return AgentTaskIdentity(
            schema_version="1.0",
            session_id=entry.session_id,
            thread_id=entry.thread_id,
            epoch=entry.epoch,
            task_id=entry.payload["task_id"],
            submit_event_id=entry.event_id,
            submit_sequence=entry.sequence,
            submit_idempotency_key=entry.payload["idempotency_key"],
            submit_payload_digest=entry.payload["payload_digest"],
        )

    @staticmethod
    def _check_submit_digest(rec, payload, digest, old_digest, *, entry=None) -> None:
        if old_digest == digest:
            return
        legacy = (
            rec.legacy_submit_scope
            if rec is not None
            else ("requested_content_scope" not in entry.payload)
        )
        if (
            legacy
            and payload["content_scope"] == "local_only"
            and old_digest
            == _payload_hash(
                {key: value for key, value in payload.items() if key != "content_scope"}
            )
        ):
            return
        if rec is None:
            raise AgentSubmitConflict
        raise IdemConflict("idempotency key reuses a conflicting submit payload")

    @staticmethod
    def _submit_receipt(raw, proposal) -> AgentSubmitReceipt:
        detached = copy.deepcopy(
            raw.model_dump(mode="python") if isinstance(raw, AgentSubmitReceipt) else raw
        )
        return AgentSubmitReceipt.model_validate(detached).validate_request(proposal)

    def _start_submit_operation(self, invoke, pending=None) -> asyncio.Task:
        if self._closed or len(self._submit_operations) >= MAX_SUBMIT_OPERATIONS:
            raise AgentSubmitUnavailable

        async def call():
            return await invoke()

        operation = asyncio.create_task(call())
        self._submit_operations.add(operation)

        def settled(task):
            self._submit_operations.discard(task)
            try:
                value = task.result()
                if pending is not None:
                    pending.receipt = self._submit_receipt(value, pending.proposal)
            except BaseException:
                if pending is not None:
                    pending.suppress()
            # No readback, dependency access or launch happens in a late callback.
            self._workspace_settled()

        operation.add_done_callback(settled)
        return operation

    async def _await_submit_operation(self, operation, timeout, pending=None):
        deadline = time.monotonic() + max(0, timeout)
        if len(self._submit_waiters) >= MAX_SUBMIT_OPERATIONS:
            if pending is not None:
                pending.suppress()
            raise AgentSubmitUnavailable
        abort = asyncio.get_running_loop().create_future()
        self._submit_waiters.add(abort)
        waiters = {operation, abort}
        if pending is not None:
            waiters.add(pending.suppressed)
        try:
            if time.monotonic() >= deadline:
                if pending is not None:
                    pending.suppress()
                operation.cancel()
                raise AgentSubmitUnavailable
            done, _ = await asyncio.wait(
                waiters,
                timeout=max(0, deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            # A completed task can win scheduling after the total budget elapsed.
            # Its result is knowledge, never a late execution authorization.
            if time.monotonic() >= deadline:
                if pending is not None:
                    pending.suppress()
                operation.cancel()
                raise AgentSubmitUnavailable
            if operation in done:
                return operation.result()
            if pending is not None:
                pending.suppress()
            operation.cancel()
            raise AgentSubmitUnavailable
        except asyncio.CancelledError:
            if pending is not None:
                pending.suppress()
            operation.cancel()
            raise
        finally:
            self._submit_waiters.discard(abort)
            abort.cancel()

    async def _lookup_submit(self, request, deadline, pending=None):
        query = AgentSubmitLookupRequest(
            session_id=request.session_id,
            thread_id=request.thread_id,
            epoch=request.epoch,
            idempotency_key=request.idempotency_key,
        )
        lookup = getattr(self.journal, "lookup_submit", None)
        if not callable(lookup):
            raise AgentSubmitUnavailable
        try:
            operation = pending.lookup if pending is not None else None
            if operation is None or operation.done():
                operation = self._start_submit_operation(
                    lambda: lookup(query.model_copy(deep=True))
                )
                if pending is not None:
                    pending.lookup = operation
            raw = await self._await_submit_operation(operation, deadline - time.monotonic())
            if self._closed:
                raise AgentSubmitUnavailable
            detached = copy.deepcopy(
                raw.model_dump(mode="python") if isinstance(raw, AgentSubmitLookupResult) else raw
            )
            return AgentSubmitLookupResult.model_validate(detached).validate_request(query)
        except (AgentSubmitConflict, asyncio.CancelledError):
            raise
        except Exception:
            raise AgentSubmitUnavailable from None

    async def _fresh_submit_authority(self, pending) -> None:
        rec, request = pending.record, pending.request

        def guard():
            self._submit_open(pending=pending, deadline=pending.authority_deadline)
            self._workspace_current(rec)

        guard()
        session = await self._require_session(request.session_id)
        guard()
        if self._check_task_epoch(rec, session):
            raise StaleRevision("Submission session changed before execution")
        if request.source_event_ids:
            texts, scope = await self._source_authority_texts(
                list(request.source_event_ids),
                session_id=request.session_id,
                thread_id=request.thread_id,
                epoch=request.epoch,
                source_revision=request.source_revision,
                op="submit",
                guard=guard,
            )
            guard()
            if pending.actor == "decisions" and request.objective != "\n".join(texts):
                raise PolicyDenied("Submitted instruction changed before execution")
            rec.task.content_scope = combine_scopes(rec.task.content_scope, scope)
            if not scope_allowed(self.provider_for(rec).info, rec.task.content_scope):
                raise PolicyDenied("Task disclosure does not permit the configured Agent provider")
        guard()
        session = await self._require_session(request.session_id)
        guard()
        if self._check_task_epoch(rec, session):
            raise StaleRevision("Submission session changed before execution")

    def _release_submit(self, pending, winner) -> None:
        if not pending.launched and pending.record.model_lease is not None:
            pending.record.model_lease.start_close()
        key = (pending.request.session_id, pending.request.idempotency_key)
        if self._pending_submits.get(key) is pending:
            self._pending_submits.pop(key)
        if (
            winner is not pending.record
            and self._tasks.get(pending.record.task.task_id) is pending.record
        ):
            self._tasks.pop(pending.record.task.task_id)

    def _check_submit_record(self, rec: _Record, entry: JournalEntry) -> None:
        identity = _payload_hash(entry.model_dump(mode="json"))
        if rec.submit_identity is not None:
            if rec.submit_identity != identity:
                raise AgentSubmitUnavailable
            return
        # An unconfirmed reservation may acquire its canonical proof without
        # comparing the mutable public Task or granting another launch.
        pending = self._pending_submits.get((entry.session_id, entry.payload["idempotency_key"]))
        if pending is None or pending.record is not rec or pending.launch_open:
            raise AgentSubmitUnavailable
        try:
            AgentSubmitReceipt(disposition="retained", entry=entry).validate_request(
                pending.proposal
            )
        except ValueError:
            raise AgentSubmitUnavailable from None

    async def _hydrate_submit(self, entry, deadline, *, guard=None) -> _Record:
        if guard is not None:
            guard()
        entry = submit_entry(entry)
        existing = self._tasks.get(entry.payload["task_id"])
        if existing is not None:
            self._check_submit_record(existing, entry)
            if existing.submit_confirmed:
                return existing
        operation = self._start_submit_operation(
            lambda: self._read_submit_record(entry, deadline, guard=guard)
        )
        try:
            rec = await self._await_submit_operation(operation, deadline - time.monotonic())
            if self._closed:
                raise AgentSubmitUnavailable
        except asyncio.CancelledError:
            raise
        except Exception:
            raise AgentSubmitUnavailable from None
        if guard is not None:
            guard()
        self._submit_open(deadline=deadline)
        existing = self._tasks.get(rec.task.task_id)
        if existing is not None:
            self._check_submit_record(existing, entry)
            if existing.submit_confirmed:
                return existing
        rec.submit_confirmed = True
        self._tasks[rec.task.task_id] = rec
        self._submit_idem[(entry.session_id, entry.payload["idempotency_key"])] = (
            entry.payload["payload_digest"],
            rec.task.task_id,
        )
        return rec

    async def _read_submit_record(self, entry, deadline, *, guard=None) -> _Record:
        def check():
            self._submit_open(deadline=deadline)
            if guard is not None:
                guard()

        check()
        payload = entry.payload
        task = Task(
            task_id=payload["task_id"],
            session_id=entry.session_id,
            thread_id=entry.thread_id,
            epoch=entry.epoch,
            objective=payload["objective"],
            objective_revision=payload["objective_revision"],
            state=TaskState.PAUSED,
            source_event_ids=list(payload["source_event_ids"]),
            content_scope=payload.get("content_scope", "local_only"),
            idempotency_key=payload["idempotency_key"],
            harness_version=payload["harness_version"],
            model_pin=payload.get("model_pin"),
            auxiliary_models=payload.get("auxiliary_models", {}),
            summary="recovered paused; uncertain writes not retried; resubmit to continue",
            **({"workspace_pin": payload["workspace_pin"]} if "workspace_pin" in payload else {}),
        )
        rec = _Record(
            task=task,
            recovered=True,
            native_task_identity=self._native_task_identity(entry),
            submit_sequence=entry.sequence,
            submit_identity=_payload_hash(entry.model_dump(mode="json")),
            legacy_submit_scope="requested_content_scope" not in payload,
            explicit_skill=payload.get("explicit_skill"),
        )
        # Retain only typed retirement hashes, the latest snapshot/pins and the
        # cumulative scope. A byte-limited page is not evidence of final absence.
        retirements: dict[tuple[int, str], str] = {}
        after = entry.sequence - 1
        while True:
            check()
            batch = await self.journal.read(after=after, limit=200, task_id=task.task_id)
            check()
            if not batch:
                break
            for event in batch:
                if event.sequence <= after:
                    raise AgentSubmitUnavailable
                after = event.sequence
                if event.origin != "agent-supervisor" or event.task_refs != [task.task_id]:
                    continue
                if (event.session_id, event.thread_id) != (task.session_id, task.thread_id):
                    continue
                if event.kind == "agent.retirement":
                    try:
                        claimed = TaskRetirement.model_validate(event.payload)
                    except ValueError:
                        continue
                    # Bind the revision after the final authoritative snapshot.
                    identity = task.model_copy(
                        update={"objective_revision": claimed.objective_revision}
                    )
                    receipt = _authenticated_retirement(event, identity, entry.sequence)
                    if receipt is not None:
                        retirements[(receipt.objective_revision, receipt.request_key_sha256)] = (
                            receipt.request_digest
                        )
                    continue
                if event.epoch != task.epoch:
                    continue
                task.content_scope = combine_scopes(
                    task.content_scope, event.payload.get("content_scope", "local_only")
                )
                pins = event.payload.get("plugin_versions", {})
                if (
                    type(pins) is not dict
                    or len(pins) > 16
                    or any(type(k) is not str or type(v) is not str for k, v in pins.items())
                ):
                    raise AgentSubmitUnavailable
                rec.plugin_versions = dict(pins)
                prefix = f"agent:{task.task_id}:{event.kind}:"
                if event.idempotency_key.startswith(prefix):
                    suffix = event.idempotency_key[len(prefix) :]
                    if suffix.isdecimal() and len(suffix) <= 19:
                        rec.journal_seq = max(rec.journal_seq, int(suffix))
                if event.kind == "agent.outcome" and isinstance(event.payload.get("task"), dict):
                    snapshot = Task.model_validate(event.payload["task"])
                    if (
                        snapshot.task_id,
                        snapshot.session_id,
                        snapshot.thread_id,
                        snapshot.epoch,
                        snapshot.idempotency_key,
                    ) != (
                        task.task_id,
                        task.session_id,
                        task.thread_id,
                        task.epoch,
                        task.idempotency_key,
                    ):
                        raise AgentSubmitUnavailable
                    snapshot.harness_version = task.harness_version
                    if snapshot.workspace_pin != task.workspace_pin:
                        raise AgentSubmitUnavailable
                    if "workspace_pin" not in task.model_fields_set:
                        snapshot.__pydantic_fields_set__.discard("workspace_pin")
                    snapshot.model_pin = task.model_pin
                    snapshot.auxiliary_models = task.auxiliary_models
                    snapshot.content_scope = combine_scopes(
                        task.content_scope, snapshot.content_scope
                    )
                    if snapshot.state not in TERMINAL:
                        snapshot.state = TaskState.PAUSED
                        snapshot.summary = (
                            "recovered paused; uncertain writes not retried; resubmit to continue"
                        )
                    if task.state not in TERMINAL or snapshot.state in TERMINAL:
                        task = rec.task = snapshot
            # Cooperative yielding makes the total deadline effective even for
            # adapters whose async reads return immediately on every page.
            await asyncio.sleep(0)
        check()
        if task.state not in TERMINAL:
            rec.retirement_keys = {
                key: digest
                for (revision, key), digest in retirements.items()
                if revision == task.objective_revision
            }
            if rec.retirement_keys:
                task.state = TaskState.CANCELLED
                task.error = "cancelled by owner"
                task.summary = RETIRED_SUMMARY
        rec.done.set()
        return rec

    async def _pending_submit_control(self, rec, request, actor, op) -> None:
        key = (rec.task.session_id, rec.task.idempotency_key)
        pending = self._pending_submits.get(key)
        if pending is None or pending.record is not rec or rec.submit_confirmed:
            return
        self._check_actor(actor, list(request.source_event_ids), op)

        def guard():
            if self._closed:
                raise AgentSubmitUnconfirmed
            self._submit_open()

        guard()
        session = await self._require_session(rec.task.session_id)
        if self._closed:
            raise AgentSubmitUnconfirmed
        if self._check_task_epoch(rec, session):
            raise StaleRevision("Submission control targets a stale session")
        if request.source_event_ids:
            texts, _ = await self._validated_source_texts(
                list(request.source_event_ids),
                session_id=rec.task.session_id,
                thread_id=rec.task.thread_id,
                epoch=rec.task.epoch,
                source_revision=getattr(request, "source_revision", None),
                op=op,
                provider_info=self.provider_for(rec).info,
                guard=guard,
            )
            if self._closed:
                raise AgentSubmitUnconfirmed
            if op == "steer" and actor == "decisions" and request.instruction != "\n".join(texts):
                raise PolicyDenied(
                    "decisions-routed steer instruction does not match authenticated text"
                )
        if request.expected_objective_revision != rec.task.objective_revision:
            raise StaleRevision("Submission control targets a stale objective revision")
        digest = _payload_hash(
            request.model_dump(mode="json", exclude={"schema_version", "idempotency_key"})
        )
        control_key = (op, request.idempotency_key)
        old = pending.controls.get(control_key)
        if old is not None and old != digest:
            raise IdemConflict(f"idempotency key reuses a conflicting {op} payload")
        if old is None and len(pending.controls) >= MAX_PENDING_SUBMITS:
            raise AgentSubmitUnconfirmed
        # Confirmation may have arrived during authentication. Normal controls
        # may proceed only after the initial native acceptance is known.
        if rec.submit_confirmed:
            return
        pending.controls[control_key] = digest
        if op == "interrupt":
            pending.suppress()
            rec.cancel_requested = True
        raise AgentSubmitUnconfirmed

    # -- interface: steer ---------------------------------------------
    def suppress_steer(self, task_id, key, owner=None):
        rec = self._tasks.get(task_id)
        attempt = rec.steer_attempt if rec is not None else None
        if (
            attempt is not None
            and attempt.original.idempotency_key == key
            and not attempt.installed
            and (owner is None or attempt.owner is owner)
        ):
            attempt.suppress()

    def _steer_open(self, attempt):
        rec = attempt.record
        self._check_completion_open(rec, require_active=True)
        current = asyncio.current_task()
        if (
            self._closed
            or not attempt.active
            or rec.closing
            or rec.cancel_requested
            or self._tasks.get(rec.task.task_id) is not rec
            or time.monotonic() >= attempt.deadline
            or (current is not None and current.cancelling())
        ):
            attempt.suppress()
            raise AgentControlUnavailable
        if rec.task.objective_revision != attempt.original.expected_objective_revision:
            raise StaleRevision("Task revision changed during native steering admission")
        self._workspace_current(rec)

    async def _steer_scope(self, rec, guard):
        # Compute against actual reads without changing the live Task on refusal.
        scopes = [rec.task.content_scope]
        for source_id in tuple(rec.task.source_event_ids):
            guard()
            try:
                source = await self.journal.resolve(source_id)
                guard()
                if (
                    (source.session_id, source.thread_id, source.epoch)
                    != (rec.task.session_id, rec.task.thread_id, rec.task.epoch)
                    or source.kind not in {"conversation.user", "conversation.window"}
                    or source.origin != "authenticated-user"
                ):
                    raise PolicyDenied("Task source disclosure cannot be verified")
                scopes.append(await self._source_scope(source_id, source))
            except Exception as exc:
                guard()
                raise PolicyDenied("Task source disclosure cannot be verified") from exc
            finally:
                guard()

        def read(method):
            async def guarded(*args, **kwargs):
                guard()
                try:
                    return await getattr(self.journal, method)(*args, **kwargs)
                finally:
                    guard()

            return guarded

        journal = SimpleNamespace(entry=read("entry"), resolve=read("resolve"), read=read("read"))
        closure = ContextScopeClosure(rec.task.session_id, rec.task.thread_id, rec.task.epoch)
        for event_id in sorted(rec.context_event_ids):
            entry = await journal.entry(event_id)
            scopes.append(await context_scope(journal, entry, closure=closure))
            guard()
        return combine_scopes(*scopes)

    async def _prepare_steer(self, attempt):
        rec, request = attempt.record, attempt.original

        def guard():
            self._steer_open(attempt)

        guard()
        session = await self._require_session(rec.task.session_id)
        guard()
        if self._check_task_epoch(rec, session) is not None:
            raise StaleRevision("Steering session changed")
        scope = await self._steer_scope(rec, guard)
        guard()
        texts, source_scope = await self._source_authority_texts(
            list(request.source_event_ids),
            session_id=rec.task.session_id,
            thread_id=rec.task.thread_id,
            epoch=rec.task.epoch,
            source_revision=None,
            op="steer",
            guard=guard,
        )
        guard()
        if attempt.actor == "decisions" and request.instruction != "\n".join(texts):
            raise PolicyDenied("Decisions steering does not match authenticated text")
        observations = []
        for source_id in request.source_event_ids:
            guard()
            resolved = await self.journal.resolve(source_id)
            guard()
            floor = await self._source_scope(source_id, resolved)
            guard()
            observations.append(
                AgentSteerSource(
                    schema_version="1.0",
                    event_id=source_id,
                    revision=resolved.payload.get("revision", 1),
                    content_scope=floor,
                )
            )
        effective = combine_scopes(scope, source_scope, request.content_scope)
        if not scope_allowed(self.provider_for(rec).info, effective):
            raise PolicyDenied("Steering disclosure does not permit the configured Agent provider")
        backend = getattr(self.journal, "agent_control_backend", None)
        if not callable(backend) or rec.native_task_identity is None:
            raise AgentControlUnavailable
        guard()
        identity = await backend()
        guard()
        return AgentSteerAdmissionRequest(
            schema_version="1.0",
            expected_backend=identity,
            task=rec.native_task_identity,
            actor=attempt.actor,
            request=request,
            effective_content_scope=effective,
            source_revisions=tuple(observations),
        )

    async def _fresh_steer(self, attempt):
        rec, proposal = attempt.record, attempt.proposal

        def guard():
            self._steer_open(attempt)

        guard()
        session = await self._require_session(rec.task.session_id)
        guard()
        if self._check_task_epoch(rec, session) is not None:
            raise StaleRevision("Steering session changed before installation")
        floor = await self._steer_scope(rec, guard)
        guard()
        texts, source_scope = await self._source_authority_texts(
            list(proposal.request.source_event_ids),
            session_id=rec.task.session_id,
            thread_id=rec.task.thread_id,
            epoch=rec.task.epoch,
            source_revision=None,
            op="steer",
            guard=guard,
        )
        guard()
        if attempt.actor == "decisions" and proposal.request.instruction != "\n".join(texts):
            raise PolicyDenied("Steering source changed before installation")
        for observed in proposal.source_revisions:
            guard()
            resolved = await self.journal.resolve(observed.event_id)
            guard()
            if resolved.payload.get("revision", 1) != observed.revision:
                raise PolicyDenied("Steering source revision changed before installation")
        guard()
        if await self.journal.agent_control_backend() != proposal.expected_backend:
            raise AgentControlUnavailable
        guard()
        scope = combine_scopes(
            rec.task.content_scope, floor, source_scope, proposal.effective_content_scope
        )
        if not scope_allowed(self.provider_for(rec).info, scope):
            raise PolicyDenied("Steering disclosure changed before installation")
        return scope

    async def steer(self, task_id: str, request: SteerRequest, actor: str = "owner") -> Task:
        request = request.model_copy(deep=True)
        original = AgentSteerChange.model_validate(request.model_dump(mode="json"))
        rec = self._get(task_id)
        await self._pending_submit_control(rec, request, actor, "steer")
        self._check_actor(actor, list(original.source_event_ids), "steer")
        digest = _payload_hash({"actor": actor, "request": original.model_dump(mode="json")})
        cached = rec.steer_idem.get(original.idempotency_key)
        if cached is not None:
            if cached[0] != digest:
                raise IdemConflict("idempotency key reuses a conflicting steer payload")
            if original.source_event_ids:
                texts, _ = await self._validated_source_texts(
                    list(original.source_event_ids),
                    session_id=rec.task.session_id,
                    thread_id=rec.task.thread_id,
                    epoch=rec.task.epoch,
                    source_revision=None,
                    op="steer",
                    provider_info=self.provider_for(rec).info,
                )
                if actor == "decisions" and original.instruction != "\n".join(texts):
                    raise PolicyDenied("Decisions steering does not match authenticated text")
            return rec.task.model_copy(deep=True)
        previous = rec.steer_attempt
        if previous is not None and not previous.installed:
            if previous.digest != digest:
                raise AgentControlConflict
            if previous.proposal is not None and previous.sent and not previous.active:
                lookup = getattr(self.journal, "lookup_steer", None)
                if not callable(lookup):
                    raise AgentControlUnavailable
                operation = self._start_submit_operation(lambda: lookup(previous.proposal))
                try:
                    raw = await self._await_submit_operation(
                        operation, AGENT_SUBMIT_LOOKUP_TIMEOUT_S
                    )
                    data = (
                        raw.model_dump(mode="python")
                        if isinstance(raw, AgentSteerLookupResult)
                        else raw
                    )
                    observed = AgentSteerLookupResult.model_validate(data).validate_request(
                        previous.proposal
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Lookup is inspection after uncertainty. A later refusal
                    # or invalid proof cannot classify the original as absent.
                    raise AgentControlUnavailable from None
                # Positive knowledge is inspection only; it cannot resurrect the
                # suppressed direct-created opportunity or replace the goal.
                previous.receipt = observed.receipt
            raise AgentControlUnavailable
        self._check_completion_open(rec, require_active=True)
        if rec.recovered and rec.worker is None:
            raise ValueError("Recovered work requires a new task; retire the paused task first")
        if rec.pending_steer is not None:
            raise StaleRevision("Another steering revision is pending")
        attempt = _SteerAttempt(
            rec, original, actor, digest, time.monotonic() + 5.0, owner=asyncio.current_task()
        )
        rec.steer_attempt = attempt

        def guard():
            self._steer_open(attempt)

        token = _STEER_SOURCE_GUARD.set(guard)
        try:
            attempt.proposal = await self._prepare_steer(attempt)
            guard()
            admit = getattr(self.journal, "admit_steer", None)
            if not callable(admit):
                raise AgentControlUnavailable
            attempt.deadline = time.monotonic() + 15.0

            async def send():
                guard()
                attempt.sent = True
                return await admit(attempt.proposal)

            operation = self._start_submit_operation(send)
            raw = await self._await_submit_operation(operation, 15.0)
            guard()
            data = raw.model_dump(mode="python") if isinstance(raw, AgentSteerReceipt) else raw
            receipt = AgentSteerReceipt.model_validate(data).validate_request(attempt.proposal)
            attempt.receipt = receipt
            if receipt.disposition != "created":
                raise AgentControlUnavailable
            attempt.deadline = time.monotonic() + 5.0
            scope = await self._fresh_steer(attempt)
            guard()
            # No await separates the one live direct-created check and install.
            rec.task.content_scope = scope
            rec.steer_idem[original.idempotency_key] = (digest, task_id)
            rec.pending_steer = {
                "instruction": original.instruction,
                "actor": actor,
                "content_scope": scope,
                "source_event_ids": list(original.source_event_ids),
                "source_revisions": {
                    item.event_id: item.revision for item in attempt.proposal.source_revisions
                },
                "intent_event_id": receipt.event_id,
                "intent_digest": attempt.proposal.request_digest(),
            }
            attempt.installed = True
            attempt.suppress()
            rec.resume.set()
            return rec.task.model_copy(deep=True)
        except BaseException as exc:
            # A failed read is also a continuation: completion/retirement may
            # have won while the adapter was awaiting or resisting cancellation.
            try:
                if isinstance(exc, Exception):
                    self._check_completion_open(rec, require_active=True)
                    if attempt.active:
                        guard()
            finally:
                attempt.suppress()
                if (
                    not attempt.sent or isinstance(exc, AgentModeOff)
                ) and rec.steer_attempt is attempt:
                    rec.steer_attempt = None
            raise
        finally:
            _STEER_SOURCE_GUARD.reset(token)

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
                    provider_info=self.provider_for(rec).info,
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
        self._check_completion_open(rec, require_active=True)
        if self._closed or rec.closing or rec.cancel_requested:
            raise AgentControlUnavailable
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
                "steer_event_id": pending["intent_event_id"],
                "intent_digest": pending["intent_digest"],
                "objective_revision": rec.task.objective_revision,
                "invalidated_evidence": invalidated,
            },
        )

    # -- interface: interrupt -----------------------------------------
    async def interrupt(
        self, task_id: str, request: InterruptRequest, actor: str = "owner"
    ) -> Task:
        rec = self._get(task_id)
        if (
            actor == "owner"
            and request.expected_objective_revision == rec.task.objective_revision
            and rec.steer_attempt is not None
        ):
            rec.steer_attempt.suppress()
        await self._pending_submit_control(rec, request, actor, "interrupt")
        if self._closed:
            raise ValueError("Agent is closing")
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
                provider_info=self.provider_for(rec).info,
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
        if rec.steer_attempt is not None:
            rec.steer_attempt.suppress()
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
        resource_retirement = start_resource_retirement(self)
        resource_retirement_deadline = time.monotonic() + 5
        for rec in self._tasks.values():
            if rec.steer_attempt is not None:
                rec.steer_attempt.suppress()
        for preflight in self._submit_preflights:
            preflight.retire()
        for pending in self._pending_submits.values():
            pending.suppress()
            pending.record.closing = True
            pending.record.cancel_requested = True
        for waiter in self._submit_waiters:
            if not waiter.done():
                waiter.set_result(None)
        operations = list(self._submit_operations)
        for operation in operations:
            operation.cancel()
        if operations:
            # asyncio.wait does not join resistant cancellation indefinitely.
            # Unsettled operations remain strongly owned until their callbacks run.
            await asyncio.wait(operations, timeout=1.0)
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
            worker = rec.worker
            if rec.standing_initial is None:
                rec.worker = None
            if worker is not None and not worker.done():
                worker.cancel()
                workers.append((rec, worker))
            rec.resume.set()
        for _rec, worker in workers:
            self._retain_workspace_worker(worker)
            if worker is asyncio.current_task():
                continue
            try:
                async with asyncio.timeout(5):
                    await asyncio.shield(worker)
            except (TimeoutError, asyncio.CancelledError):
                pass
        for rec in self._tasks.values():
            if rec.task.state not in TERMINAL:
                rec.task.state = TaskState.PAUSED
                rec.done.set()
        await self.authorizations.drain()
        host, controller = self.mcp_host, self.mcp_controller
        hosts = (host, *tuple(getattr(controller, "_uncertain_hosts", ())))
        for retiring in hosts:
            for actor in tuple(getattr(retiring, "_actors", {}).values()):
                self._retain_workspace_worker(actor._task, transport=True)
        self.mcp_host, self.mcp_controller = None, None
        if controller is not None:
            await controller.close()
        elif host is not None:
            await host.close()
        if self.capability_settings is not None:
            await self.capability_settings.close()
        service, self.skill_service = self.skill_service, None
        if service is not None:
            service.close()
        plugins, self.plugin_service = self.plugin_service, None
        if plugins is not None:
            await plugins.close()
        if (
            resource_retirement is not None
            and asyncio.current_task() not in self._resource_retirement_workers
            and not any(
                owner.operation is asyncio.current_task() for owner in self._resource_completions
            )
        ):
            try:
                async with asyncio.timeout(max(0, resource_retirement_deadline - time.monotonic())):
                    await asyncio.shield(resource_retirement)
            except TimeoutError:
                pass
        if self.model_settings is not None:
            await self.model_settings.close()
        artifact_store, self.artifact_store = self.artifact_store, None
        if artifact_store is not None:
            artifact_store.close()
        projects, self.projects = self.projects, None
        if projects is not None:
            projects.close()
        settings, self.runtime_settings = self.runtime_settings, None
        if settings is not None:
            settings.close()
        permissions, self.tool_permissions = self.tool_permissions, None
        if permissions is not None:
            permissions.close()
        # Keep the configured object retained even if its bounded facade
        # refuses: it owns late send/read/sole-close and transport retirement.
        try:
            if self.standing_resource_client is not None:
                await self.standing_resource_client.close()
        finally:
            if self.workspace_settings is not None:
                self._workspace_settled()
                self.workspace_settings.close()

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
                rec.task.state not in {TaskState.AWAITING_APPROVAL, TaskState.PAUSED}
                or pending is None
                or pending.get("approved")
                or rec.pending_steer is not None
            ):
                continue
            session_id = rec.task.session_id
            stale = await self._epoch_error(rec)
            if (
                stale is not None
                and self.policy.approval_state(pending["approval_id"]) != "expired"
            ):
                continue
            permit = self.policy.peek_approval(pending["approval_id"])
            if (
                rec.pending is not pending
                or rec.task.state not in {TaskState.AWAITING_APPROVAL, TaskState.PAUSED}
                or pending.get("approved")
                or rec.pending_steer is not None
                or permit is None
                or permit.used
                or self.policy.approval_state(permit.approval_id) not in {"pending", "expired"}
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
                    status="expired"
                    if self.policy.approval_state(permit.approval_id) == "expired"
                    else "pending",
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
        rec = self._approval_record(approval_id)
        async with rec.approval_lock:
            pending = rec.pending
            self._approval_binding(rec, pending, approval_id, resolution)
            if resolution.approved:
                stale = await self._epoch_error(rec)
                if stale is not None:
                    raise StaleRevision(stale)
                await self._task_scope(rec)
            self._approval_binding(rec, pending, approval_id, resolution)
            if resolution.approved:
                self._approval_action_current(rec, pending)
                if self.policy.approval_state(approval_id) == "expired":
                    await self._expire_approval(rec, pending)
                    raise ApprovalError("Approval expired; renew and review the replacement")
                self.policy.check_approval(
                    approval_id, resolution.action_digest, resolution.objective_revision
                )
            await self._append(
                rec,
                "approval.resolved",
                {
                    "approval_id": approval_id,
                    "approved": resolution.approved,
                    "objective_revision": resolution.objective_revision,
                },
            )
            self._approval_binding(rec, pending, approval_id, resolution)
            if not resolution.approved:
                self.policy.retire_approval(approval_id, "denied")
                rec.pending = None
                rec.resume.set()
                await self._fail(rec, "owner denied the requested approval")
                return {"approval_id": approval_id, "approved": False}
            if self.policy.approval_state(approval_id) == "expired":
                await self._expire_approval(rec, pending)
                raise ApprovalError("Approval expired before review confirmation; renew it")
            self.policy.check_approval(
                approval_id, resolution.action_digest, resolution.objective_revision
            )
            # Intent is not a spend. The effect broker consumes it once after
            # its final source, scope, cancellation and revision checks.
            self.policy.review_approval(
                approval_id, resolution.action_digest, resolution.objective_revision
            )
            pending["approved"] = True
            rec.resume.set()
            return {"approval_id": approval_id, "approved": True}

    def _approval_record(self, approval_id):
        for rec in self._tasks.values():
            if rec.pending is not None and rec.pending["approval_id"] == approval_id:
                return rec
        raise ApprovalError(f"unknown or superseded approval {approval_id!r}")

    def _approval_binding(self, rec, pending, approval_id, request):
        reservation = self._pending_submits.get((rec.task.session_id, rec.task.idempotency_key))
        if reservation is not None and reservation.record is rec and not rec.submit_confirmed:
            raise AgentSubmitUnconfirmed
        if rec.task.state in TERMINAL:
            raise TaskTerminal("Task already terminal; approval is moot")
        if (
            self._closed
            or rec.closing
            or rec.cancel_requested
            or rec.recovered
            or rec.completion_pending
            or rec.pending is not pending
            or pending is None
            or pending["approval_id"] != approval_id
            or rec.pending_steer is not None
            or rec.task.state not in {TaskState.AWAITING_APPROVAL, TaskState.PAUSED}
        ):
            raise ApprovalError("Approval is no longer awaiting owner review")
        if request.objective_revision != rec.task.objective_revision:
            raise StaleRevision("Approval targets a stale objective revision")
        if request.action_digest != pending["digest"]:
            raise ApprovalError("Approval digest does not match the pending action")

    def _approval_action_current(self, rec, pending):
        tool, args = pending["tool"], pending["arguments"]
        self._validate_catalog_history(rec)
        if (
            self.policy.digest_for(tool, args, rec.task.objective_revision) != pending["digest"]
            or self._permission_denies(rec, tool, args)
            or not self.tool_snapshot(rec.task).supports(tool)
        ):
            raise ApprovalError("Pending action is no longer eligible under native policy")
        original_target = pending.get("permission_target")
        if original_target is not None and tool in {"search", "mcp_call"}:
            current_target = self._permission_decision(rec, tool, args)
            if current_target is None or original_target["binding"] != current_target["binding"]:
                raise ApprovalError("Pending target binding changed; review a new action")
        if tool == "read_file":
            self.policy.resolve_path(args.get("path", ""))
            maximum = args.get("max_bytes", 65536)
            if type(maximum) is not int or not 1 <= maximum <= 262144:
                raise ApprovalError("Pending read exceeds its native bound")
        elif tool == "write_file":
            validate_write_arguments(args, rec.task.objective_revision)
            self.policy.resolve_path(args["path"])
            self.policy._deny_readonly_write(args["path"])
        elif tool == "shell":
            shell_directory(self.policy)
        elif tool == "search":
            self.policy.check_search(args.get("path"))
        elif tool == "mcp_call":
            scoped = self._model_mcp(rec)
            rows = (
                scoped.cached_tool_metadata(args.get("server_id"), tool=args.get("tool"))
                if scoped is not None
                else []
            )
            if not any(
                row["enabled"]
                and row["transport_grant"]
                and args.get("tool") in row["allowed_tools"]
                for row in rows
            ):
                raise ApprovalError("Pending MCP action is no longer owner configured")

    async def _expire_approval(self, rec, pending):
        """Called under approval_lock; expire once and retain the visible action."""
        if pending.get("status") == "expired":
            return
        self.policy.retire_approval(pending["approval_id"], "expired")
        pending["status"] = "expired"
        pending["approved"] = False
        rec.task.state = TaskState.PAUSED
        rec.task.error = "Approval expired; renew it for review or deny the action"
        try:
            await self._append(
                rec,
                "approval.resolved",
                {
                    "approval_id": pending["approval_id"],
                    "approved": False,
                    "reason": "expired",
                    "objective_revision": rec.task.objective_revision,
                },
            )
            await self._append(
                rec,
                "agent.state",
                {
                    "state": "paused",
                    "reason": "approval_expired",
                    "approval_id": pending["approval_id"],
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The metadata append may have committed. Keep native expiry and
            # the visible paused waiter; never retry an effect or infer success.
            rec.task.error = "Approval expired; journal confirmation is unavailable"

    def _reissue_receipt(self, original):
        return ApprovalReissueReceipt(
            previous_approval_id=original.approval_id,
            approval_id=original.replacement_id,
            action_digest=original.action_digest,
            objective_revision=original.objective_revision,
            expires_at=original.replacement_expires_at,
            replacement_status=self.policy.approval_state(original.replacement_id),
        ).model_dump(mode="json")

    async def reissue_approval(self, approval_id, request: ApprovalReissue, actor="owner"):
        if actor != "owner":
            raise PolicyDenied("Approval renewal requires owner")
        original = self.policy.peek_approval(approval_id)
        if original is None:
            raise ApprovalReceiptUnavailable(
                "Approval renewal receipt is unavailable; inspect current task"
            )
        if (original.action_digest, original.objective_revision) != (
            request.action_digest,
            request.objective_revision,
        ):
            raise ApprovalError("Approval renewal does not match the original action")
        if original.replacement_id is not None:
            return self._reissue_receipt(original)
        rec = self._approval_record(approval_id)
        async with rec.approval_lock:
            if original.replacement_id is not None:
                return self._reissue_receipt(original)
            pending = rec.pending
            self._approval_binding(rec, pending, approval_id, request)
            stale = await self._epoch_error(rec)
            if stale is not None:
                raise StaleRevision(stale)
            await self._task_scope(rec)
            self._approval_binding(rec, pending, approval_id, request)
            self._approval_action_current(rec, pending)
            if self.policy.approval_state(approval_id) != "expired":
                raise ApprovalError("Only an expired approval can be renewed")
            await self._expire_approval(rec, pending)
            self._approval_binding(rec, pending, approval_id, request)
            # Expiry publication yielded. Revalidate current native source and
            # disclosure before allocating the replacement, without inference.
            stale = await self._epoch_error(rec)
            if stale is not None:
                raise StaleRevision(stale)
            await self._task_scope(rec)
            self._approval_binding(rec, pending, approval_id, request)
            self._approval_action_current(rec, pending)
            replacement = self.policy.reissue_approval(
                approval_id, request.action_digest, request.objective_revision
            )
            self.authorizations.register(
                rec, replacement, pending["tool"], pending["arguments"], previous=approval_id
            )
            rec.approval_wait_id = replacement.approval_id
            rec.pending = {
                **pending,
                "approval_id": replacement.approval_id,
                "approved": False,
                "status": "pending",
            }
            rec.task.state = TaskState.AWAITING_APPROVAL
            rec.task.error = None
            try:
                await self._append(
                    rec,
                    "approval.requested",
                    {
                        "approval_id": replacement.approval_id,
                        "previous_approval_id": approval_id,
                        "tool": pending["tool"],
                        "arguments": pending["arguments"],
                        "action_digest": pending["digest"],
                        "objective_revision": request.objective_revision,
                        "expires_at": replacement.expires_at,
                        "reason": pending.get("reason", "tool_permission"),
                    },
                )
            finally:
                # Lost metadata ACK cannot erase the replacement or its timer.
                rec.resume.set()
            return self._reissue_receipt(original)

    def approval_for_task(self, task_id: str) -> str | None:
        rec = self._get(task_id)
        return rec.pending["approval_id"] if rec.pending else None

    # -- recovery ---------------------------------------------------------
    async def recover(self) -> list[Task]:
        async with self.integration_lock:
            return await self._recover()

    async def _recover(self) -> list[Task]:
        """Restore status without treating JSON history as native slot authority."""
        submissions: dict[str, JournalEntry] = {}
        after = 0
        while True:
            if self._closed:
                raise AgentSubmitUnavailable
            batch = await self.journal.read(after=after, limit=200)
            if not batch:
                break
            for event in batch:
                if event.sequence <= after:
                    raise AgentSubmitUnavailable
                after = event.sequence
                if event.kind != "agent.command" or event.payload.get("op") != "submit":
                    continue
                try:
                    entry = submit_entry(event)
                except ValueError:
                    continue
                task_id = entry.payload["task_id"]
                previous = submissions.get(task_id)
                if previous is not None and previous != entry:
                    raise AgentSubmitUnavailable
                submissions[task_id] = entry
        staged: dict[str, _Record] = {}
        staged_entries: dict[str, JournalEntry] = {}
        confirmed: dict[tuple[str, str], tuple[str, str]] = {}
        for task_id, entry in submissions.items():
            if task_id in self._tasks:
                self._check_submit_record(self._tasks[task_id], entry)
                continue
            if self._closed:
                raise AgentSubmitUnavailable
            session = await self._require_session(entry.session_id)
            current = (session.thread_id, session.epoch) == (entry.thread_id, entry.epoch)
            deadline = time.monotonic() + AGENT_SUBMIT_LOOKUP_TIMEOUT_S
            if current:
                result = await self._lookup_submit(
                    AgentSubmitLookupRequest(
                        session_id=entry.session_id,
                        thread_id=entry.thread_id,
                        epoch=entry.epoch,
                        idempotency_key=entry.payload["idempotency_key"],
                    ),
                    deadline,
                )
                if result.status != "recorded":
                    raise AgentSubmitUnavailable
                entry = result.entry
                task_id = entry.payload["task_id"]
            if task_id in self._tasks:
                self._check_submit_record(self._tasks[task_id], entry)
                continue
            previous = staged_entries.get(task_id)
            if previous is not None:
                if previous != entry:
                    raise AgentSubmitUnavailable
                continue
            operation = self._start_submit_operation(
                lambda entry=entry, deadline=deadline: self._read_submit_record(entry, deadline)
            )
            try:
                rec = await self._await_submit_operation(operation, deadline - time.monotonic())
            except asyncio.CancelledError:
                raise
            except Exception:
                raise AgentSubmitUnavailable from None
            rec.submit_confirmed = current
            staged[task_id] = rec
            staged_entries[task_id] = entry
            if current:
                confirmed[(entry.session_id, entry.payload["idempotency_key"])] = (
                    entry.payload["payload_digest"],
                    task_id,
                )
        if self._closed:
            raise AgentSubmitUnavailable
        if self.plugin_service is not None:
            await self.plugin_service.recover_pins(
                {
                    task_id: rec.plugin_versions
                    for task_id, rec in {**staged, **self._tasks}.items()
                    if rec.task.state not in TERMINAL
                }
            )
        if self._closed:
            raise AgentSubmitUnavailable
        for task_id, entry in staged_entries.items():
            if task_id in self._tasks:
                self._check_submit_record(self._tasks[task_id], entry)
        recovered = []
        for task_id, rec in staged.items():
            if task_id not in self._tasks:
                self._tasks[task_id] = rec
                recovered.append(rec.task.model_copy(deep=True))
        # No failed or partial recovery installs an authoritative cache path.
        for key, (digest, task_id) in confirmed.items():
            rec = self._tasks[task_id]
            if rec.submit_confirmed and rec.worker is None:
                self._submit_idem[key] = (digest, task_id)
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

    def _permission_decision(self, rec, tool, args=None):
        self._workspace_current(rec)
        if self.tool_permissions is None or tool not in SCOPED_BUILTINS | {"mcp_call"}:
            return None
        try:
            if tool == "mcp_call":
                if args is None:
                    return None
                return self.tool_permissions.resolve_target(
                    rec.task if rec is not None else None,
                    {
                        "kind": "mcp",
                        "server_id": args.get("server_id", ""),
                        "name": args.get("tool", ""),
                    },
                )
            return self.tool_permissions.resolve(rec.task if rec is not None else None, tool)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            raise PolicyDenied("Native scoped permission state is unavailable") from None

    def _permission_fingerprint(self, rec=None):
        if self.tool_permissions is None:
            return None
        try:
            return self.tool_permissions.fingerprint(rec.task if rec is not None else None)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            raise PolicyDenied("Native scoped permission state is unavailable") from None

    def _permission_denies(self, rec, tool, args=None):
        decision = self._permission_decision(rec, tool, args)
        return self.policy.denies(tool) or (
            decision is not None and decision["effective"] == "deny"
        )

    def _permission_asks(self, rec, tool, args=None):
        decision = self._permission_decision(rec, tool, args)
        return (
            decision["effective"] == "ask"
            if decision is not None
            else self.policy.needs_approval(tool)
        )

    def _grants(self, rec=None) -> str:
        if rec is not None and rec.standing_initial is not None:
            grants = f"write={self.policy.effective_write} deny_write={self.policy.deny_write}"
            if self.tool_permissions is not None:
                grants += " native_tool_rules=" + ",".join(
                    f"{tool}:{self._permission_decision(rec, tool)['effective']}"
                    for tool in ("read_file", "write_file")
                )
            return grants
        grants = (
            f"write={self.policy.effective_write} "
            f"shell={self.policy.effective_shell} "
            f"network={self.policy.network}"
        )
        if self.policy.deny_write or self.policy.deny_shell:
            grants += f" deny_write={self.policy.deny_write} deny_shell={self.policy.deny_shell}"
        if self.policy.shell_working_directory != "." or self.policy.shell_timeout_s != 120:
            grants += (
                f" shell_directory={self.policy.shell_working_directory!r}"
                f" shell_timeout_s={self.policy.shell_timeout_s}"
            )
        if self.tool_permissions is not None:
            grants += " native_tool_rules=" + ",".join(
                f"{tool}:{self._permission_decision(rec, tool)['effective']}"
                for tool in sorted(SCOPED_BUILTINS)
            )
        return grants

    def runtime_settings_busy(self) -> bool:
        """Terminal publication does not prove its worker or child has released."""
        return (
            self._closed
            or (
                self.capability_settings is not None
                and any(
                    (
                        self.capability_settings.operations,
                        self.capability_settings.actual_operations,
                        self.capability_settings.native_workers,
                    )
                )
            )
            or bool(self.authorizations.tasks)
            or (self.model_settings is not None and self.model_settings.catalog_cleanup_busy)
            or any(
                rec.task.state not in TERMINAL
                or (rec.worker is not None and not rec.worker.done())
                or rec.proc_slot.get("proc") is not None
                or rec.retire_lock.locked()
                for rec in self._tasks.values()
            )
        )

    def host_tool_snapshot(self, task: Task | None = None) -> NativeToolSnapshot:
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
            auxiliary_available=(
                bool(task.auxiliary_models)
                if task is not None
                else self.model_settings is not None
                and self.model_settings.auxiliary_configured(harness)
            ),
            catalog_available=True,
        )

    def tool_snapshot(self, task: Task | None = None) -> NativeToolSnapshot:
        if task is not None:
            rec = self._tasks.get(task.task_id)
            if rec is not None and rec.task is task and rec.standing_initial is not None:
                from services.agent.standing_runtime import tool_snapshot

                return tool_snapshot(self)
        snapshot = self.host_tool_snapshot(task)
        owner = self.capability_settings
        if owner is None:
            if self._capabilities_configured:
                raise PolicyDenied("Native capability availability is unavailable")
            return snapshot
        try:
            revision, disabled = owner.mask()
            return NativeToolSnapshot(
                tuple(name for name in snapshot.available_tools if name not in disabled),
                owner_disabled=disabled,
                owner_revision=revision,
            )
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            raise PolicyDenied("Native capability availability is unavailable") from None

    async def submit_standing(
        self, original: StandingExecutionIntent, *, capability: bytes
    ) -> StandingTaskAdmission:
        """Codex-authored native standing launch; historical proof never launches."""
        from services.agent.standing_runtime import submit_standing

        return await submit_standing(self, original, capability=capability)

    def _standing_action(self, rec, tool):
        if rec.standing_initial is not None:
            from services.agent.standing_runtime import check_action

            check_action(self, rec, tool)

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
            if turn.get("context_kind") not in {"tool_catalog", "tool_catalog_action"}:
                continue
            key = (
                turn.get("catalog_guard")
                or hashlib.sha256(turn["content"].encode("utf-8")).hexdigest()
            )
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

    def _mcp_server_ids(self, rec=None) -> list[str] | None:
        """Enabled owner-configured server ids for the prompt, or None.

        Only ids, never secrets, endpoints, or schemas. An unavailable
        host reports no servers rather than failing the whole turn.
        """
        host = self._model_mcp(rec)
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

    def _model_mcp(self, rec=None):
        return (
            ModelScopedMCP(self.mcp_host, self.provider_info_for(rec))
            if self.mcp_host is not None
            else None
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

    async def _complete(self, rec: _Record, tools: NativeToolSnapshot) -> str:
        """Admit one exact request, preserving whole native history exchanges.

        Counting is an external disclosure too. All selection attempts share
        one frozen native authority snapshot; a count never grants permission
        or permits a stale proposal to execute. Source/catalog dependencies
        remain task-wide even when their original exchange leaves the prompt.
        """
        provider, policy, journal = self.provider_for(rec), self.policy, self.journal
        if rec.task.model_pin is not None and rec.provider is None:
            raise PromptCountUnavailable("Recovered model pin cannot be rebound to a new provider")
        if rec.task.model_pin is not None:
            from services.agent.model_settings import AgentModelSettings

            pin = rec.task.model_pin
            if (pin.provider, pin.model, pin.local, pin.reasoning) != (
                provider.info.provider,
                provider.info.model,
                provider.info.local,
                AgentModelSettings.effort(provider),
            ):
                raise PromptCountUnavailable("Task model identity no longer matches its native pin")
        prepare = getattr(provider, "prepare_complete", None)
        if not callable(prepare):
            raise PromptCountUnavailable("Agent provider cannot measure complete requests")
        standing = rec.standing_initial is not None
        services = (self.skill_service, self.plugin_service, self.mcp_host, self.artifact_store)
        provider_info = copy.deepcopy(provider.info)
        history = [dict(turn) for turn in rec.history]
        guards = tuple(rec.catalog_guards.items())
        retained_guards = []
        if not standing and self.skill_service is not None:
            retained_guards.append(
                self.skill_service.capture_task_disclosure(
                    rec.task.task_id,
                    provider_info,
                    user_skill_id=rec.explicit_skill,
                    expected_skill_ids=frozenset(rec.used_skills),
                )
            )
        if not standing and self.plugin_service is not None:
            retained_guards.append(
                self.plugin_service.capture_task_disclosure(self._plugin_task(rec), provider_info)
            )

        def identity():
            task = rec.task
            return (
                task.task_id,
                task.session_id,
                task.thread_id,
                task.epoch,
                task.objective,
                task.objective_revision,
                task.harness_version,
                tuple(task.source_event_ids),
                tuple(sorted(rec.source_revisions.items())),
                tuple(sorted(rec.context_event_ids)),
                task.content_scope,
                rec.explicit_skill,
                frozenset(rec.used_skills),
                tuple(sorted(rec.plugin_versions.items())),
                self._grants(rec),
                self._permission_fingerprint(rec),
                policy.workspace,
                policy.protected_roots,
                policy.readonly_roots,
            )

        pinned = identity()
        revision = rec.task.objective_revision
        revised = False
        text = None
        context_observation = None
        observation_operation = self.context_observations.new_operation()

        def current():
            nonlocal revised
            self._workspace_current(rec)
            if rec.cancel_requested or rec.closing or self._closed:
                raise PolicyDenied("Agent inference stopped by native control")
            if rec.pending_steer is not None or rec.task.objective_revision != revision:
                revised = True
                raise _InferenceRevised(text)
            if (
                rec.recovered
                or rec.task.state != TaskState.RUNNING
                or self._tasks.get(rec.task.task_id) is not rec
                or self.provider_for(rec) is not provider
                or provider.info != provider_info
                or self.policy is not policy
                or self.journal is not journal
                or any(
                    actual is not original
                    for actual, original in zip(
                        (
                            self.skill_service,
                            self.plugin_service,
                            self.mcp_host,
                            self.artifact_store,
                        ),
                        services,
                        strict=True,
                    )
                )
                or identity() != pinned
                or rec.history != history
                or tuple(rec.catalog_guards.items()) != guards
                or self.tool_snapshot(rec.task) != tools
            ):
                raise PolicyDenied("Agent inference authority changed during context admission")
            if not all(guard() for guard in retained_guards):
                rec.task.content_scope = "local_only"
                raise PolicyDenied("Retained tool disclosure changed; task stopped")
            self._validate_catalog_history(rec)
            if not scope_allowed(provider.info, rec.task.content_scope):
                raise PolicyDenied("Task disclosure does not permit the configured Agent provider")
            return True

        async def check():
            current()
            if not standing and self.skill_service is not None:
                await self.skill_service.validate_task(
                    rec.task.task_id, provider.info, user_skill_id=rec.explicit_skill
                )
                current()
            if not standing and self.plugin_service is not None:
                await self.plugin_service.validate_task(self._plugin_task(rec), provider.info)
                current()
            await self._task_scope(rec)
            current()
            stale = await self._epoch_error(rec)
            current()
            if stale is not None:
                raise PolicyDenied(stale)
            # Source validation can yield after its first session read.
            session = await self.journal.session(rec.task.session_id)
            current()
            stale = self._check_task_epoch(rec, session)
            if stale is not None:
                raise PolicyDenied(stale)

        async def admit_export(purpose, request_sha256):
            from packages.contracts.agent_export import AgentExportAdmission, AgentExportRequest

            current()
            admission = getattr(
                journal, "standing_export_admission" if standing else "agent_export_admission", None
            )
            if not callable(admission):
                raise PromptCountUnavailable("Native Agent export admission is unavailable")
            request = AgentExportRequest(
                task_id=rec.task.task_id,
                objective_revision=revision,
                session_id=rec.task.session_id,
                thread_id=rec.task.thread_id,
                epoch=rec.task.epoch,
                source_event_ids=list(rec.task.source_event_ids),
                source_revisions=dict(rec.source_revisions),
                context_event_ids=sorted(rec.context_event_ids),
                content_scope=rec.task.content_scope,
                provider=provider_info.model_dump(mode="json"),
                request_sha256=request_sha256,
                purpose=purpose,
                request_nonce=new_id(),
            )
            if standing:
                from packages.contracts.standing_export import StandingExportRequest

                request = StandingExportRequest(
                    schema_version="1.0", initial=rec.standing_initial, export=request
                )
            try:
                receipt = await admission(request)
            except (asyncio.CancelledError, ProviderCleanupError):
                raise
            except Exception as exc:
                # A native reply failure can also arrive after an accepted
                # steer. Classify against current authority before changing
                # the old revision's disclosure floor or failing its task.
                current()
                rec.task.content_scope = "local_only"
                raise PromptCountUnavailable("Native Agent export admission was refused") from exc
            current()
            try:
                if standing:
                    from packages.contracts.standing_export import StandingExportAdmission

                    if type(receipt) is not StandingExportAdmission:
                        raise ValueError("Invalid native standing admission type")
                    receipt = receipt.validate_request(request).admission
                    request = request.export
                if type(receipt) is not AgentExportAdmission:
                    raise ValueError("Invalid native admission type")
                receipt.validate_request(request)
            except Exception as exc:
                rec.task.content_scope = "local_only"
                raise PromptCountUnavailable("Native Agent export admission is invalid") from exc
            rec.task.content_scope = combine_scopes(rec.task.content_scope, receipt.effective_scope)
            current()
            if (
                purpose == "complete_generation"
                and context_observation is not None
                and request_sha256 == context_observation.measurement.request_sha256
            ):
                self.context_observations.record(
                    operation_id=observation_operation,
                    phase="generation_export_admitted",
                    scope=context_observation.scope,
                    model=context_observation.model,
                    measurement=prepared.measurement,
                    selection=context_observation.selection,
                )

        messages = build_messages(
            rec.task.objective,
            revision,
            list(rec.task.source_event_ids),
            history,
            self._grants(rec),
            None if standing else self._mcp_server_ids(rec),
            not standing and self.skill_service is not None,
            not standing and self.plugin_service is not None,
            not standing and self.artifact_store is not None,
            tool_snapshot=tools,
        )
        prepared = None
        try:
            with agent_tool_scope(tools, export_guard=current, export_admission=admit_export):
                selection = await select_history(
                    messages[:2], history_exchanges(messages[2:]), prepare, check
                )
                prepared = selection.prepared
                if prepared.measurement is not None:
                    context_observation = self.context_observations.record(
                        operation_id=observation_operation,
                        phase="prepared",
                        scope={
                            "session_id": rec.task.session_id,
                            "thread_id": rec.task.thread_id,
                            "epoch": rec.task.epoch,
                            "source_event_ids": tuple(rec.task.source_event_ids),
                            "task_id": rec.task.task_id,
                            "objective_revision": revision,
                            "harness_version": rec.task.harness_version,
                            "through_sequence": None,
                        },
                        model=lambda: captured_model(provider, rec.task.model_pin),
                        measurement=prepared.measurement,
                        selection={
                            "unit": "exchange",
                            "kept": selection.kept_exchanges,
                            "dropped": selection.dropped_exchanges,
                            "examined": selection.kept_exchanges + selection.dropped_exchanges,
                            "history_complete": True,
                            "measurement_attempts": selection.measurement_attempts,
                        },
                    )
                # Stay in this Task and exact tool scope. A child task would
                # invalidate the provider's request-local admission binding.
                text = await prepared.complete()
                await check()
                return text
        except ProviderCleanupError:
            rec._cleanup_uncertain = True
            if standing:
                rec.standing_cleanup_uncertain = True
            # Unconfirmed owned cleanup cannot authorize another request,
            # including when steering happened at the same time.
            raise
        except (PromptCountUnavailable, ProviderRefusal) as exc:
            if revised:
                raise _InferenceRevised(text) from exc
            raise
        finally:
            if prepared is not None:
                try:
                    prepared.discard()
                except ProviderCleanupError:
                    rec._cleanup_uncertain = True
                    if standing:
                        rec.standing_cleanup_uncertain = True
                    raise

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
                # content already retained in this task's complete history.
                if rec.standing_initial is None and self.skill_service is not None:
                    await self.skill_service.validate_task(
                        rec.task.task_id,
                        self.provider_for(rec).info,
                        user_skill_id=rec.explicit_skill,
                    )
                if rec.standing_initial is None and self.plugin_service is not None:
                    await self.plugin_service.validate_task(
                        self._plugin_task(rec), self.provider_for(rec).info
                    )
                await self._task_scope(rec)
                self._validate_catalog_history(rec)
                tools = self.tool_snapshot(rec.task)
                try:
                    text = await self._complete(rec, tools)
                except asyncio.CancelledError:
                    raise
                except _InferenceRevised as exc:
                    await self._apply_steer(rec)
                    if exc.response is not None:
                        rec.history.append({"role": "assistant", "content": exc.response})
                    rec.history.append(
                        {
                            "role": "user",
                            "content": observation_text(
                                False,
                                "in-flight request discarded: the objective was revised "
                                "during context admission or inference; re-plan under "
                                "the current revision",
                            ),
                        }
                    )
                    continue
                except ActionDecodeError as exc:
                    await self._fail(rec, f"provider error: {exc}", decode_error=exc)
                    return
                except Exception as exc:  # provider failure is terminal, honest
                    if isinstance(exc, ProviderCleanupError):
                        rec._cleanup_uncertain = True
                    await self._fail(rec, f"provider error: {exc}")
                    return
                if rec.cancel_requested or rec.closing:
                    await self._cancel(rec)
                    return
                try:
                    action = parse_action(text)
                except ValueError as exc:
                    rec.history.append({"role": "assistant", "content": text})
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
            if isinstance(exc, ProviderCleanupError):
                rec._cleanup_uncertain = True
            if rec.completion_pending:
                self._pause_completion(rec)
            elif rec.closing:
                if rec.task.state not in TERMINAL:
                    rec.task.state = TaskState.PAUSED
                rec.done.set()
            else:
                await self._fail(rec, f"runtime error: {exc}")
        finally:
            completion = ResourceCompletion(self, rec)
            lease = completion.lease
            model_closed = lease is None
            try:
                await self._release_task_resources(rec)
                completion.recheck()
            except BaseException as exc:
                rec._cleanup_uncertain = True
                if rec.standing_initial is not None and isinstance(exc, ProviderCleanupError):
                    rec.standing_cleanup_uncertain = True
                raise
            finally:
                if lease is not None:
                    try:
                        completion.capture_cleanup()
                        await lease.close()
                        completion.recheck()
                    except ProviderCleanupError:
                        rec._cleanup_uncertain = True
                        if rec.standing_initial is not None:
                            rec.standing_cleanup_uncertain = True
                        # Work evidence is not rewritten by unconfirmed close.
                        # The native lease remains busy and retains capacity.
                        pass
                    except BaseException:
                        rec._cleanup_uncertain = True
                        raise
                    else:
                        model_closed = True
            if model_closed and not rec._cleanup_uncertain and not rec.standing_cleanup_uncertain:
                completion.arm()

    async def _release_task_resources(self, rec):
        completion = rec.resource_completion
        if rec.standing_initial is None and self.skill_service is not None:
            await self.skill_service.release_task(rec.task.task_id)
            if isinstance(completion, ResourceCompletion):
                completion.recheck()
        if rec.standing_initial is None and self.plugin_service is not None:
            await self.plugin_service.release_task(rec.task.task_id)
            if isinstance(completion, ResourceCompletion):
                completion.recheck()

    async def _notify_task_resources(self, rec):
        # This is only a waiter for the original operation, never a retry or
        # another source of restoration authority.
        owner = rec.resource_completion
        if (
            owner is not None
            and owner.operation is not None
            and asyncio.current_task() not in (owner.worker, owner.operation)
        ):
            await asyncio.shield(owner.operation)

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
        self._standing_action(rec, "finish")
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
        self._standing_action(rec, tool)
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
                    rec.history.append({"role": "assistant", "content": raw})
                rec.history.append({"role": "user", "content": observation_text(False, str(exc))})
                return
        digest = self.policy.digest_for(tool, args, revision)
        if tool == "mcp_call" and self.mcp_host is not None:
            self._model_mcp(rec)._check(args.get("server_id", ""))
        sources = (
            await self._uncommitted_speech_sources(rec)
            if not self._permission_denies(rec, tool, args)
            and tool in {"write_file", "shell", "mcp_call", "publish_ui"}
            else []
        )
        if await self._discard_revised_action(rec, revision, raw):
            return
        self._validate_catalog_history(rec)
        if sources:
            await self._await_approval(rec, tool, args, digest, raw, reason="uncommitted_speech")
            return
        if tool in SCOPED_BUILTINS | {"mcp_call"} and self._permission_asks(rec, tool, args):
            await self._await_approval(rec, tool, args, digest, raw)
            return
        if (
            tool == "mcp_call"
            and self.tool_permissions is None
            and self.mcp_host is not None
            and not self.policy.yolo
        ):
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
        rec.history.append({"role": "assistant", "content": raw})
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
        approval = self.policy.create_approval(digest, rec.task.objective_revision, retained=True)
        rec.approval_wait_id = approval.approval_id
        rec.pending = {
            "approval_id": approval.approval_id,
            "digest": digest,
            "tool": tool,
            "arguments": args,
            "approved": False,
            "reason": reason,
            "status": "pending",
            "permission_target": self._permission_decision(rec, tool, args),
        }
        # Clear before publishing: an owner can resolve the durable request while
        # the append HTTP response is still in flight. Never erase that signal.
        rec.resume.clear()
        rec.task.state = TaskState.AWAITING_APPROVAL
        try:
            self.authorizations.register(rec, approval, tool, args)
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
                # Recheck at most one second later even after a wall-clock
                # adjustment. This timer owns no model, journal or effect call.
                try:
                    await asyncio.wait_for(rec.resume.wait(), timeout=1)
                except TimeoutError:
                    pass
                rec.resume.clear()
                if rec.cancel_requested or rec.closing:
                    await self._cancel(rec)
                    return
                if rec.task.state in TERMINAL:
                    return
                if rec.pending_steer is not None:
                    rec.pending = None
                    rec.history.append({"role": "assistant", "content": raw})
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
                async with rec.approval_lock:
                    pending = rec.pending
                    if pending is None:
                        return
                    current_id = pending["approval_id"]
                    state = self.policy.approval_state(current_id)
                    if state == "expired":
                        await self._expire_approval(rec, pending)
                        continue
                    if state != "pending":
                        raise ApprovalError("Waiting approval is no longer available")
                    if not pending.get("approved"):
                        continue
                    permit = self.policy.peek_approval(current_id)
                    if rec.task.objective_revision != permit.objective_revision:
                        raise ApprovalError("Approval went stale; no action was executed")
                    rec.task.state = TaskState.RUNNING
                try:
                    await self._dispatch(rec, tool, args, digest, raw, approval_id=current_id)
                except ApprovalError:
                    # A supplied permit is spent before native effects. If it
                    # expired during async source validation, keep this exact
                    # action paused rather than asking the model to retry it.
                    if (
                        self.policy.approval_state(current_id) != "expired"
                        or rec.cancel_requested
                        or rec.closing
                        or rec.pending_steer is not None
                        or rec.task.objective_revision != permit.objective_revision
                    ):
                        raise
                    if rec.history and rec.history[-1] == {"role": "assistant", "content": raw}:
                        rec.history.pop()
                    async with rec.approval_lock:
                        if rec.pending is not None and rec.pending is not pending:
                            raise ApprovalError("Pending approval changed before expiry") from None
                        rec.pending = pending
                        await self._expire_approval(rec, pending)
                    continue
                return
        finally:
            # Includes denial, steering, task/worker cancellation, publication
            # failures and real dispatch cleanup. Reissue updates this one slot;
            # an unbounded list of previous permits is never kept by the task.
            current_id, rec.approval_wait_id = rec.approval_wait_id, None
            if current_id is not None:
                self.policy.release_approval(current_id)
            if rec.pending is not None and rec.pending["approval_id"] == current_id:
                rec.pending = None

    async def _run_scoped_builtin(
        self, rec, tool, args, digest, revision, approval_id, *, reviewed, speech_review, audit
    ):
        self._standing_action(rec, tool)
        policy = self.policy
        if tool == "read_file":
            arguments = {"path": args.get("path", ""), "max_bytes": args.get("max_bytes", 65536)}
        elif tool == "write_file":
            arguments = {"path": args["path"], "content": args["content"]}
        elif tool == "search":
            arguments = {
                "pattern": str(args.get("pattern", "")),
                "path": args.get("path"),
                "max_results": 50,
            }
        elif tool == "mcp_call":
            arguments = {
                "server_id": args.get("server_id", ""),
                "tool": args.get("tool", ""),
                "arguments": copy.deepcopy(args.get("arguments", {})),
            }
        else:
            arguments = {
                "command": str(args.get("command", "")),
                "timeout_s": float(args.get("timeout_s", 60)),
            }

        async def invoke():
            self._standing_action(rec, tool)
            if tool == "read_file":
                return run_read(policy, arguments["path"], arguments["max_bytes"], revision)
            if tool == "write_file":
                return run_write(
                    policy,
                    arguments["path"],
                    arguments["content"],
                    revision,
                    approval_id=approval_id,
                    digest=digest,
                )
            if tool == "search":
                return run_search(
                    policy,
                    arguments["pattern"],
                    arguments["path"],
                    revision,
                    arguments["max_results"],
                    approval_id=approval_id,
                    digest=digest,
                )
            if tool == "mcp_call":
                return await run_mcp_call(
                    self._model_mcp(rec),
                    policy,
                    arguments["server_id"],
                    arguments["tool"],
                    arguments["arguments"],
                    revision,
                    approval_id=approval_id,
                    digest=digest,
                    admission=mcp_admission,
                )
            return await run_shell(
                policy,
                arguments["command"],
                arguments["timeout_s"],
                revision,
                approval_id=approval_id,
                digest=digest,
                proc_slot=rec.proc_slot,
            )

        permissions = self.tool_permissions
        mcp_admission = None
        captured_mcp_host = self.mcp_host
        if permissions is None:
            return await invoke()
        frozen_args = copy.deepcopy(args)
        fingerprint = self._permission_fingerprint(rec)
        decision = self._permission_decision(rec, tool, args)

        def current():
            if (
                self._closed
                or rec.closing
                or rec.cancel_requested
                or rec.recovered
                or not rec.submit_confirmed
                or rec.completion_pending
                or rec.pending_steer is not None
                or rec.task.state != TaskState.RUNNING
                or self._tasks.get(rec.task.task_id) is not rec
                or rec.task.objective_revision != revision
                or self.policy is not policy
                or self.tool_permissions is not permissions
                or args != frozen_args
                or policy.digest_for(tool, args, revision) != digest
                or self._permission_fingerprint(rec) != fingerprint
                or self._permission_decision(rec, tool, args) != decision
                or not self.tool_snapshot(rec.task).supports(tool)
            ):
                raise PolicyDenied("Native action authority changed before admission")
            if tool == "mcp_call":
                if self.mcp_host is not captured_mcp_host:
                    raise PolicyDenied("Native MCP host changed before admission")
                self._model_mcp(rec)._check(arguments["server_id"])
                self._validate_catalog_history(rec)

        if tool == "mcp_call":

            async def async_current():
                await self._task_scope(rec, persist=False)
                stale = await self._epoch_error(rec)
                if stale is not None:
                    raise PolicyDenied("MCP source authority changed before admission")
                sources_now = await self._uncommitted_speech_sources(rec)
                if bool(sources_now) != speech_review:
                    raise PolicyDenied("MCP speech review authority changed before admission")
                stale = await self._epoch_error(rec)
                if stale is not None:
                    raise PolicyDenied("MCP session authority changed before admission")

            mcp_admission = MCPToolAdmission(
                policy=policy,
                tool=tool,
                arguments=frozen_args,
                broker_arguments=arguments,
                digest=digest,
                revision=revision,
                decision=decision,
                current=current,
                approval_id=approval_id,
                reviewed=reviewed,
                speech_review=speech_review,
                async_current=async_current,
            )
            try:
                return await invoke()
            finally:
                mcp_admission.active = False
                audit.update(mcp_admission.provenance)

        with native_tool_admission(
            policy=policy,
            tool=tool,
            arguments=frozen_args,
            broker_arguments={**arguments, "objective_revision": revision},
            digest=digest,
            revision=revision,
            decision=decision,
            current=current,
            approval_id=approval_id,
            reviewed=reviewed,
            speech_review=speech_review,
        ) as admission:
            try:
                return await invoke()
            finally:
                audit.update(admission.provenance)

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
        self._standing_action(rec, tool)
        await self._task_scope(rec)
        revision = rec.task.objective_revision
        stale = await self._epoch_error(rec)
        if stale is not None:
            await self._fail(rec, stale)
            return
        sources = (
            await self._uncommitted_speech_sources(rec)
            if not self._permission_denies(rec, tool, args)
            and tool in {"write_file", "shell", "mcp_call", "publish_ui"}
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
                    rec.history.append({"role": "assistant", "content": raw})
                rec.history.append({"role": "user", "content": observation_text(False, str(exc))})
                return
        if approval_id is not None:
            self.policy.check_approval(approval_id, digest, revision)
        pending_review = rec.pending
        reviewed = bool(
            approval_id is not None
            and pending_review is not None
            and pending_review.get("approved")
            and (pending_review["approval_id"], pending_review["digest"], pending_review["tool"])
            == (approval_id, digest, tool)
        )
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
                self.tool_permissions is None
                and (
                    tool == "write_file"
                    and self.policy.effective_write
                    or tool == "shell"
                    and self.policy.effective_shell
                )
            ):
                self.policy.consume_approval(approval_id, digest, revision)
        if raw and tool not in {"publish_ui", "tool_catalog"}:
            rec.history.append({"role": "assistant", "content": raw})
        if approval_id is not None:
            # One-use intent is now committed to this exact execution.
            rec.pending = None
        permission_admission = {}
        try:
            if self._permission_denies(rec, tool, args):
                raise PolicyDenied("Native action is disabled by owner policy")
            if not self.tool_snapshot(rec.task).supports(tool):
                raise ToolFailure(f"Tool {tool!r} is unavailable for this task")
            if tool == "read_transcript":
                out, evidence = await self._read_transcript(rec, args)
            elif tool == "select_context":
                from services.agent.context import select_context

                out, evidence = await select_context(self, rec.task, args)
            elif tool in SCOPED_BUILTINS | {"mcp_call"}:
                out, evidence = await self._run_scoped_builtin(
                    rec,
                    tool,
                    args,
                    digest,
                    revision,
                    approval_id,
                    reviewed=reviewed,
                    speech_review=bool(sources),
                    audit=permission_admission,
                )
            elif tool == "mcp_list":
                out, evidence = await run_mcp_list(
                    self._model_mcp(rec),
                    args.get("server_id"),
                    revision,
                    after=args.get("after", 0),
                    tool=args.get("tool"),
                )
            elif tool in {"skills_list", "skill_load"}:
                out, evidence = await self._skill_result(rec, tool, args)
            elif tool in {"plugins_list", "plugin_call"}:
                out, evidence = await self._plugin_result(rec, tool, args)
            elif tool == "publish_ui":
                out, evidence = await self._publish_ui(rec, args)
            elif tool == "consult_model":
                from services.agent.auxiliary import consult_model

                out, evidence = await consult_model(self, rec, args)
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
                {
                    "tool": tool,
                    "ok": False,
                    "evidence": exc.evidence.model_dump(mode="json"),
                    **(
                        {"permission_admission": permission_admission}
                        if permission_admission
                        else {}
                    ),
                },
            )
            rec.last_tool_ok = False
            self._effect_result(rec, tool, ok=False)
            if tool == "plugin_call":
                rec.failed_plugin = True
            rec.history.append({"role": "user", "content": observation_text(False, str(exc))})
            return
        except (ToolFailure, PolicyDenied, OSError) as exc:
            if isinstance(exc, ApprovalError) and approval_id is not None:
                raise
            rec.last_tool_ok = False
            self._effect_result(rec, tool, ok=False)
            if tool == "plugin_call":
                rec.failed_plugin = True
            if permission_admission:
                await self._append(
                    rec,
                    "agent.tool",
                    {
                        "tool": tool,
                        "ok": False,
                        "permission_admission": permission_admission,
                        "admission_only": True,
                        "effect_state": "not_established",
                    },
                )
            rec.history.append({"role": "user", "content": observation_text(False, str(exc))})
            return
        rec.task.evidence.append(evidence)
        if tool in {
            "mcp_list",
            "mcp_call",
            "skills_list",
            "skill_load",
            "plugins_list",
            "plugin_call",
        }:
            rec.foreign_context = True
        try:
            await self._append(
                rec,
                "agent.tool",
                {
                    "tool": tool,
                    "ok": True,
                    "evidence": evidence.model_dump(mode="json"),
                    **(
                        {"permission_admission": permission_admission}
                        if permission_admission
                        else {}
                    ),
                },
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
                # Keep the executed logical action as well as its complete
                # result. A real typed trial repeated inspection when that
                # action trace was omitted. Neither unit is clipped.
                action = encode_catalog({"tool": tool, "arguments": args}, max_bytes=8192)
                rec.history.append(
                    {
                        "role": "assistant",
                        "content": action,
                        "context_kind": "tool_catalog_action",
                        "catalog_guard": key,
                    }
                )
                rec.history.append(
                    {
                        "role": "user",
                        "content": out,
                        "context_kind": "tool_catalog",
                        "catalog_guard": key,
                    }
                )
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
        self._standing_action(rec, "publish_ui")
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
        self._standing_action(rec, tool)
        service = self.plugin_service
        if service is None:
            raise ToolFailure("No compute-plugin host configured")
        try:
            if tool == "plugins_list":
                if set(args) - {"plugin_id", "after"}:
                    raise ValueError("plugins_list accepts only plugin_id and after")
                result = await service.catalog(self.provider_for(rec).info, **args)
                evidence_kind = "external"
            else:
                if set(args) != {"plugin_id", "arguments"}:
                    raise ValueError("plugin_call requires only plugin_id and arguments")
                pinned_task = self._plugin_task(rec)
                version_id, changed = await service.pin_task(
                    pinned_task, args["plugin_id"], self.provider_for(rec).info
                )
                if changed:
                    await self._append(
                        rec,
                        "plugin.pinned",
                        {"plugin_id": args["plugin_id"], "version_id": version_id},
                    )
                result = await service.call_task(
                    pinned_task, args["plugin_id"], args["arguments"], self.provider_for(rec).info
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
        self._standing_action(rec, tool)
        if self.skill_service is None:
            raise ToolFailure("No skill controller configured")
        try:
            if tool == "skills_list":
                if set(args) - {"after"}:
                    raise ValueError("skills_list accepts only after")
                result = await self.skill_service.catalog(
                    self.provider_for(rec).info, after=args.get("after", 0)
                )
            else:
                if set(args) - {"skill_id", "resource"}:
                    raise ValueError("skill_load accepts only skill_id and resource")
                skill_id = args.get("skill_id")
                result = await self.skill_service.load(
                    rec.task.task_id,
                    skill_id,
                    self.provider_for(rec).info,
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
        from services.agent.transcript_pages import read_transcript

        return await read_transcript(self, rec, args)
