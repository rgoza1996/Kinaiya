"""Bounded, local LM Studio auth-v1 protocol; imports no optional runtime at startup.

Protocol: lmstudio-js 4b9af903e866808013524eece1e9ceb7a7f18c68,
llmBackendInterface.ts and baseModelBackendInterface.ts. The supported low-level
protocol lets the formatter and prediction share an explicit config stack; the
high-level applyPromptTemplate convenience method cannot supply that stack.
"""

import asyncio
import copy
import json
import math
import re
import uuid
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

from packages.contracts.models import ProviderInfo
from packages.providers.prompt_budget import (
    MAX_SAFE_COUNT,
    PromptCountUnavailable,
    PromptTokenCount,
)
from packages.providers.prompt_budget import request_digest as _digest
from packages.providers.text import ProviderCleanupError, ProviderError, ProviderRefusal

_MAX_FRAME = 512 * 1024
_MAX_WIRE = 4 * 1024 * 1024
_MAX_TEXT = 256 * 1024
_MAX_EVENTS = 8192
_IDENTITY = (
    "identifier",
    "instanceReference",
    "path",
    "modelKey",
    "indexedModelIdentifier",
    "format",
    "deviceIdentifier",
)
_STOPS = {
    "eosFound",
    "stopStringFound",
    "userStopped",
    "modelUnloaded",
    "failed",
    "toolCalls",
    "maxPredictedTokensReached",
    "contextLengthReached",
}
_BASE_KEYS = {
    "temperature",
    "maxPredictedTokens",
    "stopStrings",
    "toolCallStopStrings",
    "structured",
    "tools",
    "toolChoice",
    "toolNaming",
    "systemPrompt",
    "seed",
    "contextPrefill",
    "topKSampling",
    "repeatPenalty",
    "minPSampling",
    "topPSampling",
    "logProbs",
    "promptTemplate",
    "contextOverflowPolicy",
    "reasoning.enableThinking",
    "reasoning.parsing",
    "reasoning.budgetTokens",
    "llama.cpuThreads",
    "llama.presencePenalty",
    "llama.frequencyPenalty",
    "llama.logitBias",
    "llama.typicalPSampling",
    "llama.tfsSampling",
    "llama.xtcProbability",
    "llama.xtcThreshold",
    "llama.mirostatSampling",
    "llama.repeatPenaltyTokensCount",
    "llama.penalizeNewline",
    "speculativeDecoding.draftModel",
    "speculativeDecoding.numDraftTokensExact",
    "speculativeDecoding.minDraftLengthToConsider",
    "speculativeDecoding.minContinueDraftingProbability",
    "vision.userMaxImageDimensionPixels",
    "vision.ignoreModelPreferredMaxImageDimension",
}


def _refuse():
    return ProviderRefusal("LM Studio native response or configuration refused")


def _integer(value, minimum=0, maximum=MAX_SAFE_COUNT):
    return type(value) is int and minimum <= value <= maximum


def _encoded(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > _MAX_FRAME:
            raise _refuse()
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise _refuse() from exc


def _decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Nonfinite JSON")

    try:
        if type(raw) is not str or len(raw.encode("utf-8")) > _MAX_FRAME:
            raise ValueError("Invalid frame")
        result = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
        if type(result) is not dict:
            raise ValueError("Invalid packet")
        pending, nodes = [(result, 0)], 0
        while pending:
            value, depth = pending.pop()
            nodes += 1
            if depth > 32 or nodes > 32768:
                raise ValueError("JSON bound")
            if type(value) is dict:
                pending.extend((v, depth + 1) for v in value.values())
            elif type(value) is list:
                pending.extend((v, depth + 1) for v in value)
            elif type(value) is str:
                value.encode("utf-8")
            elif type(value) is float and not math.isfinite(value):
                raise ValueError("Nonfinite JSON")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise _refuse() from exc


def _fields(value):
    if type(value) is not dict or set(value) != {"fields"} or type(value["fields"]) is not list:
        raise _refuse()
    result = {}
    for field in value["fields"]:
        if (
            type(field) is not dict
            or set(field) != {"key", "value"}
            or type(field["key"]) is not str
            or not 1 <= len(field["key"]) <= 256
            or field["key"] in result
        ):
            raise _refuse()
        result[field["key"]] = copy.deepcopy(field["value"])
    return result


def _kv(fields):
    return {"fields": [{"key": key, "value": fields[key]} for key in sorted(fields)]}


def _identity(value, alias):
    if (
        type(value) is not dict
        or value.get("type") != "llm"
        or value.get("identifier") != alias
        or value.get("format") not in {"gguf", "mlx"}
        or "deviceIdentifier" not in value
        or value["deviceIdentifier"] is not None
        or any(
            type(value.get(k)) is not str or not 1 <= len(value[k].encode("utf-8")) <= 1024
            for k in (
                "identifier",
                "instanceReference",
                "path",
                "modelKey",
                "indexedModelIdentifier",
            )
        )
    ):
        raise _refuse()
    return {key: value.get(key) for key in _IDENTITY}


def _history(messages):
    if type(messages) is not list or not 1 <= len(messages) <= _MAX_EVENTS:
        raise _refuse()
    history = []
    for message in messages:
        if (
            type(message) is not dict
            or set(message) != {"role", "content"}
            or type(message["role"]) is not str
            or message["role"] not in {"system", "user", "assistant"}
            or type(message["content"]) is not str
        ):
            raise _refuse()
        history.append(
            {"role": message["role"], "content": [{"type": "text", "text": message["content"]}]}
        )
    result = {"messages": history}
    _encoded(result)
    return result


@dataclass
class _Operation:
    socket: object = None
    call: int = 0
    pending_call: int | None = None
    channel_started: bool = False
    channel_closed: bool = False
    terminal: bool = False
    wire_bytes: int = 0
    events: int = 0
    cleanup: asyncio.Task | None = None
    snapshot: dict | None = None


class LMStudioProvider:
    def __init__(
        self,
        base_url,
        model,
        *,
        max_tokens=128,
        timeout=60,
        reasoning_effort="none",
        allow_cloud=False,
        transport_factory=None,
    ):
        try:
            url = urlparse(base_url)
            if (
                url.scheme not in {"http", "https", "ws", "wss"}
                or url.hostname not in {"127.0.0.1", "::1", "localhost"}
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in {"", "/", "/v1", "/v1/", "/llm", "/llm/"}
                or url.port is None
            ):
                raise ValueError("Endpoint")
            scheme = "wss" if url.scheme in {"https", "wss"} else "ws"
            self._endpoint = f"{scheme}://{url.netloc}/llm"
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderError("LM Studio requires an explicit loopback native endpoint") from exc
        if (
            type(model) is not str
            or re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", model) is None
            or not _integer(max_tokens, 16, 2048)
            or reasoning_effort != "none"
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 1 <= timeout <= 900
            or type(allow_cloud) is not bool
        ):
            raise ProviderError("Unsupported LM Studio native configuration")
        self.model, self.max_tokens, self.reasoning_effort = model, max_tokens, reasoning_effort
        self.timeout = float(timeout)
        self.info = ProviderInfo(provider="lmstudio", model=model, local=True, streaming=True)
        self._transport_factory = transport_factory
        self._cleanup_timeout = min(self.timeout, 5)
        self._active = None
        self._uncertain = False
        self._closed = False
        self._measurement = None

    def _configuration(self):
        return self._endpoint, self.model, self.max_tokens, self.reasoning_effort, self.timeout

    async def _connect(self):
        if self._transport_factory is not None:
            return await self._transport_factory(self._endpoint)
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:
            raise ProviderError(
                "LM Studio requires the optional lmstudio WebSocket dependency"
            ) from exc
        return await connect(
            self._endpoint,
            open_timeout=min(self.timeout, 5),
            close_timeout=self._cleanup_timeout,
            max_size=_MAX_FRAME,
            max_queue=1,
            compression=None,
            proxy=None,
        )

    async def _send(self, op, packet):
        await op.socket.send(_encoded(packet))

    async def _receive(self, op):
        raw = await op.socket.recv()
        packet = _decode(raw)
        op.wire_bytes += len(raw.encode("utf-8"))
        op.events += 1
        if op.wire_bytes > _MAX_WIRE or op.events > _MAX_EVENTS:
            raise _refuse()
        return packet

    async def _rpc(self, op, endpoint, parameter=None):
        if op.pending_call is not None:
            raise _refuse()
        op.call += 1
        packet = {"type": "rpcCall", "callId": op.call, "endpoint": endpoint}
        if parameter is not None:
            packet["parameter"] = parameter
        _encoded(packet)
        op.pending_call = op.call
        await self._send(op, packet)
        reply = await self._receive(op)
        if type(reply.get("callId")) is not int or reply.get("callId") != op.pending_call:
            raise _refuse()
        if reply.get("type") == "rpcError" and set(reply) == {"type", "callId", "error"}:
            op.pending_call = None
            raise _refuse()
        if set(reply) != {"type", "callId", "result"} or reply.get("type") != "rpcResult":
            raise _refuse()
        op.pending_call = None
        return reply["result"]

    @asynccontextmanager
    async def _operation(self):
        if self._uncertain:
            raise ProviderCleanupError("LM Studio native cleanup is unconfirmed; reuse is blocked")
        if self._closed or self._active is not None:
            raise ProviderError("LM Studio native provider is closed or busy")
        op = _Operation()
        self._active = op
        try:
            op.socket = await self._connect()
            await self._send(
                op,
                {
                    "authVersion": 1,
                    "clientIdentifier": "guest:" + str(uuid.uuid4()),
                    "clientPasskey": str(uuid.uuid4()),
                },
            )
            if await self._receive(op) != {"success": True}:
                raise _refuse()
            yield op
        finally:
            op.cleanup = asyncio.create_task(self._finish(op))
            interrupted = False
            while not op.cleanup.done():
                try:
                    await asyncio.shield(op.cleanup)
                except asyncio.CancelledError:
                    interrupted = True
                except Exception:
                    break
            try:
                op.cleanup.result()
            except BaseException as exc:
                self._uncertain = True
                raise ProviderCleanupError(
                    "LM Studio native cleanup is unconfirmed; reuse is blocked"
                ) from exc
            self._active = None
            if interrupted:
                raise asyncio.CancelledError

    async def _finish(self, op):
        if op.socket is None:
            return
        failure = None
        try:
            async with asyncio.timeout(self._cleanup_timeout):
                if op.pending_call is not None:
                    reply = await self._receive(op)
                    if (
                        type(reply.get("callId")) is not int
                        or reply.get("callId") != op.pending_call
                        or reply.get("type") not in {"rpcResult", "rpcError"}
                        or set(reply)
                        != {
                            "type",
                            "callId",
                            "result" if reply.get("type") == "rpcResult" else "error",
                        }
                    ):
                        raise _refuse()
                    op.pending_call = None
                if op.channel_started and not op.channel_closed:
                    await self._send(
                        op, {"type": "channelSend", "channelId": 1, "message": {"type": "cancel"}}
                    )
                    while not op.channel_closed:
                        packet = await self._receive(op)
                        message = self._channel_packet(op, packet)
                        if message is not None and message.get("type") == "success":
                            if op.terminal:
                                raise _refuse()
                            self._terminal_identity(message, op.snapshot)
                            op.terminal = True
                        elif message is not None and (
                            op.terminal
                            or message.get("type") not in {"fragment", "promptProcessingProgress"}
                        ):
                            raise _refuse()
                    if not op.terminal:
                        raise _refuse()
        except BaseException as exc:
            failure = exc
        # A socket close is required as well as native completion; it cannot
        # substitute for a missing RPC result or prediction terminal.
        try:
            async with asyncio.timeout(self._cleanup_timeout):
                await op.socket.close()
                await op.socket.wait_closed()
        except BaseException as exc:
            failure = exc
        if failure is not None:
            raise ProviderCleanupError("LM Studio native cleanup is unconfirmed") from failure

    def _protected(self):
        return {
            "llm.prediction.maxPredictedTokens": {"checked": True, "value": self.max_tokens},
            "llm.prediction.reasoning.enableThinking": False,
            "llm.prediction.contextOverflowPolicy": "stopAtLimit",
            "llm.prediction.tools": {"type": "none"},
            "llm.prediction.toolChoice": {"type": "generic", "mode": "none"},
            "llm.prediction.structured": {"type": "none"},
            "llm.prediction.speculativeDecoding.draftModel": "",
            "llm.prediction.logProbs": {"checked": False, "value": 0},
        }

    async def _snapshot(self, op):
        loaded = await self._rpc(op, "listLoaded")
        if type(loaded) is not list or len(loaded) > 128:
            raise _refuse()
        selected = [m for m in loaded if type(m) is dict and m.get("identifier") == self.model]
        if len(selected) != 1:
            raise _refuse()
        identity = _identity(selected[0], self.model)
        specifier = {
            "type": "instanceReference",
            "instanceReference": identity["instanceReference"],
        }
        current = await self._rpc(
            op, "getModelInfo", {"specifier": specifier, "throwIfNotFound": True}
        )
        if _identity(current, self.model) != identity:
            raise _refuse()
        load = _fields(await self._rpc(op, "getLoadConfig", {"specifier": specifier}))
        context = load.get("llm.load.contextLength")
        if not _integer(context, 64, 32768) or self.max_tokens >= context:
            raise PromptCountUnavailable(
                "LM Studio loaded context capacity is unsupported; requires 64..32768 tokens and output space"
            )
        base = _fields(
            await self._rpc(
                op,
                "getBasePredictionConfig",
                {"specifier": specifier, "ignoreServerSessionConfig": True},
            )
        )
        if any(key not in {"llm.prediction." + k for k in _BASE_KEYS} for key in base):
            raise _refuse()
        if base.get("llm.prediction.speculativeDecoding.draftModel", "") != "":
            raise _refuse()
        protected = self._protected()
        stack = {
            "layers": [
                {"layerName": "apiOverride", "config": _kv(base)},
                {"layerName": "apiOverride", "config": _kv(protected)},
            ]
        }
        return {
            "identity": identity,
            "specifier": specifier,
            "load": load,
            "base": base,
            "stack": stack,
            "effective": {**base, **protected},
            "context": context,
        }

    async def _measure(self, op, history, snapshot):
        formatted = await self._rpc(
            op,
            "applyPromptTemplate",
            {
                "specifier": snapshot["specifier"],
                "history": history,
                "predictionConfigStack": snapshot["stack"],
                "opts": {},
            },
        )
        if (
            type(formatted) is not dict
            or set(formatted) != {"formatted"}
            or type(formatted["formatted"]) is not str
        ):
            raise _refuse()
        count = await self._rpc(
            op,
            "countTokens",
            {"specifier": snapshot["specifier"], "inputString": formatted["formatted"]},
        )
        if (
            type(count) is not dict
            or set(count) != {"tokenCount"}
            or not _integer(count["tokenCount"], 1)
        ):
            raise _refuse()
        creation = {
            "modelSpecifier": snapshot["specifier"],
            "history": history,
            "predictionConfigStack": snapshot["stack"],
            "ignoreServerSessionConfig": True,
        }
        _encoded(creation)
        measured = PromptTokenCount(
            count["tokenCount"],
            snapshot["context"],
            self.max_tokens,
            _digest(creation),
            _digest(
                {
                    "provider": "lmstudio",
                    "endpoint": self._endpoint,
                    "contract": "auth-v1-instance-text-1",
                    "formatted_sha256": _digest(formatted),
                    **snapshot,
                }
            ),
        )
        return creation, measured

    def _unchanged(self, messages, history, config):
        if self._configuration() != config or _history(messages) != history:
            raise _refuse()

    async def measure_prompt(self, messages):
        try:
            history, config = _history(messages), self._configuration()
            async with asyncio.timeout(min(self.timeout, 10)):
                async with self._operation() as op:
                    snapshot = await self._snapshot(op)
                    _, measured = await self._measure(op, history, snapshot)
                    if await self._snapshot(op) != snapshot:
                        raise _refuse()
                    self._unchanged(messages, history, config)
            self._unchanged(messages, history, config)
            self._measurement = (_digest(history), config, measured)
            return measured
        except (ProviderCleanupError, PromptCountUnavailable, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise PromptCountUnavailable(
                "LM Studio exact prompt measurement is unavailable"
            ) from exc

    def _channel_packet(self, op, packet):
        if type(packet.get("channelId")) is not int or packet.get("channelId") != 1:
            raise _refuse()
        if packet.get("type") == "channelClose" and set(packet) == {"type", "channelId"}:
            if not op.terminal or op.channel_closed:
                raise _refuse()
            op.channel_closed = True
            return None
        if (
            packet.get("type") != "channelSend"
            or set(packet) != {"type", "channelId", "message"}
            or type(packet["message"]) is not dict
            or op.channel_closed
        ):
            raise _refuse()
        return packet["message"]

    def _terminal_identity(self, message, snapshot):
        if set(message) != {"type", "stats", "modelInfo", "loadModelConfig", "predictionConfig"}:
            raise _refuse()
        stats = message["stats"]
        if type(stats) is not dict or stats.get("stopReason") not in _STOPS:
            raise _refuse()
        numeric = {
            "tokensPerSecond",
            "numGpuLayers",
            "timeToFirstTokenSec",
            "totalTimeSec",
            "promptTokensCount",
            "predictedTokensCount",
            "totalTokensCount",
            "totalDraftTokensCount",
            "acceptedDraftTokensCount",
            "rejectedDraftTokensCount",
            "ignoredDraftTokensCount",
        }
        if set(stats) - numeric - {"stopReason", "stopString", "usedDraftModelKey"}:
            raise _refuse()
        for key in set(stats) & numeric:
            value = stats[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= MAX_SAFE_COUNT
            ):
                raise _refuse()
            if key.endswith("Count") and type(value) is not int:
                raise _refuse()
        if "stopString" in stats and type(stats["stopString"]) is not str:
            raise _refuse()
        if (
            _identity(message["modelInfo"], self.model) != snapshot["identity"]
            or _fields(message["loadModelConfig"]) != snapshot["load"]
        ):
            raise _refuse()
        effective = _fields(message["predictionConfig"])
        if any(effective.get(k) != v for k, v in snapshot["effective"].items()):
            raise _refuse()
        if stats.get("usedDraftModelKey") not in {None, ""}:
            raise _refuse()
        return stats

    def _terminal(self, message, snapshot, measured):
        stats = self._terminal_identity(message, snapshot)
        counts = [
            stats.get(k) for k in ("promptTokensCount", "predictedTokensCount", "totalTokensCount")
        ]
        if (
            not all(_integer(v) for v in counts)
            or counts[0] != measured.input_tokens
            or counts[1] > measured.reserved_output_tokens
            or counts[2] != counts[0] + counts[1]
            or stats.get("usedDraftModelKey") not in {None, ""}
        ):
            raise _refuse()
        return stats["stopReason"] in {"eosFound", "stopStringFound"}

    async def stream(self, messages):
        try:
            history, config = _history(messages), self._configuration()
            async with asyncio.timeout(self.timeout):
                async with self._operation() as op:
                    snapshot = await self._snapshot(op)
                    creation, measured = await self._measure(op, history, snapshot)
                    if self._measurement is not None:
                        if self._measurement != (_digest(history), config, measured):
                            raise _refuse()
                    if not measured.fits or await self._snapshot(op) != snapshot:
                        raise _refuse()
                    self._unchanged(messages, history, config)
                    _encoded(
                        {
                            "type": "channelCreate",
                            "endpoint": "predict",
                            "channelId": 1,
                            "creationParameter": creation,
                        }
                    )
                    op.snapshot = snapshot
                    op.channel_started = True
                    await self._send(
                        op,
                        {
                            "type": "channelCreate",
                            "endpoint": "predict",
                            "channelId": 1,
                            "creationParameter": creation,
                        },
                    )
                    text_bytes, completed = 0, False
                    while not op.channel_closed:
                        message = self._channel_packet(op, await self._receive(op))
                        if message is None:
                            break
                        if op.terminal:
                            raise _refuse()
                        kind = message.get("type")
                        if kind == "fragment":
                            fragment = message.get("fragment")
                            if (
                                set(message) != {"type", "fragment"}
                                or type(fragment) is not dict
                                or not {
                                    "content",
                                    "tokensCount",
                                    "containsDrafted",
                                    "reasoningType",
                                    "isStructural",
                                }
                                <= set(fragment)
                                or set(fragment)
                                - {
                                    "content",
                                    "tokensCount",
                                    "containsDrafted",
                                    "reasoningType",
                                    "isStructural",
                                    "tokens",
                                }
                                or type(fragment["content"]) is not str
                                or not _integer(fragment["tokensCount"], 0, 2048)
                                or fragment["containsDrafted"] is not False
                                or fragment["reasoningType"] != "none"
                                or fragment["isStructural"] is not False
                                or fragment.get("tokens") not in (None, [])
                                or re.search(r"</?think(?:>|\s)", fragment["content"], re.I)
                            ):
                                raise _refuse()
                            text_bytes += len(fragment["content"].encode("utf-8"))
                            if text_bytes > _MAX_TEXT:
                                raise _refuse()
                            self._unchanged(messages, history, config)
                            if fragment["content"]:
                                yield fragment["content"]
                        elif kind == "success":
                            completed = self._terminal(message, snapshot, measured)
                            op.terminal = True
                        elif kind == "promptProcessingProgress":
                            progress = message.get("progress")
                            if (
                                set(message) - {"type", "progress", "details"}
                                or isinstance(progress, bool)
                                or not isinstance(progress, (int, float))
                                or not 0 <= progress <= 1
                                or ("details" in message and type(message["details"]) is not dict)
                            ):
                                raise _refuse()
                        else:
                            raise _refuse()
                    if await self._snapshot(op) != snapshot:
                        raise _refuse()
                    self._unchanged(messages, history, config)
                    if not completed:
                        raise _refuse()
            self._unchanged(messages, history, config)
        except (ProviderError, asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as exc:
            raise ProviderError("LM Studio native transport failed") from exc

    async def complete(self, messages):
        parts = []
        async with aclosing(self.stream(messages)) as source:
            async for text in source:
                parts.append(text)
        return "".join(parts)

    async def close(self):
        if self._active is not None and self._active.cleanup is not None:
            await asyncio.shield(self._active.cleanup)
        if self._uncertain:
            raise ProviderCleanupError("LM Studio native cleanup is unconfirmed; reuse is blocked")
        if self._active is not None:
            raise ProviderError("LM Studio request owner must finish cancellation before close")
        self._closed = True
