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
