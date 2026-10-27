"""Actual authenticated HTTP window boundaries; STT/model responses are synthetic."""

import base64
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.models import SessionRequest, SpeechWindow
from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.session.api import create_app as session_app
from services.session.store import LocalJournal, Store


@pytest.fixture
async def env(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    auth = Auth(
        {
            "owner": "synthetic-owner-token-00000000",
            "converse": "synthetic-converse-token-00000",
            "agent": "synthetic-agent-token-00000000",
        }
    )

    class Speech:
        calls = 0

        async def transcribe(self, data, filename):
            self.calls += 1
            assert data == b"PUBLIC_SYNTHETIC_AUDIO" and filename == "window.wav"
            return {"text": "Public synthetic transcript"}

    speech = Speech()
    runtime = ConverseRuntime(LocalJournal(store, "converse"), ScriptedProvider([]))
    controller = WindowConverseController(runtime)
    app = create_app(runtime, auth, speech, controller)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={"Authorization": "Bearer " + auth.tokens["owner"]},
    ) as http:
        yield SimpleNamespace(
            store=store, session=session, auth=auth, speech=speech, http=http, controller=controller
        )
    await controller.close()
    store.close()


def audio_window(env, **fields):
    s = env.session
    body = SpeechWindow(
        session_id=s.session_id,
        thread_id=s.thread_id,
        epoch=s.epoch,
        utterance_id="synthetic",
        window_index=0,
        text="UNTRUSTED_REQUEST_TEXT",
        start_ms=0,
        end_ms=1000,
        user_speaking=True,
        content_scope="public_synthetic",
    ).model_dump(mode="json")
    body["audio_base64"] = base64.b64encode(b"PUBLIC_SYNTHETIC_AUDIO").decode()
    return {**body, **fields}


async def test_audio_window_auth_replay_identity_and_actual_stt_text(env):
    body = audio_window(env)
    denied = await env.http.post(
        "/v1/converse/audio/windows",
        json=body,
        headers={"Authorization": "Bearer " + env.auth.tokens["agent"]},
    )
    assert denied.status_code == 403 and env.speech.calls == 0
    first = await env.http.post("/v1/converse/audio/windows", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["entry"]["payload"]["text"] == "Public synthetic transcript"
    assert first.json()["retained_audio"] is False
    replay = await env.http.post("/v1/converse/audio/windows", json=body)
    assert replay.status_code == 200 and replay.json()["replay"]
    assert env.speech.calls == 1
    conflict = await env.http.post(
        "/v1/converse/audio/windows", json=audio_window(env, end_ms=1200)
    )
    assert conflict.status_code == 409 and env.speech.calls == 1
    corrected = await env.http.post(
        "/v1/converse/audio/windows", json=audio_window(env, revision=2)
    )
    assert corrected.status_code == 200 and env.speech.calls == 2
    assert env.store.resolve(first.json()["entry"]["event_id"]).payload["revision"] == 2


@pytest.mark.parametrize(
    "fields,status",
    [
        ({"audio_base64": "not base64!"}, 400),
        ({"thread_id": "wrong"}, 409),
        ({"session_id": "unknown"}, 409),
        ({"epoch": 2}, 409),
        ({"revision": 3}, 409),
        ({"user_speaking": "false"}, 422),
        ({"start_ms": 2000, "end_ms": 1000}, 422),
    ],
)
async def test_invalid_audio_window_rejected_before_stt(env, fields, status):
    response = await env.http.post("/v1/converse/audio/windows", json=audio_window(env, **fields))
    assert response.status_code == status, response.text
    assert env.speech.calls == 0


async def test_journal_http_window_queries_and_auth(env):
    source = await env.controller.ingest(
        SpeechWindow.model_validate(
            {k: v for k, v in audio_window(env).items() if k != "audio_base64"}
        )
    )
    client = JournalClient(
        "http://fixture",
        env.auth.tokens["converse"],
        transport=httpx.ASGITransport(app=session_app(env.store, env.auth)),
    )
    try:
        latest = await client.latest_window(source.session_id, 1, "synthetic")
        assert latest.event_id == source.event_id
        assert await client.window_control(source.session_id, 1) is None
        assert await client.window_decision(source.event_id) is None
        assert await client.window_response(source.session_id, 1, "synthetic") is None
        assert await client.window_dispatches(session_id=source.session_id, pending_only=True) == []
    finally:
        await client.close()
