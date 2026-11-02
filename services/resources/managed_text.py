"""One managed text lifecycle, independent of its public/native protocol."""

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field

from anyio import CancelScope
from fastapi import HTTPException

from packages.contracts.operation_ownership import current_owner
from services.resources.coordinator import Qwen3AdmissionError

_END = object()
_ERRORS = {
    "paused",
    "deadline",
    "disconnected",
    "unavailable",
    "generation_failed",
    "input_limit",
    "output_limit",
    "invalid_request",
    "busy",
}


class GenerationRefusal(RuntimeError):
    def __init__(self, code):
        self.code = code


@dataclass(eq=False)
class Generation:
    policy: object
    end: float
    identity: object = None
    lease: object = None
    admission: asyncio.Task | None = None
    work: asyncio.Task | None = None
    monitor: asyncio.Task | None = None
    disconnect: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    failure: asyncio.Future | None = None
    succeeded: bool = False
    cancel_sent: bool = False
    cleanup_confirmed: bool = False
    cleanup_failed: bool = False
    resident_only: bool = False


class ManagedTextBody:
    """Own the request even when ASGI header send fails before iteration."""

    def __init__(self, broker, operation):
        self.broker, self.operation = broker, operation
        self.stopped = self.terminal = False
        self.take = None

    def __aiter__(self):
        return self

    @property
    def remaining_timeout_s(self):
        return max(0.0, self.operation.end - asyncio.get_running_loop().time())

    async def __anext__(self):
        op = self.operation
        if self.stopped or self.terminal:
            await self.aclose()
            raise StopAsyncIteration
        if self.take is not None:
            raise RuntimeError("Managed text body permits one reader")
        try:
            if not op.failure.done():
                self.broker.current(op)
                self.take = asyncio.create_task(op.queue.get())
                await asyncio.wait((self.take, op.failure), return_when=asyncio.FIRST_COMPLETED)
            if op.failure.done():
                self.terminal = True
                return self.broker.protocol.error(op.failure.result())
            data = self.take.result()
            self.broker.current(op)
            if data is _END:
                self.terminal = True
                await self.aclose()
                raise StopAsyncIteration
            return data
        except HTTPException:
            self.broker.abort(op, "paused")
            self.terminal = True
            return self.broker.protocol.error("paused")
        except StopAsyncIteration:
            raise
        except BaseException:
            await self.aclose()
            raise
        finally:
            if self.take is not None:
                if not self.take.done():
                    self.take.cancel()
                await asyncio.gather(self.take, return_exceptions=True)
                self.take = None

    async def aclose(self):
        op = self.operation
        if not self.stopped:
            self.stopped = True
            op.closed.set()
            if self.terminal and op.succeeded and not op.failure.done():
                self.broker.finish(op)
            else:
                self.broker.abort(op, "disconnected")
        if op.cleanup is not None:
            with CancelScope(shield=True):
                await asyncio.wait((op.cleanup,), timeout=1)


class ManagedTextBroker:
    actual_ownership_supported = True

    def __init__(self, coordinator, endpoint, protocol, *, request_timeout_s=120):
        from packages.providers.qwen3 import deadline

        self.coordinator, self.manager = coordinator, coordinator.manager
        self.endpoint, self.protocol = endpoint, protocol
        self.timeout = deadline(request_timeout_s)
        self.operation = None
        self.closed = False

    @property
    def request_timeout_s(self):
        return self.timeout

    def policy(self):
        try:
            if self.closed:
                raise Qwen3AdmissionError()
            return self.protocol.policy(self.coordinator)
        except (OSError, ValueError, Qwen3AdmissionError) as exc:
            raise HTTPException(409, "Managed text is paused by owner/resource policy") from exc

    def current(self, op):
        if op.closed.is_set():
            raise HTTPException(409, "Managed text resource revision or worker changed")
        self.selection_current(op)

    def selection_current(self, op):
        if self.policy() != op.policy or not self.manager.is_current(op.identity):
            raise HTTPException(409, "Managed text resource revision or worker changed")
        try:
            self.protocol.current(op.lease, op.identity)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, "Managed text endpoint incarnation changed") from exc

    def status(self):
        op = self.operation
        return {
            **self.protocol.status(),
            "active_or_retained": op is not None,
            "cleanup_failed": bool(op and op.cleanup_failed),
            "worker_ready": self.manager.owned_identity(self.protocol.worker_id) is not None,
            "request_timeout_s": self.timeout,
        }

    def release(self, op):
        if self.operation is op and op.cleanup_confirmed:
            self.operation = None

    async def cleanup(self, op, *, graceful=False):
        try:
            if not graceful:
                if op.identity is None and op.admission is not None:
                    try:
                        op.identity = await asyncio.shield(op.admission)
                    except Qwen3AdmissionError as exc:
                        if not exc.cleanup_confirmed:
                            raise RuntimeError(
                                "Managed text admission release is unconfirmed"
                            ) from exc
                    except asyncio.CancelledError:
                        if not (op.resident_only and op.admission.cancelled()):
                            raise
                if op.identity is not None:
                    stop = getattr(self.coordinator, "stop_text", None)
                    if stop is not None:
                        await stop(op.identity)
                    else:
                        await self.protocol.stop_legacy(self.manager, op.identity, op.lease)
            if op.monitor is not None:
                await asyncio.gather(op.monitor, return_exceptions=True)
            elif op.disconnect is not None:
                op.disconnect.cancel()
                await asyncio.gather(op.disconnect, return_exceptions=True)
            if op.work is not None:
                await asyncio.gather(op.work, return_exceptions=True)
            op.cleanup_confirmed = True
            op.cleanup_failed = False
            self.release(op)
        except (Exception, asyncio.CancelledError):
            op.cleanup_failed = True

    def finish(self, op):
        op.closed.set()
        if op.cleanup is None:
            op.cleanup = asyncio.create_task(self.cleanup(op, graceful=True))

    def abort(self, op, code):
        if code not in _ERRORS:
            code = "generation_failed"
        if not op.failure.done():
            op.failure.set_result(code)
        op.closed.set()
        # A resident-only lookup may wait for the allocation lock but has no
        # side effects. Cancelling that lookup cannot abandon a new process.
        # Generation admission still retains its existing shielded owner.
        if op.resident_only and op.admission is not None and not op.admission.done():
            op.admission.cancel()
        if op.work is not None and not op.work.done() and not op.cancel_sent:
            op.cancel_sent = True
            op.work.cancel()
        if op.cleanup is None or (op.cleanup.done() and op.cleanup_failed):
            op.cleanup = asyncio.create_task(self.cleanup(op))
        owner = current_owner()
        if owner is not None:
            closes = getattr(self.endpoint, "stream_closes", None)
            if closes is not None:
                closes.retain(owner)
            elif getattr(self.endpoint, "actual_ownership_supported", False) is not True:
                owner.fail()
            # The broker remains the sole abort/endpoint-close owner. Enroll
            # its original tasks, including a previously admitted allocation.
            for task in (op.admission, op.work, op.monitor, op.disconnect, op.cleanup):
                if task is not None:
                    owner.retain_task(task)
            cleanup = op.cleanup
            cleanup.add_done_callback(
                lambda _: owner.fail() if not op.cleanup_confirmed or op.cleanup_failed else None
            )

    def abort_active(self):
        owner = current_owner()
        if owner is not None:
            closes = getattr(self.endpoint, "stream_closes", None)
            if closes is not None:
                closes.retain(owner)
            elif getattr(self.endpoint, "actual_ownership_supported", False) is not True:
                owner.fail()
        if self.operation is not None:
            self.abort(self.operation, "paused")

    async def drain(self):
        op = self.operation
        if op is not None:
            self.abort(op, "paused")
            with CancelScope(shield=True):
                await asyncio.shield(op.cleanup)
            if not op.cleanup_confirmed:
                raise RuntimeError("Managed text cleanup remains retained")

    async def produce(self, op, messages):
        # Adapters validate their native protocol and defer their final public
        # bytes until real source EOF/close. Queue has bytes and a private end
        # marker only; it never depends on Qwen frame kinds or native counts.
        async with aclosing(self.protocol.stream(messages, op.identity, op.lease)) as source:
            async for data in source:
                self.current(op)
                if not isinstance(data, bytes):
                    raise ValueError("Managed text protocol returned non-bytes")
                await op.queue.put(data)
        self.current(op)
        op.succeeded = True
        await op.queue.put(_END)

    async def monitor(self, op):
        closed = asyncio.create_task(op.closed.wait())
        work = op.work
        try:
            while True:
                remaining = max(0, op.end - asyncio.get_running_loop().time())
                selected = [op.disconnect, closed] + ([work] if work is not None else [])
                done, _ = await asyncio.wait(
                    selected, timeout=min(remaining, 0.1), return_when=asyncio.FIRST_COMPLETED
                )
                if closed in done:
                    return
                if op.disconnect in done:
                    self.abort(op, "disconnected")
                    return
                if remaining <= 0:
                    self.abort(op, "deadline")
                    return
                try:
                    self.current(op)
                except HTTPException:
                    self.abort(op, "paused")
                    return
                if work in done:
                    try:
                        work.result()
                    except GenerationRefusal as exc:
                        self.abort(op, exc.code)
                        return
                    except HTTPException:
                        self.abort(op, "paused")
                        return
                    except (Exception, asyncio.CancelledError):
                        self.abort(op, "generation_failed")
                        return
                    work = None
        finally:
            op.disconnect.cancel()
            closed.cancel()
            await asyncio.gather(op.disconnect, closed, return_exceptions=True)

    @staticmethod
    async def disconnected(request):
        while (await request.receive())["type"] != "http.disconnect":
            pass

    def reserve(self, policy, timeout):
        if self.operation is not None:
            old = self.operation
            if old.cleanup_failed and old.cleanup is not None and old.cleanup.done():
                self.abort(old, "unavailable")
            raise HTTPException(409, "Managed text generation or retained cleanup is busy")
        op = Generation(policy, asyncio.get_running_loop().time() + timeout)
        op.failure = asyncio.get_running_loop().create_future()
        self.operation = op
        return op

    async def read_messages(self, request):
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise HTTPException(400, "Managed text requires bounded JSON messages")
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal():
                raise HTTPException(400, "Invalid managed text body length")
            if len(length) > 9 or int(length) > self.protocol.max_request_bytes:
                raise HTTPException(413, "Managed text request exceeds its byte bound")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > self.protocol.max_request_bytes:
                raise HTTPException(413, "Managed text request exceeds its byte bound")
            raw.extend(chunk)
        try:
            return self.protocol.parse(bytes(raw))
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, "Invalid bounded managed dialogue") from exc

    async def measure_prompt(self, request):
        """Optional count-only operation sharing the real generation owner."""
        policy = self.policy()
        timeout = min(self.timeout, 10)
        op = self.reserve(policy, timeout)
        op.resident_only = True
        started = False
        try:
            async with asyncio.timeout(timeout):
                messages = await self.read_messages(request)
                if self.policy() != policy:
                    raise HTTPException(409, "Managed text admission revision changed")
                resident = getattr(self.protocol, "resident", None)
                measure = getattr(self.protocol, "measure_prompt", None)
                if resident is None or measure is None:
                    raise HTTPException(503, "Prompt measurement is unavailable")
                started = True
                op.disconnect = asyncio.create_task(self.disconnected(request))
                op.admission = asyncio.create_task(resident(self.coordinator, policy))
                done, _ = await asyncio.wait(
                    (op.admission, op.disconnect), return_when=asyncio.FIRST_COMPLETED
                )
                if op.disconnect in done:
                    raise HTTPException(503, "Prompt measurement is unavailable")
                op.identity = op.admission.result()
                op.lease = self.protocol.ready_for(op.identity)
                self.current(op)
                op.work = asyncio.create_task(measure(messages, op.identity, op.lease))
                op.monitor = asyncio.create_task(self.monitor(op))
                await asyncio.wait((op.work, op.failure), return_when=asyncio.FIRST_COMPLETED)
                if op.failure.done():
                    raise HTTPException(503, "Prompt measurement is unavailable")
                result = op.work.result()
                from packages.providers.prompt_budget import PromptTokenCount

                if result is not None and type(result) is not PromptTokenCount:
                    raise ValueError("Invalid prompt measurement result")
                self.current(op)
                self.finish(op)
                await asyncio.shield(op.cleanup)
                if not op.cleanup_confirmed:
                    raise HTTPException(503, "Prompt measurement cleanup is unconfirmed")
                # Joining monitor/disconnect retirement is an await boundary.
                # A closed operation cannot use current(), but its captured
                # policy and worker must still be selected before publication.
                self.selection_current(op)
        except BaseException as exc:
            if started:
                self.abort(op, "deadline" if isinstance(exc, TimeoutError) else "unavailable")
                with CancelScope(shield=True):
                    await asyncio.wait((op.cleanup,), timeout=1)
            else:
                op.cleanup_confirmed = True
                self.release(op)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if (
                not started
                and isinstance(exc, HTTPException)
                and exc.status_code in {400, 409, 413}
            ):
                raise
            raise HTTPException(503, "Prompt measurement is unavailable") from exc
        # Unsupported means a fully closed exchange, not an abort. Keep the
        # healthy resident worker for ordinary dialogue and never retry here.
        if result is None:
            raise HTTPException(503, "Prompt measurement is unavailable")
        return result.to_wire()

    async def stream(self, request):
        policy = self.policy()
        op = self.reserve(policy, self.timeout)
        started = False
        try:
            async with asyncio.timeout(self.timeout):
                messages = await self.read_messages(request)
                if self.policy() != policy:
                    raise HTTPException(409, "Managed text admission revision changed")
                started = True
                op.disconnect = asyncio.create_task(self.disconnected(request))
                op.admission = asyncio.create_task(self.protocol.admit(self.coordinator, policy))
                done, _ = await asyncio.wait(
                    (op.admission, op.disconnect), return_when=asyncio.FIRST_COMPLETED
                )
                if op.disconnect in done:
                    raise HTTPException(503, "Managed text admission disconnected")
                op.identity = op.admission.result()
                op.lease = self.protocol.ready_for(op.identity)
                self.current(op)
                op.work = asyncio.create_task(self.produce(op, messages))
                op.monitor = asyncio.create_task(self.monitor(op))
                return ManagedTextBody(self, op)
        except BaseException as exc:
            if started:
                self.abort(op, "deadline" if isinstance(exc, TimeoutError) else "unavailable")
                with CancelScope(shield=True):
                    await asyncio.wait((op.cleanup,), timeout=1)
            else:
                op.cleanup_confirmed = True
                self.release(op)
            if isinstance(exc, TimeoutError):
                raise HTTPException(503, "Managed text admission deadline exceeded") from exc
            if isinstance(exc, (Qwen3AdmissionError, RuntimeError, OSError, ValueError)):
                raise HTTPException(503, "Managed text admission unavailable") from exc
            raise

    async def close(self):
        self.closed = True
        self.abort_active()
        op = self.operation
        if op is not None and op.cleanup is not None:
            with CancelScope(shield=True):
                await asyncio.wait((op.cleanup,), timeout=1)
