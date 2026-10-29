"""Actual typed/legacy-voice HTTP inputs respect the selected provider's scope."""

import json

import httpx
import pytest

from packages.contracts.models import ProviderInfo, SessionRequest
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.runtime import ConverseRuntime
from services.session.store import LocalJournal, Store


@pytest.mark.parametrize("input_mode", ["text", "voice"])
@pytest.mark.parametrize("target", ["local", "ordinary_cloud", "contributor"])
@pytest.mark.parametrize(
    "scope", [None, "local_only", "public_synthetic", "private_cloud_authorized"]
)
async def test_native_current_input_scope_matrix(tmp_path, input_mode, target, scope):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest())
    calls = []

    class Provider:
        info = ProviderInfo(
            provider="meta" if target == "contributor" else "synthetic",
            model="contributor-fixture" if target == "contributor" else target,
            local=target == "local",
        )

        async def stream(self, messages):
            calls.append(messages)
            yield "Synthetic response."

    runtime = ConverseRuntime(LocalJournal(store, "converse"), Provider())
    auth = Auth({"owner": "public-synthetic-owner-token-0000000000"})
    text = "SYNTHETIC_INPUT_SENTINEL_95"
    body = dict(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        text=text,
        input_mode=input_mode,
        idempotency_key="current",
    )
    if scope is not None:
        body["content_scope"] = scope
    effective = scope or "local_only"
    allowed = (
        target == "local"
        or effective == "public_synthetic"
        or (target == "ordinary_cloud" and effective == "private_cloud_authorized")
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime, auth)),
            base_url="http://synthetic",
            headers={"Authorization": "Bearer " + auth.tokens["owner"]},
        ) as client:
            response = await client.post("/v1/converse/turns", json=body)
        if allowed:
            assert response.status_code == 200
            assert len(calls) == 1 and text in json.dumps(calls)
            assert all(
                e.payload["content_scope"] == effective
                for e in store.read(kinds=["conversation.user", "conversation.converse"])
            )
        else:
            assert 400 <= response.status_code < 500
            assert text not in response.text
            assert calls == []
            assert not store.read(kinds=["conversation.user", "conversation.converse"])
    finally:
        await runtime.close()
        store.close()
