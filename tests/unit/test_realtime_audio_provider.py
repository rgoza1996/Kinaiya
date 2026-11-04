"""Independent #451 native-audio transport oracles; synthetic WebSockets only.

Authored from frozen contract 92f274dd before reading implementation. These
checks establish protocol/ownership behavior, not model accuracy or duplex audio.
Root executes them; test author performs static checks only.
"""

import asyncio
import base64
import copy
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from packages.providers.realtime_audio import (
    NativeAudioInput,
    NativeAudioReply,
    RealtimeAudioProvider,
)
from packages.providers.text import ProviderCleanupError, ProviderRefusal

BASE = "http://127.0.0.1:19451/v1"
MODEL = "synthetic-native-audio"
SECRET = "public-synthetic-local-key-451"
MARKER = "UNTRUSTED-SYNTHETIC-SERVER-MARKER-451"
PCM = b"\x00\x00\x01\x00\xff\x7f\x00\x80" * 400
OUT = b"\x01\x00\x02\x00\x03\x00" * 80
TEXT = "Give me some time on it."
CURRENT_TEXT = "CURRENT-SIDECAR-TRANSCRIPT-MUST-NOT-REPLACE-AUDIO"
RID, IID, UID, SID = "response-one", "assistant-one", "user-one", "session-one"


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def messages():
    return [
        {"role": "system", "content": "Public synthetic character personality."},
        {"role": "user", "content": "Earlier public question."},
        {"role": "assistant", "content": "Earlier generated reply; delivery unknown."},
        {"role": "user", "content": CURRENT_TEXT},
    ]


def session(kind="realtime", *, input_rate=16000, output_rate=24000):
    value = {
        "id": SID,
        "object": "realtime.session",
        "type": kind,
        "model": MODEL,
        "instructions": "",
        "output_modalities": ["text", "audio"],
        "max_output_tokens": 512,
        "tools": [],
        "tool_choice": "none",
        "audio": {
            "input": {"format": {"type": "audio/pcm", "rate": input_rate}, "turn_detection": None}
        },
    }
    if kind == "transcription":
        value["object"] = "realtime.transcription_session"
        value["audio"]["input"]["transcription"] = {"model": MODEL, "language": None}
    else:
        value["audio"]["output"] = {"format": {"type": "audio/pcm", "rate": output_rate}}
    return value


def completed_output(*, text=TEXT, pcm=OUT, rate=24000):
    del rate  # Rate comes from the session, never an untrusted audio delta.
    text_part = {"type": "output_text", "text": text}
    audio_part = {"type": "output_audio", "transcript": ""}
    item = {
        "id": IID,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [text_part, audio_part],
    }
    common = {"response_id": RID, "item_id": IID, "output_index": 0}
    return [
        {
            "type": "response.created",
            "response": {
                "id": RID,
                "status": "in_progress",
                "output_modalities": ["text", "audio"],
                "output": [],
            },
        },
        {
            "type": "response.output_item.added",
            "response_id": RID,
            "output_index": 0,
            "item": {**item, "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added",
            **common,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        },
        {"type": "response.output_text.delta", **common, "content_index": 0, "delta": text},
        {"type": "response.output_text.done", **common, "content_index": 0, "text": text},
        {"type": "response.content_part.done", **common, "content_index": 0, "part": text_part},
        {"type": "response.content_part.added", **common, "content_index": 1, "part": audio_part},
        {
            "type": "response.output_audio.delta",
            **common,
            "content_index": 1,
            "delta": base64.b64encode(pcm).decode(),
        },
        {"type": "response.output_audio.done", **common, "content_index": 1},
        {"type": "response.content_part.done", **common, "content_index": 1, "part": audio_part},
        {"type": "response.output_item.done", "response_id": RID, "output_index": 0, "item": item},
        {
            "type": "response.done",
            "response": {
                "id": RID,
                "status": "completed",
                "output": [item],
                "usage": {"output_tokens": 20},
            },
        },
    ]


class Current:
    def __init__(self, allowed=True):
        self.allowed, self.calls = allowed, 0

    async def __call__(self):
        self.calls += 1
        return self.allowed


class Socket:
    """Actual awaitable send/receive/close boundaries with owned settlement gates."""

    def __init__(
        self,
        *,
        output=None,
        created=None,
        held_type=None,
        held_close=False,
        close_failure=False,
        transcript="Public synthetic spoken task.",
        transcription=None,
    ):
        self.output = completed_output() if output is None else output
        self.created = created
        self.transcript = transcript
        self.transcription = transcription
        self.sent, self.received = [], []
        self.incoming = asyncio.Queue()
        self.held_type, self.on_recv, self.on_send = held_type, None, None
        self.receive_held, self.receive_release = asyncio.Event(), asyncio.Event()
        self.close_started, self.close_release, self.close_exited = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.close_failure, self.close_calls, self.close_cancelled = close_failure, 0, False
        if not held_close:
            self.close_release.set()
        self.mode = "realtime"

    def push(self, event):
        self.incoming.put_nowait(encode(event) if type(event) is dict else event)

    def start(self, uri):
        self.mode = (
            "transcription"
            if parse_qs(urlsplit(uri).query).get("intent") == ["transcription"]
            else "realtime"
        )
        self.push(
            {"type": "session.created", "session": session(self.mode)}
            if self.created is None
            else self.created
        )

    async def send(self, raw):
        assert type(raw) is str
        event = json.loads(raw)
        self.sent.append(event)
        if self.on_send is not None:
            await self.on_send(event)
        kind = event["type"]
        if kind == "session.update":
            updated = session(self.mode)
            updated.update(event["session"])
            self.push({"type": "session.updated", "session": updated})
        elif kind == "input_audio_buffer.commit":
            self.push(
                {"type": "input_audio_buffer.committed", "item_id": UID, "previous_item_id": None}
            )
            item = {
                "id": UID,
                "object": "realtime.item",
                "type": "message",
                "role": "user",
                "status": "completed",
                "content": [{"type": "input_audio"}],
            }
            self.push({"type": "conversation.item.added", "previous_item_id": None, "item": item})
            self.push({"type": "conversation.item.done", "previous_item_id": None, "item": item})
            if self.mode == "transcription":
                if self.transcription is not None:
                    for item in self.transcription:
                        self.push(item)
                else:
                    self.push(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "item_id": UID,
                            "content_index": 0,
                            "transcript": self.transcript,
                        }
                    )
        elif kind == "response.create":
            for item in self.output:
                self.push(item)

    async def recv(self):
        raw = await self.incoming.get()
        if isinstance(raw, BaseException):
            raise raw
        self.received.append(raw)
        if type(raw) is str:
            try:
                kind = json.loads(raw).get("type")
            except (ValueError, AttributeError):
                kind = None
        else:
            kind = None
        if self.held_type is not None and kind == self.held_type:
            self.receive_held.set()
            await self.receive_release.wait()
        if self.on_recv is not None:
            await self.on_recv(raw)
        return raw

    async def close(self):
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.close_release.wait()
            if self.close_failure:
                raise OSError(MARKER)
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        finally:
            self.close_exited.set()


class Connector:
    def __init__(self, *sockets):
        self.sockets, self.calls = list(sockets), []
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()

    async def __call__(self, uri, **kwargs):
        self.calls.append((uri, kwargs))
        self.entered.set()
        await self.release.wait()
        if not self.sockets:
            raise AssertionError("Unexpected connection/replay")
        socket = self.sockets.pop(0)
        socket.start(uri)
        return socket


def provider(connector, **changes):
    return RealtimeAudioProvider(BASE, MODEL, connect=connector, **changes)


async def wait(event):
    await asyncio.wait_for(event.wait(), 2)


async def ticks(count=4):
    for _ in range(count):
        await asyncio.sleep(0)


def audio():
    return NativeAudioInput(PCM, 16000)


def wire_pcm(socket):
    return b"".join(
        base64.b64decode(event["audio"], validate=True)
        for event in socket.sent
        if event["type"] == "input_audio_buffer.append"
    )


def assert_redacted(exc):
    assert MARKER not in str(exc.value)
    assert SECRET not in str(exc.value)
    assert CURRENT_TEXT not in str(exc.value)


@pytest.mark.asyncio
async def test_native_reply_uses_original_pcm_text_audio_and_explicit_local_transport():
    socket = Socket()
    connector = Connector(socket)
    target = provider(connector, api_key=SECRET)
    try:
        reply = await target.respond(audio(), messages(), current=Current())
        assert type(reply) is NativeAudioReply
        assert (reply.text, reply.pcm16, reply.rate) == (TEXT, OUT, 24000)
        assert wire_pcm(socket) == PCM
        uri, options = connector.calls[0]
        parsed = urlsplit(uri)
        assert (parsed.scheme, parsed.hostname, parsed.port, parsed.path) == (
            "ws",
            "127.0.0.1",
            19451,
            "/v1/realtime",
        )
        assert parse_qs(parsed.query) == {"model": [MODEL]}
        assert options["proxy"] is None and options["compression"] is None
        assert options["max_queue"] == 1 and options["max_size"] == 262144
        assert 0 < options["open_timeout"] <= 90 and options["close_timeout"] == 5
        assert dict(options["additional_headers"]) == {"Authorization": f"Bearer {SECRET}"}
        updates = [event["session"] for event in socket.sent if event["type"] == "session.update"]
        assert len(updates) == 1
        update = updates[0]
        assert update["type"] == "realtime" and update["output_modalities"] == ["text", "audio"]
        assert update["tools"] == [] and update["tool_choice"] == "none"
        assert update["max_output_tokens"] == 512
        assert CURRENT_TEXT not in update["instructions"]
        assert "Earlier public question." in update["instructions"]
        assert "Earlier generated reply; delivery unknown." in update["instructions"]
        assert '"assistant"' in update["instructions"]
        kinds = [event["type"] for event in socket.sent]
        assert kinds.count("response.create") == kinds.count("input_audio_buffer.commit") == 1
        assert kinds.index("session.update") < kinds.index("input_audio_buffer.append")
        assert kinds.index("input_audio_buffer.commit") < kinds.index("response.create")
        assert "conversation.item.create" not in kinds
        assert socket.close_calls == 1 and socket.close_exited.is_set()
    finally:
        await target.close()
    assert socket.close_calls == 1


@pytest.mark.asyncio
async def test_transcription_uses_same_original_audio_separate_intent_and_no_native_reply():
    socket = Socket()
    connector = Connector(socket)
    target = provider(connector)
    try:
        result = await target.transcribe(audio(), current=Current())
        assert result == socket.transcript
        assert wire_pcm(socket) == PCM
        uri, options = connector.calls[0]
        assert parse_qs(urlsplit(uri).query) == {"model": [MODEL], "intent": ["transcription"]}
        assert not dict(options.get("additional_headers") or {})
        kinds = [event["type"] for event in socket.sent]
        assert kinds.count("input_audio_buffer.commit") == 1
        assert "response.create" not in kinds
        assert socket.close_calls == 1
    finally:
        await target.close()


class TranscriptionTransitionSocket(Socket):
    """Documented initial realtime metadata, followed by a minimal ASR ack.

    No audio/model result is copied from the real metadata-only observation.
    Unbounded initial defaults are replaced before the first PCM export.
    """

    def __init__(self, *, changed_ack=None, hold_ack=False):
        metadata = session("realtime")
        metadata.update(output_modalities=None, max_output_tokens="inf", tool_choice="auto")
        super().__init__(
            created={"type": "session.created", "session": metadata},
            held_type="session.updated" if hold_ack else None,
        )
        self.changed_ack = changed_ack

    def push(self, event):
        if type(event) is dict and event.get("type") == "session.updated":
            acknowledged = {
                "id": SID,
                "object": "realtime.transcription_session",
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 16000},
                        "turn_detection": None,
                        "transcription": {"model": MODEL, "language": None},
                    }
                },
            }
            if self.changed_ack == "type":
                acknowledged["type"] = "realtime"
            elif self.changed_ack == "session-id":
                acknowledged["id"] = "foreign-session"
            elif self.changed_ack == "rate":
                acknowledged["audio"]["input"]["format"]["rate"] = 8000
            event = {"type": "session.updated", "session": acknowledged}
        super().push(event)


@pytest.mark.asyncio
async def test_initial_realtime_session_explicitly_switches_to_transcription_before_pcm():
    socket = TranscriptionTransitionSocket(hold_ack=True)
    connector = Connector(socket)
    target = provider(connector)
    original = asyncio.create_task(target.transcribe(audio(), current=Current()))
    try:
        await wait(socket.receive_held)
        assert not original.done()
        assert wire_pcm(socket) == b""
        assert [event["type"] for event in socket.sent] == ["session.update"]
        assert socket.sent[0]["session"]["type"] == "transcription"
        assert parse_qs(urlsplit(connector.calls[0][0]).query) == {
            "model": [MODEL],
            "intent": ["transcription"],
        }
        socket.receive_release.set()
        result = await asyncio.wait_for(original, 2)
        assert result == socket.transcript
        assert wire_pcm(socket) == PCM
        kinds = [event["type"] for event in socket.sent]
        assert kinds.count("input_audio_buffer.commit") == 1
        assert "response.create" not in kinds
        assert len(connector.calls) == socket.close_calls == 1
        assert socket.close_exited.is_set()
    finally:
        socket.receive_release.set()
        if not original.done():
            original.cancel()
        await asyncio.gather(original, return_exceptions=True)
        await target.close()


@pytest.mark.parametrize("changed_ack", ["type", "session-id", "rate"])
@pytest.mark.asyncio
async def test_realtime_to_transcription_requires_exact_ack_before_any_pcm(changed_ack):
    socket = TranscriptionTransitionSocket(changed_ack=changed_ack)
    connector = Connector(socket)
    target = provider(connector)
    try:
        with pytest.raises(ProviderRefusal) as exc:
            await target.transcribe(audio(), current=Current())
        assert_redacted(exc)
        assert [event["type"] for event in socket.sent] == ["session.update"]
        assert socket.sent[0]["session"]["type"] == "transcription"
        assert wire_pcm(socket) == b""
        assert len(connector.calls) == socket.close_calls == 1
        assert socket.close_exited.is_set()
    finally:
        await target.close()


@pytest.mark.asyncio
async def test_history_over_eight_turns_is_complete_ordered_and_not_silently_truncated():
    history = [{"role": "system", "content": "Synthetic personality."}]
    for index in range(24):
        history.append(
            {
                "role": "assistant" if index % 2 else "user",
                "content": f"HISTORY-{index:02d}-" + "x" * 2100,
            }
        )
    history.append({"role": "user", "content": CURRENT_TEXT})
    socket = Socket()
    target = provider(Connector(socket))
    try:
        await target.respond(audio(), history, current=Current())
        instructions = next(
            event["session"]["instructions"]
            for event in socket.sent
            if event["type"] == "session.update"
        )
        positions = [instructions.index(f"HISTORY-{index:02d}-") for index in range(24)]
        assert positions == sorted(positions)
        assert instructions.count("x" * 2100) == 24
        assert CURRENT_TEXT not in instructions
    finally:
        await target.close()


@pytest.mark.parametrize(
    "base",
    [
        "http://localhost:19451/v1",
        "http://[::1]:19451/v1",
        "https://127.0.0.1:19451/v1",
        "ws://127.0.0.1:19451/v1/realtime",
        "http://127.1:19451/v1",
        "http://127.0.0.1/v1",
        "http://127.0.0.1:0/v1",
        "http://127.0.0.1:65536/v1",
        "http://user@127.0.0.1:19451/v1",
        "http://@127.0.0.1:19451/v1",
        "http://127.0.0.1:19451/v1?x=1",
        "http://127.0.0.1:19451/v1#fragment",
        "http://127.0.0.1:19451/v1?",
        "http://127.0.0.1:19451/v1#",
        "http://127.0.0.1:19451/v1\\other",
        "http://127.0.0.1:19451/v1\n",
        "http://127.0.0.1:19451/other",
    ],
)
def test_constructor_refuses_ambiguous_or_remote_endpoints_without_connecting(base):
    connector = Connector()
    with pytest.raises(ValueError):
        RealtimeAudioProvider(base, MODEL, connect=connector)
    assert connector.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_output_tokens", 15),
        ("max_output_tokens", 4097),
        ("max_output_tokens", True),
        ("max_output_tokens", 32.0),
        ("timeout", 0),
        ("timeout", 91),
        ("timeout", True),
        ("timeout", float("inf")),
        ("timeout", float("nan")),
        ("api_key", "bad key"),
        ("api_key", "bad\nkey"),
        ("api_key", "é"),
        ("api_key", "x" * 16385),
    ],
)
def test_constructor_configuration_has_strict_finite_bounds(field, value):
    connector = Connector()
    with pytest.raises(ValueError):
        provider(connector, **{field: value})
    assert connector.calls == []


@pytest.mark.parametrize(
    "model", ["", " synthetic", "synthetic\nmodel", "a\u0085b", "é" * 129, 123]
)
def test_model_declaration_is_bounded_exact_text(model):
    connector = Connector()
    with pytest.raises(ValueError):
        RealtimeAudioProvider(BASE, model, connect=connector)
    assert connector.calls == []


@pytest.mark.parametrize(
    ("pcm", "rate"),
    [
        (b"", 16000),
        (b"\x00", 16000),
        (bytearray(PCM), 16000),
        (PCM, True),
        (PCM, 7999),
        (PCM, 96001),
        (PCM, 16000.0),
        (b"\x00\x00" * (16000 * 30 + 1), 16000),
    ],
)
@pytest.mark.asyncio
async def test_invalid_pcm_is_refused_before_connect_or_transcript_export(pcm, rate):
    connector = Connector()
    target = provider(connector)
    try:
        with pytest.raises((ProviderRefusal, ValueError)):
            await target.respond(NativeAudioInput(pcm, rate), messages(), current=Current())
        assert connector.calls == []
    finally:
        await target.close()


@pytest.mark.parametrize(
    "bad",
    [
        [],
        [{"role": "system", "content": "Only one message."}],
        [{"role": "user", "content": "Wrong first role"}, {"role": "user", "content": "Current"}],
        [{"role": "system", "content": "Owner"}, {"role": "assistant", "content": "Wrong last"}],
        [{"role": "system", "content": "Owner"}, {"role": "user", "content": 123}],
        [
            {"role": "system", "content": "Owner"},
            {"role": "user", "content": "Current", "extra": True},
        ],
        [
            {"role": "system", "content": "Owner"},
            {"role": "tool", "content": "Not history"},
            {"role": "user", "content": "Current"},
        ],
        [
            {"role": "system", "content": "x" * (1024 * 1024 + 1)},
            {"role": "user", "content": "Current"},
        ],
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_oversized_history_is_all_or_refuse_before_connect(bad):
    connector = Connector()
    target = provider(connector)
    try:
        with pytest.raises((ProviderRefusal, ValueError)):
            await target.respond(audio(), bad, current=Current())
        assert connector.calls == []
    finally:
        await target.close()


def malformed_output(name):
    events = copy.deepcopy(completed_output())
    text_delta = next(item for item in events if item["type"] == "response.output_text.delta")
    audio_delta = next(item for item in events if item["type"] == "response.output_audio.delta")
    if name == "duplicate-json":
        index = events.index(text_delta)
        events[index] = encode(text_delta)[:-1] + ',"shadow":1,"shadow":2}'
    elif name == "nonfinite-json":
        events[events.index(text_delta)] = encode(text_delta)[:-1] + ',"shadow":NaN}'
    elif name == "binary-frame":
        events[events.index(text_delta)] = encode(text_delta).encode()
    elif name == "malformed-json":
        events[events.index(text_delta)] = "{" + MARKER
    elif name == "oversized-event":
        text_delta["padding"] = "x" * 262144
    elif name == "foreign-response":
        text_delta["response_id"] = "foreign-response"
    elif name == "foreign-item":
        audio_delta["item_id"] = "foreign-item"
    elif name == "missing-response":
        del text_delta["response_id"]
    elif name == "second-response":
        events.insert(
            4,
            {
                "type": "response.created",
                "response": {"id": "foreign-response", "status": "in_progress"},
            },
        )
    elif name == "text-done-mismatch":
        next(item for item in events if item["type"] == "response.output_text.done")["text"] = (
            MARKER
        )
    elif name == "final-text-mismatch":
        events[-1]["response"]["output"][0]["content"][0]["text"] = MARKER
    elif name == "failed-terminal":
        events[-1]["response"]["status"] = "failed"
    elif name == "cancelled-terminal":
        events[-1]["response"]["status"] = "cancelled"
    elif name == "missing-audio":
        events = [item for item in events if "audio" not in item["type"]]
        events[-1]["response"]["output"][0]["content"] = [{"type": "output_text", "text": TEXT}]
    elif name == "empty-audio":
        audio_delta["delta"] = ""
    elif name == "empty-text":
        events = completed_output(text="")
    elif name == "invalid-text-unicode":
        events = completed_output(text="\ud800")
    elif name == "odd-audio":
        audio_delta["delta"] = base64.b64encode(b"\x00").decode()
    elif name == "invalid-base64":
        audio_delta["delta"] = "%%%"
    elif name == "tool-call":
        events.insert(
            2,
            {
                "type": "response.output_item.added",
                "response_id": RID,
                "output_index": 1,
                "item": {
                    "id": "call-one",
                    "type": "function_call",
                    "name": MARKER,
                    "arguments": "{}",
                },
            },
        )
    elif name == "context-overflow":
        events = [
            {"type": "error", "error": {"type": "exceed_context_size_error", "message": MARKER}}
        ]
    elif name == "eof-before-terminal":
        events[-1] = EOFError(MARKER)
    else:
        raise AssertionError("Unknown synthetic failure")
    return events


@pytest.mark.parametrize(
    "name",
    [
        "duplicate-json",
        "nonfinite-json",
        "binary-frame",
        "malformed-json",
        "oversized-event",
        "foreign-response",
        "foreign-item",
        "missing-response",
        "second-response",
        "text-done-mismatch",
        "final-text-mismatch",
        "failed-terminal",
        "cancelled-terminal",
        "missing-audio",
        "empty-audio",
        "empty-text",
        "invalid-text-unicode",
        "odd-audio",
        "invalid-base64",
        "tool-call",
        "context-overflow",
        "eof-before-terminal",
    ],
)
@pytest.mark.asyncio
async def test_malformed_native_output_is_redacted_refused_once_and_never_repaired(name):
    socket = Socket(output=malformed_output(name))
    connector = Connector(socket)
    target = provider(connector, api_key=SECRET)
    try:
        with pytest.raises(ProviderRefusal) as exc:
            await target.respond(audio(), messages(), current=Current())
        assert_redacted(exc)
        assert len(connector.calls) == 1
        assert sum(item["type"] == "response.create" for item in socket.sent) == 1
        assert socket.close_calls == 1 and socket.close_exited.is_set()
    finally:
        await target.close()


@pytest.mark.parametrize(
    "field",
    [
        "input-rate",
        "output-rate",
        "input-format",
        "output-format",
        "missing-output",
        "automatic-turn-detection",
    ],
)
@pytest.mark.asyncio
async def test_native_session_format_and_rates_must_be_verified_before_audio_export(field):
    metadata = session()
    if field == "input-rate":
        metadata["audio"]["input"]["format"]["rate"] = 32000
    elif field == "output-rate":
        metadata["audio"]["output"]["format"]["rate"] = True
    elif field == "input-format":
        metadata["audio"]["input"]["format"]["type"] = MARKER
    elif field == "output-format":
        metadata["audio"]["output"]["format"]["type"] = MARKER
    elif field == "missing-output":
        del metadata["audio"]["output"]
    else:
        metadata["audio"]["input"]["turn_detection"] = {"type": "server_vad"}
    socket = Socket(created={"type": "session.created", "session": metadata})
    connector = Connector(socket)
    target = provider(connector)
    try:
        with pytest.raises(ProviderRefusal) as exc:
            await target.respond(audio(), messages(), current=Current())
        assert_redacted(exc)
        assert wire_pcm(socket) == b""
        assert not any(item["type"] == "response.create" for item in socket.sent)
        assert len(connector.calls) == socket.close_calls == 1
    finally:
        await target.close()


@pytest.mark.asyncio
async def test_changed_update_acknowledgement_cannot_authorize_output_or_audio_export():
    socket = Socket()

    async def change_ack(event):
        if event["type"] == "session.update":
            value = session()
            value.update(event["session"])
            value["output_modalities"] = ["text"]
            socket.push({"type": "session.updated", "session": value})

    socket.on_send = change_ack
    target = provider(Connector(socket))
    try:
        with pytest.raises(ProviderRefusal):
            await target.respond(audio(), messages(), current=Current())
        assert wire_pcm(socket) == b""
    finally:
        await target.close()


@pytest.mark.parametrize(
    "transcription",
    [
        [
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "foreign-item",
                "content_index": 0,
                "transcript": "Public text",
            }
        ],
        [
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": UID,
                "content_index": 0,
                "transcript": MARKER * 2000,
            }
        ],
        [
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": UID,
                "content_index": 0,
                "transcript": 123,
            }
        ],
        [{"type": "error", "error": {"message": MARKER}}],
        [EOFError(MARKER)],
    ],
)
@pytest.mark.asyncio
async def test_transcription_requires_matching_complete_bounded_item(transcription):
    socket = Socket(transcription=transcription)
    connector = Connector(socket)
    target = provider(connector)
    try:
        with pytest.raises(ProviderRefusal) as exc:
            await target.transcribe(audio(), current=Current())
        assert_redacted(exc)
        assert len(connector.calls) == 1
        assert not any(item["type"] == "response.create" for item in socket.sent)
    finally:
        await target.close()


def with_audio_chunks(pcm):
    events = completed_output()
    index = next(
        i for i, item in enumerate(events) if item["type"] == "response.output_audio.delta"
    )
    original = events[index]
    chunks = [
        {**original, "delta": base64.b64encode(pcm[start : start + 64000]).decode()}
        for start in range(0, len(pcm), 64000)
    ]
    return [*events[:index], *chunks, *events[index + 1 :]]


@pytest.mark.parametrize(("rate", "size"), [(24000, 2 * 1024 * 1024), (8000, 8000 * 2 * 60)])
@pytest.mark.asyncio
async def test_exact_pcm_byte_or_duration_ceiling_is_accepted(rate, size):
    pcm = b"\x00\x00" * (size // 2)
    socket = Socket(
        output=with_audio_chunks(pcm),
        created={"type": "session.created", "session": session(output_rate=rate)},
    )
    target = provider(Connector(socket))
    try:
        reply = await target.respond(audio(), messages(), current=Current())
        assert reply.pcm16 == pcm and reply.rate == rate
    finally:
        await target.close()


@pytest.mark.parametrize(
    ("rate", "size"), [(24000, 2 * 1024 * 1024 + 2), (8000, 8000 * 2 * 60 + 2)]
)
@pytest.mark.asyncio
async def test_pcm_byte_and_duration_limits_are_independently_enforced(rate, size):
    socket = Socket(
        output=with_audio_chunks(b"\x00\x00" * (size // 2)),
        created={"type": "session.created", "session": session(output_rate=rate)},
    )
    target = provider(Connector(socket))
    try:
        with pytest.raises(ProviderRefusal):
            await target.respond(audio(), messages(), current=Current())
    finally:
        await target.close()


def with_text_deltas(count):
    events = completed_output(text="x" * count)
    index = next(i for i, item in enumerate(events) if item["type"] == "response.output_text.delta")
    deltas = [{**events[index], "delta": "x"} for _ in range(count)]
    return [*events[:index], *deltas, *events[index + 1 :]]


@pytest.mark.asyncio
async def test_text_and_event_counts_are_bounded_even_when_individual_events_fit():
    too_much_text = Socket(output=completed_output(text="x" * 16001))
    too_many_events = Socket(output=with_text_deltas(8193))
    for socket in (too_much_text, too_many_events):
        target = provider(Connector(socket))
        try:
            with pytest.raises(ProviderRefusal):
                await target.respond(audio(), messages(), current=Current())
        finally:
            await target.close()


@pytest.mark.asyncio
async def test_exact_received_event_ceiling_remains_a_valid_native_response():
    # Five session/commit events plus (12 - 1 + 8176) response events = 8192.
    socket = Socket(output=with_text_deltas(8176))
    target = provider(Connector(socket))
    try:
        reply = await target.respond(audio(), messages(), current=Current())
        assert reply.text == "x" * 8176 and reply.pcm16 == OUT
        assert len(socket.received) == 8192
    finally:
        await target.close()


@pytest.mark.asyncio
async def test_foreign_values_are_refused_without_calling_conversion_or_copy_hooks():
    invoked = []

    class Foreign:
        def __str__(self):
            invoked.append("str")
            raise AssertionError(MARKER)

        def __deepcopy__(self, memo):
            invoked.append("deepcopy")
            raise AssertionError(MARKER)

    connector = Connector()
    target = provider(connector)
    bad = [{"role": "system", "content": "Owner"}, {"role": "user", "content": Foreign()}]
    try:
        with pytest.raises((ProviderRefusal, ValueError)):
            await target.respond(audio(), bad, current=Current())
        assert connector.calls == [] and invoked == []
    finally:
        await target.close()


@pytest.mark.asyncio
async def test_stale_admission_has_no_connection_and_nonboolean_current_is_not_a_grant():
    for allowed in (False, None, 1, "true"):
        connector = Connector()
        target = provider(connector)
        try:
            with pytest.raises(ProviderRefusal):
                await target.respond(audio(), messages(), current=Current(allowed))
            assert connector.calls == []
        finally:
            await target.close()


@pytest.mark.asyncio
async def test_revocation_after_created_session_prevents_any_audio_or_response_export():
    current = Current()
    socket = Socket()

    async def revoke(raw):
        if json.loads(raw).get("type") == "session.created":
            current.allowed = False

    socket.on_recv = revoke
    target = provider(Connector(socket))
    try:
        with pytest.raises(ProviderRefusal):
            await target.respond(audio(), messages(), current=current)
        assert wire_pcm(socket) == b""
        assert not any(event["type"] == "response.create" for event in socket.sent)
        assert socket.close_calls == 1
    finally:
        await target.close()


@pytest.mark.asyncio
async def test_revocation_after_native_delta_prevents_returning_buffered_reply():
    current = Current()
    socket = Socket()

    async def revoke(raw):
        if json.loads(raw).get("type") == "response.output_text.delta":
            current.allowed = False

    socket.on_recv = revoke
    target = provider(Connector(socket))
    try:
        with pytest.raises(ProviderRefusal):
            await target.respond(audio(), messages(), current=current)
        assert socket.close_calls == 1
    finally:
        await target.close()


@pytest.mark.parametrize("mutate", ["audio", "messages", "send-binding"])
@pytest.mark.asyncio
async def test_original_captured_authority_cannot_be_retargeted_after_connection_await(mutate):
    supplied_audio, supplied_messages = audio(), messages()
    socket = Socket()
    poisoned_sends = []

    async def malicious_send(raw):
        poisoned_sends.append(raw)
        raise OSError(MARKER)

    async def change_owner_input(raw):
        if json.loads(raw).get("type") != "session.created":
            return
        if mutate == "audio":
            object.__setattr__(supplied_audio, "pcm16", b"\x01\x00" * 200)
            object.__setattr__(supplied_audio, "rate", 32000)
        elif mutate == "messages":
            supplied_messages[1]["content"] = MARKER
        else:
            socket.send = malicious_send

    socket.on_recv = change_owner_input
    target = provider(Connector(socket))
    try:
        try:
            reply = await target.respond(supplied_audio, supplied_messages, current=Current())
        except ProviderRefusal as failure:
            assert MARKER not in str(failure)
        else:
            assert reply.text == TEXT
            assert wire_pcm(socket) == PCM
        assert MARKER not in encode(socket.sent)
        assert poisoned_sends == []
    finally:
        await target.close()


@pytest.mark.asyncio
async def test_busy_operation_refuses_immediately_without_queue_or_connection_replay():
    socket = Socket(held_type="response.output_text.delta")
    connector = Connector(socket)
    target = provider(connector)
    original = asyncio.create_task(target.respond(audio(), messages(), current=Current()))
    try:
        await wait(socket.receive_held)
        with pytest.raises(ProviderRefusal):
            await asyncio.wait_for(target.transcribe(audio(), current=Current()), 0.5)
        assert len(connector.calls) == 1 and not original.done()
        socket.receive_release.set()
        reply = await original
        assert reply.pcm16 == OUT
        assert len(connector.calls) == 1
    finally:
        socket.receive_release.set()
        if not original.done():
            original.cancel()
        await asyncio.gather(original, return_exceptions=True)
        await target.close()


@pytest.mark.asyncio
async def test_cancellation_retains_original_held_close_and_busy_slot_without_duplicate_close():
    socket = Socket(held_type="response.output_text.delta", held_close=True)
    connector = Connector(socket)
    target = provider(connector)
    original = asyncio.create_task(target.respond(audio(), messages(), current=Current()))
    try:
        await wait(socket.receive_held)
        original.cancel()
        await wait(socket.close_started)
        original.cancel()
        await ticks()
        assert socket.close_calls == 1 and not socket.close_cancelled
        assert not socket.close_exited.is_set()
        with pytest.raises((ProviderRefusal, ProviderCleanupError)):
            await asyncio.wait_for(target.respond(audio(), messages(), current=Current()), 0.5)
        assert len(connector.calls) == 1
        socket.close_release.set()
        await wait(socket.close_exited)
        await ticks()
        assert not original.done(), "A close acknowledgement does not settle the held receive"
        socket.receive_release.set()
        with pytest.raises(asyncio.CancelledError):
            await original
    finally:
        socket.close_release.set()
        socket.receive_release.set()
        if not original.done():
            original.cancel()
        await asyncio.gather(original, return_exceptions=True)
        await target.close()
    assert socket.close_calls == 1 and not socket.close_cancelled


@pytest.mark.asyncio
async def test_provider_close_seals_and_cancelled_close_waiter_cannot_cancel_original_close():
    socket = Socket(held_type="response.output_text.delta", held_close=True)
    connector = Connector(socket)
    target = provider(connector)
    original = asyncio.create_task(target.respond(audio(), messages(), current=Current()))
    closing = None
    try:
        await wait(socket.receive_held)
        closing = asyncio.create_task(target.close())
        await wait(socket.close_started)
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
        assert socket.close_calls == 1 and not socket.close_cancelled
        assert not socket.close_exited.is_set()
        with pytest.raises((ProviderRefusal, ProviderCleanupError)):
            await target.transcribe(audio(), current=Current())
        assert len(connector.calls) == 1
        socket.close_release.set()
        await wait(socket.close_exited)
        await ticks()
        assert not original.done(), "The original receive is still outstanding after close"
        socket.receive_release.set()
        with pytest.raises((asyncio.CancelledError, ProviderRefusal)):
            await original
        await target.close()
    finally:
        socket.close_release.set()
        socket.receive_release.set()
        if not original.done():
            original.cancel()
        await asyncio.gather(original, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
    assert socket.close_calls == 1 and not socket.close_cancelled


@pytest.mark.asyncio
async def test_failed_original_close_remains_latched_and_never_opens_another_exchange():
    socket = Socket(close_failure=True)
    connector = Connector(socket)
    target = provider(connector, api_key=SECRET)
    with pytest.raises(ProviderCleanupError) as exc:
        await target.respond(audio(), messages(), current=Current())
    assert_redacted(exc)
    assert socket.close_calls == 1 and socket.close_exited.is_set()
    with pytest.raises((ProviderRefusal, ProviderCleanupError)):
        await target.respond(audio(), messages(), current=Current())
    for _ in range(2):
        try:
            await target.close()
        except ProviderCleanupError as failure:
            assert MARKER not in str(failure)
    assert len(connector.calls) == 1 and socket.close_calls == 1


@pytest.mark.asyncio
async def test_cancel_suppressed_by_current_callback_cannot_resume_audio_export():
    entered, release = asyncio.Event(), asyncio.Event()

    async def suppress_cancel():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            return True
        return True

    connector = Connector()
    target = provider(connector)
    original = asyncio.create_task(target.respond(audio(), messages(), current=suppress_cancel))
    try:
        await wait(entered)
        original.cancel()
        with pytest.raises((asyncio.CancelledError, ProviderRefusal)):
            await original
        assert connector.calls == []
    finally:
        release.set()
        if not original.done():
            original.cancel()
        await asyncio.gather(original, return_exceptions=True)
        await target.close()


@pytest.mark.asyncio
async def test_original_deadline_expires_without_reconnect_or_hidden_fallback():
    socket = Socket(held_type="response.output_text.delta")
    connector = Connector(socket)
    target = provider(connector, timeout=0.05)
    original = asyncio.create_task(target.respond(audio(), messages(), current=Current()))
    try:
        await wait(socket.close_exited)
        await ticks()
        assert not original.done(), "The native deadline closes transport but still owns held IO"
        socket.receive_release.set()
        with pytest.raises(ProviderRefusal) as exc:
            await asyncio.wait_for(original, 2)
        assert_redacted(exc)
        assert len(connector.calls) == 1 and socket.close_calls == 1
    finally:
        socket.receive_release.set()
        socket.close_release.set()
        if not original.done():
            original.cancel()
        await asyncio.gather(original, return_exceptions=True)
        await target.close()


@pytest.mark.asyncio
async def test_close_before_first_operation_prevents_connection_or_export():
    connector = Connector()
    target = provider(connector)
    await target.close()
    for call in (
        lambda: target.transcribe(audio(), current=Current()),
        lambda: target.respond(audio(), messages(), current=Current()),
    ):
        with pytest.raises((ProviderRefusal, ProviderCleanupError)):
            await call()
    assert connector.calls == []


@pytest.mark.asyncio
async def test_explicit_second_turn_gets_fresh_native_session_and_original_history():
    first, second = Socket(), Socket()
    connector = Connector(first, second)
    target = provider(connector)
    try:
        await target.respond(audio(), messages(), current=Current())
        next_history = messages()
        next_history.insert(-1, {"role": "assistant", "content": "Second-turn history only."})
        await target.respond(audio(), next_history, current=Current())
        assert len(connector.calls) == 2
        assert first.close_calls == second.close_calls == 1
        assert "Second-turn history only." not in encode(first.sent)
        assert "Second-turn history only." in encode(second.sent)
        assert wire_pcm(first) == wire_pcm(second) == PCM
    finally:
        await target.close()
