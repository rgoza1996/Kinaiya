"""Original post-worker notification and its dependent client-close owner.

These local owners acknowledge cleanup ordering only. They are not native
standing settlement or evidence that Resources physically restored anything.
"""

import asyncio
import json

from packages.contracts.models import TaskState
from services.agent.task_model_lease import TaskModelLease

_TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}


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
        # Never clear this slot: a helper replay cannot arm another attempt.
        if record.resource_completion is None:
            record.resource_completion = self
        else:
            self.suppressed = True
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


def start_resource_retirement(runtime):
    """Retain one exact client's close beyond a bounded shutdown facade."""
    if runtime._resource_retirement_started:
        return runtime._resource_retirement
    runtime._resource_retirement_started = True
    client = runtime.resource_client
    if client is None:
        return None
    records = tuple(runtime._tasks.values())
    completions = tuple(runtime._resource_completions)
    workers = {
        worker
        for worker in (
            *(rec.worker for rec in records),
            *(owner.worker for owner in completions),
            *runtime._workspace_workers,
        )
        if worker is not None
    }
    runtime._resource_retirement_workers = workers

    async def retire():
        # A worker can arm its completion just before it ends. Join originals
        # outside all runtime/record locks, then collect those actual owners.
        if workers:
            await asyncio.gather(
                *(asyncio.shield(task) for task in workers), return_exceptions=True
            )
        owners = {
            *completions,
            *runtime._resource_completions,
            *(rec.resource_completion for rec in records if rec.resource_completion is not None),
        }
        operations = {
            owner.operation
            for owner in owners
            if owner.client is client and owner.operation is not None
        }
        if operations:
            await asyncio.gather(
                *(asyncio.shield(task) for task in operations), return_exceptions=True
            )
        await client.close()
        if runtime.resource_client is client:
            runtime.resource_client = None

    operation = runtime._resource_retirement = asyncio.create_task(retire())

    def settled(task):
        if task.cancelled() or task.exception() is not None:
            runtime._workspace_cleanup_failed = True
        runtime._workspace_settled()

    operation.add_done_callback(settled)
    runtime._workspace_settled()
    return operation
