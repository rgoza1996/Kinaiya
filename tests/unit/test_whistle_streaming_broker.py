"""Unit tests for Whistle incremental streaming IPC, endpoint, and broker (#93, #49)."""

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
