"""Codex-authored one-use standing launch into the existing Agent worker.

Native proof is knowledge. Only this process's timely direct created reply and
still-owned transient capability can consume the private launch latch.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from dataclasses import dataclass, field

from core.policy import PolicyDenied
from packages.contracts.agent_submit import AgentSubmitUnavailable, AgentSubmitUnconfirmed
from packages.contracts.agent_tools import NATIVE_TOOL_ORDER, NativeToolSnapshot
from packages.contracts.models import AgentModelPin, Session, Task, TaskState
from packages.contracts.standing_admission import StandingInitialProof, StandingTaskAdmission
from packages.contracts.standing_execution import (
    StandingExecutionIntent,
    StandingExecutionModelPin,
    capability_digest,
    claim_id,
    occurrence_id,
)
from packages.contracts.standing_work import _tree
from packages.providers.disclosure import scope_allowed
from packages.providers.text import ProviderCleanupError
from services.agent.task_model_lease import TaskModelLease
from services.agent.workspace_ownership import captured_context

MAX_STANDING_PENDING = 64
STANDING_ADMISSION_TIMEOUT_S = 25.0
_TOOLS = frozenset({"read_file", "write_file", "finish"})


def tool_snapshot(runtime) -> NativeToolSnapshot:
    """Intersect a task restriction with the actual independent owner mask."""
    owner = runtime.capability_settings
    if owner is None:
        if runtime._capabilities_configured:
            raise PolicyDenied("Standing native tools are unavailable")
        disabled, revision = None, None
    else:
        try:
            revision, disabled = owner.mask()
        except Exception:
            raise PolicyDenied("Standing native tools are unavailable") from None
    return NativeToolSnapshot(
        tuple(
            name for name in NATIVE_TOOL_ORDER if name in _TOOLS and name not in (disabled or ())
        ),
        owner_disabled=disabled,
        owner_revision=revision,
        task_subset="native-files-v1",
    )


def check_action(runtime, rec, tool):
    if (
        runtime._tasks.get(rec.task.task_id) is not rec
        or runtime._closed
        or rec.closing
        or rec.cancel_requested
        or rec.recovered
        or not rec.submit_confirmed
        or rec.task.state != TaskState.RUNNING
        or rec.standing_initial is None
        or tool not in _TOOLS
        or not tool_snapshot(runtime).supports(tool)
    ):
        raise PolicyDenied("Standing native action is unavailable")


def _pin(pin):
    _tree(pin)
    return StandingExecutionModelPin.model_validate(dict(pin.__dict__))


def _authority(runtime, rec):
    policy = runtime.policy
    return (
        policy,
        runtime.workspace_settings,
        runtime.provider,
        policy.workspace,
        policy.protected_roots,
        policy.readonly_roots,
        policy.effective_write,
        policy.effective_shell,
        policy.network,
        policy.deny_write,
        policy.deny_shell,
        runtime.tool_permissions,
        runtime._permission_fingerprint(rec),
        runtime.capability_settings,
        tool_snapshot(runtime),
    )


@dataclass(eq=False)
class _Attempt:
    runtime: object
    original: StandingExecutionIntent
    deadline: float
    capability: bytes | None = field(repr=False)
    journal: object = None
    suppressed: asyncio.Future = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    key: str = ""
    active: bool = True
    sent: bool = False
    launched: bool = False
    resolved: bool = False
    record: object = None
    context: object = None
    model_owner: object = None
    lease: TaskModelLease | None = None
    authority: tuple | None = None

    def suppress(self):
        self.active = False
        self.capability = None
        if not self.suppressed.done():
            self.suppressed.set_result(None)
        if not self.launched and self.lease is not None:
            self.lease.start_close()

    def retire(self):
        self.suppress()

    def guard(self):
        runtime, task = self.runtime, self.original.task
        current = asyncio.current_task()
        if (
            not self.active
            or runtime._closed
            or runtime.journal is not self.journal
            or time.monotonic() >= self.deadline
            or current is not None
            and current.cancelling()
            or self.capability is None
        ):
            self.suppress()
            raise AgentSubmitUnavailable
        if self.key and (
            runtime._standing_pending.get(self.key) is not self
            or task.task_id in runtime._tasks
            or any(
                attempt is not self and attempt.original.task.task_id == task.task_id
                for attempt in runtime._standing_pending.values()
            )
        ):
            self.suppress()
            raise AgentSubmitUnconfirmed

    def fresh(self):
        self.guard()
        runtime, original = self.runtime, self.original
        if (
            captured_context(runtime) is not self.context
            or self.context is None
            or not self.context.current()
            or self.context.pin != original.task.workspace_pin
            or runtime.model_settings is not self.model_owner
            or runtime.session_harness(original.task.harness_version)
            != original.task.harness_version
            or type(self.lease) is not TaskModelLease
            or self.lease not in self.model_owner.catalog_leases
            or self.lease.cleanup is not None
            or self.lease.failed
            or self.lease.settled
            or self.lease.adapters != [self.record.provider]
            or self.lease.auxiliary_routes
            or _pin(self.model_owner.selection_info(original.task.harness_version)[1])
            != original.task.model_pin
            or _pin(self.record.task.model_pin) != original.task.model_pin
            or not scope_allowed(self.record.provider.info, original.task.content_scope)
            or _authority(runtime, self.record) != self.authority
        ):
            raise PolicyDenied("Standing launch authority changed")
        info = self.record.provider.info
        pin = original.task.model_pin
        if (
            info.provider,
            info.model,
            info.local,
            self.model_owner.effort(self.record.provider),
        ) != (pin.provider, pin.model, pin.local, pin.reasoning):
            raise PolicyDenied("Standing model identity changed")

    async def session(self):
        self.guard()
        original = self.original.task
        session = await self.journal.session(original.task_session.session_id)
        self.guard()
        if (
            type(session) is not Session
            or type(session.epoch) is not int
            or session.channel != "agent"
            or (session.session_id, session.thread_id, session.epoch)
            != (
                original.task_session.session_id,
                original.task_session.thread_id,
                original.task_session.epoch,
            )
            or self.runtime.session_harness(session.harness_version) != original.harness_version
        ):
            raise PolicyDenied("Standing session authority changed")

    async def run(self):
        from services.agent.runtime import _Record

        runtime, original = self.runtime, self.original
        try:
            self.guard()
            lookup = getattr(self.journal, "standing_task_lookup", None)
            if not callable(lookup):
                raise AgentSubmitUnavailable
            proof = await lookup(original)
            self.guard()
            if proof is not None:
                if type(proof) is not StandingInitialProof:
                    raise AgentSubmitUnavailable
                proof = proof.validate_request(original)
                self.resolved = True
                return StandingTaskAdmission(
                    schema_version="1.0", disposition="retained", proof=proof
                )
            key = claim_id(original)
            if (
                len(runtime._standing_pending) >= MAX_STANDING_PENDING
                or key in runtime._standing_pending
                or original.task.task_id in runtime._tasks
                or any(
                    p.original.task.task_id == original.task.task_id
                    for p in runtime._standing_pending.values()
                )
            ):
                raise AgentSubmitUnconfirmed
            self.key = key
            runtime._standing_pending[key] = self
            await self.session()
            async with runtime.integration_lock:
                self.guard()
                self.context = captured_context(runtime)
                if self.context is None or self.context.pin != original.task.workspace_pin:
                    raise PolicyDenied("Standing workspace authority is unavailable")
                owner = self.model_owner = runtime.model_settings
                if (
                    owner is None
                    or _pin(owner.selection_info(original.task.harness_version)[1])
                    != original.task.model_pin
                ):
                    raise PolicyDenied("Standing model authority is unavailable")
                owner._active_primary = runtime.provider
                provider, pin, auxiliary, pins, lease = owner.task_selection(
                    original.task.harness_version,
                    original.task.content_scope,
                    owned_primary=True,
                    include_auxiliary=False,
                )
                self.lease = lease
                if (
                    type(lease) is not TaskModelLease
                    or lease.adapters != [provider]
                    or auxiliary
                    or pins
                    or lease.auxiliary_routes
                    or provider is runtime.provider
                    or getattr(provider, "provider", provider)
                    is getattr(runtime.provider, "provider", runtime.provider)
                    or _pin(pin) != original.task.model_pin
                ):
                    raise PolicyDenied("Standing owned model capture is unavailable")
                task = original.task
                session = task.task_session
                self.record = _Record(
                    Task(
                        task_id=task.task_id,
                        session_id=session.session_id,
                        thread_id=session.thread_id,
                        epoch=session.epoch,
                        objective=task.objective,
                        content_scope=task.content_scope,
                        idempotency_key="standing:" + occurrence_id(original),
                        harness_version=task.harness_version,
                        workspace_pin=task.workspace_pin,
                        model_pin=AgentModelPin.model_validate(dict(pin.__dict__)),
                        source_event_ids=[],
                        auxiliary_models={},
                        state=TaskState.PAUSED,
                    ),
                    provider=provider,
                    model_lease=lease,
                    workspace_context=self.context,
                )
                self.authority = _authority(runtime, self.record)
                self.fresh()
            await self.session()
            self.fresh()
            admit = getattr(self.journal, "standing_admit", None)
            if not callable(admit):
                raise AgentSubmitUnavailable
            self.sent = True
            result = await admit(original, capability=self.capability)
            self.guard()
            if type(result) is not StandingTaskAdmission:
                raise AgentSubmitUnavailable
            result = result.validate_request(original)
            self.resolved = True
            if result.disposition == "retained":
                return result
            self.fresh()
            await self.session()
            self.fresh()
            # No await from this final check through one-use latch consumption
            # and installation. A late reply cannot reach this point.
            rec = self.record
            rec.standing_initial = result.proof
            rec.submit_confirmed = True
            rec.submit_sequence = result.proof.sequence
            rec.submit_identity = result.proof.entry_digest
            rec.journal_seq = 1
            rec.task.state = TaskState.ACCEPTED
            self.active = False
            self.capability = None
            self.launched = True
            runtime._tasks[rec.task.task_id] = rec
            rec.worker = asyncio.create_task(runtime._run(rec))
            return result
        except asyncio.CancelledError:
            self.suppress()
            raise
        except (PolicyDenied, AgentSubmitUnavailable, AgentSubmitUnconfirmed):
            self.suppress()
            raise
        except Exception:
            self.suppress()
            raise AgentSubmitUnavailable from None
        finally:
            if not self.launched:
                if self.resolved:
                    self.active = False
                    self.capability = None
                else:
                    self.suppress()
                if self.lease is not None:
                    try:
                        await self.lease.close()
                    except ProviderCleanupError:
                        if self.record is not None:
                            self.record.standing_cleanup_uncertain = True
            runtime._submit_preflights.discard(self)
            # Unconfirmed sent work retains its reservation. It is not absence
            # and this increment has no native claim-release operation.
            if self.key and (self.launched or self.resolved or not self.sent):
                if runtime._standing_pending.get(self.key) is self:
                    runtime._standing_pending.pop(self.key)


async def submit_standing(runtime, original, *, capability: bytes) -> StandingTaskAdmission:
    deadline = time.monotonic() + STANDING_ADMISSION_TIMEOUT_S
    try:
        if (
            type(original) is not StandingExecutionIntent
            or type(capability) is not bytes
            or len(capability) != 32
        ):
            raise ValueError
        original = StandingExecutionIntent.from_json(original.canonical_bytes())
        if not hmac.compare_digest(
            capability_digest(
                original.expected_backend,
                original.idempotency_key,
                original.executor_id,
                capability,
            ),
            original.capability_sha256,
        ):
            raise ValueError
    except Exception:
        raise PolicyDenied("Standing launch request is invalid") from None
    attempt = _Attempt(runtime, original, deadline, capability, journal=runtime.journal)
    runtime._submit_preflights.add(attempt)
    try:
        operation = runtime._start_submit_operation(attempt.run)
    except BaseException:
        attempt.suppress()
        runtime._submit_preflights.discard(attempt)
        raise
    # A task cancelled before its coroutine first runs never enters run's
    # finally. Retire its entry ownership at actual task settlement too.
    operation.add_done_callback(lambda _: runtime._submit_preflights.discard(attempt))
    try:
        return await runtime._await_submit_operation(
            operation, max(0, deadline - time.monotonic()), attempt
        )
    except asyncio.CancelledError:
        attempt.suppress()
        raise
    except Exception:
        attempt.suppress()
        raise
