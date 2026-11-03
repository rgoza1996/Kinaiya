"""End-to-end verification for positive TTS interruption settlement recovery (#340)."""

import asyncio
import base64
import json
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.models import ConverseRequest, ProviderInfo, SessionRequest
from packages.contracts.tts_models import TTSAdmissionReconcile, TTSWorkerProof
from packages.providers.speech import SpeechClient
from packages.providers.text import ScriptedProvider
from packages.providers.tts_backends import AudioWaveform
from packages.providers.voice_worker import create_app as create_voice_worker_app
from services.common.auth import Auth
from services.converse.api import create_app as create_converse_app
from services.converse.model_settings import _encode
from services.converse.runtime import ConverseRuntime
from services.converse.tts_model_settings import TTSModelConflict, TTSModelSettings
from services.session.store import LocalJournal, Store

TOKENS = {
    "owner": "o" * 32,
    "converse": "c" * 32,
    "agent": "a" * 32,
    "decisions": "d" * 32,
}
AUTH = Auth(TOKENS)
PROFILE = "Kokoro 82M"
WAVE = AudioWaveform([0.25] * 240, 24000)


class StreamingASGITransport(httpx.AsyncBaseTransport):
    """Streaming ASGI transport that yields response chunks live and forwards disconnect."""

    def __init__(self, app):
        self.app = app

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        incoming: asyncio.Queue[dict] = asyncio.Queue()
        await incoming.put({"type": "http.request", "body": body, "more_body": False})
        headers_ready = asyncio.Event()
        chunks: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue()
        response_meta: dict = {}

        async def receive():
            return await incoming.get()

        async def send(message):
            if message["type"] == "http.response.start":
                response_meta["status"] = message["status"]
                response_meta["headers"] = [
                    (k.decode("latin1"), v.decode("latin1")) for k, v in message.get("headers", [])
                ]
                headers_ready.set()
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    await chunks.put(bytes(chunk))
                if not message.get("more_body", False):
                    await chunks.put(None)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme or "http",
            "path": request.url.path,
            "raw_path": request.url.raw_path,
            "query_string": request.url.query,
            "root_path": "",
            "headers": [(bytes(k).lower(), bytes(v)) for k, v in request.headers.raw],
            "client": ("127.0.0.1", 50000),
            "server": (request.url.host or "127.0.0.1", request.url.port or 8765),
        }

        async def run_app():
            try:
                await self.app(scope, receive, send)
            except BaseException as exc:
                if not headers_ready.is_set():
                    response_meta["error"] = exc
                    headers_ready.set()
                else:
                    await chunks.put(exc)
            finally:
                await chunks.put(None)

        app_task = asyncio.create_task(run_app())
        try:
            await headers_ready.wait()
        except BaseException:
            await incoming.put({"type": "http.disconnect"})
            await asyncio.gather(app_task, return_exceptions=True)
            raise
        if "error" in response_meta:
            await asyncio.gather(app_task, return_exceptions=True)
            raise response_meta["error"]

        class LiveByteStream(httpx.AsyncByteStream):
            def __init__(self):
                self.closed = False

            async def __aiter__(self):
                while True:
                    item = await chunks.get()
                    if item is None:
                        return
                    if isinstance(item, BaseException):
                        raise item
                    yield item

            async def aclose(self):
                if self.closed:
                    return
                self.closed = True
                await incoming.put({"type": "http.disconnect"})
                await asyncio.gather(app_task, return_exceptions=True)

        return httpx.Response(
            response_meta["status"],
            headers=response_meta["headers"],
            stream=LiveByteStream(),
            request=request,
        )


class ControllableTTSBackend:
    assets_state = "not_checked"

    def __init__(self):
        self.inputs: list[str] = []
        self.block_next = False
        self.block_before_first = False
        self.started_produce = threading.Event()
        self.first_emitted = threading.Event()
        self.release_thread = threading.Event()
        self.thread_exited = threading.Event()

    def load(self):
        self.assets_state = "verified"

    def produce(self, text, emit, cancelled):
        self.inputs.append(text)
        if self.block_before_first:
            self.block_before_first = False
            self.started_produce.set()
            try:
                self.release_thread.wait(timeout=5.0)
            finally:
                self.thread_exited.set()
            if cancelled.is_set():
                return
        emit(WAVE)
        if self.block_next:
            self.block_next = False
            self.first_emitted.set()
            try:
                self.release_thread.wait(timeout=5.0)
            finally:
                self.thread_exited.set()
            if not cancelled.is_set():
                emit(WAVE)


async def wait_thread_event(event: threading.Event, timeout: float = 3.0):
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, timeout), timeout + 1.0)


@pytest.fixture
async def tts_env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    data_dir.chmod(0o700)
    store = Store(data_dir / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    runtime = ConverseRuntime(
        LocalJournal(store, "converse"),
        ScriptedProvider(
            [
                "First synthetic reply for interruption.",
                "Second synthetic reply after recovery.",
                "Third synthetic reply after recovery.",
            ]
        ),
    )
    backend = ControllableTTSBackend()
    worker_app = create_voice_worker_app(
        AUTH,
        tts_profile=PROFILE,
        tts_factory=lambda *_: backend,
        producer_close_timeout=0.3,
    )
    worker_transport = StreamingASGITransport(worker_app)
    speech = SpeechClient(
        "http://127.0.0.1:8765",
        TOKENS["converse"],
        profile=PROFILE,
        transport=worker_transport,
    )
    host_info = ProviderInfo(provider="local-voice", model=PROFILE, local=True, streaming=True)
    db_path = data_dir / "converse-tts-model.sqlite"
    runtime._tts_options_configured = True
    runtime.tts_model_settings = TTSModelSettings(
        runtime,
        None,
        speech,
        db_path,
        workspace=workspace,
        host_info=host_info,
        host_profile=PROFILE,
        host_voice="af_heart",
    )
    converse_app = create_converse_app(runtime, AUTH, speech)
    converse_transport = StreamingASGITransport(converse_app)
    converse_http = httpx.AsyncClient(
        transport=converse_transport,
        base_url="http://127.0.0.1:8762",
        headers={"Authorization": f"Bearer {TOKENS['owner']}"},
    )
    try:
        yield SimpleNamespace(
            workspace=workspace,
            data_dir=data_dir,
            db_path=db_path,
            store=store,
            session=session,
            runtime=runtime,
            backend=backend,
            worker_app=worker_app,
            worker_transport=worker_transport,
            speech=speech,
            converse_app=converse_app,
            http=converse_http,
        )
    finally:
        await converse_http.aclose()
        await runtime.tts_model_settings.close()
        await speech.close()
        await runtime.close()
        store.close()


async def create_reply(env, key: str):
    result = await env.runtime.turn(
        ConverseRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            text=f"User prompt {key}",
            content_scope="public_synthetic",
            idempotency_key=f"turn-{key}",
        )
    )
    return next(item["entry"] for item in result["events"] if item["type"] == "text")


async def test_interrupted_tts_blocks_while_worker_unwinds_and_recovers_after_settlement_and_delivery(
    tts_env,
):
    """Criteria 1, 2 & 4: Active worker or pending delivery blocks; positive proof + delivery recovers."""
    entry1 = await create_reply(tts_env, "1")
    tts_env.backend.block_next = True

    # Start streaming speech and read Frame 0 while worker thread stays blocked before Frame 1.
    req = tts_env.http.build_request(
        "POST",
        "/v1/converse/speech",
        json={
            "event_id": entry1["event_id"],
            "generation_epoch": entry1["payload"]["generation_epoch"],
        },
    )
    resp = await tts_env.http.send(req, stream=True)
    assert resp.status_code == 200
    lines = resp.aiter_lines()
    first_line = await anext(lines)
    first_frame = json.loads(first_line)
    assert first_frame["chunk_index"] == 0
    assert base64.b64decode(first_frame["audio"])
    await wait_thread_event(tts_env.backend.first_emitted)

    # Disconnect the client stream while the worker thread is still blocked.
    close_task = asyncio.create_task(resp.aclose())
    await asyncio.sleep(0.03)

    settings = tts_env.runtime.tts_model_settings
    inventory = settings.admission_inventory()
    assert len(inventory["admissions"]) == 1
    admission_info = inventory["admissions"][0]
    assert admission_info["audio_exposed"] is True
    admission_id = admission_info["admission_id"]
    pin_sha256 = admission_info["pin_sha256"]

    # While worker producer thread is still running, reconciliation and new speech must fail closed.
    with pytest.raises(TTSModelConflict):
        await settings.reconcile_admission(
            TTSAdmissionReconcile(
                expected_backend_id=settings.backend_id,
                admission_id=admission_id,
                pin_sha256=pin_sha256,
            )
        )

    # Release the worker thread so voice_worker finishes unwinding and marks the operation settled.
    tts_env.backend.release_thread.set()
    await wait_thread_event(tts_env.backend.thread_exited)
    await close_task

    # Wait briefly for local producer_cleanup and transport_cleanup to complete.
    async with asyncio.timeout(2):
        while any(
            turn.producer_cleanup is None or not turn.producer_cleanup.done()
            for turn in tts_env.runtime._tts_turns
        ):
            await asyncio.sleep(0.01)

    # Criterion 2: Even though the worker thread settled, Frame 0 was exposed to the client and
    # terminal playback delivery is NOT confirmed yet, so reconciliation and new speech fail closed!
    assert settings.unknown_admissions() == 1
    with pytest.raises(TTSModelConflict, match="playback|delivery"):
        await settings.reconcile_admission(
            TTSAdmissionReconcile(
                expected_backend_id=settings.backend_id,
                admission_id=admission_id,
                pin_sha256=pin_sha256,
            )
        )
    blocked_resp = await tts_env.http.post(
        "/v1/converse/speech",
        json={
            "event_id": entry1["event_id"],
            "generation_epoch": entry1["payload"]["generation_epoch"],
        },
    )
    assert blocked_resp.status_code == 409

    # Confirm terminal interrupted delivery via owner delivery report.
    delivery_resp = await tts_env.http.post(
        "/v1/converse/delivery",
        json={
            "event_id": entry1["event_id"],
            "generation_epoch": entry1["payload"]["generation_epoch"],
            "chunk_id": entry1["payload"]["chunk_id"],
            "status": "interrupted",
            "played_seconds": 0.01,
        },
    )
    assert delivery_resp.status_code == 200

    # Wait for automatic reconciliation triggered by terminal delivery confirmation.
    async with asyncio.timeout(2):
        while settings.unknown_admissions() != 0:
            await asyncio.sleep(0.01)

    assert settings.unknown_admissions() == 0
    assert settings.snapshot()["operation_state"]["cleanup_unknown"] == 0
    assert settings.snapshot()["operation_state"]["cleanup_failed"] == 0

    # Criterion 4: Idempotent replay of reconcile_admission returns replayed=True;
    # mismatched parameters fail closed with TTSModelConflict.
    replayed = await settings.reconcile_admission(
        TTSAdmissionReconcile(
            expected_backend_id=settings.backend_id,
            admission_id=admission_id,
            pin_sha256=pin_sha256,
        )
    )
    assert replayed["state"] == "retired"
    assert replayed["worker_outcome"] == "cancelled"
    assert replayed["replayed"] is True

    with pytest.raises(TTSModelConflict):
        await settings.reconcile_admission(
            TTSAdmissionReconcile(
                expected_backend_id="0" * 32,
                admission_id=admission_id,
                pin_sha256=pin_sha256,
            )
        )
    with pytest.raises(TTSModelConflict):
        await settings.reconcile_admission(
            TTSAdmissionReconcile(
                expected_backend_id=settings.backend_id,
                admission_id=admission_id,
                pin_sha256="f" * 64,
            )
        )

    # Next TTS synthesis turn succeeds cleanly without restarting Converse or deleting the DB!
    entry2 = await create_reply(tts_env, "2")
    ok_resp = await tts_env.http.post(
        "/v1/converse/speech",
        json={
            "event_id": entry2["event_id"],
            "generation_epoch": entry2["payload"]["generation_epoch"],
        },
    )
    assert ok_resp.status_code == 200
    frames2 = [json.loads(line) for line in ok_resp.text.splitlines() if line.strip()]
    assert len(frames2) >= 1
    assert frames2[0]["event_id"] == entry2["event_id"]
    assert settings.unknown_admissions() == 0


async def test_interrupt_endpoint_fences_playback_and_recovers_settled_tts_admission(tts_env):
    """Interrupting via /v1/converse/interrupt confirms stop barrier and recovers TTS admission."""
    entry1 = await create_reply(tts_env, "interrupt-1")
    tts_env.backend.block_next = True
    tts_env.backend.first_emitted.clear()
    tts_env.backend.release_thread.clear()
    tts_env.backend.thread_exited.clear()

    req = tts_env.http.build_request(
        "POST",
        "/v1/converse/speech",
        json={
            "event_id": entry1["event_id"],
            "generation_epoch": entry1["payload"]["generation_epoch"],
        },
    )
    resp = await tts_env.http.send(req, stream=True)
    assert resp.status_code == 200
    first_frame = json.loads(await anext(resp.aiter_lines()))
    assert first_frame["chunk_index"] == 0
    await wait_thread_event(tts_env.backend.first_emitted)

    # Trigger /v1/converse/interrupt while synthesis is active, then release worker thread.
    tts_env.backend.release_thread.set()
    int_resp = await tts_env.http.post(
        "/v1/converse/interrupt",
        json={
            "session_id": tts_env.session.session_id,
            "expected_generation": entry1["payload"]["generation_epoch"],
        },
    )
    assert int_resp.status_code == 200
    await resp.aclose()
    await wait_thread_event(tts_env.backend.thread_exited)

    # Next turn automatically reconciles any remaining settled state and succeeds.
    entry2 = await create_reply(tts_env, "interrupt-2")
    ok_resp = await tts_env.http.post(
        "/v1/converse/speech",
        json={
            "event_id": entry2["event_id"],
            "generation_epoch": entry2["payload"]["generation_epoch"],
        },
    )
    assert ok_resp.status_code == 200
    assert tts_env.runtime.tts_model_settings.unknown_admissions() == 0


async def test_schema_v1_migration_and_store_reopen_recovery(tts_env, tmp_path):
    """Criterion 5: Schema v1 -> v2 migration and recovery of persisted binding on reopen."""
    v1_path = tmp_path / "data" / "legacy-v1.sqlite"
    conn = sqlite3.connect(v1_path)
    backend_id = "a" * 32
    conn.execute(
        "CREATE TABLE tts_model_state (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,selection TEXT NOT NULL,backend_id TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE tts_model_operations (key TEXT PRIMARY KEY,digest TEXT NOT NULL,request TEXT NOT NULL,receipt TEXT NOT NULL)"
    )
    conn.execute("CREATE TABLE tts_model_admissions (id TEXT PRIMARY KEY,identity TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO tts_model_state VALUES (1,0,?,?)",
        (_encode({"source": "host", "catalog_binding": None}), backend_id),
    )
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()
    v1_path.chmod(0o600)

    migrated = TTSModelSettings(
        tts_env.runtime,
        None,
        tts_env.speech,
        v1_path,
        workspace=tts_env.workspace,
        host_info=ProviderInfo(provider="local-voice", model=PROFILE, local=True, streaming=True),
        host_profile=PROFILE,
        host_voice="af_heart",
    )
    try:
        assert migrated.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert migrated.backend_id == backend_id
        assert migrated.unknown_admissions() == 0
    finally:
        await migrated.close()

    # Also verify store reopen recovery when an un-retired admission exists in SQLite
    # and the voice worker holds positive settlement proof for its operation_id.
    entry = await create_reply(tts_env, "reopen-1")
    tts_env.backend.block_next = True
    tts_env.backend.first_emitted.clear()
    tts_env.backend.release_thread.clear()
    tts_env.backend.thread_exited.clear()

    req = tts_env.http.build_request(
        "POST",
        "/v1/converse/speech",
        json={
            "event_id": entry["event_id"],
            "generation_epoch": entry["payload"]["generation_epoch"],
        },
    )
    resp = await tts_env.http.send(req, stream=True)
    assert resp.status_code == 200
    await anext(resp.aiter_lines())
    await wait_thread_event(tts_env.backend.first_emitted)

    close_task = asyncio.create_task(resp.aclose())
    await asyncio.sleep(0.03)
    tts_env.backend.release_thread.set()
    await wait_thread_event(tts_env.backend.thread_exited)
    await close_task

    async with asyncio.timeout(2):
        while any(
            turn.producer_cleanup is None or not turn.producer_cleanup.done()
            for turn in tts_env.runtime._tts_turns
        ):
            await asyncio.sleep(0.01)

    assert tts_env.runtime.tts_model_settings.unknown_admissions() == 1
    inv_before = tts_env.runtime.tts_model_settings.admission_inventory()["admissions"][0]

    # Snapshot the v2 SQLite store while the interrupted admission is still persisted,
    # and open a fresh ConverseRuntime + TTSModelSettings against the persisted store
    # (simulating a Converse restart where in-memory leases/turns are gone).
    reopen_path = tmp_path / "data" / "reopened-v2.sqlite"
    src_conn = sqlite3.connect(tts_env.db_path)
    dst_conn = sqlite3.connect(reopen_path)
    src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()
    reopen_path.chmod(0o600)

    reopened_runtime = ConverseRuntime(
        LocalJournal(tts_env.store, "converse"),
        ScriptedProvider(["Reopened reply."]),
    )
    reopened = TTSModelSettings(
        reopened_runtime,
        None,
        tts_env.speech,
        reopen_path,
        workspace=tts_env.workspace,
        host_info=ProviderInfo(provider="local-voice", model=PROFILE, local=True, streaming=True),
        host_profile=PROFILE,
        host_voice="af_heart",
    )
    reopened_runtime.tts_model_settings = reopened
    try:
        assert reopened.unknown_admissions() == 1
        # Without terminal delivery in the journal, the reopened store still refuses to settle.
        with pytest.raises(TTSModelConflict, match="delivery"):
            await reopened.reconcile_admission(
                TTSAdmissionReconcile(
                    expected_backend_id=reopened.backend_id,
                    admission_id=inv_before["admission_id"],
                    pin_sha256=inv_before["pin_sha256"],
                )
            )

        # Confirm terminal delivery in the journal, then verify the reopened store reconciles
        # the persisted admission using journal proof + voice worker settlement proof.
        deliv_resp = await tts_env.http.post(
            "/v1/converse/delivery",
            json={
                "event_id": entry["event_id"],
                "generation_epoch": entry["payload"]["generation_epoch"],
                "chunk_id": entry["payload"]["chunk_id"],
                "status": "interrupted",
                "played_seconds": 0.01,
            },
        )
        assert deliv_resp.status_code == 200

        reconciled = await reopened.reconcile_admission(
            TTSAdmissionReconcile(
                expected_backend_id=reopened.backend_id,
                admission_id=inv_before["admission_id"],
                pin_sha256=inv_before["pin_sha256"],
            )
        )
        assert reconciled["state"] == "retired"
        assert reconciled["worker_outcome"] == "cancelled"
        assert reopened.unknown_admissions() == 0
    finally:
        await reopened.close()
        await reopened_runtime.close()


async def test_worker_quarantine_and_transport_failure_remain_fail_closed(tts_env):
    """Criterion 6: Worker quarantine or local transport cleanup failure never settles."""
    entry1 = await create_reply(tts_env, "quarantine-1")
    tts_env.backend.block_next = True
    tts_env.backend.first_emitted.clear()
    tts_env.backend.release_thread.clear()
    tts_env.backend.thread_exited.clear()

    req = tts_env.http.build_request(
        "POST",
        "/v1/converse/speech",
        json={
            "event_id": entry1["event_id"],
            "generation_epoch": entry1["payload"]["generation_epoch"],
        },
    )
    resp = await tts_env.http.send(req, stream=True)
    assert resp.status_code == 200
    await anext(resp.aiter_lines())
    await wait_thread_event(tts_env.backend.first_emitted)

    # Disconnect WITHOUT releasing the worker thread within producer_close_timeout (0.3s),
    # which triggers worker quarantine and marks the worker operation state="unknown".
    await resp.aclose()
    await asyncio.sleep(0.35)

    await tts_env.http.post(
        "/v1/converse/delivery",
        json={
            "event_id": entry1["event_id"],
            "generation_epoch": entry1["payload"]["generation_epoch"],
            "chunk_id": entry1["payload"]["chunk_id"],
            "status": "interrupted",
            "played_seconds": 0.01,
        },
    )

    settings = tts_env.runtime.tts_model_settings
    inventory = settings.admission_inventory()
    assert len(inventory["admissions"]) == 1
    admission_info = inventory["admissions"][0]

    # Because the worker thread did not exit before close deadline, worker proof is "unknown"
    # (settled=False), so reconciliation and subsequent synthesis must fail closed!
    with pytest.raises(TTSModelConflict):
        await settings.reconcile_admission(
            TTSAdmissionReconcile(
                expected_backend_id=settings.backend_id,
                admission_id=admission_info["admission_id"],
                pin_sha256=admission_info["pin_sha256"],
            )
        )

    entry2 = await create_reply(tts_env, "quarantine-2")
    refused = await tts_env.http.post(
        "/v1/converse/speech",
        json={
            "event_id": entry2["event_id"],
            "generation_epoch": entry2["payload"]["generation_epoch"],
        },
    )
    assert refused.status_code == 409
    assert settings.unknown_admissions() == 1

    # Finally release the background thread for clean test teardown.
    tts_env.backend.release_thread.set()
    await wait_thread_event(tts_env.backend.thread_exited)


async def test_pre_frame_interruption_recovers_after_worker_settlement_without_delivery_report(
    tts_env,
):
    """Criterion 3: Pre-frame interruption (audio_exposed=False) settles from worker proof alone."""
    entry1 = await create_reply(tts_env, "pre-frame-1")
    tts_env.backend.block_before_first = True
    tts_env.backend.started_produce.clear()
    tts_env.backend.release_thread.clear()
    tts_env.backend.thread_exited.clear()

    req = tts_env.http.build_request(
        "POST",
        "/v1/converse/speech",
        json={
            "event_id": entry1["event_id"],
            "generation_epoch": entry1["payload"]["generation_epoch"],
        },
    )
    resp_task = asyncio.create_task(tts_env.http.send(req, stream=True))
    await wait_thread_event(tts_env.backend.started_produce)
    resp = await resp_task
    assert resp.status_code == 200

    # Disconnect before Frame 0 is emitted, then release the worker thread so it exits cleanly.
    close_task = asyncio.create_task(resp.aclose())
    await asyncio.sleep(0.03)
    tts_env.backend.release_thread.set()
    await wait_thread_event(tts_env.backend.thread_exited)
    await close_task

    settings = tts_env.runtime.tts_model_settings
    # Wait for automatic reconciliation inside TTSTurn._retire() to finish retiring the admission
    # without needing any /v1/converse/delivery report.
    async with asyncio.timeout(2):
        while settings.unknown_admissions() != 0:
            await asyncio.sleep(0.01)

    assert settings.unknown_admissions() == 0
    entry2 = await create_reply(tts_env, "pre-frame-2")
    ok_resp = await tts_env.http.post(
        "/v1/converse/speech",
        json={
            "event_id": entry2["event_id"],
            "generation_epoch": entry2["payload"]["generation_epoch"],
        },
    )
    assert ok_resp.status_code == 200
    assert settings.unknown_admissions() == 0


async def test_mismatched_or_malformed_worker_proof_rejected_without_retiring_admission(
    tts_env, monkeypatch
):
    """Review Finding 1: Mismatched worker/operation/text/profile/request proof never retires."""
    entry1 = await create_reply(tts_env, "proof-mismatch-1")
    tts_env.backend.block_next = True
    tts_env.backend.first_emitted.clear()
    tts_env.backend.release_thread.clear()
    tts_env.backend.thread_exited.clear()

    req = tts_env.http.build_request(
        "POST",
        "/v1/converse/speech",
        json={
            "event_id": entry1["event_id"],
            "generation_epoch": entry1["payload"]["generation_epoch"],
        },
    )
    resp = await tts_env.http.send(req, stream=True)
    assert resp.status_code == 200
    await anext(resp.aiter_lines())
    await wait_thread_event(tts_env.backend.first_emitted)

    close_task = asyncio.create_task(resp.aclose())
    await asyncio.sleep(0.03)
    tts_env.backend.release_thread.set()
    await wait_thread_event(tts_env.backend.thread_exited)
    await close_task

    async with asyncio.timeout(2):
        while any(
            turn.producer_cleanup is None or not turn.producer_cleanup.done()
            for turn in tts_env.runtime._tts_turns
        ):
            await asyncio.sleep(0.01)

    settings = tts_env.runtime.tts_model_settings
    inv = settings.admission_inventory()["admissions"][0]
    admission_id = inv["admission_id"]
    pin_sha256 = inv["pin_sha256"]
    reconcile_req = TTSAdmissionReconcile(
        expected_backend_id=settings.backend_id,
        admission_id=admission_id,
        pin_sha256=pin_sha256,
    )

    real_lookup = tts_env.speech.lookup_native_tts
    stored_binding = settings._admission_records()[admission_id][1]
    genuine_proof = await real_lookup(stored_binding)
    assert genuine_proof.settled is True
    assert genuine_proof.outcome == "cancelled"

    fake_proof = genuine_proof.model_copy(update={"operation_id": "tts_op_" + "f" * 32})

    async def return_fake(_binding):
        return fake_proof

    monkeypatch.setattr(tts_env.speech, "lookup_native_tts", return_fake)

    # Confirm terminal delivery while lookup returns a mismatched operation_id proof.
    deliv_resp = await tts_env.http.post(
        "/v1/converse/delivery",
        json={
            "event_id": entry1["event_id"],
            "generation_epoch": entry1["payload"]["generation_epoch"],
            "chunk_id": entry1["payload"]["chunk_id"],
            "status": "interrupted",
            "played_seconds": 0.01,
        },
    )
    assert deliv_resp.status_code == 200
    assert settings.unknown_admissions() == 1

    for bad_proof in (
        genuine_proof.model_copy(update={"operation_id": "tts_op_" + "f" * 32}),
        genuine_proof.model_copy(update={"worker_instance": "e" * 32}),
        genuine_proof.model_copy(update={"text_sha256": "a" * 64}),
        genuine_proof.model_copy(update={"profile": "Supertonic 3"}),
        genuine_proof.model_copy(update={"request_sha256": "b" * 64}),
        {k: v for k, v in genuine_proof.model_dump(mode="json").items() if k != "protocol"},
        SimpleNamespace(**genuine_proof.model_dump(mode="json")),
    ):
        fake_proof = bad_proof
        with pytest.raises(TTSModelConflict):
            await settings.reconcile_admission(reconcile_req)
        assert settings.unknown_admissions() == 1
        assert settings.db.execute("SELECT COUNT(*) FROM tts_model_settlements").fetchone()[0] == 0

    monkeypatch.setattr(tts_env.speech, "lookup_native_tts", real_lookup)
    retired = await settings.reconcile_admission(reconcile_req)
    assert retired["state"] == "retired"
    assert retired["operation_id"] == genuine_proof.operation_id
    assert retired["worker_instance"] == genuine_proof.worker_instance
    assert settings.unknown_admissions() == 0


async def test_changed_binding_across_async_lookup_enforces_playback_ack_and_cas_delete(
    tts_env, tmp_path, monkeypatch
):
    """Review Finding 2: Changed binding_json across lookup await enforces ACK and CAS delete."""
    entry1 = await create_reply(tts_env, "cas-binding-1")
    tts_env.backend.block_next = True
    tts_env.backend.first_emitted.clear()
    tts_env.backend.release_thread.clear()
    tts_env.backend.thread_exited.clear()

    req = tts_env.http.build_request(
        "POST",
        "/v1/converse/speech",
        json={
            "event_id": entry1["event_id"],
            "generation_epoch": entry1["payload"]["generation_epoch"],
        },
    )
    resp = await tts_env.http.send(req, stream=True)
    assert resp.status_code == 200
    await anext(resp.aiter_lines())
    await wait_thread_event(tts_env.backend.first_emitted)

    close_task = asyncio.create_task(resp.aclose())
    await asyncio.sleep(0.03)
    tts_env.backend.release_thread.set()
    await wait_thread_event(tts_env.backend.thread_exited)
    await close_task

    async with asyncio.timeout(2):
        while any(
            turn.producer_cleanup is None or not turn.producer_cleanup.done()
            for turn in tts_env.runtime._tts_turns
        ):
            await asyncio.sleep(0.01)

    inv = tts_env.runtime.tts_model_settings.admission_inventory()["admissions"][0]
    admission_id = inv["admission_id"]
    pin_sha256 = inv["pin_sha256"]

    # Copy DB to a reopened store and set initial binding to audio_exposed=False
    # so we can simulate audio_exposed transitioning False -> True across the lookup await.
    reopen_path = tmp_path / "data" / "cas-binding-v2.sqlite"
    src_conn = sqlite3.connect(tts_env.db_path)
    dst_conn = sqlite3.connect(reopen_path)
    src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()
    reopen_path.chmod(0o600)

    reopened_runtime = ConverseRuntime(
        LocalJournal(tts_env.store, "converse"),
        ScriptedProvider(["CAS reply."]),
    )
    reopened = TTSModelSettings(
        reopened_runtime,
        None,
        tts_env.speech,
        reopen_path,
        workspace=tts_env.workspace,
        host_info=ProviderInfo(provider="local-voice", model=PROFILE, local=True, streaming=True),
        host_profile=PROFILE,
        host_voice="af_heart",
    )
    reopened_runtime.tts_model_settings = reopened
    try:
        identity, exposed_binding = reopened._admission_records()[admission_id]
        unexposed_binding = exposed_binding.model_copy(update={"audio_exposed": False})
        unexposed_json = _encode(unexposed_binding.model_dump(mode="json"))
        exposed_json = _encode(exposed_binding.model_dump(mode="json"))
        reopened.db.execute(
            "UPDATE tts_model_admissions SET binding=? WHERE id=? AND identity=?",
            (unexposed_json, admission_id, identity),
        )
        reopened.db.commit()

        real_lookup = tts_env.speech.lookup_native_tts

        async def lookup_flips_exposed(binding):
            proof = await real_lookup(binding)
            # Simulate binding_json transitioning from unexposed to exposed during the await.
            reopened.db.execute(
                "UPDATE tts_model_admissions SET binding=? WHERE id=? AND identity=?",
                (exposed_json, admission_id, identity),
            )
            reopened.db.commit()
            return proof

        monkeypatch.setattr(tts_env.speech, "lookup_native_tts", lookup_flips_exposed)
        reconcile_req = TTSAdmissionReconcile(
            expected_backend_id=reopened.backend_id,
            admission_id=admission_id,
            pin_sha256=pin_sha256,
        )

        # Because binding transitioned to audio_exposed=True during lookup and no playback/delivery
        # ACK exists yet in the journal, reconciliation must refuse and keep the admission!
        with pytest.raises(TTSModelConflict, match="delivery"):
            await reopened.reconcile_admission(reconcile_req)
        assert reopened.unknown_admissions() == 1

        # Now confirm terminal delivery in the journal, and test that if binding_json changes
        # across the post-lookup delivery check before DELETE, the CAS binding predicate rejects it.
        monkeypatch.setattr(tts_env.speech, "lookup_native_tts", real_lookup)
        deliv_resp = await tts_env.http.post(
            "/v1/converse/delivery",
            json={
                "event_id": entry1["event_id"],
                "generation_epoch": entry1["payload"]["generation_epoch"],
                "chunk_id": entry1["payload"]["chunk_id"],
                "status": "interrupted",
                "played_seconds": 0.01,
            },
        )
        assert deliv_resp.status_code == 200

        mutated_binding = exposed_binding.model_copy(update={"tts_operation_id": "c" * 32})
        mutated_json = _encode(mutated_binding.model_dump(mode="json"))
        real_confirms = reopened._journal_confirms_delivery

        async def confirms_then_mutates_binding(binding):
            ok = await real_confirms(binding)
            reopened.db.execute(
                "UPDATE tts_model_admissions SET binding=? WHERE id=? AND identity=?",
                (mutated_json, admission_id, identity),
            )
            reopened.db.commit()
            return ok

        monkeypatch.setattr(reopened, "_journal_confirms_delivery", confirms_then_mutates_binding)
        with pytest.raises(TTSModelConflict, match="changed"):
            await reopened.reconcile_admission(reconcile_req)
        assert reopened.unknown_admissions() == 1

        # Restore the valid exposed_json binding and real _journal_confirms_delivery; now CAS succeeds.
        reopened.db.execute(
            "UPDATE tts_model_admissions SET binding=? WHERE id=? AND identity=?",
            (exposed_json, admission_id, identity),
        )
        reopened.db.commit()
        monkeypatch.setattr(reopened, "_journal_confirms_delivery", real_confirms)
        retired = await reopened.reconcile_admission(reconcile_req)
        assert retired["state"] == "retired"
        assert reopened.unknown_admissions() == 0
    finally:
        await reopened.close()
        await reopened_runtime.close()


async def test_normal_eof_synthesis_stream_records_completed_worker_outcome(tts_env):
    """Review Finding 3: Normal EOF synthesis stream records outcome=='completed', not 'cancelled'."""
    entry1 = await create_reply(tts_env, "eof-completed-1")
    resp = await tts_env.http.post(
        "/v1/converse/speech",
        json={
            "event_id": entry1["event_id"],
            "generation_epoch": entry1["payload"]["generation_epoch"],
        },
    )
    assert resp.status_code == 200
    frames = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    assert len(frames) == 1

    turn = tts_env.runtime._tts_history[(entry1["event_id"], entry1["payload"]["generation_epoch"])]
    assert turn.native_binding is not None
    proof: TTSWorkerProof = await tts_env.speech.lookup_native_tts(turn.native_binding)
    assert proof.settled is True
    assert proof.state == "settled"
    assert proof.outcome == "completed"


async def test_ordinary_single_write_confirmed_stop_reconciles_settled_admission(tts_env):
    """Review Finding 4: Ordinary 1-write confirmed-stop path invokes post-stop reconciliation."""
    entry1 = await create_reply(tts_env, "stop-reconcile-1")
    tts_env.backend.block_next = True
    tts_env.backend.first_emitted.clear()
    tts_env.backend.release_thread.clear()
    tts_env.backend.thread_exited.clear()

    req = tts_env.http.build_request(
        "POST",
        "/v1/converse/speech",
        json={
            "event_id": entry1["event_id"],
            "generation_epoch": entry1["payload"]["generation_epoch"],
        },
    )
    resp = await tts_env.http.send(req, stream=True)
    assert resp.status_code == 200
    await anext(resp.aiter_lines())
    await wait_thread_event(tts_env.backend.first_emitted)

    # Disconnect and let the worker thread finish unwinding before stop reconciliation runs.
    close_task = asyncio.create_task(resp.aclose())
    await asyncio.sleep(0.03)
    tts_env.backend.release_thread.set()
    await wait_thread_event(tts_env.backend.thread_exited)
    await close_task

    async with asyncio.timeout(2):
        while any(
            turn.producer_cleanup is None or not turn.producer_cleanup.done()
            for turn in tts_env.runtime._tts_turns
        ):
            await asyncio.sleep(0.01)

    settings = tts_env.runtime.tts_model_settings
    # Frame 0 was exposed and no delivery/stop ACK has been published yet, so admission is retained.
    assert settings.unknown_admissions() == 1

    # Run the ordinary 1-write confirmed-stop path via /v1/converse/interrupt.
    int_resp = await tts_env.http.post(
        "/v1/converse/interrupt",
        json={
            "session_id": tts_env.session.session_id,
            "expected_generation": entry1["payload"]["generation_epoch"],
        },
    )
    assert int_resp.status_code == 200

    # Immediately after the 1-write confirmed stop returns, the unknown admission is retired!
    assert settings.unknown_admissions() == 0

    entry2 = await create_reply(tts_env, "stop-reconcile-2")
    ok_resp = await tts_env.http.post(
        "/v1/converse/speech",
        json={
            "event_id": entry2["event_id"],
            "generation_epoch": entry2["payload"]["generation_epoch"],
        },
    )
    assert ok_resp.status_code == 200
    assert settings.unknown_admissions() == 0
