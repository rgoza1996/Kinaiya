"""Interface-first legacy event-helper oracles; no ASGI/runtime qualification.

The old Session polling route and its recorded disconnect failure were reviewed
before these tests. The NEW helper implementation was unread at author seal.
Only the frozen helper interface selects behavior; stubs observe actual Tasks,
original cancellation and successful sends, never cleanup receipts or row flags.
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from services.session.legacy_events import serve_legacy_events
from starlette.websockets import WebSocketDisconnect

_GUARD = 8.0
_RETAINED_UNKNOWN = []
_DISCONNECT = {"type": "websocket.disconnect", "code": 1000}


class _ReadFault(RuntimeError):
    pass


class _SendFault(RuntimeError):
    pass


class _ReceiveFault(RuntimeError):
    pass


@dataclass(frozen=True)
class _Entry:
    sequence: int

    def model_dump(self, *, mode):
        assert mode == "json"
        return {"sequence": self.sequence, "kind": "unit.journal"}


class _Journal:
    def __init__(self, entries=(), *, error=None):
        self.entries = tuple(entries)
        self.error = error
        self.reads = []

    def read(self, *, after, limit, session_id):
        self.reads.append((asyncio.current_task(), after, limit, session_id))
        if self.error is not None:
            raise self.error
        return [entry for entry in self.entries if entry.sequence > after][:limit]


class _Socket:
    def __init__(self, *, resist_cancel=False, send_error=None, hold_send=False):
        self.incoming = asyncio.Queue()
        self.receive_entered = asyncio.Event()
        self.cancel_observed = asyncio.Event()
        self.release_receiver = asyncio.Event()
        self.send_entered = asyncio.Event()
        self.release_send = asyncio.Event()
        self.resist_cancel = resist_cancel
        self.send_error = send_error
        self.hold_send = hold_send
        self.receive_tasks = []
        self.receive_calls = 0
        self.delivered_messages = 0
        self.receive_cancellations = 0
        self.send_tasks = []
        self.sent = []

    async def receive(self):
        task = asyncio.current_task()
        if not any(task is old for old in self.receive_tasks):
            self.receive_tasks.append(task)
        self.receive_calls += 1
        self.receive_entered.set()
        while True:
            try:
                value = await self.incoming.get()
                if isinstance(value, BaseException):
                    raise value
                self.delivered_messages += 1
                return value
            except asyncio.CancelledError:
                self.receive_cancellations += 1
                self.cancel_observed.set()
                if not self.resist_cancel:
                    raise
                while not self.release_receiver.is_set():
                    try:
                        await self.release_receiver.wait()
                    except asyncio.CancelledError:
                        self.receive_cancellations += 1
                return dict(_DISCONNECT)

    async def send_json(self, value):
        self.send_tasks.append(asyncio.current_task())
        self.send_entered.set()
        if self.hold_send:
            await self.release_send.wait()
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(dict(value))

    def disconnect(self):
        self.incoming.put_nowait(dict(_DISCONNECT))


async def _until(predicate):
    async with asyncio.timeout(_GUARD):
        while not predicate():
            await asyncio.sleep(0.01)


async def _observe(task):
    done, _ = await asyncio.wait({task}, timeout=_GUARD)
    assert task in done, "The actual original app Task did not complete"
    return await task


class _Harness:
    def __init__(self, ws, store, after, session_id):
        self.ws = ws
        self.store = store
        self.baseline = set(asyncio.all_tasks())
        self.app = asyncio.create_task(
            serve_legacy_events(ws, store, after=after, session_id=session_id)
        )
        self.unresolved = set()

    async def finish(self):
        # Disposable fixture release is not a product cleanup observation.
        # Do not cancel/recreate the receiver behind its app owner's back.
        self.ws.release_receiver.set()
        self.ws.release_send.set()
        self.ws.disconnect()
        if not self.app.done():
            self.app.cancel()
        owned = {self.app, *self.ws.receive_tasks}
        _, pending = await asyncio.wait(owned, timeout=_GUARD)
        extra = set(asyncio.all_tasks()) - self.baseline - {asyncio.current_task()}
        extra.discard(self.app)
        if extra:
            _, still_pending = await asyncio.wait(extra, timeout=_GUARD)
            pending.update(still_pending)
        if pending:
            self.unresolved.update(pending)
            _RETAINED_UNKNOWN.append(self)
            raise AssertionError("Original legacy event fixture Task custody is unresolved")
        for task in owned:
            if not task.cancelled():
                task.exception()


@asynccontextmanager
async def _running(ws=None, store=None, *, after=0, session_id="unit-session"):
    harness = _Harness(ws or _Socket(), store or _Journal(), after, session_id)
    try:
        yield harness
    finally:
        primary = sys.exception()
        cleanup = asyncio.create_task(harness.finish())
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
            except BaseException:
                break
        try:
            cleanup.result()
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note("Legacy unit fixture retirement failed: " + type(error).__name__)
        if interrupted and primary is None:
            raise asyncio.CancelledError


def _assert_sole_owners(harness):
    assert harness.ws.receive_tasks
    assert len(harness.ws.receive_tasks) == 1
    receiver = harness.ws.receive_tasks[0]
    assert type(receiver) is asyncio.Task
    assert receiver is not harness.app
    assert receiver.get_loop() is harness.app.get_loop()
    assert all(task is harness.app for task, *_ in harness.store.reads)
    assert all(task is harness.app for task in harness.ws.send_tasks)
    return receiver


def _assert_no_live_helper_tasks(harness):
    remaining = set(asyncio.all_tasks()) - harness.baseline - {asyncio.current_task()}
    assert not remaining, "The app returned before its original helper Task joined"


@pytest.mark.parametrize("disconnect_exception", [False, True])
async def test_idle_original_receiver_disconnect_is_normal_and_joined(disconnect_exception):
    async with _running() as h:
        await _until(h.ws.receive_entered.is_set)
        h.ws.incoming.put_nowait(
            WebSocketDisconnect(1000) if disconnect_exception else dict(_DISCONNECT)
        )
        await _observe(h.app)
        receiver = _assert_sole_owners(h)
        assert receiver.done()
        assert h.ws.sent == []
        assert h.ws.receive_calls == 1
        assert h.ws.receive_cancellations == 0
        _assert_no_live_helper_tasks(h)


async def test_idle_polls_keep_one_persistent_uncancelled_receiver():
    async with _running() as h:
        await _until(lambda: len(h.store.reads) >= 3)
        receiver = _assert_sole_owners(h)
        assert not receiver.done()
        assert h.ws.receive_calls == 1
        assert h.ws.receive_cancellations == 0
        assert all(
            (after, limit, sid) == (0, 100, "unit-session")
            for _, after, limit, sid in h.store.reads
        )
        h.ws.disconnect()
        await _observe(h.app)
        assert receiver.done()


async def test_buffered_post_hello_frames_yield_to_other_native_tasks():
    async with _running() as h:
        await _until(h.ws.receive_entered.is_set)
        # Unit-only finite burst: no protocol command or product queue allowance.
        count = 1024
        for _ in range(count):
            h.ws.incoming.put_nowait({"type": "websocket.receive", "text": "ignored"})

        async def witness():
            await asyncio.sleep(0)
            return h.ws.delivered_messages

        observed = asyncio.create_task(witness())
        assert await _observe(observed) < count
        await _until(lambda: h.ws.delivered_messages == count)
        receiver = _assert_sole_owners(h)
        assert h.ws.sent == []
        assert h.ws.receive_cancellations == 0
        h.ws.disconnect()
        await _observe(h.app)
        assert receiver.done()
        _assert_no_live_helper_tasks(h)


async def test_post_hello_text_and_binary_are_ignored_without_scope_or_cursor_commands():
    async with _running(after=9) as h:
        await _until(h.ws.receive_entered.is_set)
        h.ws.incoming.put_nowait(
            {
                "type": "websocket.receive",
                "text": '{"after":999,"session_id":"other","command":"delete"}',
            }
        )
        h.ws.incoming.put_nowait({"type": "websocket.receive", "bytes": b"not-a-command"})
        await _until(lambda: h.ws.receive_calls >= 3 and len(h.store.reads) >= 2)
        receiver = _assert_sole_owners(h)
        assert not receiver.done()
        assert h.ws.receive_cancellations == 0
        assert all(
            (after, limit, sid) == (9, 100, "unit-session")
            for _, after, limit, sid in h.store.reads
        )
        assert h.ws.sent == []
        h.ws.disconnect()
        await _observe(h.app)
        assert receiver.done()


@pytest.mark.parametrize("session_id", [None, "unit-session"])
async def test_original_app_sends_journal_in_pages_and_advances_only_delivered_cursor(session_id):
    journal = _Journal(_Entry(sequence) for sequence in range(5, 106))
    async with _running(store=journal, after=4, session_id=session_id) as h:
        await _until(lambda: len(journal.reads) >= 3)
        assert h.ws.sent == [entry.model_dump(mode="json") for entry in journal.entries]
        assert [after for _, after, _, _ in journal.reads[:3]] == [4, 104, 105]
        assert all((limit, sid) == (100, session_id) for _, _, limit, sid in journal.reads)
        receiver = _assert_sole_owners(h)
        h.ws.disconnect()
        await _observe(h.app)
        assert receiver.done()


async def test_blocked_original_send_prevents_next_read_or_cursor_publication():
    ws = _Socket(hold_send=True)
    journal = _Journal([_Entry(3)])
    async with _running(ws, journal, after=2) as h:
        await _until(ws.send_entered.is_set)
        assert len(journal.reads) == 1
        assert ws.sent == []
        assert not h.app.done()
        ws.release_send.set()
        await _until(lambda: len(journal.reads) >= 2)
        assert ws.sent == [{"sequence": 3, "kind": "unit.journal"}]
        assert [after for _, after, _, _ in journal.reads[:2]] == [2, 3]
        receiver = _assert_sole_owners(h)
        ws.disconnect()
        await _observe(h.app)
        assert receiver.done()


async def test_original_send_exception_is_preserved_and_receiver_is_joined():
    error = _SendFault("synthetic send failure")
    ws = _Socket(send_error=error)
    async with _running(ws, _Journal([_Entry(1)])) as h:
        with pytest.raises(_SendFault) as observed:
            await _observe(h.app)
        assert observed.value is error
        assert len(h.store.reads) == 1
        assert ws.sent == []
        assert all(task.done() for task in ws.receive_tasks)
        _assert_no_live_helper_tasks(h)


async def test_original_store_read_exception_is_preserved_without_later_reads():
    error = _ReadFault("synthetic journal failure")
    async with _running(store=_Journal(error=error)) as h:
        with pytest.raises(_ReadFault) as observed:
            await _observe(h.app)
        assert observed.value is error
        assert len(h.store.reads) == 1
        assert h.store.reads[0][0] is h.app
        assert h.ws.sent == []
        assert all(task.done() for task in h.ws.receive_tasks)
        _assert_no_live_helper_tasks(h)


async def test_original_receiver_exception_is_preserved_by_app_and_positively_joined():
    error = _ReceiveFault("synthetic receive failure")
    async with _running() as h:
        await _until(h.ws.receive_entered.is_set)
        h.ws.incoming.put_nowait(error)
        with pytest.raises(_ReceiveFault) as observed:
            await _observe(h.app)
        assert observed.value is error
        receiver = _assert_sole_owners(h)
        assert receiver.done() and not receiver.cancelled()
        assert receiver.exception() is error
        assert h.ws.sent == []
        _assert_no_live_helper_tasks(h)


async def test_app_cancellation_requests_receiver_cancel_once_and_joins_actual_task():
    async with _running() as h:
        await _until(h.ws.receive_entered.is_set)
        h.app.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _observe(h.app)
        receiver = _assert_sole_owners(h)
        assert h.app.cancelled()
        assert receiver.done()
        assert h.ws.receive_cancellations == 1
        _assert_no_live_helper_tasks(h)


async def test_repeated_app_cancel_keeps_custody_past_observation_until_original_receiver_joins():
    ws = _Socket(resist_cancel=True)
    async with _running(ws) as h:
        await _until(ws.receive_entered.is_set)
        receiver = _assert_sole_owners(h)
        h.app.cancel()
        await _until(ws.cancel_observed.is_set)
        reads_after_cancel = len(h.store.reads)
        for _ in range(2):
            h.app.cancel()
            await asyncio.sleep(0.02)
            assert not h.app.done()
        # This is an observer beyond the original five seconds, not a changed
        # helper deadline or simulated physical completion/cancellation proof.
        done, _ = await asyncio.wait({h.app}, timeout=5.1)
        assert h.app not in done
        assert not receiver.done()
        assert ws.receive_cancellations == 1
        assert len(h.store.reads) == reads_after_cancel
        assert ws.sent == []
        ws.release_receiver.set()
        with pytest.raises(asyncio.CancelledError):
            await _observe(h.app)
        assert h.app.cancelled()
        assert receiver.done()
        assert ws.receive_cancellations == 1
        _assert_no_live_helper_tasks(h)
