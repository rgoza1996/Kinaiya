"""Exercise the actual authenticated speech route; no model/audio dependency."""

import asyncio

import httpx
import pytest

from packages.contracts.models import ConverseRequest, SessionRequest
from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.runtime import ConverseRuntime
from services.session.store import LocalJournal, Store


@pytest.mark.parametrize(
    ("original", "spoken"),
    [
        ("Hello🙂friend.", "Hello friend."),
        ("😊👩🏽‍💻🇵🇭", ""),
        ("😊\ufe0f\u200d", ""),
        ("1\ufe0f\u20e3\u200d", ""),
        ("😊!", ""),
        ("Hello ℹ️ friend.", "Hello friend."),
    ],
)
async def test_native_speech_cleans_only_synthesis_and_releases_emoji_only_reply(
    tmp_path, original, spoken
):
    store = Store(tmp_path / "journal.sqlite")
    runtime = ConverseRuntime(LocalJournal(store, "converse"), ScriptedProvider([original]))
    auth = Auth({"owner": "o" * 32})
    calls = []

    class Speech:
        async def synthesize(self, text):
            calls.append(text)
            yield {"audio_base64": "AAA=", "sample_rate": 24000, "index": 0}

    try:
        session = store.create_session(SessionRequest(channel="converse"))
        result = await runtime.turn(
            ConverseRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                text="Public synthetic input.",
                content_scope="public_synthetic",
                idempotency_key="public-input",
            )
        )
        event = next(e["entry"] for e in result["events"] if e["type"] == "text")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime, auth, Speech())),
            base_url="http://test",
            headers={"Authorization": "Bearer " + auth.tokens["owner"]},
        ) as client:
            response = await asyncio.wait_for(
                client.post(
                    "/v1/converse/speech",
                    json={
                        "event_id": event["event_id"],
                        "generation_epoch": event["payload"]["generation_epoch"],
                    },
                ),
                2,
            )
            assert response.status_code == 200
            assert calls == ([spoken] if spoken else [])
            assert bool(response.content) == bool(spoken)
            assert store.entry(event["event_id"]).payload["text"] == original
            assert not any((await client.get("/health")).json()["speech_operations"].values())
            calls.clear()
            await runtime.interrupt(session.session_id)
            stale = await client.post(
                "/v1/converse/speech",
                json={
                    "event_id": event["event_id"],
                    "generation_epoch": event["payload"]["generation_epoch"],
                },
            )
            assert stale.status_code == 409
            assert calls == []
    finally:
        await runtime.close()
        store.close()
