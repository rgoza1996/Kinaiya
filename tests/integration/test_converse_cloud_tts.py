"""Native cloud-TTS admission and producer ownership; synthetic PCM only."""

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.models import ConverseRequest, ProviderInfo, SessionRequest
from packages.providers.disclosure import scope_allowed
from packages.providers.meta_speech import CloudSTTLocalTTS
from packages.providers.openrouter_speech import OpenRouterTTSClient
from packages.providers.speech import SpeechProviderError
from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.runtime import ConverseRuntime
from services.resources.state import ResourceState
from services.session.store import LocalJournal, Store

AUTH = Auth({"owner": "public-synthetic-owner-token-0000000000"})
PCM = b"\0\0\x01\0\xff\xff"
FRAME = {
    "type": "audio",
    "audio": base64.b64encode(PCM).decode(),
    "sample_rate": 24000,
    "samples": len(PCM) // 2,
    "chunk_index": 0,
}


class ScopedTTS:
    profile = "Kokoro 82M (OpenRouter)"

    def __init__(self, *, blocked=False):
        self.info = ProviderInfo(
            provider="openrouter", model="hexgrad/kokoro-82m", local=False, streaming=True
        )
        self.calls, self.exports = [], []
        self.entered, self.release, self.closed = (asyncio.Event() for _ in range(3))
        if not blocked:
            self.release.set()

    def synthesize(self, text):
        pytest.fail("cloud speech used an unscoped or local fallback path")

    async def synthesize_scoped(self, text, *, content_scope="local_only", current=None):
        self.calls.append((text, content_scope))
        try:
            if (
                not scope_allowed(self.info, content_scope)
                or current is None
                or not await current()
            ):
                return
            self.exports.append(text)
            self.entered.set()
            await self.release.wait()
            if await current():
                yield {**FRAME, "event_id": "untrusted", "generation_epoch": 999}
        finally:
            self.closed.set()

    async def close(self):
        pass


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    runtime = ConverseRuntime(
        LocalJournal(store, "converse"), ScriptedProvider(["Public synthetic reply."] * 3)
    )
    yield SimpleNamespace(store=store, session=session, runtime=runtime)
    store.close()


async def reply(env, scope="public_synthetic", key="input"):
    session = env.session
    result = await env.runtime.turn(
        ConverseRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            text="Synthetic input " + key,
            content_scope=scope,
            idempotency_key=key,
        )
    )
    return next(item["entry"] for item in result["events"] if item["type"] == "text")


def body(entry):
    return {
        "event_id": entry["event_id"],
        "generation_epoch": entry["payload"]["generation_epoch"],
    }


def client(env, speech):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env.runtime, AUTH, speech)),
        base_url="http://synthetic",
        headers={"Authorization": "Bearer " + AUTH.tokens["owner"]},
    )


@pytest.mark.parametrize(
    "scope,status",
    [("local_only", 403), ("public_synthetic", 200), ("private_cloud_authorized", 200)],
)
async def test_cloud_tts_uses_persisted_reply_scope_and_real_pcm(env, scope, status):
    speech = ScopedTTS()
    entry = await reply(env, scope)
    async with client(env, speech) as http:
        # Client fields cannot upgrade the persisted reply's grant.
        response = await http.post(
            "/v1/converse/speech", json={**body(entry), "content_scope": "public_synthetic"}
        )
    assert response.status_code == status
    assert bool(speech.exports) == (status == 200)
    if status == 403:
        assert speech.calls == []
    else:
        assert speech.calls == [(entry["payload"]["text"], scope)]
        frame = json.loads(response.text)
        assert base64.b64decode(frame["audio"]) == PCM
        assert frame["samples"] == 3 and frame["sample_rate"] == 24000
        assert frame["chunk_index"] == 0
        assert frame["event_id"] == entry["event_id"]
        assert frame["generation_epoch"] == entry["payload"]["generation_epoch"]
    assert not env.runtime._speech
    await env.runtime.close()


@pytest.mark.parametrize("scope,status", [("local_only", 403), ("public_synthetic", 200)])
async def test_native_route_real_adapter_and_persistent_ledger(env, tmp_path, scope, status):
    requests = []
    pcm = b"\0\0\x01\0" * 1500

    class PCMStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield pcm[:1]
            yield pcm[1:]

        async def aclose(self):
            pass

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={
                "content-type": "audio/pcm;rate=24000;channels=1",
                "x-generation-id": "gen-tts-synthetic-native",
            },
            stream=PCMStream(),
        )

    ledger = tmp_path / "speech-ledger.json"
    speech = OpenRouterTTSClient(
        allow_cloud=True,
        api_key="synthetic-openrouter-key",
        budget_path=ledger,
        transport=httpx.MockTransport(handle),
    )
    entry = await reply(env, scope)
    try:
        async with client(env, speech) as http:
            response = await http.post("/v1/converse/speech", json=body(entry))
        assert response.status_code == status
        if status == 403:
            assert requests == [] and not ledger.exists()
        else:
            assert len(requests) == 1
            assert json.loads(requests[0].content)["input"] == entry["payload"]["text"]
            frames = [json.loads(line) for line in response.text.splitlines()]
            assert b"".join(base64.b64decode(frame["audio"]) for frame in frames) == pcm
            assert [frame["samples"] for frame in frames] == [2400, 600]
            assert all(frame["event_id"] == entry["event_id"] for frame in frames)
            calls = json.loads(ledger.read_text())["calls"]
            assert len(calls) == 1 and calls[0]["state"] == "completed"
            assert calls[0]["generation_id"] == "gen-tts-synthetic-native"
        assert not env.runtime._speech
    finally:
        await speech.close()
        await env.runtime.close()


@pytest.mark.parametrize("floor", ["missing", "history", "persona"])
async def test_cloud_tts_cannot_export_restricted_reply_derivatives(env, floor):
    if floor == "history":
        await reply(env, "local_only", "restricted-history")
    elif floor == "persona":
        env.runtime.personality_scope = "local_only"
    entry = await reply(env)
    if floor == "missing":
        # Legacy persisted output does not gain a grant from a current request.
        original = env.runtime.journal.entry

        async def legacy(event_id):
            value = await original(event_id)
            if event_id == entry["event_id"]:
                value.payload.pop("content_scope", None)
            return value

        env.runtime.journal.entry = legacy
    else:
        assert entry["payload"]["content_scope"] == "local_only"
    speech = ScopedTTS()
    async with client(env, speech) as http:
        response = await http.post("/v1/converse/speech", json=body(entry))
    assert response.status_code == 403 and speech.calls == speech.exports == []
    await env.runtime.close()


@pytest.mark.parametrize(
    "change",
    [
        "backend",
        "local_to_cloud",
        "cloud_to_local",
        "metadata",
        "owner_off_on",
        "session",
        "generation",
    ],
)
async def test_cloud_tts_rechecks_native_binding_after_producer_validation(env, change):
    entry = await reply(env)
    selected, replacement = ScopedTTS(), ScopedTTS()
    speech = CloudSTTLocalTTS(None, SimpleNamespace(), tts=selected)
    local = SimpleNamespace(
        profile="Kokoro 82M",
        synthesize=lambda text: pytest.fail("replacement local TTS was invoked"),
    )
    if change == "local_to_cloud":
        speech.tts = local
    entered, release = asyncio.Event(), asyncio.Event()
    original, validations = env.runtime.validate_speech, 0

    async def held(*args):
        nonlocal validations
        validations += 1
        if validations == 2:
            entered.set()
            await release.wait()
        return await original(*args)

    env.runtime.validate_speech = held
    async with client(env, speech) as http:
        pending = asyncio.create_task(http.post("/v1/converse/speech", json=body(entry)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if change == "backend":
                speech.tts = replacement
            elif change == "local_to_cloud":
                speech.tts = selected
            elif change == "cloud_to_local":
                speech.tts = local
            elif change == "metadata":
                selected.info = selected.info.model_copy(update={"model": "replacement"})
            elif change == "owner_off_on":
                await env.runtime.set_enabled(False, owner=True)
                await env.runtime.set_enabled(True, owner=True)
            elif change == "session":
                env.store.reset(env.session.session_id)
            else:
                (await env.runtime._epoch(env.session.session_id)).begin()
            release.set()
            await asyncio.wait_for(pending, 1)
            assert selected.exports == replacement.exports == []
            assert not env.runtime._speech
        finally:
            release.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    await env.runtime.close()


@pytest.mark.parametrize("change", ["local_only", "off", "phase", "revision", "path"])
async def test_cloud_tts_resource_policy_fences_late_pcm(env, tmp_path, change):
    state = ResourceState(tmp_path / "policy" / "state.json", profile="configured_hybrid")
    state.save(phase="ready")
    entry, selected = await reply(env), ScopedTTS(blocked=True)
    speech = CloudSTTLocalTTS(None, SimpleNamespace(), tts=selected, resource_state=state.path)
    async with client(env, speech) as http:
        pending = asyncio.create_task(http.post("/v1/converse/speech", json=body(entry)))
        try:
            await asyncio.wait_for(selected.entered.wait(), 1)
            if change == "path":
                speech.resource_state = tmp_path / "another-policy.json"
            else:
                changes = {
                    "local_only": {"profile": "local_only"},
                    "off": {"desired_converse": False},
                    "phase": {"phase": "off"},
                    "revision": {},
                }
                state.save(**changes[change])
            selected.release.set()
            if change in {"local_only", "off", "phase"}:
                with pytest.raises(SpeechProviderError):
                    await asyncio.wait_for(pending, 1)
            else:
                response = await asyncio.wait_for(pending, 1)
                assert response.status_code == 200 and response.content == b""
            assert selected.exports == [entry["payload"]["text"]]
            assert not env.runtime._speech
        finally:
            selected.release.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            state.close()
    await env.runtime.close()


async def test_cloud_tts_resource_policy_rechecks_after_awaited_native_validation(env, tmp_path):
    state = ResourceState(tmp_path / "policy" / "state.json", profile="configured_hybrid")
    state.save(phase="ready")
    entry, selected = await reply(env), ScopedTTS()
    speech = CloudSTTLocalTTS(None, SimpleNamespace(), tts=selected, resource_state=state.path)
    original, validations = env.runtime.validate_speech, 0

    async def changed(*args):
        nonlocal validations
        validations += 1
        value = await original(*args)
        if validations == 3:
            state.save()
        return value

    env.runtime.validate_speech = changed
    try:
        async with client(env, speech) as http:
            response = await http.post("/v1/converse/speech", json=body(entry))
        assert response.status_code == 200 and response.content == b""
        assert selected.calls and selected.exports == []
        assert not env.runtime._speech
    finally:
        state.close()
        await env.runtime.close()
