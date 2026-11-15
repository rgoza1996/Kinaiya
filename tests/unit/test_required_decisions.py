"""Required runtime observations cannot become indefinite Converse permission."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from packages.providers.text import ScriptedProvider
from services.converse import dependencies
from services.converse.dependencies import RequiredDecisions
from services.converse.intent import ConverseIntent
from services.converse.runtime import ConverseRuntime


def observation(**updates):
    return {
        "contract_version": 1,
        "service": "decisions",
        "consumer": "converse",
        "instance_id": "a" * 32,
        "phase": "ready",
        "ready": True,
        **updates,
    }


@asynccontextmanager
async def environment(tmp_path, monkeypatch):
    state = {
        "body": json.dumps(observation()).encode(),
        "headers": {"content-type": "application/json"},
        "status": 200,
        "calls": [],
    }

    async def request(req):
        state["calls"].append(req)
        if "before_response" in state:
            state["before_response"]()
        return httpx.Response(
            state["status"],
            headers=state["headers"],
            stream=state.get("stream") or httpx.ByteStream(state["body"]),
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        dependencies.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(request), **kwargs),
    )
    gate = RequiredDecisions("http://127.0.0.1:1", "c" * 40)
    intent = ConverseIntent(tmp_path / "intent.json")
    runtime = ConverseRuntime(None, ScriptedProvider([]), intent=intent, decisions_dependency=gate)
    try:
        yield state, gate, runtime
    finally:
        await runtime.close()
        assert gate._client.is_closed
        assert gate._task is None or gate._task.done()


async def test_missing_dependency_stays_off_until_authenticated_observation(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (state, gate, runtime):
        before = runtime.intent.path.read_bytes()
        assert runtime.enabled is False
        assert runtime.availability()["decisions"] == {
            "required": True,
            "ready": False,
            "state": "starting",
        }
        assert (await runtime.set_enabled(True))["enabled"] is False
        with pytest.raises(RuntimeError, match="Decisions runtime"):
            runtime.admission()
        await gate.start()
        assert runtime.enabled is True
        admission = runtime.admission()
        await gate.refresh()
        assert runtime.admission_current(admission)
        assert runtime.intent.path.read_bytes() == before
        assert all(req.headers["authorization"] == "Bearer " + "c" * 40 for req in state["calls"])
        assert all(req.url.path == "/v1/decisions/readiness" for req in state["calls"])
        assert all(req.headers["accept-encoding"] == "identity" for req in state["calls"])
        assert len(state["calls"]) == 2


BAD_RESPONSES = (
    [
        (status, b"{}", {"content-type": "application/json"})
        for status in (301, 302, 401, 403, 404, 500, 503)
    ]
    + [
        (200, body, {"content-type": "application/json"})
        for body in (
            b"",
            b"{",
            b"null",
            b"[]",
            b"true",
            b"\xff",
            b" " * 4097,
            b"[" * 1500 + b"]" * 1500,
            json.dumps(observation()).encode()[:-1] + b',"ready":true}',
            *(
                json.dumps(observation(**updates)).encode()
                for updates in (
                    {"contract_version": True},
                    {"contract_version": "1"},
                    {"contract_version": 2},
                    {"service": "agent"},
                    {"consumer": "agent"},
                    {"instance_id": "x" * 32},
                    {"ready": 1},
                    {"ready": "true"},
                    {"phase": "unavailable"},
                    {"phase": "pretend_ready"},
                    {"arbitrary": "No disclosure is needed to check readiness"},
                )
            ),
        )
    ]
    + [
        (200, json.dumps(observation()).encode(), headers)
        for headers in (
            {},
            {"content-type": "text/html"},
            {"content-type": "application/json", "content-encoding": "gzip"},
        )
    ]
)


@pytest.mark.parametrize("status,body,headers", BAD_RESPONSES)
async def test_bad_or_unauthenticated_response_fences_old_admission(
    tmp_path, monkeypatch, status, body, headers
):
    async with environment(tmp_path, monkeypatch) as (state, gate, runtime):
        await gate.start()
        old = runtime.admission()
        state.update(status=status, body=body, headers=headers)
        await gate.refresh()
        assert runtime.enabled is False
        assert runtime.admission_current(old) is False
        assert runtime.availability()["decisions"]["state"] == "unavailable"
        # No redirect, retry, model call, inferred Ignore, or owner-intent write.
        assert len(state["calls"]) == 2
        assert runtime.intent.read().enabled is True and runtime.intent.read().revision == 0


async def test_restart_and_recovery_allow_only_fresh_admissions(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (state, gate, runtime):
        await gate.start()
        old = runtime.admission()
        state["body"] = json.dumps(observation(instance_id="b" * 32)).encode()
        await gate.refresh()
        assert runtime.enabled and not runtime.admission_current(old)
        fresh = runtime.admission()
        state["body"] = json.dumps(observation(ready=False, phase="unavailable")).encode()
        await gate.refresh()
        assert not runtime.enabled and not runtime.admission_current(fresh)
        assert (await runtime.set_enabled(True))["enabled"] is False
        state["body"] = json.dumps(observation(instance_id="c" * 32)).encode()
        await gate.refresh()
        assert runtime.enabled and not runtime.admission_current(fresh)
        await runtime.set_enabled(False, owner=True)
        await gate.refresh()
        assert runtime.enabled is False
        assert runtime.intent.read().enabled is False and runtime.intent.read().revision == 1


@pytest.mark.parametrize("response_delay", [0.0, 0.75])
async def test_stale_positive_proof_expires_and_refresh_cannot_revive_old_work(
    tmp_path, monkeypatch, response_delay
):
    clock = [0.0]
    monkeypatch.setattr(dependencies, "monotonic", lambda: clock[0])
    async with environment(tmp_path, monkeypatch) as (state, gate, runtime):
        state["before_response"] = lambda: clock.__setitem__(0, clock[0] + response_delay)
        await gate.start()
        old = runtime.admission()
        clock[0] = dependencies.MAX_OBSERVATION_AGE + 0.01
        assert not runtime.enabled
        assert runtime.availability()["decisions"]["state"] == "stale"
        assert not runtime.admission_current(old)
        await gate.refresh()
        assert runtime.enabled and not runtime.admission_current(old)


async def test_monitor_death_and_close_invalidate_even_fresh_proof(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (_, gate, runtime):
        await gate.start()
        old = runtime.admission()
        gate._task.cancel()
        await asyncio.gather(gate._task, return_exceptions=True)
        assert not runtime.enabled and not runtime.admission_current(old)
        assert gate.snapshot()["state"] == "stopped"
        await gate.close()
        await gate.close()
        assert not gate.ready and gate._client.is_closed


async def test_uncertain_fence_does_not_become_ready_on_the_next_probe(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (_, gate, runtime):

        def failed_fence():
            raise RuntimeError("Synthetic native cleanup uncertainty")

        monkeypatch.setattr(runtime, "decisions_dependency_changed", failed_fence)
        await gate.start()
        assert not runtime.enabled
        await gate.refresh()
        assert not runtime.enabled
        assert gate.snapshot()["state"] == "cleanup_unavailable"


async def test_failed_dependency_close_still_retires_runtime_resources(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (_, gate, runtime):
        await gate.start()
        closed = []

        class Adapter:
            async def close(self):
                closed.append(True)

        runtime.memory.adapters["synthetic-owner"] = Adapter()
        original = gate.close

        async def failed_close():
            await original()
            raise OSError("Synthetic dependency close failure")

        with monkeypatch.context() as patch:
            patch.setattr(gate, "close", failed_close)
            with pytest.raises(OSError, match="Synthetic dependency close failure"):
                await runtime.close()
        assert closed == [True]
        assert runtime._memory_shutdown.done() and runtime._stt_stopping
        assert not runtime.enabled and gate._client.is_closed


async def test_stalled_body_has_one_total_deadline_and_closes_original_stream(
    tmp_path, monkeypatch
):
    class Stalled(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"{"
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(dependencies, "PROBE_TIMEOUT", 0.05)
    async with environment(tmp_path, monkeypatch) as (state, gate, runtime):
        await gate.start()
        old = runtime.admission()
        stream = Stalled()
        state["stream"] = stream
        await gate.refresh()
        assert stream.closed
        assert not runtime.enabled and not runtime.admission_current(old)
        assert len(state["calls"]) == 2


@pytest.mark.parametrize("kind", ["finalized", "window"])
async def test_real_worker_reports_ready_only_after_successful_tick_and_clears_on_loss(kind):
    from services.decisions.runtime import DecisionsWorker
    from services.decisions.window_worker import WindowDecisionsWorker

    worker = (
        DecisionsWorker(None, None, None)
        if kind == "finalized"
        else WindowDecisionsWorker(None, None, None)
    )
    entered = [asyncio.Event() for _ in range(4)]
    release = [asyncio.Event() for _ in range(3)]
    tick_count = 0

    async def tick():
        nonlocal tick_count
        index = tick_count
        tick_count += 1
        entered[index].set()
        if index < 3:
            await release[index].wait()
        else:
            await asyncio.Event().wait()
        if index == 1:
            raise OSError("Synthetic journal outage")
        return 0

    worker.tick = tick
    task = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(5):
            await entered[0].wait()
            assert worker.running and not worker.ready
            release[0].set()
            await entered[1].wait()
            assert worker.ready
            release[1].set()
            await entered[2].wait()
            assert not worker.ready and worker.last_error == "OSError"
            release[2].set()
            await entered[3].wait()
            assert worker.ready
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not worker.running and not worker.ready


async def test_dependency_loss_cancels_original_active_producer(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (state, gate, runtime):
        await gate.start()
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def producer():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                stopped.set()

        original = asyncio.create_task(producer())
        runtime._active["public-synthetic-session"] = original
        await started.wait()
        state["status"] = 503
        await gate.refresh()
        await asyncio.gather(original, return_exceptions=True)
        assert stopped.is_set() and original.cancelled()
        assert not runtime.enabled


async def test_dependency_fence_cancels_original_native_asr_and_never_replays_it(tmp_path):
    from test_native_audio_converse import NativeModel, admit, body
    from test_native_audio_converse import environment as native_environment

    from packages.providers.speech import SpeechProviderError

    model = NativeModel()
    model.asr_release.clear()
    async with native_environment(tmp_path, native=model) as env:
        request = body(env)
        waiter = asyncio.create_task(admit(env, request))
        try:
            await asyncio.wait_for(model.asr_entered.wait(), 1)
            original = env.owner._clips[request["request_id"]]
            env.runtime.decisions_dependency_changed()
            result = (await asyncio.wait_for(asyncio.gather(waiter, return_exceptions=True), 1))[0]
            assert isinstance(result, SpeechProviderError) and result.status_code == 409
            assert original.fenced and original.task.cancelled()
            assert env.store.read(kinds=["conversation.window", "conversation.converse"]) == []
            with pytest.raises(SpeechProviderError) as refused:
                await admit(env, request)
            assert refused.value.status_code == 409
            assert len(model.transcriptions) == 1
        finally:
            model.asr_release.set()
            await asyncio.gather(waiter, return_exceptions=True)


async def test_cancelled_observer_still_propagates_cancellation_when_original_is_fenced(tmp_path):
    from test_native_audio_converse import NativeModel, admit, body
    from test_native_audio_converse import environment as native_environment

    from packages.providers.speech import SpeechProviderError

    model = NativeModel()
    model.asr_release.clear()
    async with native_environment(tmp_path, native=model) as env:
        request = body(env)
        waiter = asyncio.create_task(admit(env, request))
        try:
            await asyncio.wait_for(model.asr_entered.wait(), 1)
            waiter.cancel()
            env.runtime.decisions_dependency_changed()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiter, 1)
            with pytest.raises(SpeechProviderError) as refused:
                await admit(env, request)
            assert refused.value.status_code == 409
            assert len(model.transcriptions) == 1
            assert env.store.read(kinds=["conversation.window", "conversation.converse"]) == []
        finally:
            model.asr_release.set()
            await asyncio.gather(waiter, return_exceptions=True)


async def test_dependency_fence_does_not_publish_late_native_reply_after_recovery(tmp_path):
    from test_native_audio_converse import NativeModel, admit, decide, settle, source
    from test_native_audio_converse import environment as native_environment

    model = NativeModel()
    model.reply_release.clear()
    async with native_environment(tmp_path, native=model) as env:
        entry = source(env, await admit(env))
        await env.controller.process(decide(env, entry))
        try:
            await asyncio.wait_for(model.reply_entered.wait(), 1)
            old = env.runtime.admission()
            env.runtime.decisions_dependency_changed()
            assert env.runtime.enabled and not env.runtime.admission_current(old)
            model.reply_release.set()
            await settle(env, cancelled=True)
            assert env.store.read(kinds=["conversation.converse"]) == []
            assert len(model.responses) == 1
            assert env.provider.streams == env.provider.counts == []
        finally:
            model.reply_release.set()


@pytest.mark.parametrize(
    "url", [None, "", "ftp://host", "http://u:p@host", "http://host/?q=1", "http://host/#x"]
)
def test_invalid_required_endpoint_is_not_a_fallback(url):
    with pytest.raises(ValueError, match="Decisions service URL"):
        RequiredDecisions(url, "c" * 40)
