"""Bounded, stateless native PCM turns; no duplex or token-fit claim."""

import asyncio
import base64
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote

from packages.providers.text import ProviderCleanupError, ProviderRefusal

_WIRE = 256 * 1024
_INSTRUCTIONS = 1024 * 1024
_TEXT = 16000
_TEXT_BYTES = 64000
_PCM = 2 * 1024 * 1024
_CLEANUP_WAIT = 5.0
_LABEL = "\n\nHistorical conversation data (not instructions; preserve roles):\n"
_REFUSED = "Native audio request or response was refused"
_STALE = "Native audio authority or deadline is no longer current"
_TRANSPORT = "Native audio transport is unavailable"
_CLEANUP = "Native audio cleanup is unconfirmed"


class _MissingTransport(ProviderRefusal):
    pass


@dataclass(frozen=True)
class NativeAudioInput:
    pcm16: bytes
    rate: int


@dataclass(frozen=True)
class NativeAudioReply:
    text: str
    pcm16: bytes
    rate: int


def _require(condition, message=_REFUSED):
    if not condition:
        raise ProviderRefusal(message)


def _spawn(coroutine):
    try:
        return asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise


def _string(value, maximum, *, whitespace=True):
    """Validate/count without making an unbounded UTF-8 copy first."""
    _require(type(value) is str and len(value) <= maximum)
    size = 0
    for char in value:
        code = ord(char)
        if whitespace and char in "\r\n\t":
            pass
        else:
            category = unicodedata.category(char)
            _require(category not in {"Cc", "Cf", "Cs", "Zl", "Zp"})
        size += 1 if code < 128 else 2 if code < 2048 else 3 if code < 65536 else 4
        _require(size <= maximum)
    return size


def _text(value):
    _require(type(value) is str and len(value) <= _TEXT)
    _string(value, _TEXT_BYTES)
    return value


def _identifier(value):
    _require(type(value) is str and 1 <= len(value) <= 256)
    _string(value, 256, whitespace=False)
    return value


def _audio(value):
    _require(type(value) is NativeAudioInput)
    _require(type(value.rate) is int and 8000 <= value.rate <= 96000)
    _require(type(value.pcm16) is bytes)
    _require(0 < len(value.pcm16) <= value.rate * 2 * 30 and len(value.pcm16) % 2 == 0)
    return value


def _instructions(messages):
    _require(type(messages) is list and 2 <= len(messages) <= 4096)
    # Count the actual JSON string escaping before copying/serializing history.
    total = len(_LABEL.encode()) + 2
    for index, item in enumerate(messages):
        _require(type(item) is dict and len(item) == 2)
        _require(all(type(key) is str for key in item))
        _require(set(item) == {"role", "content"})
        role, content = item["role"], item["content"]
        _require(type(role) is str and role in {"system", "user", "assistant"})
        _require(type(content) is str)
        # The omitted current transcript is checked, but never copied or encoded.
        if index == len(messages) - 1:
            _require(role == "user")
            _string(content, _INSTRUCTIONS)
            continue
        size = _string(content, _INSTRUCTIONS)
        if index == 0:
            _require(role == "system")
            total += size
        else:
            escaped = size + sum(char in '\\"\r\n\t' for char in content)
            total += len('{"role":"","content":""}') + len(role) + escaped
            if index > 1:
                total += 1
        _require(total <= _INSTRUCTIONS)
    history = [{"role": item["role"], "content": item["content"]} for item in messages[1:-1]]
    result = (
        messages[0]["content"]
        + _LABEL
        + json.dumps(history, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )
    _require(len(result.encode("utf-8")) <= _INSTRUCTIONS)
    return result


def _event(raw):
    _require(type(raw) is str and 0 < len(raw) <= _WIRE)

    # Check invalid surrogates, non-finite numbers, and duplicate keys everywhere.
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid(_):
        raise ValueError

    try:
        _require(len(raw.encode("utf-8")) <= _WIRE)
        result = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
        json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProviderRefusal(_REFUSED) from None
    _require(type(result) is dict and type(result.get("type")) is str)
    if result["type"] == "error" or result.get("error") is not None:
        raise ProviderRefusal("Native audio server refused the request")
    return result


def _pcm_format(session, direction):
    audio = session.get("audio")
    _require(type(audio) is dict)
    selected = audio.get(direction)
    _require(type(selected) is dict)
    fmt = selected.get("format")
    _require(type(fmt) is dict and fmt.get("type") == "audio/pcm")
    rate = fmt.get("rate")
    _require(type(rate) is int and 8000 <= rate <= 96000)
    return rate, selected


def _session(event, kind, rate, *, original=None, instructions=None, tokens=None):
    session = event.get("session")
    _require(type(session) is dict and session.get("type") == kind)
    identity = _identifier(session.get("id"))
    input_rate, input_audio = _pcm_format(session, "input")
    _require(input_rate == rate)
    if original is not None:
        _require(identity == original[0])
    output_rate = None
    if kind == "realtime":
        _require("turn_detection" in input_audio and input_audio["turn_detection"] is None)
        output_rate, _ = _pcm_format(session, "output")
        if original is not None:
            _require(output_rate == original[1])
            _require(session.get("instructions") == instructions)
            _require(session.get("output_modalities") == ["text", "audio"])
            _require(type(session.get("max_output_tokens")) is int)
            _require(session["max_output_tokens"] == tokens)
            _require(session.get("tools") == [] and session.get("tool_choice") == "none")
    elif original is not None and "max_output_tokens" in session:
        # Canonical transcription acknowledgements need not expose this setting.
        _require(
            type(session["max_output_tokens"]) is int and session["max_output_tokens"] == tokens
        )
    return identity, output_rate


def _input_lifecycle(event, item_id):
    if event["type"] not in {"conversation.item.added", "conversation.item.done"}:
        return False
    item = event.get("item")
    _require(type(item) is dict and item.get("id") == item_id)
    _require(item.get("type") == "message" and item.get("role") == "user")
    content = item.get("content")
    _require(type(content) is list and len(content) == 1)
    _require(type(content[0]) is dict and content[0].get("type") == "input_audio")
    _require(event.get("previous_item_id") is None and "response_id" not in event)
    _require("item_id" not in event or event["item_id"] == item_id)
    return True


def _final_message(item, identity, text):
    _require(type(item) is dict and item.get("id") == identity)
    _require(item.get("type") == "message" and item.get("role") == "assistant")
    _require(item.get("status") == "completed")
    _require(not item.get("tool_calls") and not item.get("function_call"))
    parts = item.get("content")
    _require(type(parts) is list and len(parts) == 2)
    found = set()
    for part in parts:
        _require(type(part) is dict)
        kind = part.get("type")
        _require(
            type(kind) is str and kind in {"output_text", "output_audio"} and kind not in found
        )
        found.add(kind)
        if kind == "output_text":
            _require(_text(part.get("text")) == text)
        else:
            _require(part.get("transcript", "") == "")


def _default_transport_ready() -> bool:
    try:
        import websockets
        import websockets.asyncio.client as client
    except Exception:
        return False
    version = getattr(websockets, "__version__", None)
    connect = getattr(client, "connect", None)
    if type(version) is not str or not callable(connect):
        return False
    match = re.match(r"^([0-9]+)\.", version.strip())
    if match is None or int(match[1]) < 14:
        return False
    return True


async def _default_connect(url, **options):
    if not _default_transport_ready():
        raise _MissingTransport
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        raise _MissingTransport from None
    return await connect(url, **options)


class _Operation:
    """Original acquisition, transport futures and retirement survive observers."""

    def __init__(self, owner, audio, instructions, current):
        self.owner, self.instructions, self.current = owner, instructions, current
        # Copy fields, not the caller's frozen-dataclass object: its owner can
        # still replace fields using object.__setattr__ during a foreign hook.
        self.audio = NativeAudioInput(audio.pcm16, audio.rate)
        self.observer = asyncio.current_task()
        self.deadline = asyncio.get_running_loop().time() + owner._timeout
        self.connector = owner._connect
        self.tokens = owner._tokens
        self.url = owner._url + ("&intent=transcription" if instructions is None else "")
        self.options = {
            "proxy": None,
            "compression": None,
            "max_queue": 1,
            "max_size": _WIRE,
            "open_timeout": owner._timeout,
            "close_timeout": 5,
            "additional_headers": {"Authorization": "Bearer " + owner._key} if owner._key else None,
        }
        self.fenced = False
        self.started = False
        self.acquisition = self.connection = self.send_method = self.recv_method = (
            self.close_method
        ) = None
        self.close_future = None
        self.io = set()
        self.response_id = None
        self.completed = False
        self.events = 0
        self.task = self.retirement = None

    def check(self):
        tasks = (asyncio.current_task(), self.observer)
        if any(task is not None and task.cancelling() for task in tasks):
            raise asyncio.CancelledError
        _require(not self.fenced and not self.owner._sealed and self.owner._active is self, _STALE)
        _require(asyncio.get_running_loop().time() < self.deadline, _STALE)

    async def authorize(self):
        self.check()
        try:
            allowed = await self.current()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProviderRefusal(_STALE) from None
        self.check()
        _require(allowed is True, _STALE)

    def fence(self):
        if not self.fenced:
            self.fenced = True
            if self.started and self.task is not None and not self.task.done():
                if self.task is not asyncio.current_task():
                    self.task.cancel()

    def track(self, awaitable):
        task = asyncio.ensure_future(awaitable)
        self.io.add(task)

        def observed(original):
            if not original.cancelled() and isinstance(original.exception(), ProviderCleanupError):
                self.owner._cleanup_failed = True
            # A positively terminal IO task no longer owns pending transport.
            # Do not retain thousands of completed raw websocket frame results.
            self.io.discard(original)

        task.add_done_callback(observed)
        return task

    async def acquire(self):
        # The acquisition task can first run after a fence; authorize inside the
        # original task immediately before invoking its captured connector.
        await self.authorize()
        # Assign the returned original and capture close BEFORE other validation.
        connection = await self.connector(self.url, **self.options)
        self.connection = connection
        self.close_method = getattr(connection, "close", None)
        self.send_method = getattr(connection, "send", None)
        self.recv_method = getattr(connection, "recv", None)
        _require(
            all(
                callable(method)
                for method in (self.close_method, self.send_method, self.recv_method)
            ),
            _TRANSPORT,
        )

    async def send_wire(self, wire):
        await self.authorize()
        return await self.send_method(wire)

    async def send(self, payload):
        self.check()
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        await asyncio.shield(self.track(self.send_wire(wire)))
        self.check()

    async def receive(self):
        raw = await asyncio.shield(self.track(self.recv_method()))
        await self.authorize()
        self.events += 1
        _require(self.events <= 8192)
        return _event(raw)

    async def cleanup(self):
        if self.acquisition is not None:
            await asyncio.gather(self.acquisition, return_exceptions=True)
            if self.acquisition.cancelled() or isinstance(
                self.acquisition.exception(), ProviderCleanupError
            ):
                raise ProviderCleanupError(_CLEANUP)
        if self.connection is None:
            return
        if not callable(self.close_method):
            raise ProviderCleanupError(_CLEANUP)
        if self.response_id is not None and not self.completed and callable(self.send_method):
            try:
                cancel = self.track(
                    self.send_method(
                        json.dumps(
                            {
                                "type": "response.cancel",
                                "response_id": self.response_id,
                            },
                            separators=(",", ":"),
                        )
                    )
                )
                # Best effort cancellation is separate from actual socket retirement.
                await asyncio.wait({cancel}, timeout=1.0)
            except Exception:
                pass
        try:
            self.close_future = asyncio.ensure_future(self.close_method())
            await asyncio.shield(self.close_future)
            # Closing the socket releases any original suspended send/recv; join
            # those originals too, without cancellation being mistaken for close.
            if self.io:
                await asyncio.gather(*self.io, return_exceptions=True)
        except BaseException:
            raise ProviderCleanupError(_CLEANUP) from None

    async def retire(self):
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
        try:
            await self.cleanup()
        except BaseException:
            self.owner._cleanup_failed = True
            raise ProviderCleanupError(_CLEANUP) from None

    async def protocol(self):
        self.started = True
        async with asyncio.timeout_at(self.deadline):
            await self.authorize()
            self.acquisition = _spawn(self.acquire())
            await asyncio.shield(self.acquisition)
            created = await self.receive()
            _require(created["type"] == "session.created")
            kind = "transcription" if self.instructions is None else "realtime"
            # Some realtime endpoints apply transcription intent only after the
            # explicit session.update. Validate the initial realtime envelope;
            # the strict transcription acknowledgement must precede all PCM.
            created_kind = kind
            if (
                self.instructions is None
                and type(created.get("session")) is dict
                and created["session"].get("type") == "realtime"
            ):
                created_kind = "realtime"
            original = _session(created, created_kind, self.audio.rate)
            selected = {
                "type": kind,
                "max_output_tokens": self.tokens,
                "audio": {"input": {"format": {"type": "audio/pcm", "rate": self.audio.rate}}},
            }
            if self.instructions is not None:
                selected.update(
                    instructions=self.instructions,
                    output_modalities=["text", "audio"],
                    tools=[],
                    tool_choice="none",
                )
                selected["audio"]["input"]["turn_detection"] = None
                selected["audio"]["output"] = {"format": {"type": "audio/pcm", "rate": original[1]}}
            await self.send({"type": "session.update", "session": selected})
            updated = await self.receive()
            _require(updated["type"] == "session.updated")
            _session(
                updated,
                kind,
                self.audio.rate,
                original=original,
                instructions=self.instructions,
                tokens=self.tokens,
            )
            for offset in range(0, len(self.audio.pcm16), 32768):
                await self.send(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(self.audio.pcm16[offset : offset + 32768]).decode(
                            "ascii"
                        ),
                    }
                )
            commit = {"type": "input_audio_buffer.commit"}
            if self.instructions is None:
                commit["final"] = True
            await self.send(commit)
            committed = await self.receive()
            _require(committed["type"] == "input_audio_buffer.committed")
            input_id = _identifier(committed.get("item_id"))
            _require(committed.get("previous_item_id") is None and "response_id" not in committed)
            if self.instructions is None:
                return await self.transcription(input_id)
            await self.send({"type": "response.create"})
            return await self.conversation(input_id, original[1])

    async def transcription(self, input_id):
        partial = ""
        partial_bytes = 0
        has_delta = False
        while True:
            event = await self.receive()
            if _input_lifecycle(event, input_id):
                continue
            kind = event["type"]
            if kind == "rate_limits.updated":
                _require("response_id" not in event and "item_id" not in event)
                continue
            _require(event.get("item_id") == input_id and "response_id" not in event)
            if kind == "conversation.item.input_audio_transcription.delta":
                delta = _text(event.get("delta"))
                size = _string(delta, _TEXT_BYTES)
                _require(len(partial) + len(delta) <= _TEXT and partial_bytes + size <= _TEXT_BYTES)
                partial += delta
                partial_bytes += size
                has_delta = True
                continue
            _require(kind == "conversation.item.input_audio_transcription.completed")
            text = _text(event.get("transcript"))
            _require(bool(text))
            _require(not has_delta or text == partial)
            return text

    async def conversation(self, input_id, rate):
        item_id = None
        parts, finished = {}, set()
        text, pcm = "", bytearray()
        text_bytes = 0
        while True:
            event = await self.receive()
            kind = event["type"]
            if _input_lifecycle(event, input_id):
                continue
            if kind == "rate_limits.updated":
                _require("response_id" not in event and "item_id" not in event)
                continue
            if self.response_id is None:
                _require(kind == "response.created")
                response = event.get("response")
                _require(type(response) is dict)
                self.response_id = _identifier(response.get("id"))
                _require("response_id" not in event or event["response_id"] == self.response_id)
                _require("item_id" not in event)
                _require(response.get("output", []) == [])
                _require(response.get("status", "in_progress") == "in_progress")
                continue
            if kind == "response.done":
                response = event.get("response")
                _require(type(response) is dict and response.get("id") == self.response_id)
                _require("response_id" not in event or event["response_id"] == self.response_id)
                _require("item_id" not in event or event["item_id"] == item_id)
                _require(response.get("status") == "completed")
                _require(not response.get("tool_calls") and not response.get("function_call"))
                output = response.get("output")
                _require(type(output) is list and len(output) == 1 and item_id is not None)
                _require(bool(text) and bool(pcm))
                _final_message(output[0], item_id, text)
                self.completed = True
                return NativeAudioReply(text, bytes(pcm), rate)
            _require(event.get("response_id") == self.response_id)
            if kind == "response.output_item.added":
                _require(
                    item_id is None
                    and type(event.get("output_index")) is int
                    and event["output_index"] == 0
                )
                item = event.get("item")
                _require(
                    type(item) is dict
                    and item.get("type") == "message"
                    and item.get("role") == "assistant"
                )
                item_id = _identifier(item.get("id"))
                _require(item_id != input_id and item.get("content", []) == [])
                _require("item_id" not in event or event["item_id"] == item_id)
                continue
            _require(item_id is not None)
            if kind == "response.output_item.done":
                _require(
                    type(event.get("output_index")) is int
                    and event["output_index"] == 0
                    and "item" not in finished
                )
                _require("item_id" not in event or event["item_id"] == item_id)
                _final_message(event.get("item"), item_id, text)
                finished.add("item")
                continue
            _require(
                event.get("item_id") == item_id
                and type(event.get("output_index")) is int
                and event["output_index"] == 0
            )
            index = event.get("content_index")
            _require(type(index) is int and index in {0, 1})
            if kind == "response.content_part.added":
                part = event.get("part")
                _require(type(part) is dict)
                part_type = part.get("type")
                _require(
                    part_type in {"output_text", "output_audio"}
                    and index not in parts
                    and part_type not in parts.values()
                )
                _require(part.get("text", "") == "" and part.get("transcript", "") == "")
                parts[index] = part_type
                continue
            _require(index in parts and "item" not in finished)
            part_type = parts[index]
            if kind == "response.content_part.done":
                _require(("part", index) not in finished)
                part = event.get("part")
                _require(type(part) is dict and part.get("type") == part_type)
                if part_type == "output_text":
                    _require(_text(part.get("text")) == text)
                else:
                    _require(part.get("transcript", "") == "" and bool(pcm))
                finished.add(("part", index))
                continue
            _require(("part", index) not in finished and ("stream", index) not in finished)
            if kind == "response.output_text.delta":
                _require(part_type == "output_text")
                delta = _text(event.get("delta"))
                size = _string(delta, _TEXT_BYTES)
                _require(len(text) + len(delta) <= _TEXT and text_bytes + size <= _TEXT_BYTES)
                text += delta
                text_bytes += size
            elif kind == "response.output_audio.delta":
                _require(part_type == "output_audio")
                encoded = event.get("delta")
                _require(type(encoded) is str and 0 < len(encoded) <= _WIRE)
                try:
                    delta = base64.b64decode(encoded, validate=True)
                except (ValueError, UnicodeError):
                    raise ProviderRefusal(_REFUSED) from None
                _require(len(delta) > 0 and len(delta) % 2 == 0)
                _require(len(pcm) + len(delta) <= min(_PCM, rate * 2 * 60))
                pcm.extend(delta)
            elif kind == "response.output_text.done":
                _require(part_type == "output_text" and _text(event.get("text")) == text)
                finished.add(("stream", index))
            elif kind == "response.output_audio.done":
                _require(part_type == "output_audio" and bool(pcm))
                finished.add(("stream", index))
            else:
                raise ProviderRefusal(_REFUSED)


class RealtimeAudioProvider:
    def __init__(
        self, base_url, model, *, api_key="", max_output_tokens=512, timeout=90.0, connect=None
    ):
        try:
            _require(type(base_url) is str and len(base_url) <= 64)
            match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})(?:/v1)?/?", base_url)
            _require(match is not None and 1 <= int(match[1]) <= 65535)
            _require(type(model) is str and model == model.strip() and bool(model))
            _string(model, 256, whitespace=False)
            _require(type(api_key) is str and len(api_key) <= 16384)
            _require(all(33 <= ord(char) <= 126 for char in api_key))
            _require(type(max_output_tokens) is int and 16 <= max_output_tokens <= 4096)
            _require(type(timeout) in {int, float} and 0 < timeout <= 90 and math.isfinite(timeout))
            _require(connect is None or callable(connect))
        except ProviderRefusal:
            raise ValueError("Native audio configuration is invalid") from None
        self._url = (
            "ws://127.0.0.1:" + str(int(match[1])) + "/v1/realtime?model=" + quote(model, safe="")
        )
        self._key, self._tokens, self._timeout = api_key, max_output_tokens, float(timeout)
        self._connect = _default_connect if connect is None else connect
        self._active = None
        self._sealed = self._cleanup_failed = False
        self._close_task = None

    def binding_token(self):
        """Pin the actual transport selection without exposing its credential."""
        return (
            self._url,
            self._tokens,
            self._timeout,
            hashlib.sha256(self._key.encode("ascii")).hexdigest(),
            id(self._connect),
        )

    def transport_available(self) -> bool:
        """Report local transport dependency readiness without probing the network."""
        if self._sealed or self._cleanup_failed:
            return False
        if self._connect is _default_connect:
            return _default_transport_ready()
        return callable(self._connect)

    async def _settle(self, operation):
        deadline = asyncio.get_running_loop().time() + _CLEANUP_WAIT
        interrupted = False
        done = set()
        while not operation.retirement.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                done, _ = await asyncio.wait({operation.retirement}, timeout=remaining)
                break
            except asyncio.CancelledError:
                interrupted = True
                operation.fence()
        if operation.retirement.done():
            done = {operation.retirement}
        if not done:
            self._cleanup_failed = True
            raise ProviderCleanupError(_CLEANUP)
        try:
            operation.retirement.result()
        except BaseException:
            self._cleanup_failed = True
            raise ProviderCleanupError(_CLEANUP) from None
        if self._cleanup_failed:
            raise ProviderCleanupError(_CLEANUP)
        if interrupted:
            raise asyncio.CancelledError

    async def _invoke(self, audio, instructions, current):
        if self._cleanup_failed:
            raise ProviderCleanupError(_CLEANUP)
        _require(not self._sealed, "Native audio provider is closed")
        _require(self._active is None, "Native audio provider is busy")
        _require(callable(current), _STALE)
        operation = _Operation(self, audio, instructions, current)
        self._active = operation
        try:
            operation.task = _spawn(operation.protocol())
            operation.task.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
            operation.retirement = _spawn(operation.retire())
        except BaseException:
            operation.fence()
            self._cleanup_failed = True
            raise ProviderCleanupError(_CLEANUP) from None
        # Retained futures always consume their terminal exception, even when an
        # observer has already received a bounded unconfirmed-cleanup refusal.
        operation.retirement.add_done_callback(
            lambda task: None if task.cancelled() else task.exception()
        )
        error = None
        result = None
        try:
            try:
                result = await asyncio.shield(operation.task)
            except BaseException as exc:
                error = exc
                operation.fence()
            await self._settle(operation)
            if error is not None:
                if isinstance(error, asyncio.CancelledError):
                    raise asyncio.CancelledError
                if isinstance(error, ProviderCleanupError):
                    self._cleanup_failed = True
                    raise ProviderCleanupError(_CLEANUP)
                if isinstance(error, _MissingTransport):
                    raise ProviderRefusal(
                        "Native audio requires the optional websockets transport"
                    ) from None
                if isinstance(error, ProviderRefusal):
                    # Injected transports can raise provider errors too; their
                    # arbitrary diagnostics never cross this public boundary.
                    raise ProviderRefusal(_REFUSED) from None
                raise ProviderRefusal(_TRANSPORT) from None
            # Socket close and every original transport dependency are confirmed
            # before the final authority/deadline check and atomic reply export.
            async with asyncio.timeout_at(operation.deadline):
                await operation.authorize()
            return result
        except TimeoutError:
            raise ProviderRefusal(_STALE) from None
        finally:
            if operation.retirement.done() and not self._cleanup_failed:
                if self._active is operation:
                    self._active = None

    async def transcribe(self, audio: NativeAudioInput, *, current) -> str:
        return await self._invoke(_audio(audio), None, current)

    async def respond(
        self, audio: NativeAudioInput, messages: list[dict[str, str]], *, current
    ) -> NativeAudioReply:
        audio = _audio(audio)
        return await self._invoke(audio, _instructions(messages), current)

    async def close(self):
        self._sealed = True
        operation = self._active
        if operation is not None:
            operation.fence()
        if self._close_task is None:

            async def finish():
                if operation is not None:
                    if operation.retirement is None:
                        operation.retirement = _spawn(operation.retire())
                    await asyncio.shield(operation.retirement)
                if self._cleanup_failed:
                    raise ProviderCleanupError(_CLEANUP)

            try:
                self._close_task = _spawn(finish())
            except BaseException:
                self._cleanup_failed = True
                raise ProviderCleanupError(_CLEANUP) from None
            self._close_task.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
        if operation is not None and asyncio.current_task() in {
            operation.task,
            operation.acquisition,
            operation.close_future,
            operation.retirement,
            *operation.io,
        }:
            # A callback cannot join its own work. The sealed retained closer
            # continues after this refusal unwinds that original work.
            raise ProviderCleanupError(_CLEANUP)
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            raise
        except BaseException:
            self._cleanup_failed = True
            raise ProviderCleanupError(_CLEANUP) from None
