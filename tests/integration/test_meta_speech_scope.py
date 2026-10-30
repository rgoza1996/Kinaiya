"""Native owner/disclosure/epoch gates before synthetic remote STT uploads."""

import base64
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.models import SessionRequest
from packages.providers import meta_speech
from packages.providers.meta_speech import CloudSTTLocalTTS, MetaSTTClient
from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.session.store import LocalJournal, Store

TOKEN = "public-synthetic-owner-token-0001"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    runtime = ConverseRuntime(LocalJournal(store, "converse"), ScriptedProvider([]))
    windows = WindowConverseController(runtime)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "sessionId": "synthetic",
                "transcript": "Public words.",
                "audioDurationMs": 1000,
                "turns": [],
            },
        )

    stt = MetaSTTClient(
        api_key="synthetic-cloud-key", allow_cloud=True, transport=httpx.MockTransport(respond)
    )
    local = SimpleNamespace(profile="Kokoro 82M")
    speech = CloudSTTLocalTTS(local, stt)
    monkeypatch.setattr(meta_speech, "normalize_wav", lambda data: b"synthetic normalized WAV")
    app = create_app(runtime, Auth({"owner": TOKEN}), speech, windows)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://local",
        headers={"authorization": f"Bearer {TOKEN}"},
    ) as client:
        yield SimpleNamespace(
            store=store,
            session=session,
            runtime=runtime,
            windows=windows,
            calls=calls,
            client=client,
            stt=stt,
        )
    await windows.close()
    await runtime.close()
    await stt.close()
    store.close()


@pytest.mark.parametrize(
    "scope,status",
    [
        (None, 409),
        ("local_only", 409),
        ("private_cloud_authorized", 409),
        ("public_synthetic", 200),
    ],
)
async def test_native_multipart_scope_is_authoritative(env, scope, status):
    response = await env.client.post(
        "/v1/converse/transcribe",
        data={} if scope is None else {"content_scope": scope},
        files={"file": ("recording.webm", b"public fixture")},
    )
    assert response.status_code == status
    assert len(env.calls) == (1 if status == 200 else 0)
    if status == 200:
        assert response.json()["content_scope"] == "public_synthetic"
        assert response.json()["text"] == "Public words."


async def test_wrong_owner_denied_before_decoding(env, monkeypatch):
    monkeypatch.setattr(
        meta_speech, "normalize_wav", lambda data: pytest.fail("unauthenticated decode")
    )
    response = await env.client.post(
        "/v1/converse/transcribe",
        headers={"authorization": "Bearer invalid"},
        data={"content_scope": "public_synthetic"},
        files={"file": ("recording.webm", b"fixture")},
    )
    assert response.status_code == 401 and not env.calls


def window(env):
    return {
        "session_id": env.session.session_id,
        "thread_id": env.session.thread_id,
        "epoch": env.session.epoch,
        "utterance_id": "public-utterance",
        "window_index": 0,
        "revision": 1,
        "text": "",
        "start_ms": 0,
        "end_ms": 1000,
        "user_speaking": False,
        "content_scope": "public_synthetic",
        "audio_base64": base64.b64encode(b"fixture").decode(),
    }


@pytest.mark.parametrize("bad", ["epoch", "thread", "scope"])
async def test_obsolete_window_never_uploads(env, bad):
    value = window(env)
    if bad == "epoch":
        value["epoch"] += 1
    elif bad == "thread":
        value["thread_id"] = "another-thread"
    else:
        value["content_scope"] = "local_only"
    response = await env.client.post("/v1/converse/audio/windows", json=value)
    assert response.status_code == 409 and not env.calls
    assert not env.store.read(kinds=["conversation.window"])


async def test_owner_off_on_during_decode_cannot_export_old_recording(env, monkeypatch):
    original = meta_speech._owned_decode

    async def decode(data):
        await env.runtime.set_enabled(False, owner=True)
        await env.runtime.set_enabled(True, owner=True)
        return await original(data)

    monkeypatch.setattr(meta_speech, "_owned_decode", decode)
    response = await env.client.post("/v1/converse/audio/windows", json=window(env))
    assert response.status_code == 409 and not env.calls
    assert not env.store.read(kinds=["conversation.window"])


async def test_successful_window_replay_does_not_upload_again(env):
    value = window(env)
    first = await env.client.post("/v1/converse/audio/windows", json=value)
    assert first.status_code == 200
    replay = await env.client.post("/v1/converse/audio/windows", json=value)
    assert replay.status_code == 200 and replay.json()["replay"] is True
    assert len(env.calls) == 1
    assert len(env.store.read(kinds=["conversation.window"])) == 1


async def test_native_stt_replacement_during_window_validation_invalidates_capture(env):
    from services.converse.api import _cloud_stt_current

    speech = SimpleNamespace(stt_info=env.stt.info, stt=env.stt)

    async def validate():
        speech.stt = object()
        return True

    current = _cloud_stt_current(env.runtime, speech, "public_synthetic", validate)
    assert await current() is False
