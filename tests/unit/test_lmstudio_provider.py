"""Synthetic auth-v1 native protocol fixtures; never contact a model server."""

import asyncio
import copy
import json
import sys

import pytest

from packages.providers.lmstudio import LMStudioProvider
from packages.providers.prompt_budget import PromptCountUnavailable, request_digest
from packages.providers.text import ProviderCleanupError, ProviderError, ProviderRefusal

MESSAGES = [{"role": "system", "content": "Public fixture."}, {"role": "user", "content": "你好."}]
MODEL = {
    "type": "llm",
    "identifier": "fixture-alias",
    "instanceReference": "fixture-instance-1",
    "path": "public/fixture.gguf",
    "modelKey": "fixture",
    "indexedModelIdentifier": "public/fixture.gguf",
    "format": "gguf",
    "deviceIdentifier": None,
}
LOAD = {"fields": [{"key": "llm.load.contextLength", "value": 2048}]}
BASE = {"fields": [{"key": "llm.prediction.systemPrompt", "value": ""}]}


class NativeFixture:
    def __init__(self, *, count=25, stop="eosFound", frames=None):
        self.count, self.stop, self.frames = count, stop, frames
        self.connections = []
        self.calls = []
        self.model, self.load, self.base = copy.deepcopy((MODEL, LOAD, BASE))
        self.fail_close = False
        self.close_gate = None
        self.cancel_ack = True
        self.hold_prediction = False
        self.hold_rpc = None
        self.response_hook = None
        self.cancelled = asyncio.Event()

    async def connect(self, endpoint):
        assert endpoint == "ws://127.0.0.1:1234/llm"
        ws = NativeSocket(self)
        self.connections.append(ws)
        return ws

    def terminal(self, ws, stop=None):
        creation = ws.creation
        fields = {}
        for layer in creation["predictionConfigStack"]["layers"]:
            fields.update({f["key"]: f["value"] for f in layer["config"]["fields"]})
        return {
            "type": "success",
            "stats": {
                "stopReason": stop or self.stop,
                "promptTokensCount": self.count,
                "predictedTokensCount": 2,
                "totalTokensCount": self.count + 2,
            },
            "modelInfo": copy.deepcopy(self.model),
            "loadModelConfig": copy.deepcopy(self.load),
            "predictionConfig": {"fields": [{"key": k, "value": v} for k, v in fields.items()]},
        }


class NativeSocket:
    def __init__(self, fixture):
        self.fixture = fixture
        self.queue = asyncio.Queue()
        self.sent = []
        self.creation = None
        self.closed = self.waited = False

    def put(self, packet):
        self.queue.put_nowait(json.dumps(packet) if isinstance(packet, dict) else packet)

    def channel(self, message):
        self.put({"type": "channelSend", "channelId": 1, "message": message})

    async def send(self, raw):
        packet = json.loads(raw)
        self.sent.append(packet)
        fixture = self.fixture
        if "authVersion" in packet:
            self.put({"success": True})
        elif packet["type"] == "rpcCall":
            fixture.calls.append(copy.deepcopy(packet))
            endpoint = packet["endpoint"]
            if endpoint == fixture.hold_rpc:
                return
            if endpoint == "listLoaded":
                result = [copy.deepcopy(fixture.model)]
            elif endpoint == "getModelInfo":
                result = copy.deepcopy(fixture.model)
            elif endpoint == "getLoadConfig":
                result = copy.deepcopy(fixture.load)
            elif endpoint == "getBasePredictionConfig":
                result = copy.deepcopy(fixture.base)
            elif endpoint == "applyPromptTemplate":
                result = {"formatted": "完整 native template <think>\n\n</think>\n\n"}
            elif endpoint == "countTokens":
                result = {"tokenCount": fixture.count}
            else:
                raise AssertionError("Unexpected native operation: " + endpoint)
            reply = {"type": "rpcResult", "callId": packet["callId"], "result": result}
            if fixture.response_hook:
                reply = fixture.response_hook(packet, reply)
            self.put(reply)
        elif packet["type"] == "channelCreate":
            assert packet["endpoint"] == "predict"
            self.creation = packet["creationParameter"]
            if fixture.hold_prediction:
                return
            if fixture.frames is not None:
                for frame in fixture.frames(self):
                    self.put(frame)
                return
            self.channel(
                {
                    "type": "fragment",
                    "fragment": {
                        "content": "Public reply.",
                        "tokensCount": 2,
                        "containsDrafted": False,
                        "reasoningType": "none",
                        "isStructural": False,
                    },
                }
            )
            self.channel(fixture.terminal(self))
            self.put({"type": "channelClose", "channelId": 1})
        elif packet["type"] == "channelSend":
            assert packet == {"type": "channelSend", "channelId": 1, "message": {"type": "cancel"}}
            fixture.cancelled.set()
            if fixture.cancel_ack:
                self.channel(fixture.terminal(self, "userStopped"))
                self.put({"type": "channelClose", "channelId": 1})
        else:
            raise AssertionError("Unexpected protocol packet")

    async def recv(self):
        packet = await self.queue.get()
        if isinstance(packet, Exception):
            raise packet
        return packet

    async def close(self):
        if self.fixture.close_gate:
            await self.fixture.close_gate.wait()
        if self.fixture.fail_close:
            raise OSError("synthetic private backend error")
        self.closed = True

    async def wait_closed(self):
        self.waited = True


def provider(fixture, **kwargs):
    return LMStudioProvider(
        "http://127.0.0.1:1234/v1", "fixture-alias", transport_factory=fixture.connect, **kwargs
    )


@pytest.mark.parametrize("count, fits", [(1920, True), (1921, False), (25, True)])
async def test_native_complete_template_capacity_and_output_reservation(count, fits):
    wire = NativeFixture(count=count)
    p = provider(wire)
    measured = await p.measure_prompt(MESSAGES)
    assert (measured.input_tokens, measured.context_tokens, measured.reserved_output_tokens) == (
        count,
        2048,
        128,
    )
    assert measured.fits is fits
    assert wire.connections[0].closed and wire.connections[0].waited
    assert not any(ws.creation for ws in wire.connections)
    formatter = next(c for c in wire.calls if c["endpoint"] == "applyPromptTemplate")
    counter = next(c for c in wire.calls if c["endpoint"] == "countTokens")
    assert counter["parameter"]["inputString"] == "完整 native template <think>\n\n</think>\n\n"
    assert formatter["parameter"]["specifier"] == {
        "type": "instanceReference",
        "instanceReference": "fixture-instance-1",
    }
    assert p.info.provider == "lmstudio" and p.info.local and p.info.streaming


async def test_generation_is_same_instance_history_and_effective_stack_as_measurement():
    wire = NativeFixture()
    p = provider(wire)
    measured = await p.measure_prompt(MESSAGES)
    assert await p.complete(MESSAGES) == "Public reply."
    creation = wire.connections[-1].creation
    formatter = [c for c in wire.calls if c["endpoint"] == "applyPromptTemplate"][-1]["parameter"]
    assert creation["modelSpecifier"] == formatter["specifier"]
    assert creation["history"] == formatter["history"]
    assert creation["predictionConfigStack"] == formatter["predictionConfigStack"]
    assert creation["ignoreServerSessionConfig"] is True
    assert measured.request_sha256 == request_digest(creation)
    assert all(ws.closed and ws.waited for ws in wire.connections)
    assert not hasattr(p, "managed_binding")
    endpoints = {c["endpoint"] for c in wire.calls}
    assert not endpoints & {"getOrLoad", "loadModel", "unloadModel", "preloadDraftModel"}


async def test_whole_history_is_not_capped_to_legacy_rows():
    wire = NativeFixture()
    p = provider(wire)
    messages = [{"role": "user", "content": str(i)} for i in range(80)]
    await p.measure_prompt(messages)
    request = next(c for c in wire.calls if c["endpoint"] == "applyPromptTemplate")
    assert len(request["parameter"]["history"]["messages"]) == 80


@pytest.mark.parametrize(
    "field,value", [("instanceReference", "replacement"), ("path", "new.gguf")]
)
async def test_measurement_drift_refuses_before_predict(field, value):
    wire = NativeFixture()
    p = provider(wire)
    await p.measure_prompt(MESSAGES)
    wire.model[field] = value
    with pytest.raises(ProviderError):
        await p.complete(MESSAGES)
    assert not any(ws.creation for ws in wire.connections)


async def test_effective_base_formatting_drift_refuses_before_predict():
    wire = NativeFixture()
    p = provider(wire)
    await p.measure_prompt(MESSAGES)
    wire.base["fields"][0]["value"] = "Changed public system fixture"
    with pytest.raises(ProviderError):
        await p.complete(MESSAGES)
    assert not any(ws.creation for ws in wire.connections)


@pytest.mark.parametrize("capacity", [True, 0, 63, 2**53, 2048.0])
async def test_unsupported_loaded_capacity_is_not_clamped(capacity):
    wire = NativeFixture()
    wire.load["fields"][0]["value"] = capacity
    with pytest.raises(PromptCountUnavailable, match="loaded context capacity is unsupported"):
        await provider(wire).measure_prompt(MESSAGES)


@pytest.mark.parametrize("capacity", [32769, 40960, 131072])
async def test_large_loaded_context_is_measured_without_clamping(capacity):
    wire = NativeFixture()
    wire.load["fields"][0]["value"] = capacity
    assert (await provider(wire).measure_prompt(MESSAGES)).context_tokens == capacity


async def test_candidate_bindings_are_stable_but_request_identity_changes():
    p = provider(NativeFixture())
    first = await p.measure_prompt(MESSAGES)
    second = await p.measure_prompt([*MESSAGES, {"role": "assistant", "content": "Public."}])
    assert first.binding_sha256 == second.binding_sha256
    assert first.request_sha256 != second.request_sha256


async def test_formatted_template_drift_refuses_even_when_count_is_identical():
    wire = NativeFixture()
    p = provider(wire)
    await p.measure_prompt(MESSAGES)

    def drift(packet, reply):
        if packet["endpoint"] == "applyPromptTemplate":
            reply["result"]["formatted"] = "Changed public template with the same fixture count."
        return reply

    wire.response_hook = drift
    with pytest.raises(ProviderError):
        await p.complete(MESSAGES)
    assert not any(ws.creation for ws in wire.connections)


@pytest.mark.parametrize("count", [True, -1, 0, 1.5, 2**53])
async def test_count_requires_positive_safe_integer(count):
    with pytest.raises(PromptCountUnavailable):
        await provider(NativeFixture(count=count)).measure_prompt(MESSAGES)


@pytest.mark.parametrize(
    "stop",
    [
        "maxPredictedTokensReached",
        "contextLengthReached",
        "toolCalls",
        "failed",
        "userStopped",
        "modelUnloaded",
    ],
)
async def test_noncomplete_native_terminals_refuse(stop):
    with pytest.raises(ProviderRefusal):
        await provider(NativeFixture(stop=stop)).complete(MESSAGES)


async def test_unknown_terminal_does_not_confirm_native_cleanup():
    with pytest.raises(ProviderCleanupError):
        await provider(NativeFixture(stop="unknown")).complete(MESSAGES)


async def test_cancel_is_request_owned_and_requires_native_ack_and_real_close():
    wire = NativeFixture()
    wire.hold_prediction = True
    p = provider(wire)
    task = asyncio.create_task(p.complete(MESSAGES))
    while not wire.connections or wire.connections[-1].creation is None:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wire.cancelled.is_set()
    assert wire.connections[-1].closed and wire.connections[-1].waited
    wire.hold_prediction = False
    assert await p.complete(MESSAGES) == "Public reply."


async def test_unconfirmed_native_cancel_poison_blocks_reuse():
    wire = NativeFixture()
    wire.hold_prediction, wire.cancel_ack = True, False
    p = provider(wire)
    p._cleanup_timeout = 0.01
    task = asyncio.create_task(p.complete(MESSAGES))
    while not wire.connections or wire.connections[-1].creation is None:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(ProviderCleanupError):
        await task
    before = len(wire.connections)
    with pytest.raises(ProviderCleanupError):
        await p.measure_prompt(MESSAGES)
    assert len(wire.connections) == before
    with pytest.raises(ProviderCleanupError):
        await p.close()


async def test_real_close_is_retained_through_repeated_cancellation():
    wire = NativeFixture()
    wire.close_gate = asyncio.Event()
    p = provider(wire)
    task = asyncio.create_task(p.measure_prompt(MESSAGES))
    while (
        not wire.connections
        or not wire.calls
        or wire.calls[-1]["endpoint"] != "getBasePredictionConfig"
    ):
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    wire.close_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wire.connections[0].closed and wire.connections[0].waited


async def test_close_failure_is_not_a_successful_measurement_and_blocks_reuse():
    wire = NativeFixture()
    wire.fail_close = True
    p = provider(wire)
    with pytest.raises(ProviderCleanupError):
        await p.measure_prompt(MESSAGES)
    with pytest.raises(ProviderCleanupError):
        await p.complete(MESSAGES)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://remote.example/v1",
        "http://127.0.0.1:1234/v1?x=1",
        "http://user@127.0.0.1:1234",
        "http://127.0.0.1:1234/arbitrary",
    ],
)
def test_endpoint_never_allows_remote_credentials_or_ambiguous_path(endpoint):
    with pytest.raises(ProviderError):
        LMStudioProvider(endpoint, "fixture-alias", allow_cloud=True)


@pytest.mark.parametrize(
    "body",
    [[], [{"role": "tool", "content": "x"}], [{"role": "user", "content": "x", "tools": []}]],
)
async def test_unsupported_history_refuses_without_connect(body):
    wire = NativeFixture()
    with pytest.raises(PromptCountUnavailable):
        await provider(wire).measure_prompt(body)
    assert not wire.connections


async def test_protected_prediction_settings_override_base_for_count_and_generate():
    wire = NativeFixture()
    wire.base["fields"].extend(
        [
            {"key": "llm.prediction.maxPredictedTokens", "value": {"checked": False, "value": 999}},
            {"key": "llm.prediction.reasoning.enableThinking", "value": True},
            {"key": "llm.prediction.contextOverflowPolicy", "value": "truncateMiddle"},
        ]
    )
    p = provider(wire)
    await p.complete(MESSAGES)
    stack = wire.connections[0].creation["predictionConfigStack"]
    fields = {}
    for layer in stack["layers"]:
        fields.update({f["key"]: f["value"] for f in layer["config"]["fields"]})
    assert fields["llm.prediction.reasoning.enableThinking"] is False
    assert fields["llm.prediction.maxPredictedTokens"] == {"checked": True, "value": 128}
    assert fields["llm.prediction.contextOverflowPolicy"] == "stopAtLimit"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "remote", "draft", "unknown_config"])
async def test_unsupported_model_or_base_configuration_never_starts_prediction(mutation):
    wire = NativeFixture()
    if mutation in {"missing", "duplicate"}:

        def hook(call, reply):
            if call["endpoint"] == "listLoaded":
                reply["result"] = [] if mutation == "missing" else [MODEL, MODEL]
            return reply

        wire.response_hook = hook
    elif mutation == "remote":
        wire.model["deviceIdentifier"] = "remote-device"
    else:
        key = "speculativeDecoding.draftModel" if mutation == "draft" else "futureUnknownSetting"
        wire.base["fields"].append({"key": "llm.prediction." + key, "value": "other-model"})
    with pytest.raises(ProviderError):
        await provider(wire).complete(MESSAGES)
    assert not any(ws.creation for ws in wire.connections)


@pytest.mark.parametrize(
    "bad_reply",
    [
        '{"type":"rpcResult","callId":1,"callId":1,"result":[]}',
        '{"type":"rpcResult","callId":true,"result":[]}',
        '{"type":"rpcResult","callId":99,"result":[]}',
        '{"type":"rpcResult","callId":1,"result":NaN}',
        '{"type":"rpcResult","callId":1,"result":1e999}',
        b'{"type":"rpcResult","callId":1,"result":[]}',
    ],
)
async def test_malformed_or_wrong_identity_reply_never_clears_owned_rpc(bad_reply):
    wire = NativeFixture()
    wire.response_hook = lambda _call, _reply: bad_reply
    p = provider(wire)
    p._cleanup_timeout = 0.01
    with pytest.raises(ProviderCleanupError):
        await p.measure_prompt(MESSAGES)
    assert wire.connections[0].closed
    with pytest.raises(ProviderCleanupError):
        await p.complete(MESSAGES)


async def test_cancelled_count_retains_exact_pending_rpc_until_result_and_actual_close():
    wire = NativeFixture()
    wire.hold_rpc = "countTokens"
    p = provider(wire)
    task = asyncio.create_task(p.measure_prompt(MESSAGES))
    while not wire.calls or wire.calls[-1]["endpoint"] != "countTokens":
        await asyncio.sleep(0)
    call = wire.calls[-1]
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    wire.connections[-1].put(
        {"type": "rpcResult", "callId": call["callId"], "result": {"tokenCount": 25}}
    )
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wire.connections[-1].closed and wire.connections[-1].waited
    assert not wire.cancelled.is_set()
    wire.hold_rpc = None
    assert (await p.measure_prompt(MESSAGES)).input_tokens == 25


async def test_unconfirmed_count_rpc_also_blocks_reuse():
    wire = NativeFixture()
    wire.hold_rpc = "countTokens"
    p = provider(wire)
    p._cleanup_timeout = 0.01
    task = asyncio.create_task(p.measure_prompt(MESSAGES))
    while not wire.calls or wire.calls[-1]["endpoint"] != "countTokens":
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(ProviderCleanupError):
        await task
    with pytest.raises(ProviderCleanupError):
        await p.complete(MESSAGES)


async def test_cancellation_ack_must_belong_to_captured_model_and_configuration():
    wire = NativeFixture()
    wire.hold_prediction = True
    p = provider(wire)
    task = asyncio.create_task(p.complete(MESSAGES))
    while not wire.connections or wire.connections[-1].creation is None:
        await asyncio.sleep(0)
    wire.model["instanceReference"] = "different-request-instance"
    task.cancel()
    with pytest.raises(ProviderCleanupError):
        await task


async def test_cancellation_success_can_have_no_usage_before_prompt_processing():
    wire = NativeFixture()
    wire.hold_prediction = True
    original = wire.terminal

    def partial(ws, stop=None):
        terminal = original(ws, stop)
        terminal["stats"] = {"stopReason": "userStopped"}
        return terminal

    wire.terminal = partial
    p = provider(wire)
    task = asyncio.create_task(p.complete(MESSAGES))
    while not wire.connections or wire.connections[-1].creation is None:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wire.connections[-1].closed


async def test_early_consumer_close_cancels_only_its_native_channel():
    wire = NativeFixture()
    p = provider(wire)
    source = p.stream(MESSAGES)
    assert await anext(source) == "Public reply."
    await source.aclose()
    assert wire.cancelled.is_set()
    assert wire.connections[-1].closed


@pytest.mark.parametrize(
    "fault",
    [
        "reasoning",
        "structural",
        "drafted",
        "duplicate_terminal",
        "data_after_terminal",
        "wrong_channel",
        "close_without_terminal",
    ],
)
async def test_bad_generation_frames_refuse_and_never_return_complete_text(fault):
    wire = NativeFixture()

    def frames(ws):
        fragment = {
            "content": "synthetic",
            "tokensCount": 1,
            "containsDrafted": False,
            "reasoningType": "none",
            "isStructural": False,
        }
        terminal = {"type": "channelSend", "channelId": 1, "message": wire.terminal(ws)}
        close = {"type": "channelClose", "channelId": 1}
        if fault == "close_without_terminal":
            return [close]
        if fault in {"duplicate_terminal", "data_after_terminal"}:
            second = (
                terminal
                if fault == "duplicate_terminal"
                else {
                    "type": "channelSend",
                    "channelId": 1,
                    "message": {"type": "fragment", "fragment": fragment},
                }
            )
            return [terminal, second, close]
        if fault == "reasoning":
            fragment["reasoningType"] = "reasoning"
        if fault == "structural":
            fragment["isStructural"] = True
        if fault == "drafted":
            fragment["containsDrafted"] = True
        return [
            {
                "type": "channelSend",
                "channelId": 2 if fault == "wrong_channel" else 1,
                "message": {"type": "fragment", "fragment": fragment},
            },
            terminal,
            close,
        ]

    wire.frames = frames
    p = provider(wire)
    p._cleanup_timeout = 0.01
    with pytest.raises((ProviderRefusal, ProviderCleanupError)):
        await p.complete(MESSAGES)


async def test_post_terminal_base_drift_refuses_publication_after_confirmed_cleanup():
    wire = NativeFixture()

    def hook(call, reply):
        if call["endpoint"] == "getBasePredictionConfig" and wire.connections[-1].creation:
            reply["result"]["fields"][0]["value"] = "changed"
        return reply

    wire.response_hook = hook
    p = provider(wire)
    with pytest.raises(ProviderRefusal):
        await p.complete(MESSAGES)
    assert wire.connections[-1].closed


async def test_input_mutation_during_measurement_is_not_relabelled():
    wire = NativeFixture()
    messages = copy.deepcopy(MESSAGES)

    def hook(call, reply):
        if call["endpoint"] == "countTokens":
            messages[-1]["content"] = "changed"
        return reply

    wire.response_hook = hook
    with pytest.raises(PromptCountUnavailable):
        await provider(wire).measure_prompt(messages)
    assert wire.connections[-1].closed


async def test_another_measured_prompt_cannot_silently_reuse_a_stale_stamp():
    wire = NativeFixture()
    p = provider(wire)
    await p.measure_prompt(MESSAGES)
    with pytest.raises(ProviderRefusal):
        await p.complete([{"role": "user", "content": "different"}])
    assert not any(ws.creation for ws in wire.connections)


async def test_optional_websocket_dependency_is_lazy_and_missing_is_clear(monkeypatch):
    p = LMStudioProvider("http://127.0.0.1:1234/v1", "fixture-alias")
    monkeypatch.setitem(sys.modules, "websockets.asyncio.client", None)
    with pytest.raises(ProviderError, match="optional lmstudio"):
        await p.complete(MESSAGES)


@pytest.mark.parametrize("tag", ["<think>", "</think>", "<THINK "])
@pytest.mark.parametrize("split", [1, 2, 3, 4, 5, 6])
async def test_control_tags_split_across_native_fragments_refuse_before_reasoning(tag, split):
    wire = NativeFixture()

    def frames(ws):
        for content in (tag[:split], tag[split:] + "Private fixture reasoning"):
            yield {
                "type": "channelSend",
                "channelId": 1,
                "message": {
                    "type": "fragment",
                    "fragment": {
                        "content": content,
                        "tokensCount": 1,
                        "containsDrafted": False,
                        "reasoningType": "none",
                        "isStructural": False,
                    },
                },
            }
        yield {"type": "channelSend", "channelId": 1, "message": wire.terminal(ws)}
        yield {"type": "channelClose", "channelId": 1}

    wire.frames = frames
    emitted = []
    with pytest.raises(ProviderRefusal):
        async for fragment in provider(wire).stream(MESSAGES):
            emitted.append(fragment)
    assert "Private fixture reasoning" not in "".join(emitted)


async def test_native_terminal_omits_inactive_draft_default_but_active_draft_refuses():
    wire = NativeFixture()
    original = wire.terminal

    def terminal(ws, stop=None):
        result = original(ws, stop)
        result["predictionConfig"]["fields"] = [
            field
            for field in result["predictionConfig"]["fields"]
            if field["key"] != "llm.prediction.speculativeDecoding.draftModel"
        ]
        return result

    wire.terminal = terminal
    assert await provider(wire).complete(MESSAGES) == "Public reply."

    def active(ws, stop=None):
        result = terminal(ws, stop)
        result["predictionConfig"]["fields"].append(
            {"key": "llm.prediction.speculativeDecoding.draftModel", "value": "unauthorized-draft"}
        )
        return result

    wire.terminal = active
    with pytest.raises(ProviderCleanupError):
        await provider(wire).complete(MESSAGES)


@pytest.mark.parametrize(
    "field",
    [
        "totalDraftTokensCount",
        "acceptedDraftTokensCount",
        "rejectedDraftTokensCount",
        "ignoredDraftTokensCount",
    ],
)
async def test_native_draft_usage_refuses_even_without_a_named_draft_model(field):
    wire = NativeFixture()
    original = wire.terminal

    def terminal(ws, stop=None):
        result = original(ws, stop)
        result["stats"][field] = 1
        return result

    wire.terminal = terminal
    with pytest.raises(ProviderCleanupError):
        await provider(wire).complete(MESSAGES)
