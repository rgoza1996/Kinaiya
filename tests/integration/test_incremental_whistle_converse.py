"""Integration tests for incremental Whistle STT -> Converse windows -> Decisions (#93, #49)."""

import base64
import os
import socket
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.models import SessionRequest
from packages.providers.speech import SpeechClient
from packages.providers.text import ScriptedProvider
from packages.providers.whistle_ipc import socket_path
from packages.providers.whistle_worker import serve
from services.common.auth import Auth
from services.converse.api import create_app as create_converse_app
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.decisions.window_routing import WindowClassifier
from services.resources.api import create_app as create_resources_app
from services.resources.whistle import WhistleBroker, WhistleEndpoint
from services.session.store import LocalJournal, Store

AUTH = Auth(
    {name: name[0] * 40 for name in ("owner", "converse", "agent", "resources", "decisions")}
)
INCARNATION = "c" * 64


class _IncrementalEngine:
    def __init__(self):
        self.active = False
        self.samples = 0
        self.step = 0

    def transcribe(self, data: bytes) -> str:
        return "batch fallback"

    def begin_stream(self):
        self.active = True
        self.samples = 0
        self.step = 0
        return None

    def stream_pcm(self, pcm16: bytes) -> dict:
        self.step += 1
        self.samples += len(pcm16) // 2
        if self.step == 1:
            return {
                "delta_text": "",
                "committed_text": "",
                "pending_text": "please check",
                "received_samples": self.samples,
                "final": False,
                "native_pass_ms": 10.0,
                "language": "en",
            }
        if self.step == 2:
            # Same-slot provisional refinement (delta_text is empty, pending_text refined)
            return {
                "delta_text": "",
                "committed_text": "",
                "pending_text": "please check the",
                "received_samples": self.samples,
                "final": False,
                "native_pass_ms": 11.0,
                "language": "en",
            }
        # Step 3 commits text and advances window_index
        return {
            "delta_text": "please check the",
            "committed_text": "please check the",
            "pending_text": "build status",
            "received_samples": self.samples,
            "final": False,
            "native_pass_ms": 12.0,
            "language": "en",
        }

    def finish_stream(self) -> dict:
        self.active = False
        return {
            "delta_text": "build status",
            "committed_text": "please check the build status",
            "pending_text": "",
            "received_samples": self.samples,
            "final": True,
            "native_pass_ms": 7.0,
            "language": "en",
        }


class _FakeManager:
    def __init__(self, pid: int):
        self.identity = SimpleNamespace(worker_id="whistle", pid=pid, generation=1)
        self.current = True

    def owned_identity(self, worker_id: str):
        return self.identity if self.current and worker_id == "whistle" else None

    def is_current(self, identity):
        return self.current and identity == self.identity

    async def stop_if_current(self, identity):
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
async def converse_incremental_env(tmp_path: Path):
    directory = Path(tempfile.mkdtemp(prefix="whistle-int-", dir="/tmp"))
    endpoint = WhistleEndpoint(directory)
    engine = _IncrementalEngine()
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
    assert await endpoint.probe(None, SimpleNamespace(pid=pid, returncode=None))

    manager = _FakeManager(pid)
    coordinator = _FakeCoordinator(manager)
    broker = WhistleBroker(coordinator, endpoint, request_timeout_s=5)
    resources_app = create_resources_app(coordinator, AUTH, whistle=broker)

    store = Store(tmp_path / "journal.db")
    journal = LocalJournal(store, "converse")
    session = store.create_session(SessionRequest(channel="converse"))

    runtime = ConverseRuntime(
        journal,
        ScriptedProvider(["Hello!"]),
    )
    window_controller = WindowConverseController(runtime)
    speech = SpeechClient(
        "http://127.0.0.1:8765",
        AUTH.tokens["converse"],
        stt_provider="whistle",
        stt_url="http://127.0.0.1:8764",
        stt_transport=httpx.ASGITransport(app=resources_app),
    )
    converse_app = create_converse_app(
        runtime, AUTH, speech=speech, window_controller=window_controller
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=converse_app),
        base_url="http://converse.local",
        headers={"Authorization": "Bearer " + AUTH.tokens["owner"]},
    ) as client:
        try:
            yield SimpleNamespace(
                client=client,
                session=session,
                journal=journal,
                store=store,
                runtime=runtime,
                window_controller=window_controller,
                broker=broker,
            )
        finally:
            await speech.close()
            await broker.close()
            listener.close()
            if sock.exists():
                sock.unlink()
            directory.rmdir()


async def test_incremental_whistle_persists_windows_and_separates_counters_without_auto_capture_ended(
    converse_incremental_env,
):
    env = converse_incremental_env
    session = env.session
    begin = (
        await env.client.post(
            "/v1/converse/audio/stream/begin",
            json={
                "session_id": session.session_id,
                "thread_id": session.thread_id,
                "epoch": session.epoch,
                "utterance_id": "utt_inc_1",
                "content_scope": "local_only",
            },
        )
    ).json()

    chunk_b64 = base64.b64encode(b"\x01\x00" * 160).decode("ascii")

    # Append 1: provisional tail "please check" -> window_index 0, revision 1
    r1 = await env.client.post(
        "/v1/converse/audio/stream/pcm",
        json={
            "stream_id": begin["stream_id"],
            "worker_incarnation": begin["worker_incarnation"],
            "session_id": session.session_id,
            "thread_id": session.thread_id,
            "epoch": session.epoch,
            "utterance_id": "utt_inc_1",
            "content_scope": "local_only",
            "sequence": 1,
            "sample_offset": 0,
            "user_speaking": True,
            "pcm16_base64": chunk_b64,
        },
    )
    assert r1.status_code == 200
    a1 = r1.json()
    assert a1["pcm_sequence"] == 1
    assert a1["recognizer_updates"] == 1
    assert a1["window_index"] == 0
    assert a1["window_revision"] == 1
    assert a1["entry"]["payload"]["text"] == "please check"
    assert a1["entry"]["payload"]["capture_ended"] is False

    # Append 2: provisional tail refinement "please check the" -> same window_index 0, revision 2
    r2 = await env.client.post(
        "/v1/converse/audio/stream/pcm",
        json={
            "stream_id": begin["stream_id"],
            "worker_incarnation": begin["worker_incarnation"],
            "session_id": session.session_id,
            "thread_id": session.thread_id,
            "epoch": session.epoch,
            "utterance_id": "utt_inc_1",
            "content_scope": "local_only",
            "sequence": 2,
            "sample_offset": 160,
            "user_speaking": True,
            "pcm16_base64": chunk_b64,
        },
    )
    assert r2.status_code == 200
    a2 = r2.json()
    assert a2["pcm_sequence"] == 2
    assert a2["recognizer_updates"] == 2
    assert a2["window_index"] == 0
    assert a2["window_revision"] == 2
    assert a2["entry"]["payload"]["text"] == "please check the"

    # Append 3: commits "please check the" + pending "build status" -> advances window_index to 1, revision 1
    r3 = await env.client.post(
        "/v1/converse/audio/stream/pcm",
        json={
            "stream_id": begin["stream_id"],
            "worker_incarnation": begin["worker_incarnation"],
            "session_id": session.session_id,
            "thread_id": session.thread_id,
            "epoch": session.epoch,
            "utterance_id": "utt_inc_1",
            "content_scope": "local_only",
            "sequence": 3,
            "sample_offset": 320,
            "user_speaking": True,
            "pcm16_base64": chunk_b64,
        },
    )
    assert r3.status_code == 200
    a3 = r3.json()
    assert a3["pcm_sequence"] == 3
    assert a3["recognizer_updates"] == 3
    assert a3["window_index"] == 1
    assert a3["window_revision"] == 1
    assert a3["entry"]["payload"]["text"] == "please check the build status"

    # Finish stream with capture_ended=False: decoder finalization MUST NOT silently force capture_ended=True
    fin = await env.client.post(
        "/v1/converse/audio/stream/finish",
        json={
            "stream_id": begin["stream_id"],
            "worker_incarnation": begin["worker_incarnation"],
            "session_id": session.session_id,
            "thread_id": session.thread_id,
            "epoch": session.epoch,
            "utterance_id": "utt_inc_1",
            "content_scope": "local_only",
            "sequence": 4,
            "sample_offset": 480,
            "user_speaking": False,
            "capture_ended": False,
        },
    )
    assert fin.status_code == 200
    af = fin.json()
    assert af["final"] is True
    assert af["capture_ended"] is False
    assert af["entry"]["payload"]["capture_ended"] is False
    assert af["entry"]["payload"]["user_speaking"] is False
    assert af["entry"]["payload"]["text"] == "please check the build status"

    # Run two-channel WindowClassifier over the persisted journal window
    latest_entry = await env.journal.latest_window(session.session_id, session.epoch, "utt_inc_1")
    assert latest_entry is not None
    classifier = WindowClassifier(
        ScriptedProvider(
            [
                '{"conversation":"RESPOND_WITHOUT_INTERRUPTING_USER",'
                '"agent_route":"IGNORE_FOR_IMMEDIATE_ROUTING",'
                '"target_task_id":null,"expected_objective_revision":null,'
                '"reason_code":"incremental_window_classified"}'
            ]
        )
    )
    classified = await classifier.classify(latest_entry, [], [])
    assert classified["conversation"] == "RESPOND_WITHOUT_INTERRUPTING_USER"
    assert classified["agent_route"] == "IGNORE_FOR_IMMEDIATE_ROUTING"
    assert classified["reason_code"] == "incremental_window_classified"
