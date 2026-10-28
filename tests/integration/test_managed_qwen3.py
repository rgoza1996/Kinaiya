"""Real managed processes, fenced IPC and ASGI bodies with a SYNTHETIC engine.

No Qwen runtime/assets/inference. Temporary marker files synchronize these
process tests only; production dialogue code writes no prompts or output.
"""

import asyncio
import ctypes.util
import json
import os
import platform
import sys
import tempfile
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from packages.providers import qwen3_ipc as wire
from packages.providers.qwen3 import ManagedQwen3Provider
from packages.resources.processes import WorkerManager, WorkerSpec
from services.resources.coordinator import Qwen3AdmissionError
from services.resources.qwen3 import Qwen3Broker, Qwen3Endpoint

ROOT = Path(__file__).resolve().parents[2]
MESSAGES = [{"role": "user", "content": "valid"}]

DRIVER = """import os,secrets,socket,struct,sys,time
from pathlib import Path
from packages.providers import qwen3_ipc as w
from packages.providers.whistle_isolation import deny_network
assert 'KINAIYA_QWEN_TEST_PARENT_SECRET' not in os.environ
pid,inc=os.getpid(),secrets.token_hex(32)
directory,marker=Path(sys.argv[1]),Path(sys.argv[2])
snapshot=w.snapshot_path(directory,pid)
snapshot.mkdir(mode=0o700)
for name in w.MANIFEST['assets']:
    (snapshot/name).symlink_to('/proc/self/fd/42') # Synthetic fixed shape, not model bytes.
hello=dict(version=w.VERSION,kind='hello',pid=pid,incarnation=inc,
    profile=w.PROFILE,model=w.MODEL,revision=w.REVISION,manifest_sha256=w.MANIFEST_SHA256,
    runtime=w.MANIFEST['runtime'],isolation='seccomp-network-v1',input_mode='sealed-safetensors-v1',
    max_input_tokens=2048,max_new_tokens=128)
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
    listener.bind(str(w.socket_path(directory,pid)))
    listener.listen(1)
    assert deny_network()=='seccomp-network-v1'
    while True:
        fd,_=listener._accept()
        try:
            w.write_frame(fd,w.encode_json(hello,w.MAX_FRAME_BYTES),w.MAX_FRAME_BYTES)
            req=w.parse_request(w.decode_json(w.read_frame(fd,w.MAX_REQUEST_BYTES),w.MAX_REQUEST_BYTES),inc)
            nonce=req['nonce']
            def emit(kind,seq=None,**data):
                row=w.event_message(kind,pid,inc,nonce,seq=seq,**data)
                w.write_frame(fd,w.encode_json(row,w.MAX_FRAME_BYTES),w.MAX_FRAME_BYTES)
            if req['kind']=='probe':
                emit('ready')
                continue
            mode=req['messages'][-1]['content']
            marker.write_text(str(pid))
            if mode=='hang': time.sleep(60)
            if mode=='native-error':
                emit('error',0,code='input_limit')
                continue
            if mode=='wire-oversize':
                for seq in range(40):
                    raw=w.encode_json(w.event_message('delta',pid,inc,nonce,seq=seq,text='x'),w.MAX_FRAME_BYTES)
                    raw=b' '*(w.MAX_FRAME_BYTES-len(raw))+raw
                    w.write_frame(fd,raw,w.MAX_FRAME_BYTES)
                continue
            if mode=='frame-oversize':
                os.write(fd,struct.pack('!I',w.MAX_FRAME_BYTES+1))
                continue
            if mode=='no-terminal': continue
            if mode in ('bad-nonce','bad-pid','bad-inc','bad-seq'):
                row=w.event_message('delta',pid,inc,nonce,seq=0,text='untrusted')
                row[{'bad-nonce':'nonce','bad-pid':'pid','bad-inc':'incarnation','bad-seq':'seq'}[mode]] = {
                    'bad-nonce':'c'*64,'bad-pid':pid+1,'bad-inc':'d'*64,'bad-seq':1}[mode]
                w.write_frame(fd,w.encode_json(row,w.MAX_FRAME_BYTES),w.MAX_FRAME_BYTES)
                continue
            if mode=='text-oversize':
                for seq in range(5): emit('delta',seq,text='x'*w.MAX_DELTA_BYTES)
                continue
            if mode in ('events-max','events-oversize'):
                for seq in range(w.MAX_EVENTS-(1 if mode=='events-max' else 0)):
                    emit('delta',seq,text='x')
                emit('done',w.MAX_EVENTS-1 if mode=='events-max' else w.MAX_EVENTS,finish_reason='eos',input_tokens=10,output_tokens=2)
                continue
            if mode=='escaped-max':
                for seq in range(4): emit('delta',seq,text='\\0'*w.MAX_DELTA_BYTES)
                emit('done',4,finish_reason='eos',input_tokens=10,output_tokens=2)
                continue
            emit('delta',0,text='Public synthetic ')
            if mode=='after-delta': time.sleep(60)
            emit('delta',1,text='🌙 dialogue.')
            emit('done',2,finish_reason='length' if mode=='length' else 'eos',input_tokens=10,output_tokens=128 if mode=='length' else 2)
            if mode=='post-terminal': emit('delta',3,text='late')
        except (OSError,ValueError,EOFError):
            pass
        finally:
            os.close(fd)
"""


def released(result):
    proof = result.get("stop_evidence") or {}
    return result.get("ok") and (
        result.get("release_confirmed")
        or result.get("superseded")
        or (proof.get("reaped") and proof.get("group_empty"))
    )


class Coordinator:
    """Trusted admission callback stand-in, not the production coordinator proof."""

    def __init__(self, manager):
        self.manager, self.revision, self.enabled = manager, 1, True
        self.lock = asyncio.Lock()
        self.before_start = None
        self.entered = asyncio.Event()
        self.calls = 0

    def qwen3_policy(self):
        if not self.enabled:
            raise Qwen3AdmissionError()
        return (self.revision, self.enabled)

    async def admit_qwen3(self, policy):
        self.calls += 1
        async with self.lock:
            if self.qwen3_policy() != policy:
                raise Qwen3AdmissionError(cleanup_confirmed=True)
            self.entered.set()
            if self.before_start is not None:
                await self.before_start.wait()
            result = await self.manager.start("qwen3")
            identity = self.manager.owned_identity("qwen3")
            if not result.get("ok") or identity is None:
                raise Qwen3AdmissionError(
                    cleanup_confirmed=bool(result.get("stop_evidence", {}).get("group_empty"))
                )
            if not self.enabled or self.qwen3_policy() != policy:
                outcome = await self.manager.stop_if_current(identity)
                raise Qwen3AdmissionError(cleanup_confirmed=bool(released(outcome)))
            return identity


async def wait_for(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def request(mode="valid", *, raw=None, content_type="application/json", length=None):
    queue = asyncio.Queue()
    queue.put_nowait(
        {
            "type": "http.request",
            "body": raw
            if raw is not None
            else json.dumps({"messages": [{"role": "user", "content": mode}]}).encode(),
            "more_body": False,
        }
    )
    headers = [(b"content-type", content_type.encode())]
    if length is not None:
        headers.append((b"content-length", length.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/converse/completions",
        "headers": headers,
    }
    return Request(scope, queue.get), queue


async def consume(body):
    async with aclosing(body):
        return [json.loads(chunk) async for chunk in body]


@pytest.fixture
async def env(tmp_path, monkeypatch):
    if (
        sys.platform != "linux"
        or platform.machine() != "x86_64"
        or not ctypes.util.find_library("seccomp")
    ):
        pytest.skip("Linux x86_64/seccomp synthetic process tier unavailable")
    monkeypatch.setenv("KINAIYA_QWEN_TEST_PARENT_SECRET", "synthetic-not-for-child")
    directory = Path(tempfile.mkdtemp(prefix="kq91-", dir="/tmp"))
    endpoint = Qwen3Endpoint(directory)
    script, marker = tmp_path / "synthetic_qwen.py", tmp_path / "entered-pid"
    script.write_text(DRIVER)
    manager = WorkerManager(
        [
            WorkerSpec(
                "qwen3",
                (sys.executable, str(script), str(directory), str(marker)),
                ROOT,
                env={"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
                startup_timeout_s=5,
                stop_timeout_s=0.1,
            )
        ],
        readiness=endpoint.probe,
    )
    coordinator = Coordinator(manager)
    broker = Qwen3Broker(coordinator, endpoint, request_timeout_s=3)
    initial = await manager.start("qwen3")
    assert initial["ok"], initial
    value = SimpleNamespace(
        manager=manager, endpoint=endpoint, broker=broker, coordinator=coordinator, marker=marker
    )
    try:
        yield value
    finally:
        try:
            await broker.close()
            if broker.operation is not None and broker.operation.cleanup is not None:
                await asyncio.wait_for(asyncio.shield(broker.operation.cleanup), 5)
        finally:
            await manager.close()
            endpoint.close()


async def test_real_fenced_ipc_warm_reuse_and_asgi_provider_transport(env):
    old = env.manager.owned_identity("qwen3")

    async def app(scope, receive, send):
        body = await env.broker.stream(Request(scope, receive))
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/x-ndjson")],
                }
            )
            async for chunk in body:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await body.aclose()

    provider = ManagedQwen3Provider(
        "http://127.0.0.1:9999", "synthetic", transport=httpx.ASGITransport(app)
    )
    try:
        for _ in range(2):
            assert await provider.complete(MESSAGES) == "Public synthetic 🌙 dialogue."
            assert env.broker.operation is None and env.manager.is_current(old)
        status = Path(f"/proc/{old.pid}/status").read_text()
        assert "NoNewPrivs:\t1" in status and "Seccomp:\t2" in status
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "mode",
    [
        "no-terminal",
        "bad-nonce",
        "bad-pid",
        "bad-inc",
        "bad-seq",
        "post-terminal",
        "frame-oversize",
        "wire-oversize",
        "text-oversize",
        "events-oversize",
    ],
)
async def test_native_malformed_stream_stops_exact_child_and_fresh_request_recovers(env, mode):
    old = env.manager.owned_identity("qwen3")
    body = await env.broker.stream(request(mode)[0])
    events = await consume(body)
    assert events[-1] == {"kind": "error", "code": "generation_failed"}
    await wait_for(lambda: env.broker.operation is None)
    assert not env.manager.is_current(old) and not Path(f"/proc/{old.pid}").exists()
    result = await consume(await env.broker.stream(request()[0]))
    assert result[-1]["kind"] == "done" and env.manager.owned_identity("qwen3") is not old


async def test_native_fixed_error_preserves_its_code_and_reaps(env):
    old = env.manager.owned_identity("qwen3")
    assert await consume(await env.broker.stream(request("native-error")[0])) == [
        {"kind": "error", "code": "input_limit"}
    ]
    assert not env.manager.is_current(old) and env.broker.operation is None


@pytest.mark.parametrize("mode", ["escaped-max", "events-max", "length"])
async def test_valid_native_boundaries_and_honest_length_reason(env, mode):
    old = env.manager.owned_identity("qwen3")
    rows = await consume(await env.broker.stream(request(mode)[0]))
    assert rows[-1]["kind"] == "done"
    assert rows[-1]["finish_reason"] == ("length" if mode == "length" else "eos")
    if mode == "escaped-max":
        assert sum(len(row["text"].encode()) for row in rows[:-1]) == wire.MAX_TEXT_BYTES
    if mode == "events-max":
        assert len(rows) == wire.MAX_EVENTS
    assert env.manager.is_current(old)


@pytest.mark.parametrize("mode", ["hang", "after-delta"])
async def test_actual_asgi_disconnect_before_and_after_output_reaps_then_recovers(env, mode):
    for _ in range(2):
        old = env.manager.owned_identity("qwen3")
        env.marker.unlink(missing_ok=True)
        req, incoming = request(mode)
        body = await env.broker.stream(req)
        await wait_for(env.marker.exists)
        if mode == "after-delta":
            assert json.loads(await anext(body))["kind"] == "delta"
        incoming.put_nowait({"type": "http.disconnect"})
        assert (await consume(body))[-1] == {"kind": "error", "code": "disconnected"}
        assert env.broker.operation is None and not Path(f"/proc/{old.pid}").exists()
        assert (await consume(await env.broker.stream(request()[0])))[-1]["kind"] == "done"


async def test_unstarted_first_class_body_close_reaps_and_is_idempotent(env):
    old = env.manager.owned_identity("qwen3")
    body = await env.broker.stream(request("hang")[0])
    assert 0 < body.remaining_timeout_s <= env.broker.request_timeout_s
    body.operation.end = asyncio.get_running_loop().time() - 1
    assert body.remaining_timeout_s == 0
    await body.aclose()
    await body.aclose()
    assert env.broker.operation is None and not Path(f"/proc/{old.pid}").exists()


@pytest.mark.parametrize("mode", ["hang", "valid"])
async def test_deadline_bounds_native_or_consumer_backpressure_without_reading_body(env, mode):
    env.broker.timeout = 0.15  # Internal deterministic clock boundary; public config minimum is1s.
    old = env.manager.owned_identity("qwen3")
    body = await env.broker.stream(request(mode)[0])
    await wait_for(lambda: body.operation.failure.done())
    assert await consume(body) == [{"kind": "error", "code": "deadline"}]
    assert env.broker.operation is None and not Path(f"/proc/{old.pid}").exists()


@pytest.mark.parametrize("event", ["cancel", "disconnect", "deadline", "owner-off"])
async def test_admission_is_owned_until_late_start_released(env, event):
    env.coordinator.before_start = asyncio.Event()
    if event == "deadline":
        env.broker.timeout = 0.05
    req, incoming = request("hang")
    pending = asyncio.create_task(env.broker.stream(req))
    await env.coordinator.entered.wait()
    if event == "cancel":
        pending.cancel()
    elif event == "disconnect":
        incoming.put_nowait({"type": "http.disconnect"})
    elif event == "owner-off":
        env.coordinator.enabled = False
    # Cancellation/deadline waits at most one second; readiness stays owned.
    if event == "owner-off":
        env.coordinator.before_start.set()
    result = await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2)
    assert isinstance(result[0], (HTTPException, asyncio.CancelledError))
    if event != "owner-off":
        assert env.broker.operation is not None
        with pytest.raises(HTTPException) as busy:
            await env.broker.stream(request()[0])
        assert busy.value.status_code == 409
        env.coordinator.before_start.set()
    await wait_for(lambda: env.broker.operation is None)
    assert env.manager.owned_identity("qwen3") is None


async def test_policy_revocation_wins_before_next_output(env):
    body = await env.broker.stream(request("after-delta")[0])
    assert json.loads(await anext(body))["kind"] == "delta"
    env.coordinator.revision += 1
    assert await consume(body) == [{"kind": "error", "code": "paused"}]
    assert env.broker.operation is None


async def test_retained_unknown_cleanup_refuses_before_body_or_admission(env, monkeypatch):
    release, entered = asyncio.Event(), asyncio.Event()
    original = env.manager.stop_if_current

    async def delayed(identity):
        entered.set()
        await release.wait()
        return await original(identity)

    monkeypatch.setattr(env.manager, "stop_if_current", delayed)
    old = env.manager.owned_identity("qwen3")
    body = await env.broker.stream(request("hang")[0])
    try:
        await body.aclose()
        assert entered.is_set() and env.broker.operation is body.operation
        calls = env.coordinator.calls
        req, _ = request(raw=b"not even parsed")
        with pytest.raises(HTTPException) as busy:
            await env.broker.stream(req)
        assert busy.value.status_code == 409 and env.coordinator.calls == calls
        assert env.manager.is_current(old)
    finally:
        release.set()
    await wait_for(lambda: env.broker.operation is None)
    assert not Path(f"/proc/{old.pid}").exists()


async def test_old_delayed_cleanup_cannot_stop_restored_incarnation_or_unlink_its_snapshot(
    env, monkeypatch
):
    release, entered = asyncio.Event(), asyncio.Event()
    original = env.manager.stop_if_current

    async def delayed(identity):
        entered.set()
        await release.wait()
        return await original(identity)

    monkeypatch.setattr(env.manager, "stop_if_current", delayed)
    old = env.manager.owned_identity("qwen3")
    body = await env.broker.stream(request("hang")[0])
    try:
        await body.aclose()
        assert entered.is_set()
        assert released(await original(old))
        assert (await env.manager.start("qwen3"))["ok"]
        new = env.manager.owned_identity("qwen3")
        ready = env.endpoint.ready
        snapshot = wire.snapshot_path(env.endpoint.directory, new.pid)
        inode = snapshot.stat().st_ino
        release.set()
        await wait_for(lambda: env.broker.operation is None)
        assert env.manager.is_current(new) and new is not old
        assert env.endpoint.ready == ready and snapshot.stat().st_ino == inode
        assert not Path(f"/proc/{old.pid}").exists()
    finally:
        release.set()


async def test_failed_cleanup_remains_visible_and_exact_retry_never_replays_generation(
    env, monkeypatch
):
    original = env.manager.stop_if_current
    calls = []

    async def unknown(identity):
        calls.append(identity)
        return {"ok": False, "error": "synthetic-unknown"}

    monkeypatch.setattr(env.manager, "stop_if_current", unknown)
    body = await env.broker.stream(request("hang")[0])
    await body.aclose()
    assert env.broker.status()["cleanup_failed"] and env.broker.operation is body.operation
    original_calls = env.coordinator.calls
    with pytest.raises(HTTPException):
        await env.broker.stream(request()[0])
    await wait_for(lambda: len(calls) == 2)
    assert env.coordinator.calls == original_calls and calls[0] is calls[1]
    monkeypatch.setattr(env.manager, "stop_if_current", original)
    with pytest.raises(HTTPException):
        await env.broker.stream(request()[0])
    await wait_for(lambda: env.broker.operation is None)
    assert env.manager.owned_identity("qwen3") is None


async def test_real_transport_finalizer_is_retained_after_process_reaped(env, monkeypatch):
    release, closing = asyncio.Event(), asyncio.Event()
    original = env.endpoint.stream

    async def held(messages, identity):
        try:
            async with aclosing(original(messages, identity)) as source:
                async for value in source:
                    yield value
        finally:
            closing.set()
            # Model the existing owned transport mechanism: actual close
            # continues independently when its awaiting task is cancelled.
            cleanup = asyncio.create_task(release.wait())
            interrupted = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            if interrupted:
                raise asyncio.CancelledError

    monkeypatch.setattr(env.endpoint, "stream", held)
    old = env.manager.owned_identity("qwen3")
    body = await env.broker.stream(request()[0])
    try:
        assert json.loads(await anext(body))["kind"] == "delta"
        assert json.loads(await anext(body))["kind"] == "delta"
        await closing.wait()
        await body.aclose()
        assert not Path(f"/proc/{old.pid}").exists() and env.broker.operation is body.operation
        with pytest.raises(HTTPException):
            await env.broker.stream(request()[0])
    finally:
        release.set()
    await wait_for(lambda: env.broker.operation is None)


@pytest.mark.parametrize(
    "raw,ctype,length,status",
    [
        (b"{}", "application/json", None, 400),
        (b'{"messages":[],"extra":1}', "application/json", None, 400),
        (b'{"messages":[],"messages":[]}', "application/json", None, 400),
        (b"[]", "application/json", None, 400),
        (b"{}", "text/plain", None, 400),
        (b"{}", "application/json", "-1", 400),
        (b"{}", "application/json", str(wire.MAX_REQUEST_BYTES + 1), 413),
        (b"x" * (wire.MAX_REQUEST_BYTES + 1), "application/json", None, 413),
    ],
)
async def test_bounded_http_request_rejects_before_admission_and_preserves_warm_worker(
    env, raw, ctype, length, status
):
    old = env.manager.owned_identity("qwen3")
    with pytest.raises(HTTPException) as failure:
        await env.broker.stream(request(raw=raw, content_type=ctype, length=length)[0])
    assert failure.value.status_code == status
    assert (
        env.coordinator.calls == 0 and env.broker.operation is None and env.manager.is_current(old)
    )


async def test_same_pid_snapshot_inode_replacement_is_never_deleted(env):
    old = env.manager.owned_identity("qwen3")
    ready = env.endpoint.ready
    assert released(await env.manager.stop_if_current(old))
    snapshot = wire.snapshot_path(env.endpoint.directory, old.pid)
    saved = snapshot.with_name(snapshot.name + "-saved")
    snapshot.rename(saved)
    snapshot.mkdir(mode=0o700)
    sentinel = snapshot / "config.json"
    sentinel.symlink_to("/proc/self/fd/77")
    try:
        with pytest.raises(ValueError, match="incarnation changed"):
            env.endpoint.forget(old.pid, ready[1])
        assert sentinel.is_symlink() and env.endpoint.ready == ready
    finally:
        sentinel.unlink()
        snapshot.rmdir()
        saved.rename(snapshot)


async def test_endpoint_close_prunes_only_fixed_shape_and_refuses_unknown_sentinel(env):
    assert released(await env.manager.stop_if_current(env.manager.owned_identity("qwen3")))
    partial = wire.snapshot_path(env.endpoint.directory, 2147483647)
    partial.mkdir(mode=0o700)
    (partial / "config.json").symlink_to("/proc/self/fd/99")
    sentinel = env.endpoint.directory / "foreign-owner-data"
    sentinel.write_text("synthetic preservation sentinel")
    try:
        with pytest.raises(ValueError):
            env.endpoint.close()
        assert sentinel.read_text() == "synthetic preservation sentinel"
    finally:
        sentinel.unlink()


@pytest.mark.parametrize("mode", ["hang", "after-delta"])
async def test_consumer_task_cancellation_reaps_without_requiring_iteration_resume(env, mode):
    old = env.manager.owned_identity("qwen3")
    body = await env.broker.stream(request(mode)[0])
    if mode == "after-delta":
        assert json.loads(await anext(body))["kind"] == "delta"
    pending = asyncio.create_task(anext(body))
    await wait_for(lambda: env.marker.exists() and body.take is not None)
    pending.cancel()
    result = await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2)
    assert isinstance(result[0], asyncio.CancelledError)
    assert env.broker.operation is None and not Path(f"/proc/{old.pid}").exists()


async def test_repeated_old_normal_body_close_cannot_stop_new_request_on_same_resident(env):
    old_body = await env.broker.stream(request()[0])
    assert (await consume(old_body))[-1]["kind"] == "done"
    identity = env.manager.owned_identity("qwen3")
    env.marker.unlink(missing_ok=True)
    new_body = await env.broker.stream(request("hang")[0])
    try:
        await wait_for(env.marker.exists)
        await old_body.aclose()
        await old_body.aclose()
        assert env.broker.operation is new_body.operation and env.manager.is_current(identity)
    finally:
        await new_body.aclose()


async def test_reservation_during_body_parse_refuses_overlap_and_cancel_preserves_warm_worker(env):
    incoming = asyncio.Queue()
    req = Request(
        {"type": "http", "headers": [(b"content-type", b"application/json")]}, incoming.get
    )
    old = env.manager.owned_identity("qwen3")
    pending = asyncio.create_task(env.broker.stream(req))
    await wait_for(lambda: env.broker.operation is not None)
    with pytest.raises(HTTPException) as busy:
        await env.broker.stream(request()[0])
    assert busy.value.status_code == 409 and env.coordinator.calls == 0
    pending.cancel()
    result = await asyncio.gather(pending, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert env.broker.operation is None and env.manager.is_current(old)


async def test_kernel_peer_identity_rejects_correct_socket_under_a_claimed_foreign_pid(
    env, monkeypatch
):
    current = env.manager.owned_identity("qwen3")
    path = wire.socket_path(env.endpoint.directory, current.pid)
    ready = env.endpoint.ready
    monkeypatch.setattr(wire, "socket_path", lambda directory, pid: path)
    # The socket belongs to the real managed child; a reported/numeric PID
    # cannot replace the kernel SO_PEERCRED check.
    assert not await env.endpoint.probe(None, SimpleNamespace(pid=os.getpid(), returncode=None))
    assert env.endpoint.ready == ready and env.manager.is_current(current)


async def test_owner_off_prevents_even_empty_new_admission(env):
    env.coordinator.enabled = False
    with pytest.raises(HTTPException) as paused:
        await env.broker.stream(request()[0])
    assert paused.value.status_code == 409 and env.coordinator.calls == 0
    assert env.broker.operation is None


@pytest.mark.parametrize("entry", ["regular", "foreign-link", "directory", "extra"])
async def test_close_rejects_unsafe_snapshot_contents_without_touching_external_sentinel(
    env, tmp_path, entry
):
    assert released(await env.manager.stop_if_current(env.manager.owned_identity("qwen3")))
    snapshot = wire.snapshot_path(env.endpoint.directory, 2147483646)
    snapshot.mkdir(mode=0o700)
    external = tmp_path / "external-sentinel"
    external.write_text("synthetic external sentinel")
    path = snapshot / ("unexpected" if entry == "extra" else "config.json")
    if entry == "regular":
        path.write_text("synthetic")
    elif entry == "directory":
        path.mkdir()
    else:
        path.symlink_to(external if entry == "foreign-link" else "/proc/self/fd/99")
    try:
        with pytest.raises(ValueError):
            env.endpoint.close()
        assert external.read_text() == "synthetic external sentinel" and path.exists() == (
            entry in {"regular", "foreign-link", "directory"}
        )
    finally:
        if entry == "directory":
            path.rmdir()
        else:
            path.unlink()
        snapshot.rmdir()


@pytest.fixture
async def native_env(env, tmp_path):
    from services.common.auth import Auth
    from services.resources.api import create_app
    from services.resources.coordinator import ResourceCoordinator
    from services.resources.state import ResourceState

    binding = {
        "provider": "managed-qwen3",
        "model": wire.MODEL,
        "profile": wire.PROFILE,
        "manifest_sha256": wire.MANIFEST_SHA256,
        "resources_url": "http://127.0.0.1:9999",
        **env.endpoint.limits,
    }

    class Control:
        def __init__(self):
            self.enabled_value = True

        async def health(self):
            return {
                "provider": {
                    "provider": "managed-qwen3",
                    "model": wire.MODEL,
                    "local": True,
                    "streaming": True,
                },
                "managed_text": dict(binding),
                "resource_enabled": self.enabled_value,
            }

        async def enabled(self, value):
            self.enabled_value = value

        async def close(self):
            pass

    coordinator = ResourceCoordinator(
        env.manager,
        ResourceState(tmp_path / "native-state/resources.json"),
        Control(),
        memory_probe=lambda: 32 * 1024**3,
        qwen3_restore_reserve_bytes=8 * 1024**3,
        managed_text_binding=binding,
    )
    await coordinator.initialize()
    assert coordinator.state.record.phase == "ready"
    broker = Qwen3Broker(coordinator, env.endpoint, request_timeout_s=3)
    auth = Auth({name: name[0] * 40 for name in ("owner", "converse", "agent", "resources")})
    app = create_app(coordinator, auth, qwen3=broker)
    try:
        yield SimpleNamespace(
            **{**vars(env), "broker": broker, "coordinator": coordinator, "auth": auth, "app": app}
        )
    finally:
        await broker.close()
        await coordinator.close()


async def test_actual_coordinator_api_auth_and_broker_cancel_recovery(native_env):
    env = native_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(env.app), base_url="http://127.0.0.1:9999"
    ) as http:
        for role in ("agent", "resources"):
            result = await http.post(
                "/v1/converse/completions",
                json={"messages": MESSAGES},
                headers={"Authorization": "Bearer " + env.auth.tokens[role]},
            )
            assert result.status_code == 403 and env.broker.operation is None
        for role in ("owner", "converse"):
            result = await http.post(
                "/v1/converse/completions",
                json={"messages": MESSAGES},
                headers={"Authorization": "Bearer " + env.auth.tokens[role]},
            )
            assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
            assert json.loads(result.content.splitlines()[-1])["kind"] == "done"
        old = env.manager.owned_identity("qwen3")
        body = await env.broker.stream(request("hang")[0])
        await body.aclose()
        assert not Path(f"/proc/{old.pid}").exists() and env.broker.operation is None
        result = await http.post(
            "/v1/converse/completions",
            json={"messages": MESSAGES},
            headers={"Authorization": "Bearer " + env.auth.tokens["converse"]},
        )
        assert result.status_code == 200 and env.manager.owned_identity("qwen3") is not old


@pytest.mark.parametrize("action", ["off", "handoff"])
async def test_actual_coordinator_revocation_wins_active_generation(native_env, action):
    env = native_env
    if action == "handoff":
        assert (
            await env.coordinator.action(
                {
                    "operation": "profile",
                    "profile": "local_work_priority",
                    "idempotency_key": "synthetic-profile",
                }
            )
        )["ok"]
    old = env.manager.owned_identity("qwen3")
    body = await env.broker.stream(request("after-delta")[0])
    assert json.loads(await anext(body))["kind"] == "delta"
    payload = (
        {"operation": "converse", "enabled": False}
        if action == "off"
        else {"operation": "handoff", "required_bytes": 128 * 1024**2}
    )
    assert (await env.coordinator.action({**payload, "idempotency_key": "synthetic-" + action}))[
        "ok"
    ]
    assert await consume(body) == [{"kind": "error", "code": "paused"}]
    assert env.broker.operation is None and not Path(f"/proc/{old.pid}").exists()
    with pytest.raises(HTTPException) as paused:
        await env.broker.stream(request()[0])
    assert paused.value.status_code == 409


async def test_actual_api_header_send_failure_closes_never_iterated_native_owner(native_env):
    env = native_env
    old = env.manager.owned_identity("qwen3")
    req, incoming = request("hang")
    scope = {
        **req.scope,
        "http_version": "1.1",
        "scheme": "http",
        "root_path": "",
        "query_string": b"",
        "server": ("127.0.0.1", 9999),
        "client": ("127.0.0.1", 12345),
        "headers": [
            *req.scope["headers"],
            (b"authorization", ("Bearer " + env.auth.tokens["converse"]).encode()),
        ],
    }

    async def send(message):
        if message["type"] == "http.response.start":
            raise OSError("synthetic closed ASGI send")

    with pytest.raises(OSError):
        await env.app(scope, incoming.get, send)
    assert env.broker.operation is None and not Path(f"/proc/{old.pid}").exists()


async def test_cancelled_real_cleanup_is_visible_retained_and_retry_uses_same_binding(
    env, monkeypatch
):
    original = env.manager.stop_if_current
    old = env.manager.owned_identity("qwen3")

    async def interrupted(identity):
        assert identity is old
        raise asyncio.CancelledError

    monkeypatch.setattr(env.manager, "stop_if_current", interrupted)
    body = await env.broker.stream(request("hang")[0])
    try:
        await body.aclose()
        assert env.broker.operation is body.operation
        assert env.broker.status()["cleanup_failed"] and env.manager.is_current(old)
    finally:
        monkeypatch.setattr(env.manager, "stop_if_current", original)
        # Retrying only the retained close does not replay inference.
        env.broker.abort(body.operation, "unavailable")
        await wait_for(lambda: env.broker.operation is None)
    assert not Path(f"/proc/{old.pid}").exists()
