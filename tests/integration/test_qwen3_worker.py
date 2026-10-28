"""Synthetic integration checks for the bounded Qwen3 worker protocol.

Uses stub native modules and an owned synthetic child; never loads a model,
touches the network, or runs inference.
"""

import asyncio
import ctypes.util
import os
import platform
import sys
import tempfile
import types
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

import pytest

from packages.providers import qwen3_ipc
from packages.providers.qwen3_ipc import (
    MAX_DELTA_BYTES,
    MAX_EVENTS,
    MAX_FRAME_BYTES,
    MAX_REQUEST_BYTES,
    MAX_TEXT_BYTES,
    decode_json,
    encode_json,
    event_message,
    parse_event,
    parse_hello,
    parse_request,
    request_message,
)

ROOT = Path(__file__).resolve().parents[2]
NONCE = "c" * 64
INCARNATION = "d" * 64


def _install_fakes(monkeypatch, tmp_path, input_len, texts, out_tokens):
    import sys as _sys

    captured = {}
    fake_torch = types.ModuleType("torch")
    fake_torch.float32 = object()
    fake_torch.set_num_threads = lambda count: None
    fake_torch.set_num_interop_threads = lambda count: None
    fake_torch.inference_mode = nullcontext

    class _Ids:
        shape = (1, input_len)

    class _Out:
        shape = (1, input_len + out_tokens)

        def __getitem__(self, key):
            return 7 if out_tokens == 16 else 99

    class _BaseStreamer:
        def __init__(self, tokenizer, skip_prompt=True, skip_special_tokens=False):
            self.tokenizer = tokenizer

    fake_transformers = types.ModuleType("transformers")
    fake_transformers.TextStreamer = _BaseStreamer

    class _Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            return "rendered"

        def __call__(self, text, **kwargs):
            return {"input_ids": _Ids()}

    class _Model:
        generation_config = types.SimpleNamespace(eos_token_id=[99])

        def eval(self):
            pass

        def generate(self, input_ids, **kwargs):
            captured["gen"] = kwargs
            for piece in texts:
                kwargs["streamer"].on_finalized_text(piece)
            return _Out()

    class _AutoTokenizer:
        @staticmethod
        def from_pretrained(path, **kwargs):
            return _Tokenizer()

    class _AutoModel:
        @staticmethod
        def from_pretrained(path, **kwargs):
            return _Model()

    fake_transformers.AutoTokenizer = _AutoTokenizer
    fake_transformers.AutoModelForCausalLM = _AutoModel
    monkeypatch.setitem(_sys.modules, "torch", fake_torch)
    monkeypatch.setitem(_sys.modules, "transformers", fake_transformers)
    snapshot = tmp_path / "snap"
    snapshot.mkdir(exist_ok=True)
    for name in qwen3_ipc.MANIFEST["assets"]:
        (snapshot / name).write_bytes(b"synthetic")
    return captured, snapshot


def _engine(monkeypatch, tmp_path, **kwargs):
    from packages.providers.qwen3_backend import Qwen3Engine

    _, snapshot = _install_fakes(monkeypatch, tmp_path, **kwargs)
    return Qwen3Engine(snapshot, max_input_tokens=64, max_new_tokens=16)


def test_bounded_stream_terminal_after_join(monkeypatch, tmp_path):
    engine = _engine(monkeypatch, tmp_path, input_len=4, texts=["héllo ", "wörld ✓"], out_tokens=3)
    events = []
    for event in engine.generate([{"role": "user", "content": "hi"}]):
        if event["kind"] == "done":
            assert engine.producer_alive() is False
        events.append(event)
    assert len(events) <= MAX_EVENTS
    deltas = events[:-1]
    done = events[-1]
    assert deltas and all(item["kind"] == "delta" for item in deltas)
    total = sum(len(item["text"].encode("utf-8")) for item in deltas)
    assert total <= MAX_TEXT_BYTES
    assert all(1 <= len(item["text"].encode("utf-8")) <= MAX_DELTA_BYTES for item in deltas)
    assert "".join(item["text"] for item in deltas) == "héllo wörld ✓"
    assert done == {
        "kind": "done",
        "finish_reason": "eos",
        "input_tokens": 4,
        "output_tokens": 3,
    }
    assert engine.producer_alive() is False


def test_length_exhaustion_is_explicit(monkeypatch, tmp_path):
    engine = _engine(monkeypatch, tmp_path, input_len=4, texts=["abc"], out_tokens=16)
    done = list(engine.generate([{"role": "user", "content": "hi"}]))[-1]
    assert done["finish_reason"] == "length"
    assert done["output_tokens"] == 16


def test_output_limit_refuses(monkeypatch, tmp_path):
    from packages.providers.qwen3_backend import Qwen3OutputLimitError

    engine = _engine(
        monkeypatch, tmp_path, input_len=4, texts=["y" * (MAX_TEXT_BYTES + 1)], out_tokens=1
    )
    with pytest.raises(Qwen3OutputLimitError):
        list(engine.generate([{"role": "user", "content": "hi"}]))


def test_malformed_requests_rejected():
    good = request_message("generate", INCARNATION, NONCE, [{"role": "user", "content": "x"}])
    bad_nonce = dict(good, nonce="short")
    with pytest.raises(ValueError):
        parse_request(bad_nonce, INCARNATION)
    with pytest.raises(ValueError):
        parse_request(dict(good, incarnation="e" * 64), INCARNATION)
    with pytest.raises(ValueError):
        request_message("generate", INCARNATION, NONCE, [])
    with pytest.raises(ValueError):
        parse_event(
            event_message("delta", 1, INCARNATION, NONCE, seq=5, text=""),
            1,
            INCARNATION,
            NONCE,
            seq=5,
        )
    with pytest.raises(ValueError):
        parse_event(
            event_message("delta", 1, INCARNATION, NONCE, seq=1, text="ok"),
            1,
            INCARNATION,
            NONCE,
            seq=2,
        )
    with pytest.raises(ValueError):
        parse_event(
            {"version": 1, "kind": "ready", "pid": 2, "incarnation": INCARNATION, "nonce": NONCE},
            1,
            INCARNATION,
            NONCE,
        )


DRIVER = r"""
import os, socket, sys, threading
from packages.providers import qwen3_ipc
from packages.providers.whistle_isolation import deny_network
from packages.providers.qwen3_worker import serve
sockdir, parent, mode = sys.argv[1], int(sys.argv[2]), sys.argv[3]
d = qwen3_ipc.private_directory(sockdir)
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(str(qwen3_ipc.socket_path(d, os.getpid())))
listener.listen(1)
isolation = deny_network()
class Eng:
    def __init__(self):
        self.active = False
        self.thread = None
    def generate(self, messages):
        self.retained = self.produce(messages)
        return self.retained
    def produce(self, messages):
        self.active = True
        if mode in {'live_done', 'transport', 'live_limit'}:
            self.thread = threading.Thread(target=threading.Event().wait, daemon=True)
            self.thread.start()
        try:
            if mode == 'live_limit':
                yield {'kind': 'delta', 'text': 'x' * (qwen3_ipc.MAX_DELTA_BYTES + 1)}
            elif mode == 'transport':
                for _ in range(1000):
                    yield {'kind': 'delta', 'text': 'synthetic'}
            elif mode == 'no_terminal':
                yield {'kind': 'delta', 'text': 'partial'}
                return
            else:
                yield {'kind': 'delta', 'text': 'synthetic'}
            yield {'kind': 'done', 'finish_reason': 'eos', 'input_tokens': 1, 'output_tokens': 1}
        finally:
            self.active = False
    def producer_alive(self):
        return self.active or (self.thread is not None and self.thread.is_alive())
if mode == 'wire_limit':
    import packages.providers.qwen3_worker as worker
    worker.MAX_WIRE_BYTES = 1024
serve(d, Eng(), 'a' * 64, isolation, listener, parent, dict(qwen3_ipc.MANIFEST['runtime']), 64, 16)
"""


@asynccontextmanager
async def _child(mode="healthy"):
    from packages.resources.processes import WorkerManager, WorkerSpec

    with tempfile.TemporaryDirectory(prefix="qwen3-synthetic-") as directory:

        async def ready(_worker_id, process):
            try:
                reader, writer = await asyncio.open_unix_connection(
                    str(qwen3_ipc.socket_path(directory, process.pid))
                )
            except (FileNotFoundError, ConnectionRefusedError):
                return False
            try:
                qwen3_ipc.peer_pid(writer.get_extra_info("socket"), process.pid)
                hello = decode_json(
                    await asyncio.wait_for(qwen3_ipc.receive_frame(reader, MAX_FRAME_BYTES), 2),
                    MAX_FRAME_BYTES,
                )
                incarnation = parse_hello(
                    hello, process.pid, max_input_tokens=64, max_new_tokens=16
                )
                await qwen3_ipc.send_frame(
                    writer,
                    encode_json(request_message("probe", incarnation, NONCE), MAX_REQUEST_BYTES),
                    MAX_REQUEST_BYTES,
                )
                value = decode_json(
                    await asyncio.wait_for(qwen3_ipc.receive_frame(reader, MAX_FRAME_BYTES), 2),
                    MAX_FRAME_BYTES,
                )
                parse_event(value, process.pid, incarnation, NONCE)
                assert await asyncio.wait_for(reader.read(1), 2) == b""
                return True
            finally:
                writer.close()
                await writer.wait_closed()

        manager = WorkerManager(
            [
                WorkerSpec(
                    id="qwen3-synthetic",
                    argv=(sys.executable, "-c", DRIVER, directory, str(os.getpid()), mode),
                    cwd=ROOT,
                    env={"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
                    startup_timeout_s=5,
                )
            ],
            readiness=ready,
        )
        identity = None
        try:
            result = await manager.start("qwen3-synthetic")
            assert result["ok"] and result["ready"], result
            identity = manager.owned_identity("qwen3-synthetic")
            assert identity is not None
            yield manager, identity, directory
        finally:
            if identity is not None:
                result = await manager.stop_if_current(identity)
                assert result["ok"], result
                assert result["stop_evidence"]["reaped"] and result["stop_evidence"]["group_empty"]
            await manager.close()


@asynccontextmanager
async def _connection(identity, directory):
    reader, writer = await asyncio.open_unix_connection(
        str(qwen3_ipc.socket_path(directory, identity.pid))
    )
    try:
        hello = decode_json(
            await asyncio.wait_for(qwen3_ipc.receive_frame(reader, MAX_FRAME_BYTES), 2),
            MAX_FRAME_BYTES,
        )
        incarnation = parse_hello(hello, identity.pid, max_input_tokens=64, max_new_tokens=16)
        yield reader, writer, incarnation
    finally:
        writer.close()
        await writer.wait_closed()


async def _generate(writer, incarnation, text="synthetic"):
    value = request_message("generate", incarnation, NONCE, [{"role": "user", "content": text}])
    await qwen3_ipc.send_frame(writer, encode_json(value, MAX_REQUEST_BYTES), MAX_REQUEST_BYTES)


async def _event(reader, identity, incarnation, seq):
    value = decode_json(
        await asyncio.wait_for(qwen3_ipc.receive_frame(reader, MAX_FRAME_BYTES), 2), MAX_FRAME_BYTES
    )
    return parse_event(
        value, identity.pid, incarnation, NONCE, seq=seq, max_input_tokens=64, max_new_tokens=16
    )


pytestmark = pytest.mark.skipif(
    sys.platform != "linux"
    or platform.machine() != "x86_64"
    or not ctypes.util.find_library("seccomp"),
    reason="Optional Linux x86_64/seccomp process tier unavailable",
)


@pytest.mark.asyncio
async def test_owned_synthetic_child_readiness():
    async with _child() as (_manager, identity, directory):
        async with _connection(identity, directory) as (reader, writer, incarnation):
            await _generate(writer, incarnation)
            assert (await _event(reader, identity, incarnation, 0))["text"] == "synthetic"
            assert (await _event(reader, identity, incarnation, 1))["finish_reason"] == "eos"
            # This is the real connection-close boundary required by the parent.
            assert await asyncio.wait_for(reader.read(1), 2) == b""
        # Explicit iterator close also released the retained engine iterator.
        async with _connection(identity, directory) as (reader, writer, incarnation):
            await _generate(writer, incarnation, "next distinct request")
            assert (await _event(reader, identity, incarnation, 0))["kind"] == "delta"
            assert (await _event(reader, identity, incarnation, 1))["kind"] == "done"
            assert await asyncio.wait_for(reader.read(1), 2) == b""


@pytest.mark.asyncio
async def test_generate_request_may_exceed_control_default():
    async with _child() as (_manager, identity, directory):
        async with _connection(identity, directory) as (reader, writer, incarnation):
            await _generate(writer, incarnation, "x" * 5000)
            assert (await _event(reader, identity, incarnation, 0))["kind"] == "delta"
            assert (await _event(reader, identity, incarnation, 1))["kind"] == "done"
            assert await asyncio.wait_for(reader.read(1), 2) == b""


@pytest.mark.parametrize("mode", ["live_done", "transport", "live_limit"])
@pytest.mark.asyncio
async def test_live_native_producer_forces_exact_child_exit(mode):
    async with _child(mode) as (manager, identity, directory):
        async with _connection(identity, directory) as (reader, writer, incarnation):
            await _generate(writer, incarnation)
            if mode != "live_limit":
                assert (await _event(reader, identity, incarnation, 0))["kind"] == "delta"
            if mode == "transport":
                writer.close()
                await writer.wait_closed()
            else:
                assert await asyncio.wait_for(reader.read(1), 2) == b""
        process = manager._workers["qwen3-synthetic"].process
        assert await asyncio.wait_for(process.wait(), 3) == 1
        assert not manager.is_current(identity)


@pytest.mark.asyncio
async def test_missing_terminal_is_fixed_failure_and_eof():
    async with _child("no_terminal") as (_manager, identity, directory):
        async with _connection(identity, directory) as (reader, writer, incarnation):
            await _generate(writer, incarnation)
            assert (await _event(reader, identity, incarnation, 0))["text"] == "partial"
            assert (await _event(reader, identity, incarnation, 1))["code"] == "generation_failed"
            assert await asyncio.wait_for(reader.read(1), 2) == b""


@pytest.mark.parametrize("field", ["nonce", "incarnation"])
@pytest.mark.asyncio
async def test_worker_rejects_foreign_request_then_accepts_new_connection(field):
    async with _child() as (_manager, identity, directory):
        async with _connection(identity, directory) as (reader, writer, incarnation):
            request = request_message(
                "generate", incarnation, NONCE, [{"role": "user", "content": "synthetic"}]
            )
            request[field] = "wrong"
            await qwen3_ipc.send_frame(
                writer, encode_json(request, MAX_REQUEST_BYTES), MAX_REQUEST_BYTES
            )
            assert await asyncio.wait_for(reader.read(1), 2) == b""
        async with _connection(identity, directory) as (reader, writer, incarnation):
            await _generate(writer, incarnation)
            assert (await _event(reader, identity, incarnation, 0))["kind"] == "delta"
            assert (await _event(reader, identity, incarnation, 1))["kind"] == "done"
            assert await asyncio.wait_for(reader.read(1), 2) == b""


@pytest.mark.asyncio
async def test_native_aggregate_cap_never_publishes_success():
    async with _child("wire_limit") as (_manager, identity, directory):
        async with _connection(identity, directory) as (reader, writer, incarnation):
            await _generate(writer, incarnation)
            kinds = []
            while True:
                try:
                    event = await _event(reader, identity, incarnation, len(kinds))
                except asyncio.IncompleteReadError:
                    break
                kinds.append(event["kind"])
            assert "done" not in kinds
