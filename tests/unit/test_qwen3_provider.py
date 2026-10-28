"""Managed Qwen transport/configuration tests; no optional model runtime."""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from packages.providers import qwen3_ipc as wire
from packages.providers.qwen3 import (
    MAX_HTTP_BYTES,
    ManagedQwen3Provider,
    public_event,
)
from packages.providers.text import ProviderCleanupError, ProviderError, owned_http_stream_cleanup
from services.resources.qwen3 import ManagedQwen3Config, qwen3_worker

MESSAGES = [{"role": "user", "content": "Public synthetic greeting."}]
DONE = {"kind": "done", "finish_reason": "eos", "input_tokens": 10, "output_tokens": 2}


def frames(*rows):
    return b"".join(wire.encode_json(row, wire.MAX_FRAME_BYTES) + b"\n" for row in rows)


class Bytes(httpx.AsyncByteStream):
    def __init__(self, chunks, *, hold=False, failure=False):
        self.chunks = chunks
        self.closing, self.release = asyncio.Event(), asyncio.Event()
        self.close_calls = 0
        self.cancelled = False
        self.failure = failure
        if not hold:
            self.release.set()

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.close_calls += 1
        self.closing.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.failure:
            raise RuntimeError("synthetic close failure, not public")


def provider_for(data, **kwargs):
    stream = data if isinstance(data, Bytes) else Bytes([data])
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, stream=stream, headers={"Content-Type": "application/x-ndjson"})

    return (
        ManagedQwen3Provider(
            "http://127.0.0.1:8765/",
            "synthetic-scoped-token",
            transport=httpx.MockTransport(handle),
            **kwargs,
        ),
        stream,
        calls,
    )


async def test_static_binding_is_detached_and_complete_collects_one_stream():
    data = frames(
        {"kind": "delta", "text": "A lunar "}, {"kind": "delta", "text": "🌙 café."}, DONE
    )
    # Arbitrary byte boundaries include split JSON and a Unicode escape.
    provider, stream, calls = provider_for(Bytes([data[:17], data[17:49], data[49:]]))
    try:
        assert provider.info.model_dump() == {
            "schema_version": "1.0",
            "provider": "managed-qwen3",
            "model": wire.MODEL,
            "local": True,
            "streaming": True,
        }
        binding = provider.managed_binding
        assert binding == {
            "provider": "managed-qwen3",
            "model": wire.MODEL,
            "profile": wire.PROFILE,
            "manifest_sha256": wire.MANIFEST_SHA256,
            "resources_url": "http://127.0.0.1:8765",
            "max_input_tokens": 2048,
            "max_new_tokens": 128,
        }
        binding["model"] = "forged"
        assert provider.managed_binding["model"] == wire.MODEL
        assert "synthetic-scoped-token" not in str(binding)
        assert await provider.complete(MESSAGES) == "A lunar 🌙 café."
        assert len(calls) == 1 and calls[0].url.path == "/v1/converse/completions"
        assert json.loads(calls[0].content) == {"messages": MESSAGES}
        assert calls[0].headers["Authorization"] == "Bearer synthetic-scoped-token"
        assert calls[0].headers["Accept-Encoding"] == "identity"
        assert stream.close_calls == 1
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "https://127.0.0.1"},
        {"base_url": "http://public.invalid"},
        {"base_url": "http://localhost/path"},
        {"base_url": "http://user:pw@localhost"},
        {"base_url": "http://localhost?x=y"},
        {"base_url": 1},
        {"token": ""},
        {"token": "a\nb"},
        {"max_input_tokens": True},
        {"max_input_tokens": 63},
        {"max_input_tokens": 4097},
        {"max_new_tokens": 15},
        {"max_new_tokens": 513},
        {"max_new_tokens": 128.0},
        {"request_timeout_s": float("inf")},
        {"request_timeout_s": True},
        {"request_timeout_s": 0},
        {"request_timeout_s": 901},
    ],
)
def test_invalid_config_refuses_before_client_creation(monkeypatch, changes):
    def forbidden(**kwargs):
        raise AssertionError("HTTP client created for invalid configuration")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    fields = {"base_url": "http://localhost", "token": "synthetic", **changes}
    with pytest.raises(ValueError):
        ManagedQwen3Provider(**fields)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b'{"kind":"delta","text":"x"}\n',
        frames(DONE).rstrip(b"\n"),
        b"[]\n",
        b'{"kind":"done","kind":"done"}\n',
        b'{"kind":[]}\n',
        b'{"kind":"delta","text":"\\ud800"}\n',
        b'{"kind":"delta","text":""}\n',
        frames({"kind": "done", "finish_reason": "eos", "input_tokens": True, "output_tokens": 2}),
        frames({**DONE, "input_tokens": 2049}),
        frames({**DONE, "output_tokens": 129}),
        frames({**DONE, "finish_reason": "length"}),
        frames({**DONE, "seq": 0}),
        frames({"kind": "error", "code": "arbitrary native trace"}),
        frames(DONE, {"kind": "delta", "text": "late"}),
        b"x" * (wire.MAX_FRAME_BYTES + 1),
        frames({"kind": "delta", "text": "a" * (wire.MAX_DELTA_BYTES + 1)}),
        frames(*([{"kind": "delta", "text": "x" * wire.MAX_DELTA_BYTES}] * 5), DONE),
        frames(*([{"kind": "delta", "text": "x"}] * wire.MAX_EVENTS), DONE),
    ],
)
async def test_untrusted_frames_refuse_without_repair_or_retry(data):
    provider, stream, calls = provider_for(data)
    try:
        with pytest.raises(ProviderError):
            await provider.complete(MESSAGES)
        assert len(calls) == 1 and stream.close_calls == 1
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "code", sorted(wire.ERROR_CODES | {"busy", "paused", "unavailable", "deadline", "disconnected"})
)
async def test_finite_public_error_codes_are_visible_and_never_retried(code):
    provider, _, calls = provider_for(frames({"kind": "error", "code": code}))
    try:
        with pytest.raises(ProviderError, match=code):
            await provider.complete(MESSAGES)
        assert len(calls) == 1
    finally:
        await provider.close()


async def test_length_is_not_successful_eos():
    provider, _, _ = provider_for(
        frames(
            {"kind": "delta", "text": "Partial"},
            {**DONE, "finish_reason": "length", "output_tokens": 128},
        )
    )
    try:
        source = provider.stream(MESSAGES)
        assert await anext(source) == "Partial"
        with pytest.raises(ProviderError, match="output token limit"):
            await anext(source)
    finally:
        await provider.close()


async def test_decoded_and_terminal_inclusive_exact_boundaries():
    # Escaped NUL expands sixfold while remaining a valid 8192-byte delta.
    rows = [{"kind": "delta", "text": "\0" * wire.MAX_DELTA_BYTES}] * 4
    provider, _, _ = provider_for(frames(*rows, DONE))
    try:
        assert len((await provider.complete(MESSAGES)).encode()) == wire.MAX_TEXT_BYTES
    finally:
        await provider.close()
    provider, _, _ = provider_for(
        frames(*([{"kind": "delta", "text": "x"}] * (wire.MAX_EVENTS - 1)), DONE)
    )
    try:
        assert len(await provider.complete(MESSAGES)) == wire.MAX_EVENTS - 1
    finally:
        await provider.close()


async def test_http_aggregate_bound_is_enforced_independently_of_frame_bounds():
    # Whitespace pads legal frames. Their decoded text still remains tiny.
    delta = b" " * (wire.MAX_FRAME_BYTES - 28) + b'{"kind":"delta","text":"x"}\n'
    assert len(delta) <= wire.MAX_FRAME_BYTES + 1
    provider, _, _ = provider_for(Bytes([delta] * (MAX_HTTP_BYTES // len(delta) + 1)))
    try:
        with pytest.raises(ProviderError, match="byte bound"):
            await provider.complete(MESSAGES)
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "http://public.invalid"}),
        httpx.Response(403, content=b"private synthetic details must not surface"),
        httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=Bytes([])),
    ],
)
async def test_http_status_and_content_encoding_do_not_leak_or_redirect(response):
    calls = []

    def handle(request):
        calls.append(request)
        return response

    provider = ManagedQwen3Provider(
        "http://localhost", "synthetic", transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(ProviderError) as result:
            await provider.complete(MESSAGES)
        assert "private synthetic" not in str(result.value) and len(calls) == 1
    finally:
        await provider.close()


@pytest.mark.parametrize("phase", ["eof", "error", "status"])
async def test_real_http_close_remains_owned_through_repeated_cancellation(phase):
    data = frames(DONE) if phase == "eof" else b"malformed\n"
    stream = Bytes([data], hold=True)
    provider = ManagedQwen3Provider(
        "http://localhost",
        "synthetic",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(503 if phase == "status" else 200, stream=stream)
        ),
    )

    async def consume():
        with owned_http_stream_cleanup():
            return await provider.complete(MESSAGES)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(stream.closing.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and not stream.cancelled and stream.close_calls == 1
        stream.release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert stream.close_calls == 1 and not stream.cancelled
    finally:
        stream.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()


async def test_failed_real_close_is_explicit_uncertain_cleanup():
    stream = Bytes([frames(DONE)], failure=True)
    provider, _, _ = provider_for(stream)
    try:
        with owned_http_stream_cleanup(), pytest.raises(ProviderCleanupError):
            await provider.complete(MESSAGES)
        assert stream.close_calls == 1
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "messages",
    [
        None,
        [],
        [{"role": "tool", "content": "x"}],
        [{"role": "user", "content": "x", "extra": 1}],
        [{"role": "user", "content": "x" * (wire.MAX_MESSAGES_BYTES + 1)}],
    ],
)
async def test_invalid_dialogue_never_makes_http_request(messages):
    provider, _, calls = provider_for(frames(DONE))
    try:
        with pytest.raises(ProviderError):
            await provider.complete(messages)
        assert not calls
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("memory_bytes", 0),
        ("memory_bytes", 32 * 1024**3 + 1),
        ("host_memory_reserve_bytes", 8 * 1024**3 - 1),
        ("cpu_affinity_count", True),
        ("cpu_affinity_count", 1),
        ("cpu_affinity_count", 2.0),
        ("cpu_affinity_count", 5),
        ("max_input_tokens", 63),
        ("max_new_tokens", 513),
        ("request_timeout_s", 901),
        ("startup_timeout_s", float("nan")),
        ("unknown", "x"),
    ],
)
def test_managed_config_is_strict_and_bounded(field, value):
    with pytest.raises(ValidationError):
        ManagedQwen3Config(python=sys.executable, model_dir="/synthetic/model", **{field: value})


def test_factory_uses_metadata_only_fixed_argv_and_no_parent_credentials(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    model, workspace = tmp_path / "model", tmp_path / "agent"
    model.mkdir()
    workspace.mkdir()
    for name, pin in wire.MANIFEST["assets"].items():
        with (model / name).open("wb") as target:
            target.truncate(pin["bytes"])  # Sparse synthetic metadata, never real model data.
    config = ManagedQwen3Config(python=sys.executable, model_dir=str(model))

    def forbidden(*args, **kwargs):
        raise AssertionError("Parent must not read/copy model file contents")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    spec, endpoint = qwen3_worker(config, root, workspace)
    try:
        assert spec.id == "qwen3" and spec.argv[:3] == (
            sys.executable,
            "-m",
            "packages.providers.qwen3_worker",
        )
        assert spec.startup_timeout_s == 120
        assert spec.env == {
            "PYTHONPATH": str(root),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
        assert (
            config.memory_bytes == 16 * 1024**3 and config.host_memory_reserve_bytes == 8 * 1024**3
        )
        assert endpoint.limits == wire.validate_limits(2048, 128)
        leaf = model / "config.json"
        leaf.unlink()
        leaf.symlink_to(model / "generation_config.json")
        with pytest.raises(ValueError):
            qwen3_worker(config, root, workspace)
    finally:
        endpoint.close()


@pytest.mark.parametrize("kind", [None, [], {}, 1, True])
def test_public_kind_must_be_a_string(kind):
    with pytest.raises(ValueError):
        public_event({"kind": kind}, 0, max_input_tokens=2048, max_new_tokens=128)
