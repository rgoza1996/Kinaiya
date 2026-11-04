"""Ordinary journal streaming with one owned disconnect receiver."""

import asyncio
from dataclasses import dataclass

from starlette.websockets import WebSocketDisconnect


@dataclass(slots=True)
class _Receiver:
    ws: object
    loop: asyncio.AbstractEventLoop
    gate: asyncio.Event
    coroutine: object = None
    task: object = None
    entered: object = None
    completed: object = None
    stop: bool = False
    setup_unknown: bool = False
    setup_error: BaseException | None = None
    cancel_requested: bool = False
    deadline: float | None = None
    cleanup_unknown: bool = False


async def _receive(original):
    # No receive effect is permitted until the returned original is enrolled.
    await original.gate.wait()
    task = asyncio.current_task()
    if task is not original.task:
        original.setup_unknown = True
        return
    original.entered = task
    reads = 0
    try:
        while not original.stop:
            message = await original.ws.receive()
            if original.stop or message["type"] == "websocket.disconnect":
                return
            # Post-hello frames have no command semantics. Yield even when the
            # ASGI receive source has an immediately available frame backlog.
            reads = (reads + 1) % 64
            if reads == 0:
                await asyncio.sleep(0)
    except WebSocketDisconnect:
        return
    finally:
        original.completed = task


async def _hold_unknown(original):
    # With no identifiable original Task, a factory failure is not no-effect
    # proof. Keep the unopened gate, coroutine, facade and failure in app-Task
    # custody. Cancellation cannot manufacture a positive receiver retirement.
    pending = original.loop.create_future()
    while True:
        try:
            await asyncio.wait({pending})
        except asyncio.CancelledError:
            pass


async def _finished(original):
    if original.setup_unknown:
        await _hold_unknown(original)
    task = original.task
    if not task.done():
        return False
    if not task.cancelled() and (original.entered is not task or original.completed is not task):
        original.setup_unknown = True
        await _hold_unknown(original)
    task.result()
    return True


async def _retire(original):
    original.stop = True
    if original.setup_unknown:
        await _hold_unknown(original)
    task = original.task
    if original.deadline is None:
        original.deadline = original.loop.time() + 5
    if not task.done() and not original.cancel_requested:
        original.cancel_requested = True
        task.cancel()
    interrupted = False
    while not task.done():
        remaining = original.deadline - original.loop.time()
        if remaining <= 0:
            original.cleanup_unknown = True
        try:
            # Once the observation budget expires, this SAME app Task retains
            # its actual receiver until join. There is no renewed allowance or
            # bounded-terminal-return claim for a cancellation-resistant ASGI.
            await asyncio.wait({task}, timeout=remaining if remaining > 0 else None)
        except asyncio.CancelledError:
            interrupted = True
    if original.loop.time() >= original.deadline:
        original.cleanup_unknown = True
    if not task.cancelled() and (original.entered is not task or original.completed is not task):
        original.setup_unknown = True
        await _hold_unknown(original)
    try:
        task.result()
    except asyncio.CancelledError:
        # Only the positively terminal ORIGINAL receiver is joined here.
        return interrupted, None
    except BaseException as error:
        return interrupted, error
    return interrupted, None


async def serve_legacy_events(ws, store, *, after: int, session_id: str | None):
    """Keep the app Task as sender; a disconnect is a normal channel end.

    Authentication and the media-mode branch belong to the calling API route.
    A blocked synchronous journal read or current send still belongs to this
    original app Task; the receiver does not promise to preempt either one.
    """
    loop = asyncio.get_running_loop()
    app_task = asyncio.current_task()
    cancellations = app_task.cancelling()
    original = _Receiver(ws, loop, asyncio.Event())
    original.coroutine = _receive(original)
    try:
        original.task = loop.create_task(original.coroutine)
    except BaseException as error:
        original.setup_error = error
        original.setup_unknown = True
    else:
        if (
            type(original.task) is not asyncio.Task
            or original.task.get_loop() is not loop
            or original.task.get_coro() is not original.coroutine
        ):
            original.setup_unknown = True
        else:
            original.gate.set()

    failure = None
    try:
        if original.setup_unknown:
            await _hold_unknown(original)
        while True:
            # ASGI sends may be immediately ready throughout a journal backlog.
            # Give the sole receiver progress before beginning each bounded page.
            await asyncio.sleep(0)
            if await _finished(original):
                return
            entries = store.read(after=after, limit=100, session_id=session_id)
            for event in entries:
                if await _finished(original):
                    return
                await ws.send_json(event.model_dump(mode="json"))
                after = event.sequence
                if await _finished(original):
                    return
            if not entries:
                # Polling never cancels or replaces the original receiver.
                await asyncio.wait({original.task}, timeout=0.1)
    except WebSocketDisconnect:
        pass
    except BaseException as error:
        failure = error
    finally:
        interrupted, receiver_error = await _retire(original)
        if interrupted or app_task.cancelling() > cancellations:
            raise asyncio.CancelledError from failure
        if failure is not None:
            raise failure
        if receiver_error is not None:
            raise receiver_error
