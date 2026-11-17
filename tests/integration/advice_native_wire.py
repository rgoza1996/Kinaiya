"""Synthetic native protocol fixture, adapted from catalog export guards.

No sockets leave this process. The production LM Studio adapter receives the
same protocol frames as the existing guard fixture, with recorded requests and
an optional generation-reply gate for operation-lifecycle controls.
"""

import asyncio
import copy
import json
from collections import Counter

from packages.providers.lmstudio import LMStudioProvider

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


class NativeWire:
    def __init__(self, hold=None):
        self.hold = hold
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.connect_calls = self.predictions = 0
        self.calls = Counter()
        self.sockets = []
        self.packets = []
        self.cancelled, self.cancel_entered = asyncio.Event(), asyncio.Event()
        self.cancel_release = asyncio.Event()
        self.hold_cancel_ack = False
        self.cancel_ack = True

    async def gate(self, boundary):
        if boundary == "cancel-ack" and self.hold_cancel_ack:
            self.cancel_entered.set()
            await self.cancel_release.wait()
        if self.hold is not None and boundary == self.hold:
            self.entered.set()
            await self.release.wait()

    async def connect(self, endpoint):
        assert endpoint == "ws://127.0.0.1:1234/llm"
        self.connect_calls += 1
        await self.gate("connect")
        socket = NativeSocket(self)
        self.sockets.append(socket)
        return socket

    def provider(self):
        return LMStudioProvider(
            "http://127.0.0.1:1234/v1",
            "synthetic-alias",
            transport_factory=self.connect,
            timeout=2,
        )


class NativeSocket:
    def __init__(self, wire):
        self.wire, self.queue = wire, asyncio.Queue()
        self.close_calls = self.wait_calls = 0
        self.terminal = None

    def put(self, packet, boundary=None):
        self.queue.put_nowait((packet, boundary))

    async def send(self, raw):
        packet = json.loads(raw)
        self.wire.packets.append(copy.deepcopy(packet))
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
                if message["type"] == "success":
                    self.terminal = copy.deepcopy(message)
                self.put({"type": "channelSend", "channelId": 1, "message": message}, "model-reply")
            self.put({"type": "channelClose", "channelId": 1})
        elif packet["type"] == "channelSend":
            assert packet == {"type": "channelSend", "channelId": 1, "message": {"type": "cancel"}}
            assert self.terminal is not None
            self.wire.cancelled.set()
            # Replace queued prediction output with a native cancellation
            # terminal from the same captured model and configuration.
            while not self.queue.empty():
                self.queue.get_nowait()
            if self.wire.cancel_ack:
                terminal = copy.deepcopy(self.terminal)
                terminal["stats"]["stopReason"] = "userStopped"
                self.put({"type": "channelSend", "channelId": 1, "message": terminal}, "cancel-ack")
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
