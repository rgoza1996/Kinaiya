"""Integration tests for incremental Whistle STT -> Converse windows -> Decisions (#93, #49)."""

import asyncio
import base64
import hashlib
import json
import os
import select
import socket
import tempfile
import threading
from contextlib import AsyncExitStack
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
        self.begin_calls = self.finish_calls = 0

    def transcribe(self, data: bytes) -> str:
        return "batch fallback"

    def begin_stream(self):
        self.begin_calls += 1
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
        self.finish_calls += 1
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
    stop_reader, stop_writer = socket.socketpair()

    class FixtureStopped(Exception):
        pass

    def accept():
        ready, _, _ = select.select((listener, stop_reader), (), ())
        if stop_reader in ready:
            raise FixtureStopped
        return listener.accept()

    def run_server():
        try:
            serve(
                directory,
                engine,
                INCARNATION,
                "seccomp-network-v1",
                SimpleNamespace(accept=accept),
                pid,
            )
        except FixtureStopped:
            pass

    server_thread = threading.Thread(target=run_server, daemon=True)

    async def stop_server():
        # The broker can unlink its IPC pathname. Use a private wakeup and join
        # the original server before closing fds, including failed setup (#600).
        stop_writer.sendall(b"\x00")
        await asyncio.to_thread(server_thread.join, 1)
        assert not server_thread.is_alive()
        stop_reader.close()
        stop_writer.close()
        listener.close()
        if sock.exists():
            sock.unlink()
        directory.rmdir()

    async with AsyncExitStack() as cleanup:
        server_thread.start()
        cleanup.push_async_callback(stop_server)
        assert await endpoint.probe(None, SimpleNamespace(pid=pid, returncode=None))

        manager = _FakeManager(pid)
        coordinator = _FakeCoordinator(manager)
        broker = WhistleBroker(coordinator, endpoint, request_timeout_s=5)
        cleanup.push_async_callback(broker.close)
        resources_app = create_resources_app(coordinator, AUTH, whistle=broker)
        speech = SpeechClient(
            "http://127.0.0.1:8765",
            AUTH.tokens["converse"],
            stt_provider="whistle",
            stt_url="http://127.0.0.1:8764",
            stt_transport=httpx.ASGITransport(app=resources_app),
        )
        cleanup.push_async_callback(speech.close)
        store = Store(tmp_path / "journal.db")
        cleanup.callback(store.close)
        journal = LocalJournal(store, "converse")
        session = store.create_session(SessionRequest(channel="converse"))
        runtime = ConverseRuntime(journal, ScriptedProvider(["Hello!"]))
        cleanup.push_async_callback(runtime.close)
        window_controller = WindowConverseController(runtime)
        cleanup.push_async_callback(window_controller.close)
        converse_app = create_converse_app(
            runtime, AUTH, speech=speech, window_controller=window_controller
        )
        client = await cleanup.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=converse_app),
                base_url="http://converse.local",
                headers={"Authorization": "Bearer " + AUTH.tokens["owner"]},
            )
        )
        yield SimpleNamespace(
            client=client,
            session=session,
            journal=journal,
            store=store,
            runtime=runtime,
            window_controller=window_controller,
            broker=broker,
            app=converse_app,
            engine=engine,
            resources_app=resources_app,
            speech=speech,
        )


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


class _LoseSuccessfulResponse(httpx.AsyncBaseTransport):
    """Complete the original real ASGI call, then lose its owner-facing ACK."""

    def __init__(self, app, path):
        self.inner = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        self.path = path
        self.lost = 0

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if request.url.path == self.path and response.status_code == 200 and not self.lost:
            await response.aread()
            await response.aclose()
            self.lost += 1
            raise httpx.ReadError("PUBLIC-LOST-INCREMENTAL-ACK", request=request)
        return response

    async def aclose(self):
        await self.inner.aclose()


@pytest.mark.parametrize(
    "phase", ["begin", "begin_upstream", "pcm_committed", "pcm_unobserved", "finish"]
)
async def test_incremental_original_lifecycle_is_inspectable_without_stt_replay(
    converse_incremental_env, phase
):
    """#654: source, consumed PCM and final text have distinct native outcomes."""
    env = converse_incremental_env
    scope = {
        "session_id": env.session.session_id,
        "thread_id": env.session.thread_id,
        "epoch": env.session.epoch,
        "utterance_id": "public-lifecycle-original",
        "content_scope": "local_only",
    }
    operation = (
        "begin" if phase.startswith("begin") else "pcm" if phase.startswith("pcm_") else phase
    )
    target = "/v1/converse/audio/stream/" + operation
    async with AsyncExitStack() as resources:
        app = env.app
        if phase == "begin_upstream":
            upstream = _LoseSuccessfulResponse(
                env.resources_app, "/v1/audio/transcriptions/stream/begin"
            )
            speech = SpeechClient(
                "http://127.0.0.1:8765",
                AUTH.tokens["converse"],
                stt_provider="whistle",
                stt_url="http://127.0.0.1:8764",
                stt_transport=upstream,
            )
            resources.push_async_callback(speech.close)
            app = create_converse_app(
                env.runtime, AUTH, speech=speech, window_controller=env.window_controller
            )
        transport = _LoseSuccessfulResponse(app, target if phase == "begin" else "")
        await _inspect_original_lifecycle(env, phase, scope, target, transport)


async def _inspect_original_lifecycle(env, phase, scope, target, transport):
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://converse.local",
        headers={"Authorization": "Bearer " + AUTH.tokens["owner"]},
    ) as client:
        capability = await client.get("/v1/converse/audio/stream/ownership")
        assert capability.status_code == 200
        owner_instance = capability.json()["owner_instance"]
        if phase.startswith("begin"):
            if phase == "begin":
                with pytest.raises(httpx.ReadError, match="PUBLIC-LOST-INCREMENTAL-ACK"):
                    await client.post(target, json=scope)
                assert transport.lost == 1
            else:
                refused = await client.post(target, json=scope)
                assert refused.status_code == 500
            original = scope
            assert env.engine.begin_calls == 1
            assert env.engine.active is True
            assert env.store.read(kinds=["conversation.window"]) == []
        else:
            began = await client.post("/v1/converse/audio/stream/begin", json=scope)
            assert began.status_code == 200
            start = began.json()
            original = {
                **scope,
                "stream_id": start["stream_id"],
                "worker_incarnation": start["worker_incarnation"],
                "sequence": 1,
                "sample_offset": 0,
                "user_speaking": True,
                "pcm16_base64": base64.b64encode(b"\x01\x00" * 160).decode(),
            }
            if phase == "finish":
                accepted = await client.post("/v1/converse/audio/stream/pcm", json=original)
                assert accepted.status_code == 200
                original = {
                    **scope,
                    "stream_id": start["stream_id"],
                    "worker_incarnation": start["worker_incarnation"],
                    "sequence": 2,
                    "sample_offset": 160,
                    "capture_ended": False,
                    "user_speaking": True,
                }
            ingest = env.window_controller.ingest

            async def unavailable(window):
                if phase == "pcm_committed":
                    await ingest(window)
                raise httpx.ReadError("PUBLIC-ORIGINAL-PUBLICATION-UNCONFIRMED")

            env.window_controller.ingest = unavailable
            try:
                failed = await client.post(target, json=original)
                assert failed.status_code == 500
            finally:
                env.window_controller.ingest = ingest
            assert env.engine.begin_calls == 1 and env.engine.step == 1
            assert env.engine.samples == 160
            assert env.engine.finish_calls == int(phase == "finish")
            windows = env.store.read(kinds=["conversation.window"])
            assert len(windows) == int(phase in {"pcm_committed", "finish"})

        # Exact-original read-only inspection cannot infer success/failure from
        # coincident values, rerun STT or open another stream after uncertainty.
        canonical = json.dumps(original, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        inspected = await client.post(
            target + "/lookup",
            json={
                "schema_version": "1.0",
                "protocol": "converse_stream_owner_v1",
                "owner_instance": owner_instance,
                "request_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            },
        )
        assert inspected.status_code == 200, {
            "phase": phase,
            "status": inspected.status_code,
            "begin_calls": env.engine.begin_calls,
            "pcm_calls": env.engine.step,
            "finish_calls": env.engine.finish_calls,
        }
        proof = inspected.json()
        canonical = json.dumps(original, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert proof["request_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
        assert proof["outcome"] in {"confirmed", "publication_pending"}
        assert proof["owner_instance"] == owner_instance
        assert proof["native_original"] is not None
        if phase in {"pcm_unobserved", "finish"}:
            assert proof["outcome"] == "publication_pending"
            assert proof["result"] is None
        else:
            assert proof["outcome"] == "confirmed"
            assert proof["result"] is not None
        assert len(env.store.read(kinds=["conversation.window"])) == int(
            phase in {"pcm_committed", "finish"}
        )
        assert env.engine.begin_calls == 1
        assert env.engine.step == int(not phase.startswith("begin"))
        assert env.engine.finish_calls == int(phase == "finish")


async def test_incremental_openapi_preserves_closed_legacy_input_contracts_without_inference(
    converse_incremental_env,
):
    env = converse_incremental_env
    response = await env.client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    required = {
        "begin": {"session_id", "thread_id", "epoch", "utterance_id"},
        "pcm": {
            "session_id",
            "thread_id",
            "epoch",
            "utterance_id",
            "stream_id",
            "worker_incarnation",
            "sequence",
            "sample_offset",
            "user_speaking",
            "pcm16_base64",
        },
        "finish": {
            "session_id",
            "thread_id",
            "epoch",
            "utterance_id",
            "stream_id",
            "worker_incarnation",
            "sequence",
            "sample_offset",
        },
        "cancel": {"stream_id"},
    }
    for phase, fields in required.items():
        schema = paths["/v1/converse/audio/stream/" + phase]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) == fields
    assert env.engine.begin_calls == env.engine.step == env.engine.finish_calls == 0
    assert env.store.read(kinds=["conversation.window"]) == []
