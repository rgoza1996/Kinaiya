"""Original claim retirement and dependent client/legacy-notification owners.

Local cleanup alone is not physical Resources proof. Claim retirement consumes
the genuine complete native report; neither owner implements Task settlement.
"""

import asyncio
import json

from core.policy import PolicyDenied
from packages.contracts.agent_submit import AgentSubmitUnavailable
from packages.contracts.models import TaskState
from packages.contracts.operation_ownership import ActualOperation, actual_owner
from packages.contracts.standing_admission import StandingTaskAdmission
from packages.contracts.standing_closure import StandingAdmissionClosure
from packages.contracts.standing_execution import StandingExecutionClaim, claim_id
from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingReceipt,
    StandingResourceFinalizationReceipt,
    StandingResourceFinalizeContext,
    StandingResourceFinalizeRequest,
    checked_finalization,
    finalization_bytes,
)
from packages.contracts.standing_work import StandingUnavailable
from services.agent.task_model_lease import TaskModelLease

_TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}


class StandingClaimOwner:
    """Original current-process work and a separate, closure-only finalizer."""

    def __init__(self, runtime, original, capability):
        dependencies = list(runtime._standing_dependencies)
        for client in (runtime.journal, runtime.standing_resource_client):
            if client is not None and not any(old is client for old in dependencies):
                dependencies.append(client)
        if runtime._closed or len(runtime._standing_claims) >= 64 or len(dependencies) > 128:
            raise AgentSubmitUnavailable
        self.runtime, self.original = runtime, original
        self.journal, self.resources = runtime.journal, runtime.standing_resource_client
        self.authority = (
            runtime.policy,
            runtime.workspace_settings,
            runtime.model_settings,
            runtime.tool_permissions,
            runtime.capability_settings,
        )
        self._closure_capability = capability
        self.scope = ActualOperation(lambda _: runtime._workspace_settled())
        self.scope.standing_claim = self
        self._root = object()
        self.scope.hold(self._root)
        self.final_scope = ActualOperation(lambda _: runtime._workspace_settled())
        self._final_root = object()
        self.final_scope.hold(self._final_root)
        self.preflight = self.worker = self.operation = None
        self.attempt = self.record = self.lease = None
        self.claim = self.binding = self.created = self.closure = self.receipt = None
        self.request = None
        self.bind_operation = None
        self.binding_started = False
        self.foreign_initial = False
        self.fenced = self.failed = False
        self.work_settled = False
        runtime._standing_claims.add(self)
        for client in (self.journal, self.resources):
            if client is not None and not any(
                old is client for old in runtime._standing_dependencies
            ):
                runtime._standing_dependencies.append(client)

    def guard(self):
        if (
            self.fenced
            or self.failed
            or self.scope.failed
            or self.runtime._closed
            or self.runtime.journal is not self.journal
            or self.runtime.standing_resource_client is not self.resources
            or any(
                current is not original
                for current, original in zip(
                    (
                        self.runtime.policy,
                        self.runtime.workspace_settings,
                        self.runtime.model_settings,
                        self.runtime.tool_permissions,
                        self.runtime.capability_settings,
                    ),
                    self.authority,
                    strict=True,
                )
            )
        ):
            self.fence()
            raise PolicyDenied("Standing claim work is permanently fenced")

    def fail(self):
        self.failed = True
        self.runtime._workspace_cleanup_failed = True

    def fence(self):
        self.fenced = True
        if self.attempt is not None:
            self.attempt.suppress()

    def retain_preflight(self, task):
        self.preflight = task
        self.scope.retain_task(task)
        task.add_done_callback(self._work_done)

    def attach_record(self, record, lease):
        self.record, self.lease = record, lease
        record.standing_owner = self
        lease.retain(self.scope)

    def retain_worker(self, task):
        self.worker = task
        self.scope.retain_task(task)
        task.add_done_callback(self._work_done)

    def _work_done(self, task):
        if task is self.worker and (task.cancelled() or task.exception() is not None):
            self.fail()
        if (
            self.preflight is not None
            and self.preflight.done()
            and (self.worker is None or self.worker.done())
        ):
            self.start_retirement()

    async def bind(self, claim):
        self.claim = checked_finalization(claim, StandingExecutionClaim)
        if finalization_bytes(self.claim.request) != finalization_bytes(self.original):
            raise StandingUnavailable
        self.binding_started = True

        async def original():
            receipt = await self.resources.bind_standing_claim(self.claim)
            self.binding = checked_finalization(
                receipt, StandingResourceBindingReceipt
            ).validate_claim(self.claim)
            return self.binding

        self.bind_operation = self._child(original)
        return await asyncio.shield(self.bind_operation)

    async def admit(self, capability):
        async def original():
            result = await self.journal.standing_admit(self.original, capability=capability)
            if type(result) is not StandingTaskAdmission:
                raise StandingUnavailable
            result = result.validate_request(self.original)
            if result.disposition == "created":
                self.created = result.proof
            else:
                # This process did not create/own the foreign accepted worker.
                self.foreign_initial = True
            return result

        return await asyncio.shield(self._child(original))

    def _child(self, invoke, *, cleanup=False):
        if self.scope.finished:
            raise StandingUnavailable
        with actual_owner(self.scope):
            task = asyncio.create_task(invoke())
        self.scope.retain_task(task, cleanup=cleanup)
        return task

    async def control(self, invoke):
        self.guard()
        if len(self.scope.handles) >= 64:
            raise StandingUnavailable
        task = self._child(invoke)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Request cancellation, retaining the actual child and its late
            # cleanup. Never enroll the HTTP/test caller in the joined scope.
            task.cancel()
            raise

    def start_retirement(self):
        if self.operation is not None:
            return self.operation
        self.fence()
        # Keep the work root until the original creation paths actually end.
        # A separate coroutine never joins its own operation or HTTP exchanges.
        with actual_owner(self.final_scope):
            self.operation = asyncio.create_task(self._retire())
        self.operation.add_done_callback(self._retired)
        self.runtime._workspace_settled()
        return self.operation

    async def _settle_work(self):
        if self.work_settled:
            return
        if self.preflight is not None:
            await asyncio.gather(asyncio.shield(self.preflight), return_exceptions=True)
        if self.worker is not None:
            await asyncio.gather(asyncio.shield(self.worker), return_exceptions=True)
        if self.lease is not None:
            self.lease.retain(self.scope)
            # Also close a model captured before an unlaunched/cancelled worker.
            with actual_owner(self.scope):
                close = self.lease.start_close()
            await asyncio.gather(asyncio.shield(close), return_exceptions=True)
        self.scope.release(self._root)
        await self.scope.join()
        self.work_settled = True

    def _healthy(self, *, retired=False):
        if self.failed or self.scope.failed or self.final_scope.failed:
            raise StandingUnavailable
        if retired and self.foreign_initial:
            raise StandingUnavailable
        if any(
            getattr(client, "_submit_cleanup_failed", False)
            for client in (self.journal, self.resources)
        ):
            raise StandingUnavailable
        if (
            retired
            and self.lease is not None
            and (
                type(self.lease) is not TaskModelLease
                or self.lease.cleanup is None
                or not self.lease.cleanup.done()
                or self.lease.cleanup.cancelled()
                or self.lease.cleanup.exception() is not None
                or not self.lease.settled
                or self.lease.failed
            )
        ):
            raise StandingUnavailable
        if (
            retired
            and self.record is not None
            and (
                self.record.standing_owner is not self
                or self.record.model_lease is not self.lease
                or self.record.task.task_id != self.original.task.task_id
                or self.record._cleanup_uncertain
                or self.record.standing_cleanup_uncertain
            )
        ):
            raise StandingUnavailable

    async def _join_final(self):
        # The finalizer's creation root is not an operation it waits for.
        while self.final_scope.handles - {self._final_root}:
            self.final_scope.changed.clear()
            if self.final_scope.handles - {self._final_root}:
                await self.final_scope.changed.wait()

    async def _joined_call(self, invoke, reconcile=None):
        try:
            result = await invoke()
        except Exception:
            # Await actual work/read/sole-close before exact-key reconciliation.
            await self._join_final()
            self._healthy()
            if reconcile is None:
                raise
            result = await reconcile()
        await self._join_final()
        self._healthy()
        return result

    async def _retire(self):
        try:
            # A shielded bind must first reveal its own actual disposition.
            if self.bind_operation is not None:
                await asyncio.gather(asyncio.shield(self.bind_operation), return_exceptions=True)
            if self.binding_started and self.binding is None:
                self.binding = await self._joined_call(
                    lambda: self.resources.lookup_standing_binding(self.claim)
                )
                if self.binding is not None:
                    self.binding = checked_finalization(
                        self.binding, StandingResourceBindingReceipt
                    ).validate_claim(self.claim)
            if self.binding is not None:
                closure = await self._joined_call(
                    lambda: self.journal.standing_close_admission(
                        self.original, capability=self._closure_capability
                    ),
                    lambda: self.journal.standing_closure_lookup(self.original),
                )
                self.closure = checked_finalization(
                    closure, StandingAdmissionClosure
                ).validate_request(self.original)
                self.request = StandingResourceFinalizeRequest(
                    schema_version="1.0",
                    idempotency_key="agent-standing-finalize:" + claim_id(self.original),
                    original=self.original,
                    expected_resource_backend=self.binding.request.resource_backend,
                    expected_resource_instance=self.binding.request.resource_instance,
                )
                context = await self._joined_call(
                    lambda: self.resources.seal_standing_claim(self.request),
                    lambda: self.resources.lookup_standing_seal(self.request),
                )
                context = checked_finalization(
                    context, StandingResourceFinalizeContext
                ).validate_request(self.request)
                if finalization_bytes(context.closure) != finalization_bytes(self.closure):
                    raise StandingUnavailable
            await self._settle_work()
            self._healthy(retired=True)
            if self.binding is None:
                if self.binding_started:
                    raise StandingUnavailable
                return
            if self.closure.initial is not None and (
                self.created is None
                or finalization_bytes(self.closure.initial) != finalization_bytes(self.created)
                or self.lease is None
            ):
                raise StandingUnavailable
            if self.closure.initial is None and self.created is not None:
                raise StandingUnavailable
            receipt = await self._joined_call(
                lambda: self.resources.finalize_standing_claim(self.request),
                lambda: self.resources.lookup_standing_finalization(self.request),
            )
            receipt = checked_finalization(
                receipt, StandingResourceFinalizationReceipt
            ).validate_request(self.request)
            context = receipt.finalization.context
            if finalization_bytes(context.binding) != finalization_bytes(
                self.binding.request
            ) or finalization_bytes(context.closure) != finalization_bytes(self.closure):
                raise StandingUnavailable
            self._healthy(retired=True)
            self.receipt = receipt
        except BaseException:
            self.fail()
            raise
        finally:
            # Even unsuccessful testimony cannot abandon original cleanup.
            await self._settle_work()
            self.final_scope.release(self._final_root)
            await self.final_scope.join()
            self._closure_capability = None

    def _retired(self, operation):
        if operation.cancelled() or operation.exception() is not None:
            self.fail()
        if not self.failed and not self.scope.failed and not self.final_scope.failed:
            self.runtime._standing_claims.discard(self)
        self.runtime._workspace_settled()


async def close_standing_dependency(runtime, client):
    """One retained original closer, also shared with launcher Journal retirement."""
    for original, operation in runtime._standing_dependency_closers:
        if original is client:
            return await asyncio.shield(operation)

    async def retire():
        while getattr(client, "_submit_exchanges", ()):
            tasks = {
                task
                for exchange in tuple(client._submit_exchanges)
                for task in (exchange.task, exchange.close_task)
                if task is not None
            }
            if not tasks:
                raise StandingUnavailable
            await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
        await client.close()

    with actual_owner(None):
        operation = asyncio.create_task(retire())
    runtime._standing_dependency_closers.append((client, operation))
    runtime._retain_workspace_worker(operation, transport=True)
    return await asyncio.shield(operation)


def start_standing_shutdown(runtime):
    if runtime._standing_shutdown is not None:
        return runtime._standing_shutdown
    runtime._closed = True
    owners = tuple(runtime._standing_claims)
    for owner in owners:
        owner.fence()
        if owner.record is not None:
            owner.record.closing = owner.record.cancel_requested = True
            owner.record.resume.set()
        for task in (owner.preflight, owner.worker):
            if task is not None and not task.done():
                task.cancel()
        owner.start_retirement()

    async def retire():
        await asyncio.gather(
            *(asyncio.shield(owner.operation) for owner in owners), return_exceptions=True
        )
        for owner in owners:
            # A failed facade is not permission to retire unknown originals.
            await owner.scope.join()
            await owner.final_scope.join()
        errors = []
        try:
            await runtime._close_runtime()
        except BaseException as error:
            errors.append(error)
        closed = await asyncio.gather(
            *(
                close_standing_dependency(runtime, client)
                for client in runtime._standing_dependencies
            ),
            return_exceptions=True,
        )
        errors.extend(error for error in closed if isinstance(error, BaseException))
        if errors:
            raise BaseExceptionGroup("Standing dependency retirement is unconfirmed", errors)

    with actual_owner(None):
        runtime._standing_shutdown = asyncio.create_task(retire())
    runtime._retain_workspace_worker(runtime._standing_shutdown, transport=True)
    return runtime._standing_shutdown


class ResourceCompletion:
    def __init__(self, runtime, record):
        self.runtime, self.record = runtime, record
        self.worker = asyncio.current_task()
        self.lease, self.client = record.model_lease, runtime.resource_client
        self.task_id = record.task.task_id
        self.cleanup = None
        self.operation = None
        self.armed = False
        self.suppressed = record.task.state not in _TERMINAL
        if getattr(record, "standing_owner", None) is not None:
            # The claim finalizer owns physical restoration after *all* Agent
            # work. The legacy task-only callback must never restore early.
            self.suppressed = True
        # Never clear this slot: a helper replay cannot arm another attempt.
        if record.resource_completion is None:
            record.resource_completion = self
        else:
            self.suppressed = True
        if self.client is not None:
            _client_retirement(runtime, self.client).completions.add(self)
        self.recheck()

    def recheck(self):
        rec, runtime = self.record, self.runtime
        if (
            rec.resource_completion is not self
            or runtime._tasks.get(self.task_id) is not rec
            or rec.task.task_id != self.task_id
            or rec.worker is not self.worker
            or rec.model_lease is not self.lease
            or runtime.resource_client is not self.client
            or rec.recovered
            or rec.closing
            or rec._cleanup_uncertain
            or rec.standing_cleanup_uncertain
            or runtime._resource_retirement_started
            and self.worker not in runtime._resource_retirement_workers
        ):
            self.suppressed = True
        return not self.suppressed

    def capture_cleanup(self):
        if type(self.lease) is TaskModelLease:
            self.cleanup = self.lease.start_close()

    def ready(self):
        if not self.recheck() or self.client is None or self.record.task.state not in _TERMINAL:
            return False
        if self.lease is None:
            return self.record.standing_initial is None
        return (
            type(self.lease) is TaskModelLease
            and isinstance(self.cleanup, asyncio.Task)
            and self.lease.cleanup is self.cleanup
            and self.cleanup.done()
            and not self.cleanup.cancelled()
            and self.cleanup.exception() is None
            and self.lease.settled is True
            and self.lease.failed is False
        )

    def arm(self):
        if self.armed or asyncio.current_task() is not self.worker:
            return
        self.armed = True
        if not self.ready():
            return
        self.operation = asyncio.create_task(self._run())
        self.runtime._resource_completions.add(self)
        self.operation.add_done_callback(self._settled)
        self.runtime._workspace_settled()

    def _failed(self):
        self.record._cleanup_uncertain = True
        if self.record.standing_initial is not None:
            self.record.standing_cleanup_uncertain = True
        self.runtime._workspace_cleanup_failed = True

    async def _run(self):
        try:
            # The notification never belongs to the worker it must join.
            await asyncio.shield(self.worker)
            if not self.ready():
                return
            await self.client.task_finished(self.task_id)
            if not self.ready():
                raise RuntimeError("Resource completion ownership changed")
        except BaseException:
            self._failed()
            raise

    def _settled(self, operation):
        if operation.cancelled() or operation.exception() is not None:
            self._failed()
            print(
                json.dumps(
                    {
                        "service": "agent",
                        "resource_restore": "unconfirmed",
                        "task_id": self.task_id,
                    }
                ),
                flush=True,
            )
        self.runtime._resource_completions.discard(self)
        self.runtime._workspace_settled()


class ResourceClientRetirement:
    def __init__(self, runtime, client):
        self.runtime, self.client = runtime, client
        self.completions = set()
        self.operation = None

    async def _run(self):
        workers = {owner.worker for owner in self.completions}
        if workers:
            await asyncio.gather(
                *(asyncio.shield(task) for task in workers), return_exceptions=True
            )
        operations = {owner.operation for owner in self.completions if owner.operation is not None}
        if operations:
            await asyncio.gather(
                *(asyncio.shield(task) for task in operations), return_exceptions=True
            )
        await self.client.close()
        if self.runtime.resource_client is self.client:
            self.runtime.resource_client = None

    def start(self):
        if self.operation is None:
            self.operation = asyncio.create_task(self._run())
            self.operation.add_done_callback(self._settled)
            self.runtime._workspace_settled()
        return self.operation

    def _settled(self, operation):
        if operation.cancelled() or operation.exception() is not None:
            self.runtime._workspace_cleanup_failed = True
        self.runtime._workspace_settled()


def _client_retirement(runtime, client):
    # Keep captured identities independently of current record/client slots and
    # pending notifications. Equality of two client facades is not ownership.
    for owner in runtime._resource_retirements:
        if owner.client is client:
            return owner
    owner = ResourceClientRetirement(runtime, client)
    runtime._resource_retirements.append(owner)
    return owner


def start_resource_retirement(runtime):
    """Retain exact client closers beyond one bounded shutdown facade."""
    if runtime._resource_retirement_started:
        return runtime._resource_retirement
    runtime._resource_retirement_started = True
    if runtime.resource_client is not None:
        _client_retirement(runtime, runtime.resource_client)
    workers = runtime._resource_retirement_workers = {
        worker
        for worker in (
            *(rec.worker for rec in runtime._tasks.values()),
            *(
                completion.worker
                for owner in runtime._resource_retirements
                for completion in owner.completions
            ),
            *runtime._workspace_workers,
        )
        if worker is not None
        and worker is not getattr(runtime, "_standing_shutdown", None)
        and not (
            getattr(runtime, "_standing_shutdown", None) is not None
            and worker is getattr(runtime.authorizations, "_journal_retirement", None)
        )
    }

    async def retire():
        # A captured worker can still enter finally and capture its actual
        # client. Let all originals end before fixing the client-close set.
        if workers:
            await asyncio.gather(
                *(asyncio.shield(task) for task in workers), return_exceptions=True
            )
        operations = [owner.start() for owner in runtime._resource_retirements]
        results = await asyncio.gather(
            *(asyncio.shield(task) for task in operations), return_exceptions=True
        )
        if any(isinstance(result, BaseException) for result in results):
            raise RuntimeError("Resource client retirement is unconfirmed") from None

    operation = runtime._resource_retirement = asyncio.create_task(retire())

    def settled(task):
        if task.cancelled() or task.exception() is not None:
            runtime._workspace_cleanup_failed = True
        runtime._workspace_settled()

    operation.add_done_callback(settled)
    runtime._workspace_settled()
    return operation
