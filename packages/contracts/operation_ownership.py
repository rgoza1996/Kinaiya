"""Internal enrollment in an existing actual owner, never another close owner.

The Resources barrier installs this only around its trusted operations. A root
task remains enrolled while children are created, and an HTTP exchange holds a
token until its original work/sole-close callbacks have both settled.
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar

_current = ContextVar("kinaiya_actual_operation", default=None)


def current_owner():
    return _current.get()


@contextmanager
def actual_owner(owner):
    token = _current.set(owner)
    try:
        yield
    finally:
        _current.reset(token)


def retain_task(task, *, owner=None, cleanup=False):
    owner = current_owner() if owner is None else owner
    if owner is not None:
        owner.retain_task(task, cleanup=cleanup)
    return task


class ActualOperation:
    """A bounded hub owns this object until all original handles settle."""

    def __init__(self, settled, *, task_id=None):
        self.task_id = task_id
        self.handles = set()
        self.failed = False
        self.finished = False
        self.changed = asyncio.Event()
        self._settled = settled
        self._retire = []

    def hold(self, handle):
        if self.finished:
            self.fail()
            raise RuntimeError("Actual operation already retired")
        self.handles.add(handle)

    def fail(self):
        self.failed = True
        self.changed.set()
        if self.finished:
            self._settled(self)

    def release(self, handle, *, failed=False):
        if failed:
            self.fail()
        self.handles.discard(handle)
        self.changed.set()
        if not self.handles and not self.finished:
            self.finished = True
            for callback in self._retire:
                callback()
            self._settled(self)

    def on_retire(self, callback):
        if self.finished:
            callback()
        else:
            self._retire.append(callback)

    def retain_task(self, task, *, cleanup=False):
        if task in self.handles:
            return
        self.hold(task)

        def settled(done):
            failed = done.cancelled()
            if not failed:
                failed = done.exception() is not None
            self.release(done, failed=cleanup and failed)

        task.add_done_callback(settled)

    async def join(self):
        while self.handles:
            self.changed.clear()
            if self.handles:
                await self.changed.wait()


class StreamClosures:
    """Retain original native stream wait_closed tasks after a bounded wait."""

    def __init__(self):
        self.pending = set()
        self.owners = set()
        self.failed = False

    def retain(self, owner):
        self.owners.add(owner)
        owner.on_retire(lambda: self.owners.discard(owner))
        if self.failed:
            owner.fail()
        for task in self.pending:
            owner.retain_task(task, cleanup=True)

    async def close(self, writer, *, timeout=0.2):
        try:
            writer.close()
        except BaseException:
            self.failed = True
            for owner in {*self.owners, current_owner()} - {None}:
                owner.fail()
            raise
        task = asyncio.create_task(writer.wait_closed())
        self.pending.add(task)
        owner = current_owner()
        if owner is not None:
            self.retain(owner)
        for owner in self.owners:
            owner.retain_task(task, cleanup=True)

        def settled(done):
            self.pending.discard(done)
            if done.cancelled() or done.exception() is not None:
                self.failed = True
                for owner in self.owners:
                    owner.fail()

        task.add_done_callback(settled)
        # This is the original sole close. Timeout/cancellation never cancels
        # its actual wait and cannot convert it to a completed cleanup proof.
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if done:
            task.result()
        return bool(done)
