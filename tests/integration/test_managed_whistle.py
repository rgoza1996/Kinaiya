"""Real owned processes/IPC/seccomp/HTTP with a SYNTHETIC engine, never a model.

The fixture writes only PID entry markers to synchronize races. Production
Whistle code writes no audio/transcript/marker files. Decoder evidence is separate.
"""

import asyncio
import base64
import ctypes.util
import io
import json
import os
import platform
import struct
import sys
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.models import SessionRequest, SpeechWindow
from packages.providers.speech import SpeechClient
from packages.providers.text import ScriptedProvider
from packages.providers.whistle_ipc import (
    MAX_CONTROL_BYTES,
    decode_json,
    encode_json,
    receive_frame,
    send_frame,
    socket_path,
)
from packages.resources.processes import WorkerIdentity, WorkerManager, WorkerSpec
from services.common.auth import Auth
from services.converse.api import create_app as converse_app
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.resources.api import create_app
from services.resources.coordinator import ResourceCoordinator
from services.resources.state import ResourceState
from services.resources.whistle import WhistleBroker, WhistleEndpoint
from services.session.store import LocalJournal, Store

ROOT = Path(__file__).resolve().parents[2]
AUTH = Auth({name: name[0] * 40 for name in ("owner", "converse", "agent", "resources")})

DRIVER = """import os,socket,struct,sys,time
from pathlib import Path
from packages.providers.whistle_worker import serve
from packages.providers.whistle_ipc import socket_path
from packages.providers.whistle_isolation import deny_network
assert 'KINAIYA_TEST_PARENT_SECRET' not in os.environ
class SyntheticEngine:
    def transcribe(self,data):
        code=struct.unpack('<h',data[-2:])[0]
        if code==1:
            Path(sys.argv[2]).write_text(str(os.getpid()))
            time.sleep(60)
        return 'Public synthetic Whistle transcript'
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
    listener.bind(str(socket_path(sys.argv[1],os.getpid())))
    listener.listen(1)
    isolation=deny_network()
    serve(sys.argv[1],SyntheticEngine(),'b'*64,isolation,listener,os.getppid())
"""


def wav(code=0):
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(struct.pack("<h", code) * 160)
    return out.getvalue()


class Control:
    def __init__(self):
        self.value = True

    async def health(self):
        return {"provider": {"local": False}, "resource_enabled": self.value}

    async def enabled(self, value):
        self.value = value

    async def close(self):
        pass


async def wait_for(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    if (
        sys.platform != "linux"
        or platform.machine() != "x86_64"
        or not ctypes.util.find_library("seccomp")
    ):
        pytest.skip("Optional Linux x86_64/seccomp process tier unavailable")
    monkeypatch.setenv("KINAIYA_TEST_PARENT_SECRET", "synthetic-parent-value-not-for-child")
    directory = Path(tempfile.mkdtemp(prefix="kw85-", dir="/tmp"))
    endpoint = WhistleEndpoint(directory)
    script = tmp_path / "synthetic_worker.py"
    script.write_text(DRIVER)
    marker = tmp_path / "entered-pid"
    manager = WorkerManager(
        [
            WorkerSpec(
                "whistle",
                (sys.executable, str(script), str(directory), str(marker)),
                ROOT,
                env={"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
                startup_timeout_s=5,
                stop_timeout_s=0.1,
            )
        ],
        readiness=endpoint.probe,
    )
    coordinator = ResourceCoordinator(
        manager,
        ResourceState(tmp_path / "state/resources-state.json"),
        Control(),
        memory_probe=lambda: 8 * 1024**3,
    )
    await coordinator.initialize()
    assert coordinator.state.record.phase == "ready", coordinator.status()
    broker = WhistleBroker(coordinator, endpoint, request_timeout_s=3)
    app = create_app(coordinator, AUTH, broker)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={"Authorization": "Bearer " + AUTH.tokens["owner"]},
    ) as http:
        value = SimpleNamespace(
            coordinator=coordinator,
            manager=manager,
            broker=broker,
            endpoint=endpoint,
            http=http,
            marker=marker,
            app=app,
            tmp_path=tmp_path,
        )
        try:
            yield value
        finally:
            await broker.close()
            await coordinator.close()
            endpoint.close()


async def post(env, code=0, **kwargs):
    return await env.http.post(
        "/v1/audio/transcriptions",
        content=wav(code),
        headers={"Content-Type": "audio/wav", **kwargs},
    )


async def action(env, operation, key=None, **fields):
    return await env.coordinator.action(
        {"operation": operation, "idempotency_key": key or operation, **fields}
    )


async def test_real_fenced_ipc_repeated_calls_and_role_auth(env):
    original = env.manager.owned_identity("whistle")
    for role in ("owner", "converse"):
        result = await post(env, Authorization="Bearer " + AUTH.tokens[role])
        assert result.status_code == 200, result.text
        assert result.json() == {
            "text": "Public synthetic Whistle transcript",
            "provider": "whistle",
            "profile": "experimental_buffered_cpu",
        }
    for role in ("agent", "resources"):
        assert (await post(env, Authorization="Bearer " + AUTH.tokens[role])).status_code == 403
    assert env.manager.is_current(original)
    assert env.broker.operation is None
    status = Path(f"/proc/{original.pid}/status").read_text()
    assert "NoNewPrivs:\t1" in status and "Seccomp:\t2" in status


async def test_missing_isolation_exits_owned_child_before_any_native_construction(tmp_path):
    directory = Path(tempfile.mkdtemp(prefix="kw85-", dir="/tmp"))
    marker = tmp_path / "unexpected-native-construction"
    script = tmp_path / "missing_isolation.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        "from packages.providers import whistle_worker as worker, whistle_backend as backend\n"
        "marker=Path(sys.argv[-1])\n"
        "def refuse(): raise SystemExit(23)  # injected isolation refusal\n"
        "def unexpected(*args): marker.touch(); raise AssertionError('native load')\n"
        "worker.deny_network=refuse\nbackend.WhistleEngine=unexpected\n"
        "sys.argv=sys.argv[:-1]\nworker.main()\n"
    )
    manager = WorkerManager(
        [
            WorkerSpec(
                "whistle",
                (
                    sys.executable,
                    str(script),
                    "--socket-dir",
                    str(directory),
                    "--weights",
                    "/synthetic/no-weights",
                    "--library",
                    "/synthetic/no-library",
                    "--workspace",
                    str(tmp_path / "agent"),
                    str(marker),
                ),
                ROOT,
                env={"PYTHONPATH": str(ROOT)},
                startup_timeout_s=1,
                stop_timeout_s=0.1,
            )
        ],
        readiness=lambda spec, process: False,
    )
    try:
        result = await manager.start("whistle")
        assert not result["ok"] and result["returncode"] == 23 and not marker.exists()
        assert manager.owned_identity("whistle") is None
        assert manager.status()["workers"]["whistle"]["owned_pgid"] is None
    finally:
        await manager.close()
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()


@pytest.mark.parametrize("mode", ["cancel", "deadline"])
async def test_cancellation_reaps_exact_process_then_new_request_recovers_automatically(env, mode):
    for _iteration in range(2):
        old = env.manager.owned_identity("whistle")
        env.marker.unlink(missing_ok=True)
        if mode == "deadline":
            env.broker.timeout = 0.04
        task = asyncio.create_task(post(env, 1))
        await wait_for(env.marker.exists)
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            response = await task
            assert response.status_code == 504
            env.broker.timeout = 3
        await wait_for(lambda: env.broker.operation is None)
        evidence = env.manager.status()["workers"]["whistle"]["stop_evidence"]
        assert evidence["reaped"] and evidence["group_empty"] and not env.manager.is_current(old)
        response = await post(env)
        assert response.status_code == 200, response.text
        new = env.manager.owned_identity("whistle")
        assert new is not old and new.pid != old.pid
        stale = await env.manager.stop_if_current(old)
        assert stale == {"ok": True, "worker_id": "whistle", "superseded": True}
        assert "stop_evidence" not in stale and env.manager.is_current(new)


async def test_busy_refusal_precedes_body_or_new_inference(env):
    task = asyncio.create_task(post(env, 1))
    await wait_for(env.marker.exists)
    try:
        response = await env.http.post(
            "/v1/audio/transcriptions", content=b"not wav", headers={"Content-Type": "audio/wav"}
        )
        assert response.status_code == 409
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize(
    "body,content_type,status", [(b"not wav", "audio/wav", 400), (wav(), "application/json", 415)]
)
async def test_bad_body_does_not_disturb_resident_worker(env, body, content_type, status):
    old = env.manager.owned_identity("whistle")
    result = await env.http.post(
        "/v1/audio/transcriptions", content=body, headers={"Content-Type": content_type}
    )
    assert result.status_code == status and env.manager.is_current(old)
    assert env.broker.operation is None


async def test_owner_off_and_handoff_win_and_direct_controls_remain_available(env):
    task = asyncio.create_task(post(env, 1))
    await wait_for(env.marker.exists)
    off = await action(env, "converse", enabled=False)
    assert off["ok"] and off["policy"]["phase"] == "off"
    response = await task
    assert response.status_code in (409, 503) and "text" not in response.json()
    assert "Public synthetic Whistle transcript" not in response.text
    assert (await post(env)).status_code == 409
    assert (await action(env, "restore"))["policy"]["phase"] == "off"
    assert (await action(env, "converse", key="on", enabled=True))["ok"]
    await wait_for(lambda: env.broker.operation is None)
    assert (await post(env)).status_code == 200
    await action(env, "profile", profile="local_work_priority")
    handoff = await action(env, "handoff", required_bytes=128 * 1024**2)
    assert handoff["ok"] and handoff["policy"]["phase"] == "local_work"
    assert (await post(env)).status_code == 409
    assert (await env.http.get("/v1/resources")).status_code == 200
    assert (await action(env, "restore", key="restore-after-handoff"))["ok"]
    assert (await post(env)).status_code == 200


async def test_cancelled_stt_already_reaped_still_allows_measured_handoff(env):
    task = asyncio.create_task(post(env, 1))
    await wait_for(env.marker.exists)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_for(lambda: env.broker.operation is None)
    await action(env, "profile", profile="local_work_priority")
    result = await action(env, "handoff", required_bytes=128 * 1024**2)
    assert result["ok"] and result["policy"]["phase"] == "local_work", result


async def test_off_waiting_on_recovery_lock_prevents_resurrection(env):
    old = env.manager.owned_identity("whistle")
    assert (await env.manager.stop_if_current(old))["ok"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def before(worker):
        entered.set()
        await release.wait()

    env.coordinator.before_start = before
    request = asyncio.create_task(post(env))
    await entered.wait()
    off = asyncio.create_task(action(env, "converse", enabled=False))
    await wait_for(lambda: env.coordinator._whistle_preemptions == 1)
    release.set()
    assert (await request).status_code in (409, 503)
    assert (await off)["policy"]["phase"] == "off"
    assert env.manager.owned_identity("whistle") is None
    assert env.manager.status()["workers"]["whistle"]["state"] == "stopped"


async def test_unknown_cleanup_is_retained_and_cannot_start_replacement(env, monkeypatch):
    original_stop = env.manager.stop_if_current
    old = env.manager.owned_identity("whistle")

    async def unknown(identity):
        return {"ok": False, "error": "ownership-unverified"}

    monkeypatch.setattr(env.manager, "stop_if_current", unknown)
    task = asyncio.create_task(post(env, 1))
    await wait_for(env.marker.exists)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.broker.operation is not None and env.broker.operation.cleanup_failed
    assert (await post(env)).status_code == 409
    assert env.manager.owned_identity("whistle") is old
    monkeypatch.setattr(env.manager, "stop_if_current", original_stop)
    assert (await post(env)).status_code == 409
    await wait_for(lambda: env.broker.operation is None)
    assert (await post(env)).status_code == 200


async def test_forged_numeric_identity_and_delayed_stop_cannot_touch_new_or_sentinel(env):
    old = env.manager.owned_identity("whistle")
    forged = WorkerIdentity(old.worker_id, old.pid, old.start_time)
    assert not env.manager.is_current(forged)
    assert (await env.manager.stop_if_current(forged))["error"] == "unowned-identity"
    sentinel = WorkerManager(
        [WorkerSpec("sentinel", (sys.executable, "-c", "import time; time.sleep(60)"), ROOT)],
        readiness=lambda spec, process: True,
    )
    try:
        assert (await sentinel.start("sentinel"))["ready"]
        handle = sentinel.owned_identity("sentinel")
        assert (await env.manager.stop_if_current(old))["ok"]
        assert (await post(env)).status_code == 200
        new = env.manager.owned_identity("whistle")
        assert (await env.manager.stop_if_current(old))["superseded"]
        assert env.manager.is_current(new) and sentinel.is_current(handle)
    finally:
        await sentinel.close()


async def test_malformed_ipc_request_is_refused_before_engine_without_losing_worker(env):
    identity = env.manager.owned_identity("whistle")
    reader, writer = await asyncio.open_unix_connection(
        str(socket_path(env.endpoint.directory, identity.pid))
    )
    try:
        hello = decode_json(await receive_frame(reader, MAX_CONTROL_BYTES))
        await send_frame(
            writer,
            encode_json(
                {"version": 1, "kind": [], "incarnation": hello["incarnation"], "nonce": "a" * 64}
            ),
            MAX_CONTROL_BYTES,
        )
        assert await reader.read() == b""
    finally:
        writer.close()
        await writer.wait_closed()
    assert not env.marker.exists()
    assert env.manager.is_current(identity) and (await post(env)).status_code == 200


async def test_stale_incarnation_result_is_discarded_and_worker_reaped(env):
    old = env.manager.owned_identity("whistle")
    ready = env.endpoint.ready
    env.endpoint.ready = (ready[0], "c" * 64, *ready[2:])
    result = await post(env)
    assert result.status_code == 503 and "text" not in result.json()
    assert "Public synthetic Whistle transcript" not in result.text
    await wait_for(lambda: env.broker.operation is None)
    assert not env.manager.is_current(old)
    assert (await post(env)).status_code == 200


async def asgi_request(app, path, body, headers, when_disconnected):
    incoming = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": body, "more_body": False})
    outgoing = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        "client": ("127.0.0.1", 12345),
        "server": ("fixture", 80),
    }

    async def send(message):
        outgoing.append(message)

    task = asyncio.create_task(app(scope, incoming.get, send))
    await when_disconnected()
    await incoming.put({"type": "http.disconnect"})
    await asyncio.wait_for(task, 5)
    return outgoing


async def test_disconnect_during_recovery_retains_admission_then_reaps_without_inference(env):
    old = env.manager.owned_identity("whistle")
    assert (await env.manager.stop_if_current(old))["ok"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def before(worker):
        entered.set()
        await release.wait()

    env.coordinator.before_start = before
    body = wav(1)
    try:
        result = await asgi_request(
            env.app,
            "/v1/audio/transcriptions",
            body,
            {
                "authorization": "Bearer " + AUTH.tokens["owner"],
                "content-type": "audio/wav",
                "content-length": str(len(body)),
            },
            entered.wait,
        )
        assert next(row["status"] for row in result if row["type"] == "http.response.start") == 499
        assert env.broker.operation is not None and not env.broker.operation.admission.done()
        assert not env.marker.exists()
    finally:
        release.set()
    await wait_for(lambda: env.broker.operation is None)
    assert env.manager.owned_identity("whistle") is None and not env.marker.exists()
    assert (await post(env)).status_code == 200


@pytest.mark.parametrize("path", ["/v1/converse/transcribe", "/v1/converse/audio/windows"])
async def test_actual_browser_asgi_disconnect_cancels_upstream_native_owner_without_append(
    env, path
):
    store = Store(env.tmp_path / "converse.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    runtime = ConverseRuntime(LocalJournal(store, "converse"), ScriptedProvider([]))
    controller = WindowConverseController(runtime)
    speech = SpeechClient(
        "http://127.0.0.1:8765",
        AUTH.tokens["converse"],
        stt_url="http://127.0.0.1:8764",
        stt_provider="whistle",
        stt_transport=httpx.ASGITransport(app=env.app),
    )
    app = converse_app(runtime, AUTH, speech, controller)
    old = env.manager.owned_identity("whistle")
    if path.endswith("/transcribe"):
        request = httpx.Request(
            "POST", "http://fixture" + path, files={"file": ("public.wav", wav(1), "audio/wav")}
        )
        body = request.read()
        headers = dict(request.headers)
    else:
        window = SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id="public-synthetic",
            window_index=0,
            text="",
            start_ms=0,
            end_ms=1000,
            user_speaking=True,
            content_scope="public_synthetic",
        ).model_dump(mode="json")
        body = json.dumps({**window, "audio_base64": base64.b64encode(wav(1)).decode()}).encode()
        headers = {"content-type": "application/json", "content-length": str(len(body))}
    headers["authorization"] = "Bearer " + AUTH.tokens["owner"]
    try:
        result = await asgi_request(app, path, body, headers, lambda: wait_for(env.marker.exists))
        assert (
            next(
                message["status"] for message in result if message["type"] == "http.response.start"
            )
            == 499
        )
        await wait_for(lambda: env.broker.operation is None)
        assert not env.manager.is_current(old)
        assert not [entry for entry in store.read() if entry.kind == "conversation.window"]
        assert (await post(env)).status_code == 200
    finally:
        await speech.close()
        await controller.close()
        store.close()


async def test_delayed_http_cleanup_after_off_on_never_stops_restored_incarnation(env, monkeypatch):
    original = env.manager.stop_if_current
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(identity):
        entered.set()
        await release.wait()
        return await original(identity)

    monkeypatch.setattr(env.manager, "stop_if_current", delayed)
    old = env.manager.owned_identity("whistle")
    pending = asyncio.create_task(post(env, 1))
    await wait_for(env.marker.exists)
    pending.cancel()
    await entered.wait()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert env.broker.operation is not None
    assert (await action(env, "converse", enabled=False))["ok"]
    assert (await action(env, "converse", key="new-on", enabled=True))["ok"]
    restored = env.manager.owned_identity("whistle")
    assert restored is not old
    release.set()
    await wait_for(lambda: env.broker.operation is None)
    assert env.manager.is_current(restored)
    assert (await post(env)).status_code == 200


async def test_replayed_off_does_not_revoke_a_legitimate_new_recovery(env):
    assert (await action(env, "converse", key="old-off", enabled=False))["ok"]
    assert (await action(env, "converse", key="current-on", enabled=True))["ok"]
    assert (await env.manager.stop_if_current(env.manager.owned_identity("whistle")))["ok"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def before(worker):
        entered.set()
        await release.wait()

    env.coordinator.before_start = before
    pending = asyncio.create_task(post(env))
    await entered.wait()
    replay = asyncio.create_task(action(env, "converse", key="old-off", enabled=False))
    await asyncio.sleep(0)
    assert env.coordinator._whistle_preemptions == 0
    release.set()
    assert (await pending).status_code == 200
    assert (await replay)["policy"]["phase"] == "off"  # historical result, not current policy
    assert env.coordinator.state.record.phase == "ready" and env.coordinator.intent.read().enabled
    assert (await post(env)).status_code == 200


@pytest.mark.parametrize("available", [None, 0, 256 * 1024**2 - 1])
async def test_new_request_recovery_requires_fresh_whistle_memory_reserve(env, available):
    old = env.manager.owned_identity("whistle")
    assert (await env.manager.stop_if_current(old))["ok"]
    env.coordinator.whistle_restore_reserve = 256 * 1024**2
    env.coordinator.memory_probe = lambda: available
    result = await post(env)
    assert result.status_code == 503
    assert env.broker.operation is None and env.manager.owned_identity("whistle") is None
    assert env.manager.status()["workers"]["whistle"]["owned_pid"] == old.pid
    env.coordinator.memory_probe = lambda: 8 * 1024**3
    assert (await post(env)).status_code == 200


async def test_recovery_rechecks_memory_after_asset_validation_await_but_not_for_resident_calls(
    env,
):
    original = env.manager.owned_identity("whistle")
    env.coordinator.memory_probe = lambda: 0
    assert (await post(env)).status_code == 200  # no new allocation
    assert env.manager.is_current(original)
    assert (await env.manager.stop_if_current(original))["ok"]
    env.coordinator.memory_probe = lambda: 8 * 1024**3

    async def validation(worker):
        await asyncio.sleep(0)
        env.coordinator.memory_probe = lambda: None

    env.coordinator.before_start = validation
    assert (await post(env)).status_code == 503
    assert env.broker.operation is None and env.manager.owned_identity("whistle") is None


async def test_oversized_declared_and_chunked_body_refuse_before_native(env):
    original = env.manager.owned_identity("whistle")
    declared = await env.http.post(
        "/v1/audio/transcriptions",
        content=b"",
        headers={"Content-Type": "audio/wav", "Content-Length": "10000001"},
    )
    assert declared.status_code == 413

    async def chunks():
        for _index in range(11):
            yield bytes(1_000_000)

    streamed = await env.http.post(
        "/v1/audio/transcriptions", content=chunks(), headers={"Content-Type": "audio/wav"}
    )
    assert streamed.status_code == 413
    assert env.manager.is_current(original) and not env.marker.exists()


async def test_kernel_peer_pid_cannot_be_replaced_by_a_client_report(env):
    fake_pid = os.getpid() + 99999
    path = socket_path(env.endpoint.directory, fake_pid)

    async def handler(reader, writer):
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handler, str(path))
    try:
        with pytest.raises(ValueError, match="peer"):
            await env.endpoint.exchange(fake_pid, None, "probe")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "tamper",
    [
        {"nonce": "0" * 64},
        {"pid": True},
        {"incarnation": "0" * 64},
        {"kind": "unexpected"},
        {"text": []},
    ],
)
async def test_result_nonce_pid_incarnation_and_shape_are_bound(env, tamper):
    from packages.providers.whistle_assets import MANIFEST, PROFILE

    pid = os.getpid()
    incarnation = "d" * 64
    path = socket_path(env.endpoint.directory, pid)

    async def handler(reader, writer):
        try:
            await send_frame(
                writer,
                encode_json(
                    {
                        "version": 1,
                        "kind": "hello",
                        "pid": pid,
                        "incarnation": incarnation,
                        "profile": PROFILE,
                        "isolation": "seccomp-network-v1",
                        "weights_sha256": MANIFEST["weights"]["sha256"],
                        "library_sha256": MANIFEST["library"]["sha256"],
                    }
                ),
                MAX_CONTROL_BYTES,
            )
            request = decode_json(await receive_frame(reader, MAX_CONTROL_BYTES))
            await receive_frame(reader, 10_000_000)
            await send_frame(
                writer,
                encode_json(
                    {
                        "version": 1,
                        "kind": "result",
                        "pid": pid,
                        "incarnation": incarnation,
                        "nonce": request["nonce"],
                        "text": "Public fixture",
                        **tamper,
                    }
                ),
                MAX_CONTROL_BYTES,
            )
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handler, str(path))
    endpoint = WhistleEndpoint(env.endpoint.directory)
    info = path.lstat()
    endpoint.ready = (pid, incarnation, info.st_dev, info.st_ino)
    try:
        with pytest.raises(ValueError):
            await endpoint.transcribe(wav(), SimpleNamespace(pid=pid))
    finally:
        server.close()
        await server.wait_closed()
