"""Native Converse disclosure and exact count/generation compilation, synthetic HTTP."""

import json

import httpx
import pytest

from packages.contracts.models import AppendRequest, ConverseRequest, SessionRequest
from packages.providers.text import MetaResponsesProvider
from services.converse.runtime import ConverseRuntime
from services.session.store import LocalJournal, Store


@pytest.mark.parametrize("scope", ["local_only", "private_cloud_authorized", "public_synthetic"])
async def test_native_disclosure_precedes_count_and_exact_compilation(tmp_path, scope):
    calls = []

    def respond(request):
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 123}
            )
        return httpx.Response(
            200,
            text='data: {"type":"response.output_text.delta","delta":"Hello there."}\n\ndata: {"type":"response.completed"}\n\n',
        )

    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload={
                "text": "private historical marker",
                "final": True,
                "content_scope": "local_only",
            },
            idempotency_key="private",
        ),
        "owner",
    )
    provider = MetaResponsesProvider(
        api_key="synthetic-meta-key",
        allow_cloud=True,
        reasoning_effort="minimal",
        context_tokens=4096,
        max_tokens=128,
        transport=httpx.MockTransport(respond),
    )
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider, context_budgeted=True)
    request = ConverseRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        text="Public synthetic current request.",
        content_scope=scope,
        idempotency_key="current",
    )
    try:
        if scope != "public_synthetic":
            with pytest.raises(ValueError):
                await runtime.turn(request)
            assert not calls
        else:
            await runtime.turn(request)
            counts = [body for path, body in calls if path.endswith("input_tokens")]
            generations = [body for path, body in calls if path == "/v1/responses"]
            assert len(generations) == 1 and len(counts) >= 2
            assert counts[-1] == generations[0]
            assert all("private historical marker" not in json.dumps(body) for _, body in calls)
            assert generations[0]["reasoning"] == {"effort": "minimal"}
    finally:
        await runtime.close()
        await provider.close()
        store.close()


@pytest.mark.parametrize("change", ["output", "model", "effort", "headers"])
async def test_native_recheck_after_final_count_cannot_change_generation(tmp_path, change):
    calls = []

    def respond(request):
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("input_tokens"):
            # Fits with the counted 128 output tokens, but not with 2048.
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 3800}
            )
        return httpx.Response(
            200,
            text='data: {"type":"response.output_text.delta","delta":"Public reply."}\n\n'
            'data: {"type":"response.completed"}\n\n',
        )

    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    journal = LocalJournal(store, "converse")
    provider = MetaResponsesProvider(
        api_key="synthetic-meta-key",
        allow_cloud=True,
        reasoning_effort="minimal",
        context_tokens=4096,
        max_tokens=128,
        transport=httpx.MockTransport(respond),
    )
    native_session, changed = journal.session, False

    async def validate_session(*args, **kwargs):
        nonlocal changed
        # Native current-source validation awaits the journal after the final
        # successful count. Mutations here used to bypass all count checks.
        if sum(path.endswith("input_tokens") for path, _ in calls) >= 3 and not changed:
            if change == "output":
                provider.max_tokens = 2048
            elif change == "model":
                provider.model = "replacement"
            elif change == "effort":
                provider.effort = "low"
            else:
                provider.http.headers["authorization"] = "Bearer replacement-synthetic-key"
            changed = True
        return await native_session(*args, **kwargs)

    journal.session = validate_session
    runtime = ConverseRuntime(journal, provider, context_budgeted=True)
    try:
        result = await runtime.turn(
            ConverseRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                text="Public synthetic current request.",
                content_scope="public_synthetic",
                idempotency_key="current",
            )
        )
        assert changed and not result["text"]
        assert any(event.get("error") == "PromptCountUnavailable" for event in result["events"])
        assert len(calls) == 3 and all(path.endswith("input_tokens") for path, _ in calls)
        assert all(body["max_output_tokens"] == 128 for _, body in calls)
    finally:
        await runtime.close()
        await provider.close()
        store.close()
