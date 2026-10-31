"""Local export fences with fake native sockets and HTTP trap transports only."""

import asyncio
import copy
import json
from collections import Counter

import httpx
import pytest

from packages.contracts.agent_tools import agent_tool_scope, native_tool_snapshot
from packages.providers import qwen3
from packages.providers.gguf import ManagedGGUFProvider
from packages.providers.lmstudio import LMStudioProvider
from packages.providers.qwen3 import ManagedQwen3Provider
from packages.providers.text import ProviderRefusal

_MESSAGES = [{"role": "user", "content": "Public synthetic catalog observation."}]
_REFUSAL = "Agent tool metadata disclosure changed"
_TOOLS = native_tool_snapshot(catalog_available=True)
_MODEL = {
    "type": "llm",
    "identifier": "synthetic-alias",
    "instanceReference": "synthetic-instance",
    "path": "public/synthetic.gguf",
    "modelKey": "synthetic",
    "indexedModelIdentifier": "public/synthetic.gguf",
    "format": "gguf",
    "deviceIdentifier": None,
}
_LOAD = {"fields": [{"key": "llm.load.contextLength", "value": 2048}]}
_BASE = {"fields": [{"key": "llm.prediction.systemPrompt", "value": ""}]}


async def _collect(provider, method):
    if method == "complete":
        return await provider.complete(_MESSAGES)
    return "".join([text async for text in provider.stream(_MESSAGES)])


def _guard(value):
    def permitted():
        if value == "raises":
            raise RuntimeError("Synthetic private guard detail")
        return value

    return permitted


class _NativeWire:
    def __init__(self, hold=None):
        self.hold = hold
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.connect_calls = self.predictions = 0
        self.calls = Counter()
        self.sockets = []

    async def gate(self, boundary):
        if self.hold is not None and boundary == self.hold:
            self.entered.set()
            await self.release.wait()

    async def connect(self, endpoint):
        assert endpoint == "ws://127.0.0.1:1234/llm"
        self.connect_calls += 1
        await self.gate("connect")
        socket = _NativeSocket(self)
        self.sockets.append(socket)
        return socket

    def provider(self):
        return LMStudioProvider(
            "http://127.0.0.1:1234/v1",
            "synthetic-alias",
            transport_factory=self.connect,
            timeout=2,
        )


class _NativeSocket:
    def __init__(self, wire):
        self.wire, self.queue = wire, asyncio.Queue()
        self.close_calls = self.wait_calls = 0

    def put(self, packet, boundary=None):
        self.queue.put_nowait((packet, boundary))

    async def send(self, raw):
        packet = json.loads(raw)
        if "authVersion" in packet:
            self.put({"success": True})
        elif packet["type"] == "rpcCall":
            endpoint = packet["endpoint"]
            self.wire.calls[endpoint] += 1
            result = {
                "listLoaded": [_MODEL],
                "getModelInfo": _MODEL,
                "getLoadConfig": _LOAD,
                "getBasePredictionConfig": _BASE,
                "applyPromptTemplate": {"formatted": "Public synthetic formatted prompt"},
                "countTokens": {"tokenCount": 25},
            }[endpoint]
            self.put(
                {"type": "rpcResult", "callId": packet["callId"], "result": copy.deepcopy(result)},
                (endpoint, self.wire.calls[endpoint]),
            )
        elif packet["type"] == "channelCreate":
            assert packet["endpoint"] == "predict"
            self.wire.predictions += 1
            creation = packet["creationParameter"]
            fields = {}
            for layer in creation["predictionConfigStack"]["layers"]:
                fields.update({field["key"]: field["value"] for field in layer["config"]["fields"]})
            for message in (
                {
                    "type": "fragment",
                    "fragment": {
                        "content": "Public result",
                        "tokensCount": 1,
                        "containsDrafted": False,
                        "reasoningType": "none",
                        "isStructural": False,
                    },
                },
                {
                    "type": "success",
                    "stats": {
                        "stopReason": "eosFound",
                        "promptTokensCount": 25,
                        "predictedTokensCount": 1,
                        "totalTokensCount": 26,
                    },
                    "modelInfo": _MODEL,
                    "loadModelConfig": _LOAD,
                    "predictionConfig": {
                        "fields": [{"key": k, "value": v} for k, v in fields.items()]
                    },
                },
            ):
                self.put({"type": "channelSend", "channelId": 1, "message": message})
            self.put({"type": "channelClose", "channelId": 1})
        else:
            raise AssertionError("Unexpected fake native send")

    async def recv(self):
        packet, boundary = await self.queue.get()
        await self.wire.gate(boundary)
        return json.dumps(packet)

    async def close(self):
        self.close_calls += 1

    async def wait_closed(self):
        self.wait_calls += 1


class _Body(httpx.AsyncByteStream):
    def __init__(self, payload):
        self.payload, self.closed = payload, 0

    async def __aiter__(self):
        yield self.payload

    async def aclose(self):
        self.closed += 1


def _managed(kind, *, trap):
    requests = []
    if kind == "qwen":
        payload = b"".join(
            json.dumps(frame).encode() + b"\n"
            for frame in (
                {"kind": "delta", "text": "Public result"},
                {"kind": "done", "finish_reason": "eos", "input_tokens": 25, "output_tokens": 1},
            )
        )
    else:
        payload = (
            b"".join(
                b"data: " + json.dumps({"choices": [choice]}).encode() + b"\n\n"
                for choice in (
                    {"index": 0, "delta": {"content": "Public result"}},
                    {"index": 0, "delta": {}, "finish_reason": "stop"},
                )
            )
            + b"data: [DONE]\n\n"
        )
    body = _Body(payload)

    def respond(request):
        requests.append(request)
        if trap:
            raise AssertionError("Withdrawn metadata reached HTTP transport")
        return httpx.Response(200, stream=body)

    fields = {"transport": httpx.MockTransport(respond)}
    if kind == "qwen":
        provider = ManagedQwen3Provider(
            "http://127.0.0.1:8914/", "public-synthetic-token", **fields
        )
    else:
        provider = ManagedGGUFProvider(
            "http://127.0.0.1:8914/",
            "public-synthetic-token",
            model="Synthetic display model",
            alias="synthetic-alias",
            profile="synthetic-profile",
            manifest_sha256="a" * 64,
            **fields,
        )
    return provider, requests, body


@pytest.mark.parametrize("method", ["complete", "stream"])
@pytest.mark.parametrize("guard", [False, 1, "raises"])
async def test_lmstudio_initial_refusal_never_opens_a_native_connection(method, guard):
    wire = _NativeWire()
    provider = wire.provider()
    try:
        with agent_tool_scope(_TOOLS, export_guard=_guard(guard)):
            with pytest.raises(ProviderRefusal) as caught:
                await _collect(provider, method)
        assert str(caught.value) == _REFUSAL
        assert wire.connect_calls == wire.predictions == 0
        assert not wire.calls
        assert provider._active is None and not provider._uncertain
    finally:
        await provider.close()


@pytest.mark.parametrize("method", ["complete", "stream"])
@pytest.mark.parametrize(
    "boundary",
    [
        "connect",
        ("getBasePredictionConfig", 1),
        ("applyPromptTemplate", 1),
        ("countTokens", 1),
        ("getBasePredictionConfig", 2),
    ],
)
async def test_lmstudio_revocation_across_setup_and_count_awaits_never_predicts(method, boundary):
    wire = _NativeWire(hold=boundary)
    provider = wire.provider()
    allowed, worker = True, None
    try:
        with agent_tool_scope(_TOOLS, export_guard=lambda: allowed):
            worker = asyncio.create_task(_collect(provider, method))
            await asyncio.wait_for(wire.entered.wait(), 1)
            allowed = False
            wire.release.set()
            with pytest.raises(ProviderRefusal) as caught:
                await asyncio.wait_for(worker, 1)
        assert str(caught.value) == _REFUSAL
        assert wire.predictions == 0
        assert wire.connect_calls == 1
        assert all(socket.close_calls == socket.wait_calls == 1 for socket in wire.sockets)
        assert provider._active is None and not provider._uncertain
        if boundary in ("connect", ("getBasePredictionConfig", 1)):
            assert wire.calls["applyPromptTemplate"] == wire.calls["countTokens"] == 0
        elif boundary == ("applyPromptTemplate", 1):
            assert wire.calls["applyPromptTemplate"] == 1 and wire.calls["countTokens"] == 0
        else:
            assert wire.calls["applyPromptTemplate"] == wire.calls["countTokens"] == 1
    finally:
        wire.release.set()
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 3)
        await provider.close()


@pytest.mark.parametrize("kind", ["qwen", "gguf"])
@pytest.mark.parametrize("method", ["complete", "stream"])
@pytest.mark.parametrize("guard", [False, 1, "raises"])
async def test_managed_refusal_reaches_no_http_transport(kind, method, guard):
    provider, requests, body = _managed(kind, trap=True)
    try:
        with agent_tool_scope(_TOOLS, export_guard=_guard(guard)):
            with pytest.raises(ProviderRefusal) as caught:
                await _collect(provider, method)
        assert str(caught.value) == _REFUSAL
        assert requests == [] and body.closed == 0
    finally:
        await provider.close()


@pytest.mark.parametrize("method", ["complete", "stream"])
async def test_qwen_final_dispatch_rechecks_after_native_message_validation(monkeypatch, method):
    provider, requests, _ = _managed("qwen", trap=True)
    allowed = True
    native_validate = qwen3.validate_messages

    def validate(messages):
        nonlocal allowed
        result = native_validate(messages)
        allowed = False
        return result

    monkeypatch.setattr(qwen3, "validate_messages", validate)
    try:
        with agent_tool_scope(_TOOLS, export_guard=lambda: allowed):
            with pytest.raises(ProviderRefusal, match=_REFUSAL):
                await _collect(provider, method)
        assert requests == []
    finally:
        await provider.close()


@pytest.mark.parametrize("method", ["complete", "stream"])
@pytest.mark.parametrize("kind", ["lmstudio", "qwen", "gguf"])
async def test_allowed_local_export_remains_one_inference_with_real_close(kind, method):
    if kind == "lmstudio":
        wire = _NativeWire()
        provider = wire.provider()
    else:
        provider, requests, body = _managed(kind, trap=False)
    try:
        with agent_tool_scope(_TOOLS, export_guard=lambda: True):
            assert await _collect(provider, method) == "Public result"
        if kind == "lmstudio":
            assert wire.predictions == wire.connect_calls == 1
            assert all(socket.close_calls == socket.wait_calls == 1 for socket in wire.sockets)
        else:
            assert len(requests) == body.closed == 1
    finally:
        await provider.close()
