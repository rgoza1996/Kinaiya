"""Unit tests for Whistle incremental streaming IPC, endpoint, and broker (#93, #49)."""

import asyncio
import hashlib
import os
import socket
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from packages.providers.whistle_ipc import socket_path
from packages.providers.whistle_worker import serve
from services.common.auth import Auth
from services.resources.api import create_app
from services.resources.whistle import WhistleBroker, WhistleEndpoint

AUTH = Auth({name: name[0] * 40 for name in ("owner", "converse", "agent", "resources")})
INCARNATION = "a" * 64


class _FakeStreamEngine:
    def __init__(self):
        self.active = False
        self.samples = 0
        self.committed = ""
        self.pcm_calls = 0
        self.finish_calls = 0

    def transcribe(self, data: bytes) -> str:
        if self.active:
            raise RuntimeError("stream active")
        return "batch transcript"

    def begin_stream(self):
        if self.active:
            raise RuntimeError("already active")
        self.active = True
        self.samples = 0
        self.committed = ""
        return None

    def stream_pcm(self, pcm16: bytes) -> dict:
        if not self.active:
            raise RuntimeError("not active")
        self.pcm_calls += 1
        added = len(pcm16) // 2
        self.samples += added
        if self.pcm_calls == 1:
            return {
                "delta_text": "",
                "committed_text": "",
                "pending_text": "hello wor",
                "received_samples": self.samples,
                "final": False,
                "native_pass_ms": 12.5,
                "language": "en",
            }
        self.committed = "hello world"
        return {
            "delta_text": "hello world",
            "committed_text": self.committed,
            "pending_text": "testing",
            "received_samples": self.samples,
            "final": False,
            "native_pass_ms": 15.0,
            "language": "en",
        }

    def finish_stream(self) -> dict:
        if not self.active:
            raise RuntimeError("not active")
        self.finish_calls += 1
        self.active = False
        final_text = (self.committed + " testing").strip()
        return {
            "delta_text": "testing",
            "committed_text": final_text,
            "pending_text": "",
            "received_samples": self.samples,
            "final": True,
            "native_pass_ms": 8.0,
            "language": "en",
        }


class _FakeManager:
    def __init__(self, pid: int):
        self.identity = SimpleNamespace(worker_id="whistle", pid=pid, generation=1)
        self.current = True
        self.stopped = 0

    def owned_identity(self, worker_id: str):
        return self.identity if self.current and worker_id == "whistle" else None

    def is_current(self, identity):
        return self.current and identity == self.identity

    async def stop_if_current(self, identity):
        self.stopped += 1
        self.current = False
        return {
            "ok": True,
            "worker_id": "whistle",
            "release_confirmed": True,
            "stop_evidence": {"reaped": True, "group_empty": True},
        }


class _FakeCoordinator:
    def __init__(self, manager):
        self.manager = manager
        self.standing_barrier = None
        self.state = SimpleNamespace(record=SimpleNamespace(phase="ready"))
        self.policy_rev = (1, "conversation_priority", True)

    def register_standing_composition(self, **kwargs):
        pass

    def whistle_policy(self):
        return self.policy_rev

    async def admit_whistle(self, policy):
        return self.manager.identity

    def status(self):
        return {"phase": "ready"}


@pytest.fixture
async def broker_fixture():
    directory = Path(tempfile.mkdtemp(prefix="whistle-unit-", dir="/tmp"))
    endpoint = WhistleEndpoint(directory)
    engine = _FakeStreamEngine()
    pid = os.getpid()
    sock = socket_path(directory, pid)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock))
    listener.listen(4)
    server_thread = threading.Thread(
        target=serve,
        args=(directory, engine, INCARNATION, "seccomp-network-v1", listener, pid),
        daemon=True,
    )
    server_thread.start()
    process = SimpleNamespace(pid=pid, returncode=None)
    assert await endpoint.probe(None, process)
    manager = _FakeManager(pid)
    coordinator = _FakeCoordinator(manager)
    broker = WhistleBroker(coordinator, endpoint, request_timeout_s=5)
    app = create_app(coordinator, AUTH, whistle=broker)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://resources.local",
        headers={"Authorization": "Bearer " + AUTH.tokens["converse"]},
    ) as client:
        try:
            yield SimpleNamespace(
                client=client,
                broker=broker,
                endpoint=endpoint,
                engine=engine,
                manager=manager,
                coordinator=coordinator,
            )
        finally:
            await broker.close()
            listener.close()
            if sock.exists():
                sock.unlink()
            directory.rmdir()


async def test_whistle_broker_incremental_lifecycle_and_idempotent_ack_recovery(broker_fixture):
    env = broker_fixture
    start_resp = await env.client.post(
        "/v1/audio/transcriptions/stream/begin",
        json={
            "session_id": "sess_1",
            "thread_id": "thr_1",
            "epoch": 1,
            "utterance_id": "utt_1",
            "content_scope": "local_only",
        },
    )
    assert start_resp.status_code == 200
    started = start_resp.json()
    stream_id = started["stream_id"]
    incarnation = started["worker_incarnation"]
    assert incarnation == INCARNATION

    # While stream is active, batch transcription shares the single slot and is refused with 409
    batch_conflict = await env.client.post(
        "/v1/audio/transcriptions",
        content=b"RIFF....",
        headers={"Content-Type": "audio/wav"},
    )
    assert batch_conflict.status_code == 409

    # First PCM append (160 samples = 320 bytes)
    pcm1 = b"\x01\x00" * 160
    sha1 = hashlib.sha256(pcm1).hexdigest()
    headers1 = {
        "Content-Type": "application/octet-stream",
        "x-kinaiya-stream-id": stream_id,
        "x-kinaiya-worker-incarnation": incarnation,
        "x-kinaiya-session-id": "sess_1",
        "x-kinaiya-thread-id": "thr_1",
        "x-kinaiya-epoch": "1",
        "x-kinaiya-utterance-id": "utt_1",
        "x-kinaiya-content-scope": "local_only",
        "x-kinaiya-sequence": "1",
        "x-kinaiya-sample-offset": "0",
        "x-kinaiya-pcm-sha256": sha1,
    }
    resp1 = await env.client.post(
        "/v1/audio/transcriptions/stream/pcm", content=pcm1, headers=headers1
    )
    assert resp1.status_code == 200
    ack1 = resp1.json()
    assert ack1["received_samples"] == 160
    assert ack1["pending_text"] == "hello wor"
    assert env.engine.pcm_calls == 1

    # Idempotent retry of the exact last append returns cached ack without calling native engine again
    retry1 = await env.client.post(
        "/v1/audio/transcriptions/stream/pcm", content=pcm1, headers=headers1
    )
    assert retry1.status_code == 200
    assert retry1.json()["replay"] is True
    assert env.engine.pcm_calls == 1

    # Second contiguous PCM append (offset 160, 160 samples)
    pcm2 = b"\x02\x00" * 160
    sha2 = hashlib.sha256(pcm2).hexdigest()
    headers2 = {
        **headers1,
        "x-kinaiya-sequence": "2",
        "x-kinaiya-sample-offset": "160",
        "x-kinaiya-pcm-sha256": sha2,
    }
    resp2 = await env.client.post(
        "/v1/audio/transcriptions/stream/pcm", content=pcm2, headers=headers2
    )
    assert resp2.status_code == 200
    ack2 = resp2.json()
    assert ack2["received_samples"] == 320
    assert ack2["committed_text"] == "hello world"
    assert ack2["pending_text"] == "testing"

    # Finish stream
    fin_resp = await env.client.post(
        "/v1/audio/transcriptions/stream/finish",
        json={
            "stream_id": stream_id,
            "worker_incarnation": incarnation,
            "session_id": "sess_1",
            "thread_id": "thr_1",
            "epoch": 1,
            "utterance_id": "utt_1",
            "content_scope": "local_only",
            "sequence": 3,
            "sample_offset": 320,
        },
    )
    assert fin_resp.status_code == 200
    fin = fin_resp.json()
    assert fin["final"] is True
    assert fin["committed_text"] == "hello world testing"
    assert fin["pending_text"] == ""
    assert env.broker.operation is None


async def _begin_retention_stream(env):
    response = await env.client.post(
        "/v1/audio/transcriptions/stream/begin",
        json={
            "session_id": "sess_retention",
            "thread_id": "thr_retention",
            "epoch": 1,
            "utterance_id": "utt_retention",
            "content_scope": "local_only",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _retention_headers(started, sequence, offset, pcm):
    return {
        "Content-Type": "application/octet-stream",
        "x-kinaiya-stream-id": started["stream_id"],
        "x-kinaiya-worker-incarnation": started["worker_incarnation"],
        "x-kinaiya-session-id": "sess_retention",
        "x-kinaiya-thread-id": "thr_retention",
        "x-kinaiya-epoch": "1",
        "x-kinaiya-utterance-id": "utt_retention",
        "x-kinaiya-content-scope": "local_only",
        "x-kinaiya-sequence": str(sequence),
        "x-kinaiya-sample-offset": str(offset),
        "x-kinaiya-pcm-sha256": hashlib.sha256(pcm).hexdigest(),
    }


async def test_tiny_contiguous_chunks_retain_only_current_work_and_last_ack(
    broker_fixture, monkeypatch
):
    env = broker_fixture
    started = await _begin_retention_stream(env)
    operation = env.broker.operation

    def silent_pcm(pcm):
        env.engine.pcm_calls += 1
        env.engine.samples += len(pcm) // 2
        return {
            "delta_text": "",
            "committed_text": "",
            "pending_text": "",
            "received_samples": env.engine.samples,
            "final": False,
            "native_pass_ms": 0,
            "language": None,
        }

    monkeypatch.setattr(env.engine, "stream_pcm", silent_pcm)
    pcm = b"\0\0"
    for sequence in range(1, 257):
        headers = _retention_headers(started, sequence, sequence - 1, pcm)
        response = await env.client.post(
            "/v1/audio/transcriptions/stream/pcm", content=pcm, headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["received_samples"] == sequence
        assert env.broker.operation is operation
        assert operation.work is None
        assert operation.work_tasks == []
        assert operation.stream["last_ack"]["sequence"] == sequence
    replay = await env.client.post(
        "/v1/audio/transcriptions/stream/pcm", content=pcm, headers=headers
    )
    assert replay.status_code == 200 and replay.json()["replay"] is True
    assert env.engine.pcm_calls == 256
    finished = await env.client.post(
        "/v1/audio/transcriptions/stream/finish",
        json={
            "stream_id": started["stream_id"],
            "worker_incarnation": started["worker_incarnation"],
            "session_id": "sess_retention",
            "thread_id": "thr_retention",
            "epoch": 1,
            "utterance_id": "utt_retention",
            "content_scope": "local_only",
            "sequence": 257,
            "sample_offset": 256,
        },
    )
    assert finished.status_code == 200, finished.text
    assert env.broker.operation is None
    assert operation.work_tasks == []


async def test_unknown_cleanup_retains_original_work_even_after_late_settlement(
    broker_fixture, monkeypatch
):
    env = broker_fixture
    started = await _begin_retention_stream(env)
    operation = env.broker.operation
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    originals = []

    async def held_pcm(pcm, identity, *, expected_samples):
        originals.append(asyncio.current_task())
        entered.set()
        while not release.is_set():
            try:
                await asyncio.wait_for(release.wait(), timeout=10)
            except asyncio.CancelledError:
                cancelled.set()
        return {
            "delta_text": "",
            "committed_text": "",
            "pending_text": "",
            "received_samples": expected_samples,
            "final": False,
            "native_pass_ms": 0,
            "language": None,
        }

    async def unknown_stop(identity):
        return {"ok": False, "release_confirmed": False}

    stop = env.manager.stop_if_current
    monkeypatch.setattr(env.endpoint, "stream_pcm", held_pcm)
    monkeypatch.setattr(env.manager, "stop_if_current", unknown_stop)
    pcm = b"\0\0"
    request = asyncio.create_task(
        env.client.post(
            "/v1/audio/transcriptions/stream/pcm",
            content=pcm,
            headers=_retention_headers(started, 1, 0, pcm),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        original = originals[0]
        assert operation.work is original
        assert operation.work_tasks == [original]
        env.broker.abort_active()
        await asyncio.wait_for(cancelled.wait(), timeout=10)
        await asyncio.wait_for(asyncio.shield(operation.cleanup), timeout=10)
        assert not original.done()
        assert env.broker.operation is operation
        assert operation.work_tasks == [original]
        assert operation.cleanup_failed and not operation.cleanup_confirmed
        blocked = await asyncio.wait_for(
            env.client.post(
                "/v1/audio/transcriptions/stream/begin",
                json={"session_id": "new", "thread_id": "new", "epoch": 1, "utterance_id": "new"},
            ),
            timeout=10,
        )
        assert blocked.status_code == 409
        assert not original.done()
        release.set()
        response = await asyncio.wait_for(asyncio.shield(request), timeout=10)
        assert response.status_code == 409
        await asyncio.wait_for(asyncio.shield(operation.cleanup), timeout=10)
        assert original.done()
        assert env.broker.operation is operation
        assert operation.work_tasks == [original]
        assert operation.stream["sequence"] == 0
        assert operation.stream["last_ack"] is None
        monkeypatch.setattr(env.manager, "stop_if_current", stop)
        env.broker.abort_active()
        await asyncio.wait_for(asyncio.shield(operation.cleanup), timeout=10)
        assert operation.cleanup_confirmed
        assert env.broker.operation is None
    finally:
        release.set()
        monkeypatch.setattr(env.manager, "stop_if_current", stop)
        env.broker.abort_active()
        await asyncio.wait_for(
            asyncio.shield(asyncio.gather(request, return_exceptions=True)), timeout=10
        )
        if operation.cleanup is not None:
            await asyncio.wait_for(asyncio.shield(operation.cleanup), timeout=10)


async def test_whistle_broker_non_contiguous_offset_aborts_and_reaps_worker(broker_fixture):
    env = broker_fixture
    start_resp = await env.client.post(
        "/v1/audio/transcriptions/stream/begin",
        json={
            "session_id": "sess_1",
            "thread_id": "thr_1",
            "epoch": 1,
            "utterance_id": "utt_gap",
            "content_scope": "local_only",
        },
    )
    started = start_resp.json()
    pcm = b"\x01\x00" * 160
    sha = hashlib.sha256(pcm).hexdigest()
    bad_headers = {
        "Content-Type": "application/octet-stream",
        "x-kinaiya-stream-id": started["stream_id"],
        "x-kinaiya-worker-incarnation": started["worker_incarnation"],
        "x-kinaiya-session-id": "sess_1",
        "x-kinaiya-thread-id": "thr_1",
        "x-kinaiya-epoch": "1",
        "x-kinaiya-utterance-id": "utt_gap",
        "x-kinaiya-content-scope": "local_only",
        "x-kinaiya-sequence": "1",
        "x-kinaiya-sample-offset": "80",  # Non-contiguous gap!
        "x-kinaiya-pcm-sha256": sha,
    }
    resp = await env.client.post(
        "/v1/audio/transcriptions/stream/pcm", content=pcm, headers=bad_headers
    )
    assert resp.status_code == 409


async def test_whistle_broker_gated_intake_interleavings_cancellation_and_owner_off(
    broker_fixture,
):
    import asyncio

    from fastapi import HTTPException

    env = broker_fixture
    start_resp = await env.client.post(
        "/v1/audio/transcriptions/stream/begin",
        json={
            "session_id": "sess_1",
            "thread_id": "thr_1",
            "epoch": 1,
            "utterance_id": "utt_interleave",
            "content_scope": "local_only",
        },
    )
    assert start_resp.status_code == 200
    started = start_resp.json()
    stream_id = started["stream_id"]
    incarnation = started["worker_incarnation"]

    pcm1 = b"\x01\x00" * 160
    sha1 = hashlib.sha256(pcm1).hexdigest()
    headers1 = {
        "content-type": "application/octet-stream",
        "x-kinaiya-stream-id": stream_id,
        "x-kinaiya-worker-incarnation": incarnation,
        "x-kinaiya-session-id": "sess_1",
        "x-kinaiya-thread-id": "thr_1",
        "x-kinaiya-epoch": "1",
        "x-kinaiya-utterance-id": "utt_interleave",
        "x-kinaiya-content-scope": "local_only",
        "x-kinaiya-sequence": "1",
        "x-kinaiya-sample-offset": "0",
        "x-kinaiya-pcm-sha256": sha1,
    }

    class _GatedRequest:
        def __init__(self, headers: dict, body: bytes, entered: asyncio.Event, gate: asyncio.Event):
            self.headers = headers
            self._body = body
            self._entered = entered
            self._gate = gate

        async def stream(self):
            self._entered.set()
            await self._gate.wait()
            yield self._body

        async def is_disconnected(self):
            return False

        async def receive(self):
            await asyncio.Future()

    # 1. Gated append/append and append/finish interleaving during body intake:
    entered1 = asyncio.Event()
    gate1 = asyncio.Event()
    req1 = _GatedRequest(headers1, pcm1, entered1, gate1)
    task1 = asyncio.create_task(env.broker.append_stream(req1))
    await entered1.wait()

    # Concurrent duplicate append and concurrent finish must be rejected before intake/dispatch.
    dup_resp = await env.client.post(
        "/v1/audio/transcriptions/stream/pcm", content=pcm1, headers=headers1
    )
    assert dup_resp.status_code == 409
    fin_conflict = await env.client.post(
        "/v1/audio/transcriptions/stream/finish",
        json={
            "stream_id": stream_id,
            "worker_incarnation": incarnation,
            "session_id": "sess_1",
            "thread_id": "thr_1",
            "epoch": 1,
            "utterance_id": "utt_interleave",
            "content_scope": "local_only",
            "sequence": 1,
            "sample_offset": 0,
        },
    )
    assert fin_conflict.status_code == 409
    assert env.engine.pcm_calls == 0
    assert env.engine.finish_calls == 0

    gate1.set()
    ack1 = await task1
    assert ack1["sequence"] == 1
    assert ack1["received_samples"] == 160
    assert env.engine.pcm_calls == 1

    # Exact acknowledged replay remains preserved after completion.
    replay1 = await env.client.post(
        "/v1/audio/transcriptions/stream/pcm", content=pcm1, headers=headers1
    )
    assert replay1.status_code == 200
    assert replay1.json()["replay"] is True
    assert env.engine.pcm_calls == 1

    # 2. Cancellation while append intake is held prevents native dispatch:
    pcm2 = b"\x02\x00" * 160
    sha2 = hashlib.sha256(pcm2).hexdigest()
    headers2 = {
        **headers1,
        "x-kinaiya-sequence": "2",
        "x-kinaiya-sample-offset": "160",
        "x-kinaiya-pcm-sha256": sha2,
    }
    entered2 = asyncio.Event()
    gate2 = asyncio.Event()
    req2 = _GatedRequest(headers2, pcm2, entered2, gate2)
    task2 = asyncio.create_task(env.broker.append_stream(req2))
    await entered2.wait()

    cancel_resp = await env.client.post(
        "/v1/audio/transcriptions/stream/cancel",
        json={"stream_id": stream_id, "worker_incarnation": incarnation},
    )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["cancelled"] is True

    gate2.set()
    with pytest.raises(HTTPException) as exc_info:
        await task2
    assert exc_info.value.status_code == 409
    assert env.engine.pcm_calls == 1


async def test_whistle_broker_owner_off_during_append_intake_refuses_before_dispatch(
    broker_fixture,
):
    import asyncio

    from fastapi import HTTPException

    env = broker_fixture
    start = (
        await env.client.post(
            "/v1/audio/transcriptions/stream/begin",
            json={
                "session_id": "sess_1",
                "thread_id": "thr_1",
                "epoch": 1,
                "utterance_id": "utt_owner_off",
                "content_scope": "local_only",
            },
        )
    ).json()
    pcm1 = b"\x01\x00" * 160
    sha1 = hashlib.sha256(pcm1).hexdigest()
    headers_off = {
        "content-type": "application/octet-stream",
        "x-kinaiya-stream-id": start["stream_id"],
        "x-kinaiya-worker-incarnation": start["worker_incarnation"],
        "x-kinaiya-session-id": "sess_1",
        "x-kinaiya-thread-id": "thr_1",
        "x-kinaiya-epoch": "1",
        "x-kinaiya-utterance-id": "utt_owner_off",
        "x-kinaiya-content-scope": "local_only",
        "x-kinaiya-sequence": "1",
        "x-kinaiya-sample-offset": "0",
        "x-kinaiya-pcm-sha256": sha1,
    }
    entered = asyncio.Event()
    gate = asyncio.Event()

    class _GatedRequest:
        headers = headers_off

        async def stream(self):
            entered.set()
            await gate.wait()
            yield pcm1

        async def is_disconnected(self):
            return False

        async def receive(self):
            await asyncio.Future()

    task = asyncio.create_task(env.broker.append_stream(_GatedRequest()))
    await entered.wait()

    # Owner disables Converse / policy changes while intake is held.
    env.coordinator.policy_rev = (2, "sleep", False)
    gate.set()
    with pytest.raises(HTTPException) as exc_off:
        await task
    assert exc_off.value.status_code == 409
    assert env.engine.pcm_calls == 0
    assert env.broker.operation is None


async def test_whistle_broker_owner_off_during_finish_intake_refuses_before_dispatch(
    broker_fixture,
):
    import asyncio
    import json

    from fastapi import HTTPException

    env = broker_fixture
    start = (
        await env.client.post(
            "/v1/audio/transcriptions/stream/begin",
            json={
                "session_id": "sess_1",
                "thread_id": "thr_1",
                "epoch": 1,
                "utterance_id": "utt_finish_off",
                "content_scope": "local_only",
            },
        )
    ).json()
    entered = asyncio.Event()
    gate = asyncio.Event()
    finish_body = json.dumps(
        {
            "stream_id": start["stream_id"],
            "worker_incarnation": start["worker_incarnation"],
            "session_id": "sess_1",
            "thread_id": "thr_1",
            "epoch": 1,
            "utterance_id": "utt_finish_off",
            "content_scope": "local_only",
            "sequence": 1,
            "sample_offset": 0,
        }
    ).encode("utf-8")

    class _GatedFinishRequest:
        headers = {"content-type": "application/json"}

        async def stream(self):
            entered.set()
            await gate.wait()
            yield finish_body

        async def is_disconnected(self):
            return False

        async def receive(self):
            await asyncio.Future()

    task = asyncio.create_task(env.broker.finish_stream(_GatedFinishRequest()))
    await entered.wait()
    env.coordinator.policy_rev = (2, "sleep", False)
    gate.set()
    with pytest.raises(HTTPException) as exc_fin_off:
        await task
    assert exc_fin_off.value.status_code == 409
    assert env.engine.finish_calls == 0
    assert env.broker.operation is None
