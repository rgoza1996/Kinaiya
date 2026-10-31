"""Retrieved context remains disclosure-bound after its native read completes."""

import json
from types import SimpleNamespace

import httpx
import pytest
from test_agent_context_admission import generated
from test_agent_export_admission import export_request, revise, source, window

from core.policy import ToolPolicy
from packages.contracts.models import AppendRequest, ProviderInfo, SessionRequest, TaskRequest
from packages.providers.disclosure import (
    MAX_CONTEXT_PROVENANCE_NODES,
    ContextScopeClosure,
    context_scope,
)
from packages.providers.text import MetaResponsesProvider
from services.agent import AgentRuntime
from services.session.store import LocalJournal, Store


@pytest.fixture(name="env")
def native_context_env(tmp_path):
    store = Store(tmp_path / "synthetic.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    yield SimpleNamespace(store=store, session=session, source=source(store, session))
    store.close()


def derived_context(env, *, causes, key="derived", scope="public_synthetic"):
    return env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.converse",
            payload={
                "text": "PUBLIC_SYNTHETIC_DERIVED_ASSISTANT_CONTEXT",
                "final": True,
                "revision": 1,
                "content_scope": scope,
            },
            causes=causes,
            idempotency_key=key,
        ),
        "converse",
    )


def replace_native_row(env, entry, **changes):
    """Exercise corrupt/unavailable provenance without weakening native append."""
    raw = entry.model_dump(mode="json")
    raw.update(changes)
    with env.store.db:
        env.store.db.execute(
            "UPDATE entries SET value=? WHERE event_id=?",
            (json.dumps(raw), entry.event_id),
        )


def local_request(env, *entries):
    return export_request(
        env.session,
        context_event_ids=[entry.event_id for entry in entries],
        provider=ProviderInfo(provider="fixture", model="local", local=True),
    )


@pytest.mark.parametrize("retrieval", ["event_id", "scan"])
@pytest.mark.parametrize("boundary", ["count", "generation"])
async def test_retained_context_revoked_in_hook_never_reaches_transport(
    env, tmp_path, retrieval, boundary
):
    """The context is unrelated to the task's instruction source bindings."""
    calls, counts, generations, changed = [], 0, 0, False

    async def hook(request):
        nonlocal counts, generations, changed
        is_count = request.url.path.endswith("/input_tokens")
        if is_count:
            counts += 1
        else:
            generations += 1
        if (boundary == "count" and is_count and counts == 2) or (
            boundary == "generation" and not is_count and generations == 2
        ):
            revise(env.store, env.session, env.source)
            changed = True

    def respond(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 123}
            )
        if generations == 1:
            arguments = (
                {"event_ids": [env.source.event_id]} if retrieval == "event_id" else {"limit": 20}
            )
            action = {"tool": "read_transcript", "arguments": arguments}
        else:
            action = {"tool": "read_file", "arguments": {"path": "public.txt"}}
        return generated(body, action)

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
    (workspace / "public.txt").write_text("PUBLIC FILE")
    runtime = AgentRuntime(
        LocalJournal(env.store, "agent"), provider, workspace, ToolPolicy(workspace)
    )
    try:
        task = await runtime.submit(
            TaskRequest(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=env.session.epoch,
                objective="Inspect the journal context, then inspect public.txt",
                content_scope="public_synthetic",
                idempotency_key="retained-context-task",
            )
        )
        final = await runtime.wait(task.task_id, timeout=3)
        assert changed and final.state == "failed"
        assert final.source_event_ids == []
        assert final.content_scope == "local_only"
        assert len(calls) == (2 if boundary == "count" else 3)
        assert "PUBLIC_SYNTHETIC_SOURCE" not in json.dumps(calls[:2])
        if boundary == "generation":
            assert "PUBLIC_SYNTHETIC_SOURCE" in json.dumps(calls[-1])
        assert not any(e.kind in {"file", "command"} for e in final.evidence)
    finally:
        await runtime.close()
        await provider.close()


@pytest.mark.parametrize("kind", ["user", "window", "revision"])
def test_atomic_context_admission_folds_whole_source_chain(env, kind):
    if kind == "window":
        env.session = env.store.create_session(SessionRequest(channel="converse"))
        original = window(env.store, env.session)
        window(env.store, env.session, revision=2, scope="local_only")
        window(env.store, env.session, revision=3)
    elif kind == "revision":
        original = revise(env.store, env.session, env.source, scope="public_synthetic")
        revise(env.store, env.session, env.source, revision=3, scope="local_only")
        revise(env.store, env.session, env.source, revision=4, scope="public_synthetic")
    else:
        original = env.source
        revise(env.store, env.session, original)
        revise(env.store, env.session, original, revision=3, scope="public_synthetic")
    request = export_request(env.session, context_event_ids=[original.event_id])
    with pytest.raises(PermissionError):
        env.store.agent_export_admission(request)
    local = request.model_copy(
        update={"provider": ProviderInfo(provider="fixture", model="local", local=True)}
    )
    assert env.store.agent_export_admission(local).effective_scope == "local_only"


@pytest.mark.parametrize("kind", ["user", "window", "revision", "converse"])
def test_unchanged_context_is_bound_without_becoming_instruction_authority(env, kind):
    if kind == "window":
        env.session = env.store.create_session(SessionRequest(channel="converse"))
        entry = window(env.store, env.session)
    elif kind == "revision":
        entry = revise(env.store, env.session, env.source, scope="public_synthetic")
    elif kind == "converse":
        entry = source(env.store, env.session, kind="conversation.converse", key="converse")
    else:
        entry = env.source
    request = export_request(env.session, context_event_ids=[entry.event_id])
    result = env.store.agent_export_admission(request).validate_request(request)
    assert result.context_event_ids == [entry.event_id]
    assert result.source_event_ids == []
    assert result.effective_scope == "public_synthetic"


@pytest.mark.parametrize("case", ["missing", "other-session", "epoch"])
def test_invalid_retained_context_refuses_even_for_local_provider(env, case):
    if case == "missing":
        event_id = "absent"
    elif case == "other-session":
        other = env.store.create_session(SessionRequest(channel="agent"))
        event_id = source(env.store, other).event_id
    else:
        event_id = env.source.event_id
        env.session = env.store.reset(env.session.session_id)
    request = export_request(
        env.session,
        context_event_ids=[event_id],
        provider=ProviderInfo(provider="fixture", model="local", local=True),
    )
    with pytest.raises(ValueError, match="authority is unavailable"):
        env.store.agent_export_admission(request)


@pytest.mark.parametrize("depth", [1, 3])
@pytest.mark.parametrize("scope", ["local_only", "private_cloud_authorized", "public_synthetic"])
async def test_derived_context_folds_cause_updates_without_instruction_authority(env, depth, scope):
    entry = env.source
    for index in range(depth):
        entry = derived_context(env, causes=[entry.event_id], key=f"derived-{index}")
    public = export_request(env.session, context_event_ids=[entry.event_id])
    assert env.store.agent_export_admission(public).effective_scope == "public_synthetic"
    revise(env.store, env.session, env.source, scope=scope)
    journal = LocalJournal(env.store, "agent")
    assert await context_scope(journal, entry) == scope
    if scope == "public_synthetic":
        receipt = env.store.agent_export_admission(public)
    else:
        with pytest.raises(PermissionError):
            env.store.agent_export_admission(public)
        receipt = env.store.agent_export_admission(local_request(env, entry))
    assert receipt.effective_scope == scope
    assert receipt.context_event_ids == [entry.event_id]
    assert receipt.source_event_ids == [] and receipt.source_revisions == {}


@pytest.mark.parametrize("selected", ["user", "revision", "derived"])
@pytest.mark.parametrize("cause", ["original", "restricted"])
async def test_all_revision_causes_fold_without_false_resolution_cycle(env, selected, cause):
    restricted = source(env.store, env.session, scope="local_only", key="restricted")
    revision = env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.revision",
            payload={
                "target_event_id": env.source.event_id,
                "text": "PUBLIC SYNTHETIC UPDATED TEXT",
                "revision": 2,
                "content_scope": "public_synthetic",
            },
            causes=[env.source.event_id if cause == "original" else restricted.event_id],
            idempotency_key="causal-revision",
        ),
        "owner",
    )
    revise(env.store, env.session, env.source, revision=3, scope="public_synthetic")
    entry = {
        "user": env.source,
        "revision": revision,
        "derived": derived_context(env, causes=[env.source.event_id]),
    }[selected]
    expected = "public_synthetic" if cause == "original" else "local_only"
    assert await context_scope(LocalJournal(env.store, "agent"), entry) == expected
    assert env.store.agent_export_admission(local_request(env, entry)).effective_scope == expected


async def test_window_revision_causes_remain_in_complete_context_floor(env):
    env.session = env.store.create_session(SessionRequest(channel="converse"))
    restricted = source(env.store, env.session, scope="local_only", key="restricted")
    first = window(env.store, env.session)
    middle = window(env.store, env.session, revision=2)
    replace_native_row(env, middle, causes=[restricted.event_id])
    last = window(env.store, env.session, revision=3)
    for selected in (first, last):
        entry = derived_context(env, causes=[selected.event_id], key=f"derived-{selected.sequence}")
        assert await context_scope(LocalJournal(env.store, "agent"), entry) == "local_only"
        assert env.store.agent_export_admission(local_request(env, entry)).effective_scope == (
            "local_only"
        )


async def test_shared_causal_dag_is_deduplicated_and_keeps_restrictive_floor(env, monkeypatch):
    left = derived_context(env, causes=[env.source.event_id], key="left")
    right = derived_context(env, causes=[env.source.event_id], key="right")
    top = derived_context(env, causes=[left.event_id, right.event_id], key="top")
    revise(env.store, env.session, env.source)
    calls = []
    native_resolve = env.store.resolve

    def resolve(event_id):
        calls.append(event_id)
        return native_resolve(event_id)

    monkeypatch.setattr(env.store, "resolve", resolve)
    request = local_request(env, top, right, left)
    receipt = env.store.agent_export_admission(request)
    assert receipt.effective_scope == "local_only"
    assert calls.count(env.source.event_id) == 1
    calls.clear()
    assert await context_scope(LocalJournal(env.store, "agent"), top) == "local_only"
    assert calls.count(env.source.event_id) == 1


@pytest.mark.parametrize(
    "defect",
    ["missing", "self-cycle", "cycle", "forward", "session", "thread", "epoch", "bad-id"],
)
async def test_invalid_causal_provenance_refuses_even_locally(env, defect):
    entry = derived_context(env, causes=[env.source.event_id])
    if defect == "missing":
        replace_native_row(env, entry, causes=["missing-native-cause"])
    elif defect == "self-cycle":
        replace_native_row(env, entry, causes=[entry.event_id])
    elif defect == "cycle":
        replace_native_row(env, env.source, causes=[entry.event_id])
    elif defect == "forward":
        later = derived_context(env, causes=[], key="later")
        replace_native_row(env, entry, causes=[later.event_id])
    elif defect == "bad-id":
        replace_native_row(env, entry, causes=["\ud800"])
    else:
        field = {"session": "session_id", "thread": "thread_id", "epoch": "epoch"}[defect]
        replace_native_row(env, env.source, **{field: 2 if defect == "epoch" else "other"})
    request = local_request(env, entry)
    with pytest.raises(ValueError, match="^Agent export source authority is unavailable$"):
        env.store.agent_export_admission(request)
    with pytest.raises((KeyError, ValueError)):
        await context_scope(LocalJournal(env.store, "agent"), entry)


async def test_cause_lookup_cannot_alias_another_public_native_row(env):
    original = source(env.store, env.session, scope="local_only", key="restricted-original")
    other = source(env.store, env.session, key="public-alias")
    entry = derived_context(env, causes=[original.event_id])
    replace_native_row(env, original, **other.model_dump(mode="json"))
    with pytest.raises(ValueError, match="^Agent export source authority is unavailable$"):
        env.store.agent_export_admission(local_request(env, entry))
    with pytest.raises(ValueError, match="^Native context provenance is unavailable$"):
        await context_scope(LocalJournal(env.store, "agent"), entry)


@pytest.mark.parametrize("extra", [0, 1])
async def test_causal_closure_has_exact_native_node_bound(env, extra):
    entry = env.source
    for index in range(MAX_CONTEXT_PROVENANCE_NODES - 1 + extra):
        entry = derived_context(env, causes=[entry.event_id], key=f"causal-{index}")
    request = local_request(env, entry)
    journal = LocalJournal(env.store, "agent")
    if extra:
        with pytest.raises(ValueError, match="authority is unavailable"):
            env.store.agent_export_admission(request)
        with pytest.raises(ValueError, match="resource bound"):
            await context_scope(journal, entry)
    else:
        assert env.store.agent_export_admission(request).effective_scope == "public_synthetic"
        assert await context_scope(journal, entry) == "public_synthetic"


async def test_causal_bound_is_shared_across_retained_roots_and_counts_revisions(env):
    roots = []
    for index in range(128):
        parent = source(env.store, env.session, key=f"parent-{index}")
        roots.append(derived_context(env, causes=[parent.event_id], key=f"root-{index}"))
    request = local_request(env, *roots)
    assert env.store.agent_export_admission(request).effective_scope == "public_synthetic"
    journal = LocalJournal(env.store, "agent")
    closure = ContextScopeClosure(env.session.session_id, env.session.thread_id, env.session.epoch)
    for entry in roots:
        assert await context_scope(journal, entry, closure=closure) == "public_synthetic"
    revise(env.store, env.session, parent, scope="public_synthetic")
    with pytest.raises(ValueError, match="authority is unavailable"):
        env.store.agent_export_admission(request)
    closure = ContextScopeClosure(env.session.session_id, env.session.thread_id, env.session.epoch)
    with pytest.raises(ValueError, match="resource bound"):
        for entry in roots:
            await context_scope(journal, entry, closure=closure)


@pytest.mark.parametrize("field", ["session_id", "thread_id", "epoch", "type"])
async def test_shared_context_closure_requires_exact_native_identity(env, field):
    identity = {
        "session_id": env.session.session_id,
        "thread_id": env.session.thread_id,
        "epoch": env.session.epoch,
    }
    if field != "type":
        identity[field] = 2 if field == "epoch" else "other"
    closure = object() if field == "type" else ContextScopeClosure(**identity)
    with pytest.raises(ValueError, match="^Native context provenance is unavailable$"):
        await context_scope(LocalJournal(env.store, "agent"), env.source, closure=closure)


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("boundary", ["count", "generation", "response"])
async def test_derived_context_revocation_at_actual_provider_boundaries(
    env, tmp_path, depth, boundary
):
    entry = env.source
    for index in range(depth):
        entry = derived_context(env, causes=[entry.event_id], key=f"derived-{index}")
    calls, counts, generations, changed = [], 0, 0, False

    def revoke():
        nonlocal changed
        assert not changed
        revise(env.store, env.session, env.source)
        changed = True

    async def request_hook(request):
        nonlocal counts, generations
        is_count = request.url.path.endswith("/input_tokens")
        if is_count:
            counts += 1
        else:
            generations += 1
        if (boundary == "count" and is_count and counts == 2) or (
            boundary == "generation" and not is_count and generations == 2
        ):
            revoke()

    async def response_hook(response):
        if (
            boundary == "response"
            and response.request.url.path == "/v1/responses"
            and generations == 2
        ):
            revoke()

    def respond(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body, changed))
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 123}
            )
        action = (
            {"tool": "read_transcript", "arguments": {"event_ids": [entry.event_id]}}
            if generations == 1
            else {"tool": "read_file", "arguments": {"path": "public.txt"}}
        )
        return generated(body, action)

    provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="public-synthetic-fixture",
        allow_cloud=True,
        structured_actions=True,
        context_tokens=32768,
        transport=httpx.MockTransport(respond),
    )
    provider.http.event_hooks["request"].append(request_hook)
    provider.http.event_hooks["response"].append(response_hook)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "public.txt").write_text("PUBLIC FILE")
    runtime = AgentRuntime(
        LocalJournal(env.store, "agent"), provider, workspace, ToolPolicy(workspace)
    )
    try:
        task = await runtime.submit(
            TaskRequest(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=env.session.epoch,
                objective="Inspect derived journal context, then public.txt",
                content_scope="public_synthetic",
                idempotency_key="retained-derived-context-task",
            )
        )
        final = await runtime.wait(task.task_id, timeout=3)
        assert changed and final.state == "failed"
        assert final.content_scope == "local_only"
        assert final.source_event_ids == []
        assert runtime._tasks[task.task_id].context_event_ids == {entry.event_id}
        assert len(calls) == {"count": 2, "generation": 3, "response": 4}[boundary]
        marker = "PUBLIC_SYNTHETIC_DERIVED_ASSISTANT_CONTEXT"
        assert all(marker not in json.dumps(body) for _, body, withdrawn in calls if withdrawn)
        assert not any(e.kind in {"file", "command"} for e in final.evidence)
        assert not any(
            row.kind == "agent.tool" and row.payload.get("tool") == "read_file"
            for row in env.store.read(session_id=env.session.session_id)
        )
    finally:
        await runtime.close()
        await provider.close()
