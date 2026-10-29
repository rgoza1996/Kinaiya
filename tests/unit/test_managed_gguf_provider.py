"""Managed-native client contract, using public synthetic SSE transports only."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from packages.providers.gguf import ManagedGGUFProvider
from packages.providers.text import (
    CompatibleTextProvider,
    ProviderError,
    ProviderRefusal,
    owned_http_stream_cleanup,
)
from services.resources.providers import managed_text_binding

MESSAGES = [
    {"role": "system", "content": "Public synthetic persona."},
    {"role": "user", "content": "Public synthetic greeting."},
]
PIN = "b" * 64
TOKEN = "synthetic-converse-credential"


def event(text=None, finish=None):
    choice = {"index": 0, "delta": {} if text is None else {"content": text}}
    if finish is not None:
        choice["finish_reason"] = finish
    return ("data: " + json.dumps({"choices": [choice]}) + "\r\n\r\n").encode()


SUCCESS = event("A lunar ") + event("🌙 reply.", "stop") + b"data: [DONE]\n\n"


class Bytes(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed += 1


def client_for(raw=SUCCESS, *, status=200, headers=None):
    stream = raw if isinstance(raw, Bytes) else Bytes([raw])
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, stream=stream, headers=headers or {})

    provider = ManagedGGUFProvider(
        "http://127.0.0.1:8914/",
        TOKEN,
        model="Public display model",
        alias="synthetic/runtime-alias",
        profile="synthetic-native-cpu",
        manifest_sha256=PIN,
        transport=httpx.MockTransport(respond),
    )
    return provider, stream, requests


async def collect(provider):
    return "".join([text async for text in provider.stream(MESSAGES)])


@pytest.mark.parametrize("method", ["stream", "complete"])
async def test_both_methods_use_one_stream_and_truthful_terminal(method):
    provider, stream, requests = client_for(
        Bytes([SUCCESS[i : i + 1] for i in range(len(SUCCESS))])
    )
    try:
        text = await (collect(provider) if method == "stream" else provider.complete(MESSAGES))
        assert text == "A lunar 🌙 reply."
        assert len(requests) == 1
        request = requests[0]
        assert (
            request.method == "POST"
            and str(request.url) == "http://127.0.0.1:8914/v1/chat/completions"
        )
        assert json.loads(request.content) == {
            "model": "synthetic/runtime-alias",
            "messages": MESSAGES,
            "max_tokens": 128,
            "stream": True,
        }
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        assert request.headers["Accept-Encoding"] == "identity"
        assert stream.closed == 1
    finally:
        await provider.close()


async def test_static_binding_is_exact_detached_total_context_not_input_limit():
    provider, _, requests = client_for()
    try:
        expected = {
            "provider": "managed-gguf",
            "model": "Public display model",
            "profile": "synthetic-native-cpu",
            "manifest_sha256": PIN,
            "resources_url": "http://127.0.0.1:8914",
            "context_tokens": 2048,
            "max_new_tokens": 128,
        }
        assert provider.managed_binding == expected
        assert provider.info.provider == "managed-gguf" and provider.info.local is True
        assert provider.info.model == expected["model"] and provider.info.streaming is True
        detached = provider.managed_binding
        detached["context_tokens"] = 4096
        assert managed_text_binding(provider) == expected
        assert TOKEN not in str(provider.managed_binding)
        assert requests == []  # Static binding neither probes nor loads a worker.
    finally:
        await provider.close()


@pytest.mark.parametrize("method", ["stream", "complete"])
@pytest.mark.parametrize(
    "raw",
    [
        event("prefix"),  # EOF is not a stop token.
        event("prefix", "stop"),  # A finish field without DONE is incomplete.
        b"data: [DONE]\n\n",  # DONE alone is not native completion.
        event("prefix", "length") + b"data: [DONE]\n\n",
        event("prefix", "stop") + b"data: [DONE]\n\n" + event("late"),
        event("prefix", "stop") + b"data: [DONE]",  # Unterminated frame.
        b'data: {"error":{"message":"synthetic"}}\n\n',
        b'data: {"choices":[{"index":true,"delta":{"content":"x"}}]}\n\n',
    ],
)
async def test_no_false_success_or_hidden_truncation(method, raw):
    provider, stream, requests = client_for(raw)
    try:
        with pytest.raises(ProviderRefusal):
            await (collect(provider) if method == "stream" else provider.complete(MESSAGES))
        assert len(requests) == 1 and stream.closed == 1
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "status,headers,body",
    [
        (307, {"location": "http://foreign.invalid/v1/chat/completions"}, b""),
        (503, {}, b"synthetic unavailable"),
        (200, {"content-encoding": "gzip"}, SUCCESS),
        (200, {}, b":" + b"x" * (1024 * 1024 + 1)),
    ],
)
async def test_transport_refuses_redirect_encoding_and_wire_overflow(status, headers, body):
    provider, stream, requests = client_for(body, status=status, headers=headers)
    try:
        with pytest.raises(ProviderError):
            await provider.complete(MESSAGES)
        assert len(requests) == 1 and requests[0].url.host == "127.0.0.1"
        assert provider.http.follow_redirects is False
        assert provider.http._trust_env is False
        assert stream.closed == 1
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "tool", "content": "x"}],
        [{"role": "user", "content": "x", "extra": True}],
        [{"role": "user", "content": "x" * (128 * 1024 + 1)}],
    ],
)
async def test_invalid_messages_refuse_before_http(messages):
    provider, _, requests = client_for()
    try:
        with pytest.raises(ValueError):
            await provider.complete(messages)
        assert requests == []
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "change",
    [
        {"base_url": "https://127.0.0.1:8914"},
        {"base_url": "http://remote.invalid"},
        {"base_url": "http://localhost:8914/v1"},
        {"base_url": "http://user:pass@localhost:8914"},
        {"token": ""},
        {"token": "x\nsecret"},
        {"manifest_sha256": "0" * 63},
        {"model": "é" * 65},
        {"profile": "\ud800"},
        {"alias": "alias --model other"},
        {"context_tokens": True},
        {"context_tokens": 32769},
        {"max_new_tokens": True},
        {"context_tokens": 128, "max_new_tokens": 128},
        {"request_timeout_s": float("nan")},
    ],
)
def test_construction_rejects_unprotected_identity_and_limits(change):
    config = dict(
        base_url="http://127.0.0.1:8914",
        token=TOKEN,
        model="Public model",
        alias="synthetic-alias",
        profile="synthetic-profile",
        manifest_sha256=PIN,
    )
    with pytest.raises((ValueError, ProviderError)):
        ManagedGGUFProvider(**{**config, **change})


@pytest.mark.parametrize(
    "mutation", ["foreign-name", "wrong-model", "cloud", "not-streaming", "input-field"]
)
async def test_foreign_binding_cannot_claim_managed_identity(mutation):
    provider, _, _ = client_for()
    try:
        info = provider.info.model_copy()
        binding = provider.managed_binding
        if mutation == "foreign-name":
            info.provider = "openai-compatible"
        elif mutation == "wrong-model":
            info.model = "other-model"
        elif mutation == "cloud":
            info.local = False
        elif mutation == "not-streaming":
            info.streaming = False
        else:
            binding["max_input_tokens"] = binding.pop("context_tokens")
        with pytest.raises(ProviderError):
            managed_text_binding(SimpleNamespace(info=info, managed_binding=binding))
        unmanaged = CompatibleTextProvider("http://localhost/v1", "synthetic")
        try:
            assert managed_text_binding(unmanaged) is None
        finally:
            await unmanaged.close()
    finally:
        await provider.close()


async def test_complete_does_not_escape_owned_real_close_on_repeated_cancel():
    class Held(Bytes):
        def __init__(self):
            super().__init__([SUCCESS])
            self.closing, self.release = asyncio.Event(), asyncio.Event()
            self.close_cancelled = False

        async def aclose(self):
            self.closed += 1
            self.closing.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise

    held = Held()
    provider, _, requests = client_for(held)

    async def run():
        with owned_http_stream_cleanup():
            return await provider.complete(MESSAGES)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(held.closing.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not held.close_cancelled
        assert len(requests) == 1
        held.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert held.closed == 1 and not held.close_cancelled
    finally:
        held.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()
