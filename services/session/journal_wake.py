"""Bounded process-local notification after SQLite exits its transaction."""

import asyncio
import secrets
import sqlite3
import threading

from packages.contracts.journal_wake import (
    MAX_REVISION,
    MAX_WAITERS,
    JournalWakeCursor,
    JournalWakeObservation,
    JournalWakeWait,
)


class WakeUnavailable(ValueError):
    pass


class JournalWake:
    def __init__(self):
        self.instance = secrets.token_hex(16)
        self.revision = 0
        self.lock = threading.RLock()
        self.waiters = {}
        self.closed = False

    def _cursor(self):
        if self.closed:
            raise WakeUnavailable("Original journal wake owner is unavailable")
        return JournalWakeCursor(
            schema_version="1.0",
            protocol="journal_wake_v1",
            owner_instance=self.instance,
            revision=self.revision,
        )

    def snapshot(self):
        with self.lock:
            return self._cursor()

    def changed(self, *, closed=False):
        # This hint must never change the outcome of an already committed write.
        with self.lock:
            if self.closed:
                return
            if closed or self.revision >= MAX_REVISION:
                self.closed = True
            else:
                self.revision += 1
            for event, loop in tuple(self.waiters.items()):
                try:
                    loop.call_soon_threadsafe(event.set)
                except RuntimeError:
                    # Failure to schedule is not proof that the original wait
                    # exited. Its finally path alone releases this bounded slot.
                    pass

    async def wait(self, request):
        request = JournalWakeWait.model_validate(request)
        event, loop = asyncio.Event(), asyncio.get_running_loop()
        with self.lock:
            cursor = self._cursor()
            if (
                request.cursor.owner_instance != self.instance
                or request.cursor.revision > self.revision
            ):
                raise WakeUnavailable("Journal wake cursor belongs to another original state")
            if cursor == request.cursor and request.wait_ms:
                if len(self.waiters) >= MAX_WAITERS:
                    raise WakeUnavailable("Original journal wake capacity remains occupied")
                self.waiters[event] = loop
            else:
                return self._observation(request, cursor)
        try:
            # New commits set the same registered event immediately. This is a
            # maximum idle heartbeat, never an inference-admission cadence.
            try:
                async with asyncio.timeout(request.wait_ms / 1000):
                    await event.wait()
            except TimeoutError:
                pass
            with self.lock:
                return self._observation(request, self._cursor())
        finally:
            with self.lock:
                self.waiters.pop(event, None)

    @staticmethod
    def _observation(original, cursor):
        return JournalWakeObservation(
            schema_version="1.0",
            protocol="journal_wake_v1",
            original=original,
            cursor=cursor,
            changed=cursor.revision > original.cursor.revision,
        )


class WakeConnection(sqlite3.Connection):
    """Retain SQLite outcomes, including a script's committed prefix on error.

    Observe exit in finally: deferring a committed prefix until a later read
    could strand a waiter and incorrectly make that read generate a wake.
    """

    wake_owner = None
    _wake_changes = 0

    def _settled(self):
        if self.wake_owner is None:
            return
        try:
            in_transaction, changed = self.in_transaction, self.total_changes
        except sqlite3.Error:
            self.wake_owner.changed(closed=True)
            return
        if not in_transaction:
            if changed != self._wake_changes:
                self._wake_changes = changed
                self.wake_owner.changed()

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self._settled()

    def commit(self):
        try:
            return super().commit()
        finally:
            self._settled()

    def rollback(self):
        try:
            return super().rollback()
        finally:
            self._settled()

    def execute(self, *args, **kwargs):
        try:
            return super().execute(*args, **kwargs)
        finally:
            self._settled()

    def executemany(self, *args, **kwargs):
        try:
            return super().executemany(*args, **kwargs)
        finally:
            self._settled()

    def executescript(self, *args, **kwargs):
        try:
            return super().executescript(*args, **kwargs)
        finally:
            self._settled()

    def close(self):
        result = super().close()
        if self.wake_owner is not None:
            self.wake_owner.changed(closed=True)
        return result
