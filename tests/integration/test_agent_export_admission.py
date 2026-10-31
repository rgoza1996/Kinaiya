"""Real native source admission and HTTP boundary tests; synthetic content only."""

import asyncio
import json
import sys
import threading
from types import SimpleNamespace

import httpx
import pytest

from core.policy import ToolPolicy
from packages.contracts.agent_export import (
    MAX_AGENT_EXPORT_BYTES,
    AgentExportAdmission,
    AgentExportRequest,
)
from packages.contracts.client import JournalClient
from packages.contracts.models import AppendRequest, ProviderInfo, SessionRequest, TaskRequest
from packages.providers.text import MetaResponsesProvider
from services.agent import AgentRuntime
from services.common.auth import Auth
from services.session.api import create_app
from services.session.store import JournalError, LocalJournal, Store


def source(store, session, *, kind="conversation.user", scope="public_synthetic", key="source"):
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind=kind,
            payload={
                "text": "PUBLIC_SYNTHETIC_SOURCE",
                "final": True,
                "revision": 1,
                "content_scope": scope,
            },
            idempotency_key=key,
        ),
        "converse" if kind == "conversation.converse" else "owner",
    )


def revise(store, session, original, *, revision=2, scope="local_only"):
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.revision",
            payload={
                "target_event_id": original.event_id,
                "text": "PUBLIC_SYNTHETIC_SOURCE",
                "revision": revision,
                "content_scope": scope,
            },
            idempotency_key=f"revision-{revision}",
        ),
        "owner",
    )


def export_request(session, original=None, **changes):
    value = dict(
        task_id="synthetic-task",
        objective_revision=1,
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        source_event_ids=[original.event_id] if original else [],
        source_revisions={original.event_id: 1} if original else {},
        content_scope="public_synthetic",
        provider=ProviderInfo(provider="meta", model="synthetic", local=False),
        request_sha256="ab" * 32,
        purpose="complete_count",
        request_nonce="synthetic-nonce",
    )
    value.update(changes)
    return AgentExportRequest(**value)


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "synthetic.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    original = source(store, session)
    auth = Auth(
        {role: role[0] * 32 for role in ("agent", "owner", "converse", "decisions", "resources")}
    )
    app = create_app(store, auth)
    yield SimpleNamespace(store=store, session=session, source=original, app=app, auth=auth)
    store.close()


def test_atomic_metadata_receipt_does_not_write_or_store_prompt(env):
    before = env.store.read()
    request = export_request(env.session, env.source)
    result = env.store.agent_export_admission(request)
    assert type(result) is AgentExportAdmission
    assert result.validate_request(request).journal_sequence == env.source.sequence
    assert result.effective_scope == "public_synthetic"
    assert env.store.read() == before
    assert "PUBLIC_SYNTHETIC_SOURCE" not in result.model_dump_json()


@pytest.mark.parametrize("role", ["owner", "converse", "decisions", "resources"])
async def test_local_adapter_requires_agent_role(env, role):
    with pytest.raises(PermissionError):
        await LocalJournal(env.store, role).agent_export_admission(export_request(env.session))
    result = await LocalJournal(env.store, "agent").agent_export_admission(
        export_request(env.session)
    )
    assert result.journal_sequence == env.source.sequence


@pytest.mark.parametrize("role", [None, "owner", "converse", "decisions", "resources", "agent"])
async def test_endpoint_is_agent_only_and_strict(env, role):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://fixture"
    ) as client:
        headers = {"Authorization": "Bearer " + role[0] * 32} if role else {}
        result = await client.post(
            "/v1/transcript/agent-export-admission",
            json=export_request(env.session, env.source).model_dump(mode="json"),
            headers=headers,
        )
        assert result.status_code == (200 if role == "agent" else 401 if role is None else 403)


async def test_http_client_roundtrip_echo_is_exact_and_empty_sources_supported(env):
    client = JournalClient("http://fixture", "a" * 32, transport=httpx.ASGITransport(app=env.app))
    try:
        for request in (export_request(env.session, env.source), export_request(env.session)):
            admitted = await client.agent_export_admission(request)
            assert admitted.validate_request(request).binding_sha256 == request.binding_digest()
            assert admitted.request_nonce == request.request_nonce
    finally:
        await client.close()


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "revision",
        "other-session",
        "other-thread",
        "epoch",
        "assistant",
        "not-final",
        "origin",
    ],
)
def test_missing_stale_or_unauthenticated_sources_refuse(env, case):
    request = export_request(env.session, env.source)
    if case == "missing":
        request = export_request(
            env.session, source_event_ids=["absent"], source_revisions={"absent": 1}
        )
    elif case == "revision":
        revise(env.store, env.session, env.source, scope="public_synthetic")
    elif case == "other-session":
        other = env.store.create_session(SessionRequest(channel="agent"))
        request = export_request(other, env.source)
    elif case == "other-thread":
        request = export_request(env.session, env.source, thread_id="wrong")
    elif case == "epoch":
        env.store.reset(env.session.session_id)
    else:
        entry = env.source
        if case == "assistant":
            entry = source(env.store, env.session, kind="conversation.converse", key="assistant")
            request = export_request(env.session, entry)
        else:
            data = entry.model_dump(mode="json")
            if case == "not-final":
                data["payload"]["final"] = False
            else:
                data["origin"] = "agent-supervisor"
            with env.store.db:
                env.store.db.execute(
                    "UPDATE entries SET value=? WHERE event_id=?",
                    (json.dumps(data), entry.event_id),
                )
    with pytest.raises(JournalError, match="^Agent export source authority is unavailable$"):
        env.store.agent_export_admission(request)


def test_sticky_user_revision_floor_and_private_provider_policy(env):
    revise(env.store, env.session, env.source, scope="local_only")
    revise(env.store, env.session, env.source, revision=3, scope="public_synthetic")
    request = export_request(env.session, env.source, source_revisions={env.source.event_id: 3})
    with pytest.raises(PermissionError):
        env.store.agent_export_admission(request)
    local = request.model_copy(
        update={"provider": ProviderInfo(provider="fixture", model="local", local=True)}
    )
    result = env.store.agent_export_admission(local)
    assert result.effective_scope == "local_only"


@pytest.mark.parametrize(
    "raw,status",
    [
        (b'{"task_id":"a","task_id":"b"}', 422),
        (b'{"a":NaN}', 422),
        (b"\xff", 422),
        (b" " * (MAX_AGENT_EXPORT_BYTES + 1), 413),
    ],
)
async def test_endpoint_bounded_closed_json(env, raw, status):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app),
        base_url="http://fixture",
        headers={"Authorization": "Bearer " + "a" * 32},
    ) as client:
        response = await client.post(
            "/v1/transcript/agent-export-admission",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == status
        assert "PUBLIC_SYNTHETIC_SOURCE" not in response.text


async def test_native_lock_linearizes_before_concurrent_revision(env, monkeypatch):
    entered, release, writer_attempted, written = (threading.Event() for _ in range(4))
    native_resolve = env.store.resolve

    def held_resolve(event_id):
        result = native_resolve(event_id)
        entered.set()
        assert release.wait(2)
        return result

    monkeypatch.setattr(env.store, "resolve", held_resolve)
    request = export_request(env.session, env.source)
    admitted = asyncio.create_task(asyncio.to_thread(env.store.agent_export_admission, request))
    assert await asyncio.to_thread(entered.wait, 2)

    def writer():
        writer_attempted.set()
        revise(env.store, env.session, env.source)
        written.set()

    writing = asyncio.create_task(asyncio.to_thread(writer))
    try:
        assert await asyncio.to_thread(writer_attempted.wait, 2)
        assert not await asyncio.to_thread(written.wait, 0.02)
    finally:
        release.set()
    receipt = await admitted
    await writing
    assert receipt.source_revisions == {env.source.event_id: 1}
    assert receipt.journal_sequence == env.source.sequence
    assert written.is_set()
    with pytest.raises(JournalError):
        env.store.agent_export_admission(request)


@pytest.mark.parametrize("boundary", ["last-session", "count-hook", "generation-hook"])
async def test_source_revocation_before_native_admission_prevents_actual_export(
    env, tmp_path, boundary
):
    delegate = JournalClient("http://native", "a" * 32, transport=httpx.ASGITransport(app=env.app))
    changed = False
    calls, admission_receipts = [], []

    def revoke():
        nonlocal changed
        if not changed:
            changed = True
            revise(env.store, env.session, env.source)

    class GatedJournal:
        def __getattr__(self, name):
            return getattr(delegate, name)

        async def session(self, session_id):
            caller = sys._getframe(1).f_code.co_name
            result = await delegate.session(session_id)
            if boundary == "last-session" and caller == "check":
                revoke()
            return result

        async def agent_export_admission(self, request):
            result = await delegate.agent_export_admission(request)
            admission_receipts.append(result)
            return result

    async def hook(request):
        if boundary == "count-hook" and request.url.path.endswith("/input_tokens"):
            revoke()
        elif boundary == "generation-hook" and request.url.path == "/v1/responses":
            revoke()

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 123}
            )
        pytest.fail("Revoked generation reached actual transport")

    provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="public-synthetic-fixture",
        allow_cloud=True,
        structured_actions=True,
        context_tokens=32768,
        transport=httpx.MockTransport(respond),
    )
    provider.http.event_hooks["request"].append(hook)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = AgentRuntime(GatedJournal(), provider, workspace, ToolPolicy(workspace))
    try:
        task = await runtime.submit(
            TaskRequest(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=env.session.epoch,
                objective="PUBLIC_SYNTHETIC_SOURCE",
                source_event_ids=[env.source.event_id],
                source_revision=1,
                content_scope="public_synthetic",
                idempotency_key="task",
            )
        )
        final = await runtime.wait(task.task_id, timeout=3)
        assert changed and final.state == "failed" and final.content_scope == "local_only"
        assert calls == (["/v1/responses/input_tokens"] if boundary == "generation-hook" else [])
        assert len(admission_receipts) == (1 if boundary == "generation-hook" else 0)
        assert final.error == (
            "provider error: Native Agent export admission was refused"
            if boundary == "generation-hook"
            else "provider error: Agent history context is unavailable"
        )
    finally:
        await runtime.close()
        await provider.close()
        await delegate.close()


def window(
    store,
    session,
    *,
    revision=1,
    scope="public_synthetic",
    utterance="spoken",
    index=0,
    text="PUBLIC_SYNTHETIC_WINDOW",
):
    from packages.contracts.models import SpeechWindow

    body = SpeechWindow(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        utterance_id=utterance,
        window_index=index,
        revision=revision,
        text=text,
        start_ms=0,
        end_ms=1000,
        user_speaking=False,
        capture_ended=True,
        content_scope=scope,
    )
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.window",
            payload=body.model_dump(mode="json"),
            idempotency_key=f"window-{utterance}-{index}-{revision}",
        ),
        "owner",
    )


async def test_same_slot_window_revision_floor_is_sticky_in_async_and_atomic_paths(env):
    from packages.providers.disclosure import source_scope

    session = env.store.create_session(SessionRequest(channel="converse"))
    first = window(env.store, session)
    window(env.store, session, revision=2, scope="local_only")
    last = window(env.store, session, revision=3)
    journal = LocalJournal(env.store, "agent")
    # Both an original and a later source pointer must include the full chain.
    for selected in (first, last):
        assert await source_scope(journal, selected.event_id) == "local_only"
        request = export_request(session, selected, source_revisions={selected.event_id: 3})
        with pytest.raises(PermissionError):
            env.store.agent_export_admission(request)
        local = request.model_copy(
            update={"provider": ProviderInfo(provider="fixture", model="local", local=True)}
        )
        assert env.store.agent_export_admission(local).effective_scope == "local_only"


async def test_other_window_slots_do_not_taint_or_exhaust_selected_chain(env):
    from packages.providers.disclosure import source_scope

    session = env.store.create_session(SessionRequest(channel="converse"))
    for number in range(205):
        window(env.store, session, scope="local_only", utterance=f"other_{number}")
    first = window(env.store, session, utterance="chosen")
    window(env.store, session, revision=2, utterance="chosen")
    journal = JournalClient("http://native", "a" * 32, transport=httpx.ASGITransport(app=env.app))
    try:
        assert await source_scope(journal, first.event_id) == "public_synthetic"
        request = export_request(session, first, source_revisions={first.event_id: 2})
        assert (await journal.agent_export_admission(request)).effective_scope == "public_synthetic"
    finally:
        await journal.close()


@pytest.mark.parametrize("defect", ["missing", "origin", "capped", "unknown_scope"])
async def test_unverified_window_chain_refuses_even_local_provider(env, defect):
    from packages.providers.disclosure import source_scope

    session = env.store.create_session(SessionRequest(channel="converse"))
    first = window(env.store, session)
    middle = window(env.store, session, revision=2)
    window(env.store, session, revision=3)
    revision = 3
    if defect == "capped":
        for revision in range(4, 201):
            window(env.store, session, revision=revision)
    elif defect == "missing":
        with env.store.db:
            env.store.db.execute("DELETE FROM entries WHERE event_id=?", (middle.event_id,))
    else:
        raw = middle.model_dump(mode="json")
        if defect == "origin":
            raw["origin"] = "agent-supervisor"
        else:
            raw["payload"]["content_scope"] = "unknown"
        with env.store.db:
            env.store.db.execute(
                "UPDATE entries SET value=? WHERE event_id=?", (json.dumps(raw), middle.event_id)
            )
    with pytest.raises(ValueError, match="^Native source chain is unavailable$"):
        await source_scope(LocalJournal(env.store, "agent"), first.event_id)
    request = export_request(
        session,
        first,
        source_revisions={first.event_id: revision},
        provider=ProviderInfo(provider="fixture", model="local", local=True),
    )
    with pytest.raises(JournalError):
        env.store.agent_export_admission(request)


@pytest.mark.parametrize("defect", ["new_text", "later_stop"])
def test_obsolete_or_interrupted_window_source_refuses(env, defect):
    from packages.contracts.models import WindowDecision

    session = env.store.create_session(SessionRequest(channel="converse"))
    first = window(env.store, session)
    newer = window(
        env.store,
        session,
        index=1,
        text="DIFFERENT" if defect == "new_text" else "PUBLIC_SYNTHETIC_WINDOW",
    )
    if defect == "later_stop":
        control = WindowDecision(
            source_event_id=newer.event_id,
            source_sequence=newer.sequence,
            source_revision=1,
            conversation="RESPOND_INTERRUPT_USER",
            agent_route="INTERRUPT_CURRENT_TASK",
            target_task_id="synthetic-task",
            expected_objective_revision=1,
        )
        env.store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="routing.window_decided",
                payload=control.model_dump(mode="json"),
                causes=[newer.event_id],
                idempotency_key="stop",
            ),
            "decisions",
        )
    with pytest.raises(JournalError):
        env.store.agent_export_admission(export_request(session, first))


@pytest.mark.parametrize(
    "changes",
    [
        {"utterance_id": "spoken"},
        {"window_index": 0},
        {"utterance_id": "spoken", "window_index": True},
        {"utterance_id": "spoken", "window_index": 1_000_001},
        {"utterance_id": "spoken", "window_index": 0, "kinds": ["conversation.user"]},
        {"utterance_id": "spoken", "window_index": 0, "thread_id": None},
        {"utterance_id": "spoken", "window_index": 0, "epoch": None},
    ],
)
def test_exact_slot_filters_require_complete_native_scope(env, changes):
    query = dict(
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
        kinds=["conversation.window"],
    )
    query.update(changes)
    with pytest.raises(JournalError):
        env.store.read(**query)


class ResponseBody(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    "defect",
    ["oversize", "nonce", "duplicate", "nonfinite", "encoding", "length", "copied_counter"],
)
async def test_http_client_refuses_bad_or_oversize_receipts_and_closes_stream(env, defect):
    request = export_request(env.session, env.source)
    response = env.store.agent_export_admission(request).model_dump(mode="json")
    headers = {"Content-Type": "application/json"}
    if defect == "oversize":
        raw = b" " * (MAX_AGENT_EXPORT_BYTES + 1)
    elif defect == "duplicate":
        raw = b'{"task_id":"one","task_id":"two"}'
    elif defect == "nonfinite":
        raw = b'{"journal_sequence":NaN}'
    else:
        if defect == "nonce":
            response["request_nonce"] = "wrong"
        elif defect == "copied_counter":
            response["journal_sequence"] = True
        elif defect == "encoding":
            headers["Content-Encoding"] = "gzip"
        elif defect == "length":
            headers["Content-Length"] = "1"
        raw = json.dumps(response).encode()
    body = ResponseBody([raw[:32], raw[32:]])
    client = JournalClient(
        "http://native",
        "synthetic-agent-token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, headers=headers, stream=body)),
    )
    try:
        with pytest.raises((ValueError, httpx.DecodingError)):
            await client.agent_export_admission(request)
        assert body.closed
    finally:
        await client.close()


@pytest.mark.parametrize(
    "extra",
    [
        "&window_index=0",
        "&utterance_id=other",
        "&epoch=1",
        "&window_index=0.0",
        "&window_index=00",
    ],
)
async def test_http_exact_slot_filters_reject_ambiguous_or_coerced_counters(env, extra):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app),
        base_url="http://fixture",
        headers={"Authorization": "Bearer " + "a" * 32},
    ) as client:
        url = (
            f"/v1/transcript?session_id={env.session.session_id}&thread_id={env.session.thread_id}"
            "&epoch=1&kinds=conversation.window&utterance_id=spoken&window_index=0"
        )
        if extra in {"&window_index=0.0", "&window_index=00"}:
            url = url.removesuffix("&window_index=0") + extra
        else:
            url += extra
        response = await client.get(url)
        assert response.status_code == 422


async def test_http_source_admission_has_an_overall_deadline_and_closes_body(env, monkeypatch):
    from packages.contracts import client as journal_client

    class StalledBody(ResponseBody):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b"unreachable"

    monkeypatch.setattr(journal_client, "AGENT_EXPORT_TIMEOUT_S", 0.01)
    body = StalledBody([])
    client = JournalClient(
        "http://native",
        "synthetic-agent-token",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"Content-Type": "application/json"}, stream=body)
        ),
    )
    try:
        with pytest.raises(TimeoutError):
            await client.agent_export_admission(export_request(env.session, env.source))
        assert body.closed
    finally:
        await client.close()
