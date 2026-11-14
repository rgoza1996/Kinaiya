"""Synthetic task-orchestrator grammar, actual isolation and owned cancellation."""

import asyncio
import hashlib
import threading
from types import SimpleNamespace

import pytest

from core.plugins.errors import PluginCandidateFailure, PluginRegistrationError
from core.task_harness import MAX_PHASES, TaskHarnessFailure, compute_phase, json_bytes, orchestrate
from services.agent.task_harness import task_harness_description
from services.agent.tools import ToolFailure

SOURCE = b"def run(payload):\n    return payload['input']\n"
BINDING = {"task_id": "synthetic-task", "objective_revision": 1}
REQUEST = {"operation": "transcribe_video", "source_path": "public.mp4", "source_sha256": "a" * 64}


@pytest.mark.parametrize(
    "change",
    [
        {"source_path": "../private.mp4"},
        {"source_sha256": "invalid"},
        {"source_bytes": True},
        {"source_bytes": 16_777_217},
        {"allowed_operations": []},
        {"allowed_operations": ["sam_hands", "sam_hands"]},
        {"allowed_operations": ["arbitrary_tool"]},
        {"required_operations": ["transcribe_video"]},
        {"prompt": "native-only prompt must not be disclosed"},
    ],
)
def test_native_source_metadata_refuses_malformed_or_authority_bearing_fields(change):
    description = {
        "source_path": "public.mp4",
        "source_sha256": "a" * 64,
        "source_bytes": 16,
        "allowed_operations": ["sam_hands"],
        "required_operations": [],
        **change,
    }

    class Factory:
        def __call__(self, *_):
            pytest.fail("metadata lookup constructed a media broker")

        def available(self, *_):
            return True

        def describe(self, *_):
            return description

    with pytest.raises(ToolFailure, match="Native task media description is unavailable"):
        task_harness_description(SimpleNamespace(task_harness_broker_factory=Factory()), object())


async def test_generated_phases_execute_broker_then_use_actual_observation():
    calls, payloads = [], []

    async def compute(source, payload):
        assert source == SOURCE
        payloads.append(payload)
        if payload["observation"] is None:
            return {"op": "consult_media", "request": REQUEST, "state": {"phase": 1}}
        return {"op": "finish", "result": payload["observation"]["text"]}

    async def consult(request):
        calls.append(request)
        return {"text": "Observed synthetic result", "operation": request["operation"]}

    result = await orchestrate(
        SOURCE, binding=BINDING, inputs={}, consult=consult, current=lambda: None, compute=compute
    )
    assert calls == [REQUEST]
    assert payloads[1]["state"] == {"phase": 1}
    assert result["result"] == "Observed synthetic result"
    assert result["source_sha256"] == hashlib.sha256(SOURCE).hexdigest()
    assert result["phases"] == 2
    assert result["task_completion"] == "not_established"
    assert len(result["consultations"]) == 1


@pytest.mark.parametrize(
    "action",
    [
        {"op": "finish", "result": "invented"},
        {"op": "shell", "request": {}, "state": None},
        {"op": "consult_media", "request": REQUEST, "state": None, "permission": "allow"},
        {"op": "consult_media", "request": [], "state": None},
        [],
        None,
    ],
)
async def test_untrusted_action_cannot_create_authority_or_finish_without_consultation(action):
    async def compute(*_):
        return action

    async def consult(_):
        pytest.fail("invalid candidate action reached broker")

    with pytest.raises(TaskHarnessFailure):
        await orchestrate(
            SOURCE,
            binding=BINDING,
            inputs={},
            consult=consult,
            current=lambda: None,
            compute=compute,
        )


async def test_phase_bound_refuses_before_a_sixteenth_media_call():
    calls = []

    async def compute(*_):
        return {"op": "consult_media", "request": REQUEST, "state": None}

    async def consult(request):
        calls.append(request)
        return {"text": "synthetic"}

    with pytest.raises(TaskHarnessFailure, match="exhausted"):
        await orchestrate(
            SOURCE,
            binding=BINDING,
            inputs={},
            consult=consult,
            current=lambda: None,
            compute=compute,
        )
    assert len(calls) == MAX_PHASES - 1


async def test_provider_failure_is_not_retried():
    calls = []

    async def compute(*_):
        return {"op": "consult_media", "request": REQUEST, "state": None}

    async def consult(request):
        calls.append(request)
        raise RuntimeError("synthetic original failed")

    with pytest.raises(RuntimeError, match="original failed"):
        await orchestrate(
            SOURCE,
            binding=BINDING,
            inputs={},
            consult=consult,
            current=lambda: None,
            compute=compute,
        )
    assert calls == [REQUEST]


async def test_revision_change_after_compute_prevents_dispatch():
    current_revision = [1]

    def current():
        if current_revision[0] != 1:
            raise RuntimeError("stale")

    async def compute(*_):
        current_revision[0] = 2
        return {"op": "consult_media", "request": REQUEST, "state": None}

    async def consult(_):
        pytest.fail("stale code reached provider")

    with pytest.raises(RuntimeError, match="stale"):
        await orchestrate(
            SOURCE, binding=BINDING, inputs={}, consult=consult, current=current, compute=compute
        )


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), 2**53, {1: "bad"}, b"bytes", "\ud800", "x" * 131073]
)
def test_json_bounds(value):
    with pytest.raises(TaskHarnessFailure):
        json_bytes(value)


async def test_source_rejects_network_import_before_compute():
    async def compute(*_):
        pytest.fail("invalid source executed")

    with pytest.raises(PluginRegistrationError):
        await orchestrate(
            b"import socket\ndef run(payload):\n return {}\n",
            binding=BINDING,
            inputs={},
            consult=None,
            current=lambda: None,
            compute=compute,
        )


async def test_cancellation_waits_for_original_compute_thread(monkeypatch):
    import core.task_harness as module

    started, release, observed = threading.Event(), threading.Event(), threading.Event()

    def blocked(*_, cancel_event, **__):
        started.set()
        assert release.wait(5)
        assert cancel_event.is_set()
        observed.set()
        raise PluginCandidateFailure("candidate cancelled; group killed")

    monkeypatch.setattr(module, "run_plugin", blocked)
    operation = asyncio.create_task(compute_phase(SOURCE, {}))
    while not started.is_set():
        await asyncio.sleep(0)
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()
    operation.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert observed.is_set()


async def test_original_compute_cleanup_failure_is_not_hidden_by_cancellation(monkeypatch):
    import core.task_harness as module

    started, release = threading.Event(), threading.Event()

    def broken(*_, **__):
        started.set()
        assert release.wait(5)
        raise OSError("synthetic original process reap failed")

    monkeypatch.setattr(module, "run_plugin", broken)
    operation = asyncio.create_task(compute_phase(SOURCE, {}))
    while not started.is_set():
        await asyncio.sleep(0)
    operation.cancel()
    release.set()
    with pytest.raises(OSError, match="reap failed"):
        await operation


async def test_actual_isolated_program_consumes_broker_result():
    source = b"""def run(payload):
    if payload["observation"] is None:
        return {"op": "consult_media", "request": payload["input"], "state": None}
    return {"op": "finish", "result": {"transcript": payload["observation"]["text"]}}
"""
    calls = []

    async def consult(request):
        calls.append(request)
        return {"text": "Synthetic isolated observation"}

    result = await orchestrate(
        source, binding=BINDING, inputs=REQUEST, consult=consult, current=lambda: None
    )
    assert result["result"] == {"transcript": "Synthetic isolated observation"}
    assert calls == [REQUEST]


async def test_actual_isolation_cannot_read_host_or_open_socket():
    # Bypass source checks to exercise the existing kernel boundary directly.
    for source in [
        b"def run(payload):\n return open('/etc/passwd').read()\n",
        b"import socket\ndef run(payload):\n return socket.socket().fileno()\n",
    ]:
        with pytest.raises(PluginCandidateFailure):
            await compute_phase(source, {})
