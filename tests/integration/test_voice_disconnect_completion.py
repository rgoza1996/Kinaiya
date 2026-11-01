"""Deterministic ASGI sender/monitor completion ties; synthetic native audio only."""

import asyncio
import threading

import pytest
from fastapi import HTTPException
from test_voice_producer import WAVE, NativeBackend, Request, app_for, entered, health

from packages.providers import voice_worker
from packages.providers.tts_backends import TTSBackendError
from services.resources.state import ResourceState


class CompletionTie:
    """Return both real response tasks from wait, without changing their results."""

    def __init__(self, monkeypatch, release_send, *, sender_first=False, hold_return=False):
        self.release_send = release_send
        self.sender_first = sender_first
        self.hold_return = hold_return
        self.ready = asyncio.Event()
        self.return_allowed = asyncio.Event()
        self.on_sender_done = None
        self.stream = self.sender_error = None
        self.done = None
        self.used = False
        self.original_wait = asyncio.wait
        monkeypatch.setattr(voice_worker.asyncio, "wait", self.wait)

    async def wait(self, futures, *args, **kwargs):
        tasks = set(futures)
        monitor = next(
            (
                task
                for task in tasks
                if isinstance(task, asyncio.Task)
                and task.get_coro().__qualname__.endswith("SynthesisStream.watch")
            ),
            None,
        )
        sender = next(
            (
                task
                for task in tasks
                if isinstance(task, asyncio.Task)
                and task.get_coro().__qualname__ == "StreamingResponse.stream_response"
            ),
            None,
        )
        if self.used or monitor is None or sender is None:
            return await self.original_wait(tasks, *args, **kwargs)
        self.used = True
        # Retain the actual monitor owner before its coroutine frame finishes.
        self.stream = monitor.get_coro().cr_frame.f_locals["self"]
        if self.sender_first:
            await self.original_wait({sender})
            await self.on_sender_done()
            await self.original_wait({monitor})
        else:
            await self.original_wait({monitor})
            self.release_send.set()
            await self.original_wait({sender})
        assert sender.done() and monitor.done()
        self.sender_error = None if sender.cancelled() else sender.exception()
        self.done = {sender, monitor}
        self.ready.set()
        if self.hold_return:
            await self.return_allowed.wait()
        return self.done, set()


def with_send_failure(app, failure):
    async def failing_app(scope, receive, send):
        async def failing_send(message):
            await send(message)
            if message["type"] == "http.response.body" and message.get("body"):
                raise failure

        await app(scope, receive, failing_send)

    return failing_app


@pytest.mark.parametrize("spec", ["2.0", "2.4"])
async def test_exact_disconnect_sender_tie_keeps_native_cleanup_owned(monkeypatch, spec):
    cleanup, release = threading.Event(), threading.Event()
    active = maximum = 0

    def produce(text, emit, cancelled):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            emit(WAVE)
            if text == "First public fixture.":
                assert cancelled.wait(3)
                cleanup.set()
                assert release.wait(3)
        finally:
            active -= 1

    send_gate = asyncio.Event()
    tie = CompletionTie(monkeypatch, send_gate)
    backend = NativeBackend(produce)
    app = app_for(backend)
    first = Request(app, "First public fixture.", spec=spec, slow=True)
    first.resume_send = send_gate
    second = None
    try:
        await asyncio.wait_for(first.first.wait(), 3)
        second = Request(app, "Second public fixture.", spec=spec)
        await asyncio.wait_for(second.body_read.wait(), 3)
        await first.disconnect()
        await asyncio.wait_for(tie.ready.wait(), 3)
        assert len(tie.done) == 2
        assert isinstance(tie.sender_error, HTTPException)
        assert tie.sender_error.status_code == 499
        assert tie.sender_error is tie.stream.monitor_failure
        await entered(cleanup)
        assert backend.inputs == ["First public fixture."]
        assert not second.first.is_set()
        assert not first.task.done()
        assert len(first.frames()) == 1
        assert (await health(app))["active_producers"] == 1
    finally:
        release.set()
        if second is not None:
            await second.finish()
        await first.finish()
    assert first.maximum_receive_active == second.maximum_receive_active == 1
    assert maximum == 1
    assert len(first.frames()) == len(second.frames()) == 1
    assert backend.inputs == ["First public fixture.", "Second public fixture."]
    assert backend.loads == 1
    settled = await health(app)
    assert settled["active_producers"] == 0
    assert settled["producer_stopped"] is True
    assert settled["synthesis_quarantined"] is False


@pytest.mark.parametrize(
    "failure",
    [
        HTTPException(499, "Independent synthetic send499"),
        HTTPException(503, "Independent synthetic send503"),
        TTSBackendError("Independent synthetic output error"),
        RuntimeError("Independent synthetic send error"),
    ],
    ids=["distinct499", "send503", "output-error", "send-error"],
)
async def test_disconnect_tie_does_not_hide_independent_send_failure(monkeypatch, failure):
    def produce(text, emit, cancelled):
        emit(WAVE)
        assert cancelled.wait(3)

    send_gate = asyncio.Event()
    tie = CompletionTie(monkeypatch, send_gate)
    app = app_for(NativeBackend(produce))
    request = Request(with_send_failure(app, failure), slow=True)
    request.resume_send = send_gate
    await asyncio.wait_for(request.first.wait(), 3)
    await request.disconnect()
    await asyncio.wait_for(tie.ready.wait(), 3)
    assert tie.sender_error is failure
    assert tie.sender_error is not tie.stream.monitor_failure
    assert tie.stream.monitor_failure.status_code == 499
    if isinstance(failure, HTTPException):
        with pytest.raises(RuntimeError, match="response already started") as caught:
            await request.finish()
        assert caught.value.__cause__ is failure
    else:
        with pytest.raises(type(failure)) as caught:
            await request.finish()
        assert caught.value is failure
    assert request.maximum_receive_active == 1
    assert len(request.frames()) == 1
    assert (await health(app))["producer_stopped"] is True


@pytest.mark.parametrize("status", [409, 503])
async def test_exact_monitor_policy_failure_in_tie_is_not_a_disconnect(
    monkeypatch, tmp_path, status
):
    policy = ResourceState(tmp_path / "synthetic-policy.json")
    policy.save(phase="ready")

    def produce(text, emit, cancelled):
        emit(WAVE)
        assert cancelled.wait(3)

    send_gate = asyncio.Event()
    tie = CompletionTie(monkeypatch, send_gate)
    app = app_for(NativeBackend(produce), resource_policy=policy.path)
    request = Request(app, slow=True)
    request.resume_send = send_gate
    try:
        await asyncio.wait_for(request.first.wait(), 3)
        if status == 409:
            policy.save(phase="off", desired_converse=False)
        else:
            policy.path.unlink()  # Only this test's synthetic native policy fixture.
        await asyncio.wait_for(tie.ready.wait(), 3)
        assert tie.sender_error is tie.stream.monitor_failure
        assert tie.sender_error.status_code == status
        with pytest.raises(RuntimeError, match="response already started") as caught:
            await request.finish()
        assert caught.value.__cause__ is tie.sender_error
        assert request.maximum_receive_active == 1
        assert len(request.frames()) == 1
        assert (await health(app))["producer_stopped"] is True
    finally:
        policy.close()


async def test_disconnect_tie_preserves_independent_native_backend_failure(monkeypatch):
    fail_native = threading.Event()

    def produce(text, emit, cancelled):
        emit(WAVE)
        assert fail_native.wait(3)
        raise TTSBackendError("Synthetic native backend failure")

    send_gate = asyncio.Event()
    tie = CompletionTie(monkeypatch, send_gate, sender_first=True)
    app = app_for(NativeBackend(produce))
    request = Request(app, slow=True)
    request.resume_send = send_gate
    tie.on_sender_done = request.disconnect
    await asyncio.wait_for(request.first.wait(), 3)
    fail_native.set()
    send_gate.set()
    await asyncio.wait_for(tie.ready.wait(), 3)
    assert tie.sender_error is tie.stream.failure
    assert isinstance(tie.sender_error, TTSBackendError)
    assert tie.stream.monitor_failure.status_code == 499
    with pytest.raises(TTSBackendError, match="Selected voice backend returned invalid audio"):
        await request.finish()
    assert request.maximum_receive_active == 1
    assert len(request.frames()) == 1
    assert (await health(app))["producer_stopped"] is True


async def test_external_response_cancellation_is_not_swallowed_by_disconnect_tie(monkeypatch):
    def produce(text, emit, cancelled):
        emit(WAVE)
        assert cancelled.wait(3)

    send_gate = asyncio.Event()
    tie = CompletionTie(monkeypatch, send_gate, hold_return=True)
    app = app_for(NativeBackend(produce))
    request = Request(app, slow=True)
    request.resume_send = send_gate
    await asyncio.wait_for(request.first.wait(), 3)
    await request.disconnect()
    await asyncio.wait_for(tie.ready.wait(), 3)
    assert tie.sender_error is tie.stream.monitor_failure
    assert tie.sender_error.status_code == 499
    request.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request.finish()
    assert request.maximum_receive_active == 1
    assert len(request.frames()) == 1
    assert (await health(app))["producer_stopped"] is True
