"""Independent #255 oracles: real SQLite/ASGI/runtime; synthetic measured providers."""

import asyncio
import copy
import hashlib
import importlib
import importlib.util
import json
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest

from core.policy import ToolPolicy
from packages.contracts.client import JournalClient
from packages.contracts.models import (
    AppendRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    TaskRequest,
)
from packages.providers.completion_budget import PreparedComplete
from packages.providers.prompt_budget import PromptTokenCount
from packages.providers.text import ProviderError
from services.agent import AgentRuntime
from services.common.auth import Auth
from services.session.api import create_app
from services.session.store import LocalJournal, Store

BASE = "http://synthetic.invalid"
DOMAIN = b"kinaiya.agent.transcript-page-scope.v1\0"
CLEANUPS = []


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def scan_request(session, **updates):
    name = "packages.contracts.transcript_scan"
    assert importlib.util.find_spec(name) is not None, "Frozen transcript scan packets are missing"
    module = importlib.import_module(name)
    return module.TranscriptScanRequest.model_validate(
        {
            "schema_version": "1.0",
            "session_id": session.session_id,
            "thread_id": session.thread_id,
            "epoch": session.epoch,
            "after": 0,
            "through_sequence": None,
            "limit": 2,
            **updates,
        }
    )


def body(session, **updates):
    return {
        "schema_version": "1.0",
        "session_id": session.session_id,
        "thread_id": session.thread_id,
        "epoch": session.epoch,
        "after": 0,
        "through_sequence": None,
        "limit": 2,
        **updates,
    }


class GateConnection:
    """Stable wrapper installed before every owner; faults arm without rebinding."""

    def __init__(self, inner):
        self.inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()
        self.armed = False
        self.active = 0

    def execute(self, sql, parameters=()):
        if self.armed and "entries" in sql.lower() and sql.lstrip().lower().startswith("select"):
            self.active += 1
            self.entered.set()
            try:
                if not self.release.wait(5):
                    raise AssertionError("Synthetic scan gate was not joined")
            finally:
                self.active -= 1
        return self.inner.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *args):
        return self.inner.__exit__(*args)


@pytest.fixture
async def env(tmp_path):
    store = Store(tmp_path / "synthetic.sqlite")
    gate = GateConnection(store.db)
    store.db = gate
    session = store.create_session(SessionRequest(channel="agent"))
    roles = ("owner", "agent", "converse", "decisions", "resources")
    auth = Auth({role: f"synthetic-{role}-" + "x" * 32 for role in roles})
    app = create_app(store, auth)
    journal = JournalClient(BASE, auth.tokens["agent"], transport=httpx.ASGITransport(app=app))
    raw = httpx.AsyncClient(base_url=BASE, transport=httpx.ASGITransport(app=app))
    state = SimpleNamespace(
        store=store,
        gate=gate,
        session=session,
        auth=auth,
        app=app,
        journal=journal,
        raw=raw,
        key=0,
        runtimes=[],
    )
    try:
        async with app.router.lifespan_context(app):
            try:
                yield state
            finally:
                gate.release.set()
                for runtime in state.runtimes:
                    await runtime.close()
                    operations = runtime.workspace_operation_state()
                    assert operations["closed"]
                    assert all(value == 0 for key, value in operations.items() if key != "closed")
                await journal.close()
                await raw.aclose()
    finally:
        gate.release.set()
        for runtime in state.runtimes:
            await runtime.close()
        await journal.close()
        await raw.aclose()
        assert not journal._submit_exchanges
        assert gate.active == 0
        operations = app.state.standing_owner_operations
        assert operations.active_operations == 0
        store.close()
        with pytest.raises(sqlite3.ProgrammingError):
            gate.inner.execute("SELECT 1")
        CLEANUPS.append(
            {
                "sqlite_closed": True,
                "http_closed": raw.is_closed and journal.http.is_closed,
                "retained_exchanges": 0,
                "native_operations": 0,
                "runtimes": len(state.runtimes),
                "stable_connection": True,
            }
        )


def append(
    env,
    text="Synthetic transcript",
    *,
    scope="public_synthetic",
    session=None,
    kind="conversation.user",
    payload=None,
    causes=None,
):
    env.key += 1
    session = session or env.session
    return env.store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind=kind,
            payload={
                "text": text,
                "final": True,
                "revision": 1,
                "content_scope": scope,
                **(payload or {}),
            },
            causes=causes or [],
            idempotency_key=f"synthetic-{env.key}",
        ),
        "owner" if kind != "conversation.converse" else "converse",
    )


def headers(env, role="agent", **extra):
    return {"Authorization": "Bearer " + env.auth.tokens[role], **extra}


def observation(messages):
    values = [
        item["content"]
        for item in messages
        if item["role"] == "user" and item["content"].startswith(("[ok]", "[error]"))
    ]
    assert values, "Expected an actual native tool observation"
    latest = values[-1]
    return (
        (latest.startswith("[ok]"), json.loads(latest[5:]))
        if latest.startswith("[ok] ")
        else (False, latest)
    )


def finish():
    return {
        "tool": "finish",
        "arguments": {"summary": "Inspected synthetic transcript.", "objective_revision": 1},
    }


class MeasuredFixture:
    """Deterministic synthetic units, never a real model/tokenizer measurement."""

    def __init__(self, choose, *, local=True, context=2_000_000):
        self.info = ProviderInfo(provider="fixture", model="synthetic-page-oracle", local=local)
        self.choose, self.context = choose, context
        self.calls, self.preparations = [], []
        self.failures = []

    async def prepare_complete(self, messages):
        frozen = copy.deepcopy(messages)
        self.preparations.append(frozen)
        raw = canonical(frozen)
        count = PromptTokenCount(
            input_tokens=max(1, len(raw)),
            context_tokens=self.context,
            reserved_output_tokens=64,
            request_sha256=digest(raw),
            binding_sha256="b" * 64,
        )
        return PreparedComplete(count, lambda: self.complete(frozen))

    async def complete(self, messages):
        self.calls.append(copy.deepcopy(messages))
        try:
            value = self.choose(self, messages)
            if hasattr(value, "__await__"):
                value = await value
        except (AssertionError, KeyError, TypeError, IndexError) as error:
            # Keep the exact oracle assertion for the test thread. An assertion
            # escaping a model fixture can strand the old runtime's done event.
            self.failures.append(error)
            raise ProviderError("Synthetic oracle callback failed") from None
        return json.dumps(value)


async def run(env, provider, *, steps=12, objective="Inspect selected journal context."):
    runtime = AgentRuntime(
        LocalJournal(env.store, "agent"),
        provider,
        env.store.path.rsplit("/", 1)[0],
        ToolPolicy(env.store.path.rsplit("/", 1)[0]),
        max_steps=steps,
    )
    env.runtimes.append(runtime)
    task = await runtime.submit(
        TaskRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            objective=objective,
            content_scope="public_synthetic",
            idempotency_key=f"task-{len(env.runtimes)}",
        )
    )
    final = await runtime.wait(task.task_id, timeout=8)
    await runtime.close()
    if provider.failures:
        raise provider.failures[0]
    return runtime, final


def page_action(event_id=None, **extra):
    return {
        "tool": "read_transcript",
        "arguments": {
            "paged": True,
            **({"event_ids": [event_id]} if event_id is not None else {}),
            **extra,
        },
    }


def continuation(page, entry):
    return {
        "entry_view": entry["entry_view"],
        "offset": entry["next_offset"],
        "expected_revision": entry["revision"],
        "expected_entry_sha256": entry["entry_sha256"],
        "scope_token": page["scope_token"],
    }


def assert_page(page, *, scan=False):
    assert set(page) == {
        "schema_version",
        "scope_token",
        "entries",
        "through_sequence",
        "next_after",
        "scan_exhausted",
    }
    assert page["schema_version"] == "1.0" and len(page["scope_token"]) == 64
    assert len(canonical(page)) <= 131072
    assert sum(len(entry["text"]) for entry in page["entries"]) <= 12000
    if not scan:
        assert page["through_sequence"] is page["next_after"] is page["scan_exhausted"] is None


@pytest.mark.parametrize("adapter", ["store", "local", "http"])
async def test_native_scan_pages_real_rows_and_fixed_highwater_without_chasing_new_appends(
    env, adapter
):
    rows = [append(env, f"Synthetic retained {n}") for n in range(5)]
    request = scan_request(env.session)

    async def scan(value):
        if adapter == "store":
            return env.store.transcript_scan(value)
        return await (
            LocalJournal(env.store, "agent") if adapter == "local" else env.journal
        ).transcript_scan(value)

    first = await scan(request)
    assert first.entries == rows[:2] and first.exhausted is False
    assert first.through_sequence == rows[-1].sequence
    late = append(env, "Synthetic appended after initial native snapshot")
    seen, page = list(first.entries), first
    for _ in range(5):
        if page.exhausted:
            break
        request = scan_request(
            env.session, after=page.next_after, through_sequence=first.through_sequence
        )
        page = await scan(request)
        page.validate_request(request)
        seen.extend(page.entries)
    assert page.exhausted and page.next_after is None
    assert seen == rows and late not in seen


async def test_native_byte_limited_prefix_is_not_false_eof_or_skipped_row(env):
    rows = [append(env, "x" * 60000 + str(index)) for index in range(7)]
    request = scan_request(env.session, limit=20)
    first = env.store.transcript_scan(request)
    assert 0 < len(first.entries) < 7 and not first.exhausted
    assert len(canonical(first.model_dump(mode="json"))) <= 262144
    second = env.store.transcript_scan(
        scan_request(
            env.session, limit=20, after=first.next_after, through_sequence=first.through_sequence
        )
    )
    assert [*first.entries, *second.entries] == rows
    assert second.exhausted


@pytest.mark.parametrize(
    "change",
    [
        {"epoch": 2},
        {"thread_id": "foreign"},
        {"session_id": "absent"},
        {"after": 1, "through_sequence": 0},
        {"through_sequence": 9999},
    ],
)
async def test_native_api_definite_scope_and_cursor_conflicts_are_409(env, change):
    response = await env.raw.post(
        "/v1/transcript/scan", headers=headers(env), json=body(env.session, **change)
    )
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "role,status",
    [(None, 401), ("owner", 403), ("converse", 403), ("decisions", 403), ("resources", 403)],
)
async def test_native_scan_role_boundary(env, role, status):
    response = await env.raw.post(
        "/v1/transcript/scan", headers=headers(env, role) if role else {}, json=body(env.session)
    )
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "case,status",
    [
        ("query", 422),
        ("capability", 422),
        ("duplicate", 422),
        ("oversized", 422),
        ("compressed", 415),
        ("media", 415),
    ],
)
async def test_native_scan_rejects_ambiguous_private_or_oversized_transport(env, case, status):
    raw, extra, path = (
        canonical(body(env.session)),
        {"Content-Type": "application/json"},
        "/v1/transcript/scan",
    )
    if case == "query":
        path += "?untrusted=SYNTHETIC_PRIVATE_SENTINEL"
    elif case == "capability":
        extra["X-Kinaiya-Standing-Capability"] = "a" * 64
    elif case == "duplicate":
        raw = b'{"epoch":1,' + raw[1:]
    elif case == "oversized":
        raw += b" " * 8193
    elif case == "compressed":
        extra["Content-Encoding"] = "gzip"
    else:
        extra["Content-Type"] = "text/plain"
    response = await env.raw.post(path, content=raw, headers=headers(env, **extra))
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in response.text


async def test_native_scan_refuses_active_foreign_transaction_and_malformed_oversized_row(env):
    row = append(env)
    request = scan_request(env.session)
    env.store.db.execute("BEGIN")
    try:
        with pytest.raises((ValueError, RuntimeError, PermissionError)):
            env.store.transcript_scan(request)
        assert env.gate.inner.in_transaction
    finally:
        env.gate.inner.rollback()
    assert env.store.transcript_scan(request).entries == [row]
    with env.gate.inner:
        env.gate.inner.execute(
            "UPDATE entries SET value=? WHERE event_id=?", ("x" * 262145, row.event_id)
        )
    with pytest.raises((ValueError, RuntimeError, PermissionError)):
        env.store.transcript_scan(request)


async def test_actual_scan_operation_survives_cancelled_waiter_until_native_sql_settles(env):
    assert callable(getattr(env.journal, "transcript_scan", None)), "Missing owned scan exchange"
    append(env)
    env.gate.armed = True
    task = asyncio.create_task(env.journal.transcript_scan(scan_request(env.session)))
    try:
        assert await asyncio.to_thread(env.gate.entered.wait, 1)
        assert env.app.state.standing_owner_operations.active_operations == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert env.app.state.standing_owner_operations.active_operations == 1
    finally:
        env.gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(100):
            if env.app.state.standing_owner_operations.active_operations == 0:
                break
            await asyncio.sleep(0.005)
    assert env.app.state.standing_owner_operations.active_operations == 0


async def test_explicit_legacy_clip_is_labelled_while_short_shape_stays_exact(env):
    short = append(env, "Synthetic short text")
    long = append(env, "Synthetic filler " * 180 + "SYNTHETIC_FINAL_TAIL")
    outputs = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return {
                "tool": "read_transcript",
                "arguments": {"event_ids": [short.event_id, long.event_id]},
            }
        ok, value = observation(messages)
        assert ok
        outputs.extend(value)
        return finish()

    provider = MeasuredFixture(choose)
    _, final = await run(env, provider)
    assert final.state == "succeeded", final.error
    assert set(outputs[0]) == {"event_id", "kind", "origin", "sequence", "revision", "text"}
    clipped = outputs[1]
    assert len(clipped["text"]) == 2000
    assert clipped["truncated"] is True and clipped["total_chars"] == len(long.payload["text"])
    assert clipped["entry_view"] == "resolved"
    hint = clipped["continuation_hint"]
    assert (
        isinstance(hint, str) and "paged" in hint and long.event_id in hint and "resolved" in hint
    )


@pytest.mark.parametrize("view", ["resolved", "immutable"])
async def test_full_unicode_and_control_tail_reconstructed_from_actual_model_tool_pages(env, view):
    text = ("🌱e\u0301\n\t\x01" * 510) + "SYNTHETIC_FINAL_TAIL"
    target = append(
        env, text, payload={"speaker": "user", "start_ms": 1, "details": {"float": 1.5}}
    )
    unrelated = append(env, "SYNTHETIC_UNRELATED_TRANSCRIPT")
    pages, pieces = [], []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(target.event_id, entry_view=view, max_chars=700)
        ok, value = observation(messages)
        assert ok, value
        assert_page(value)
        entry = value["entries"][0]
        assert entry["entry_view"] == view
        assert entry["metadata"] == {
            key: val for key, val in target.payload.items() if key != "text"
        }
        assert entry["text_sha256"] == digest(text)
        assert entry["entry_sha256"] == digest(canonical(target.model_dump(mode="json")))
        assert entry["created_at"] == target.model_dump(mode="json")["created_at"]
        assert entry["offset"] == sum(map(len, pieces))
        pieces.append(entry["text"])
        pages.append(value)
        if entry["next_offset"] is None:
            assert entry["truncated"] is False
            return finish()
        assert entry["truncated"] is True
        return page_action(target.event_id, max_chars=700, **continuation(value, entry))

    provider = MeasuredFixture(choose)
    runtime, final = await run(env, provider)
    assert final.state == "succeeded", final.error
    assert "".join(pieces) == text and len(pages) > 1
    first = json.dumps(provider.calls[0])
    assert text not in first and unrelated.payload["text"] not in first
    rec = runtime._tasks[final.task_id]
    assert rec.context_event_ids == {target.event_id} and final.source_event_ids == []
    expected_scope = {
        "schema_version": "1.0",
        "task_id": final.task_id,
        "objective_revision": final.objective_revision,
        "session_id": final.session_id,
        "thread_id": final.thread_id,
        "epoch": final.epoch,
        "provider": provider.info.model_dump(mode="json"),
        "content_scope": final.content_scope,
    }
    assert pages[-1]["scope_token"] == digest(DOMAIN + canonical(expected_scope))
    tools = env.store.read(task_id=final.task_id, kinds=["agent.tool"], limit=200)
    assert tools and "SYNTHETIC_FINAL_TAIL" not in json.dumps(
        [row.model_dump(mode="json") for row in tools]
    )
    assert all(row.payload["evidence"]["kind"] == "read" for row in tools)


@pytest.mark.parametrize("length", [0, 7])
async def test_exact_end_offset_and_empty_text_are_complete_empty_slices(env, length):
    target = append(env, "x" * length)
    result = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(target.event_id, max_chars=1)
        ok, value = observation(messages)
        assert ok
        if len(provider.calls) == 2 and length:
            entry = value["entries"][0]
            args = continuation(value, entry)
            args["offset"] = length
            return page_action(target.event_id, **args)
        result.append(value)
        return finish()

    _, final = await run(env, MeasuredFixture(choose))
    assert final.state == "succeeded", final.error
    entry = result[0]["entries"][0]
    assert entry["text"] == "" and entry["next_offset"] is None and not entry["truncated"]


async def test_resolved_and_immutable_revision_views_never_splice(env):
    original = append(env, "Synthetic original " + "a" * 2100)
    append(
        env,
        "Synthetic corrected " + "b" * 2100,
        kind="conversation.revision",
        payload={"target_event_id": original.event_id, "revision": 2},
    )
    values = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(original.event_id, entry_view="immutable")
        ok, value = observation(messages)
        assert ok
        values.append(value["entries"][0])
        if len(values) == 1:
            return page_action(original.event_id, entry_view="resolved")
        return finish()

    _, final = await run(env, MeasuredFixture(choose))
    assert final.state == "succeeded", final.error
    assert values[0]["revision"] == 1 and values[1]["revision"] == 2
    assert values[0]["text"].startswith("Synthetic original")
    assert values[1]["text"].startswith("Synthetic corrected")
    assert values[0]["entry_sha256"] != values[1]["entry_sha256"]


@pytest.mark.parametrize("change", ["revision", "hash", "scope", "provider", "reset"])
async def test_stale_continuation_never_exposes_a_new_tail(env, change):
    target = append(env, "x" * 2500 + "SYNTHETIC_OLD_TAIL")
    errors = []
    holder = {}

    def choose(provider, messages):
        calls = len(original.calls) + len(replacement.calls)
        if calls == 1:
            return page_action(target.event_id, max_chars=100)
        ok, value = observation(messages)
        if calls == 2:
            assert ok, value
            args = continuation(value, value["entries"][0])
            if change == "revision":
                append(
                    env,
                    "y" * 2500 + "SYNTHETIC_NEW_TAIL",
                    kind="conversation.revision",
                    payload={"target_event_id": target.event_id, "revision": 2},
                )
            elif change == "hash":
                args["expected_entry_sha256"] = "0" * 64
            elif change == "scope":
                args["scope_token"] = "0" * 64
            elif change == "provider":
                assert holder["runtime"]._tasks[holder["task_id"]].provider is replacement
                assert holder["runtime"].provider is original
            else:
                env.store.reset(env.session.session_id)
            return page_action(target.event_id, **args)
        errors.append((ok, value))
        return finish()

    original = MeasuredFixture(choose)
    replacement = MeasuredFixture(choose)
    replacement.info = replacement.info.model_copy(update={"model": "synthetic-replacement"})

    class SwitchingJournal(LocalJournal):
        async def append(self, request):
            result = await super().append(request)
            if (
                change == "provider"
                and request.kind == "agent.tool"
                and request.payload.get("tool") == "read_transcript"
                and request.payload.get("ok") is True
            ):
                # Change the accepted adapter between generations, after the
                # first read completed; owner defaults do not repin a task.
                holder["runtime"]._tasks[request.payload["task_id"]].provider = replacement
            return result

    runtime = AgentRuntime(
        SwitchingJournal(env.store, "agent"),
        original,
        env.store.path.rsplit("/", 1)[0],
        ToolPolicy(env.store.path.rsplit("/", 1)[0]),
    )
    env.runtimes.append(runtime)
    holder["runtime"] = runtime
    task = await runtime.submit(
        TaskRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            objective="Inspect selected context.",
            content_scope="public_synthetic",
            idempotency_key="stale-page",
        )
    )
    holder["task_id"] = task.task_id
    final = await runtime.wait(task.task_id, timeout=5)
    await runtime.close()
    assert not original.failures and not replacement.failures
    assert final.state == "failed" or errors and not errors[-1][0]
    if change == "provider":
        assert len(original.calls) == 1 and len(replacement.calls) == 2
        assert errors and not errors[-1][0]
        assert original.info != replacement.info
    assert "SYNTHETIC_NEW_TAIL" not in json.dumps([*original.calls, *replacement.calls])


async def test_filtered_empty_scan_page_advances_to_public_history_with_fixed_snapshot(env):
    hidden = [append(env, f"SYNTHETIC_PRIVATE_{n}", scope="local_only") for n in range(3)]
    visible = append(env, "SYNTHETIC_PUBLIC_LATER")
    pages = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(limit=2)
        ok, value = observation(messages)
        assert ok, value
        assert_page(value, scan=True)
        pages.append(value)
        if len(pages) == 1:
            assert value["entries"] == [] and value["scan_exhausted"] is False
            assert value["next_after"] >= hidden[1].sequence
        if value["scan_exhausted"]:
            return finish()
        return page_action(
            limit=2,
            after=value["next_after"],
            through_sequence=value["through_sequence"],
            scope_token=value["scope_token"],
        )

    provider = MeasuredFixture(choose, local=False)
    runtime, final = await run(env, provider)
    assert final.state == "succeeded", final.error
    assert len({page["through_sequence"] for page in pages}) == 1
    entries = [entry for page in pages for entry in page["entries"]]
    assert visible.event_id in {entry["event_id"] for entry in entries}
    assert all(entry["entry_view"] == "immutable" for entry in entries)
    sent = json.dumps(provider.calls)
    assert all(row.event_id not in sent and row.payload["text"] not in sent for row in hidden)
    assert not {row.event_id for row in hidden} & runtime._tasks[final.task_id].context_event_ids


@pytest.mark.parametrize(
    "arguments",
    [
        {"paged": "true"},
        {"paged": True, "offset": True},
        {"paged": True, "max_chars": 0},
        {"paged": True, "max_chars": 12001},
        {"paged": True, "event_ids": ["x", "y"]},
        {"paged": True, "entry_view": "resolved"},
        {"paged": True, "offset": 1},
        {"paged": True, "through_sequence": 1},
        {"paged": True, "scope_token": "0" * 64},
        {"max_chars": 2},
        {"entry_view": "immutable"},
        {"paged": True, "extra": "untrusted"},
    ],
)
async def test_model_facing_closed_paging_arguments_are_refused(env, arguments):
    errors = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return {"tool": "read_transcript", "arguments": arguments}
        errors.append(observation(messages))
        return finish()

    _, final = await run(env, MeasuredFixture(choose))
    assert final.state == "failed"
    assert errors and not errors[-1][0]


async def test_cumulative_128_roots_survive_prompt_eviction_and_129th_is_explicit_refusal(env):
    rows = [append(env, f"Synthetic root {n}") for n in range(129)]
    errors = []

    def choose(provider, messages):
        index = len(provider.calls) - 1
        if index:
            ok, value = observation(messages)
            if index <= 128:
                assert ok, value
            else:
                errors.append((ok, value))
                return finish()
        return page_action(rows[index].event_id, max_chars=50)

    provider = MeasuredFixture(choose, context=24000)
    runtime, final = await run(env, provider, steps=132)
    rec = runtime._tasks[final.task_id]
    assert final.state == "failed"
    assert errors and not errors[-1][0]
    assert rec.context_event_ids == {row.event_id for row in rows[:128]}
    assert rows[128].event_id not in rec.context_event_ids
    assert final.source_event_ids == []
    assert rows[0].event_id not in json.dumps(provider.calls[-1])


async def test_legacy_aggregate_overflow_refuses_instead_of_silently_omitting_a_readable_entry(env):
    rows = [append(env, str(n) * 2100) for n in range(7)]
    errors = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return {
                "tool": "read_transcript",
                "arguments": {"event_ids": [row.event_id for row in rows]},
            }
        errors.append(observation(messages))
        return finish()

    _, final = await run(env, MeasuredFixture(choose))
    assert final.state == "failed" or errors and not errors[-1][0]
    assert errors and "paged" in str(errors[-1][1]).lower()


def native_response(request, entries=None, **updates):
    return {
        "schema_version": "1.0",
        "request": request.model_dump(mode="json"),
        "through_sequence": 0,
        "entries": entries or [],
        "next_after": None,
        "exhausted": True,
        **updates,
    }


@pytest.mark.parametrize(
    "fault", ["request", "scope", "cursor", "oversized", "duplicate", "encoding", "redirect"]
)
async def test_client_refuses_mismatched_malformed_or_unbounded_transport(env, fault):
    request = scan_request(env.session)
    value = native_response(request)
    if fault == "request":
        value["request"]["limit"] = 3
    elif fault == "scope":
        row = append(env).model_dump(mode="json")
        row["session_id"] = "foreign"
        value.update(entries=[row], through_sequence=row["sequence"])
    elif fault == "cursor":
        value.update(exhausted=False, next_after=1)
    raw = canonical(value)
    if fault == "oversized":
        raw += b" " * 262145
    elif fault == "duplicate":
        raw = b'{"exhausted":false,' + raw[1:]
    calls = []

    def respond(http_request):
        calls.append(http_request)
        assert "x-kinaiya-standing-capability" not in http_request.headers
        return httpx.Response(
            302 if fault == "redirect" else 200,
            content=raw,
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip" if fault == "encoding" else "identity",
                "Location": BASE + "/forbidden",
            },
        )

    client = JournalClient(BASE, "synthetic-credential", transport=httpx.MockTransport(respond))
    try:
        assert callable(getattr(client, "transcript_scan", None)), "Missing owned scan exchange"
        with pytest.raises((ValueError, RuntimeError, PermissionError)):
            await client.transcript_scan(request)
        assert len(calls) == 1
    finally:
        await client.close()
        assert not client._submit_exchanges


class ClosingStream(httpx.AsyncByteStream):
    def __init__(self, raw, *, fail_close=False, resist_cancel=True):
        self.raw, self.fail_close, self.resist_cancel = raw, fail_close, resist_cancel
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.closed = self.cancelled = 0

    async def __aiter__(self):
        yield self.raw

    async def aclose(self):
        self.closed += 1
        self.entered.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                if not self.resist_cancel:
                    raise
        if self.fail_close:
            raise RuntimeError("Synthetic actual close failure")


async def join_actual_exchanges(client):
    exchanges = tuple(client._submit_exchanges)
    await asyncio.gather(*(exchange.task for exchange in exchanges), return_exceptions=True)
    await asyncio.gather(
        *(exchange.close_task for exchange in exchanges if exchange.close_task is not None),
        return_exceptions=True,
    )
    # Actual handles can be done before their registry/sticky callbacks run.
    await asyncio.sleep(0)
    assert all(exchange.task.done() for exchange in exchanges)
    assert all(exchange.close_task is None or exchange.close_task.done() for exchange in exchanges)
    return exchanges


async def test_real_sqlite_gate_fixture_and_native_teardown_control(env):
    row = append(env, "Synthetic fixture control")
    assert env.store.db is env.gate
    assert env.app.state.standing_resource_finalization_owner._connection is env.gate
    env.gate.armed = True
    work = asyncio.create_task(asyncio.to_thread(env.store.entry, row.event_id))
    try:
        assert await asyncio.to_thread(env.gate.entered.wait, 1)
        assert not work.done() and env.gate.active == 1
    finally:
        env.gate.release.set()
    assert await work == row
    assert env.gate.active == 0


async def test_missing_text_remains_nonconversation_metadata_but_explicit_nonstring_refuses(env):
    env.key += 1
    row = env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="agent.tool",
            payload={"content_scope": "public_synthetic", "native_note": "synthetic non-speech"},
            idempotency_key=f"synthetic-{env.key}",
        ),
        "agent",
    )
    raw = row.model_dump(mode="json")
    observations = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(row.event_id, entry_view="immutable")
        ok, value = observation(messages)
        observations.append((ok, value))
        if len(provider.calls) == 2:
            assert ok and value["entries"][0]["text"] == ""
            assert value["entries"][0]["kind"] == "agent.tool"
            assert value["entries"][0]["origin"] == "agent-supervisor"
            assert value["entries"][0]["metadata"] == raw["payload"]
            raw["payload"]["text"] = 7
            with env.gate.inner:
                env.gate.inner.execute(
                    "UPDATE entries SET value=? WHERE event_id=?", (json.dumps(raw), row.event_id)
                )
            return page_action(row.event_id, entry_view="immutable")
        return finish()

    _, final = await run(env, MeasuredFixture(choose))
    assert final.state == "failed"
    assert len(observations) == 2 and observations[0][0] and not observations[1][0]


async def test_model_scan_byte_bound_does_not_advance_past_unreturned_authorized_metadata(env):
    rows = [append(env, "🌱\x01" * 400, payload={"padding": "p" * 48000}) for _ in range(3)]
    pages = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(limit=5)
        ok, value = observation(messages)
        assert ok, value
        assert_page(value, scan=True)
        pages.append(value)
        if len(pages) == 1:
            assert 0 < len(value["entries"]) < len(rows)
            assert value["scan_exhausted"] is False
            assert value["next_after"] == value["entries"][-1]["sequence"]
        seen = {entry["event_id"] for page in pages for entry in page["entries"]}
        if all(row.event_id in seen for row in rows):
            return finish()
        assert not value["scan_exhausted"]
        return page_action(
            limit=5,
            after=value["next_after"],
            through_sequence=value["through_sequence"],
            scope_token=value["scope_token"],
        )

    _, final = await run(env, MeasuredFixture(choose))
    assert final.state == "succeeded", final.error
    selected = [
        entry["event_id"]
        for page in pages
        for entry in page["entries"]
        if entry["event_id"] in {row.event_id for row in rows}
    ]
    assert selected == [row.event_id for row in rows]


async def test_oversized_unsplittable_metadata_is_refusal_not_false_empty_history(env):
    row = append(env, "Synthetic small text")
    raw = row.model_dump(mode="json")
    raw["payload"]["padding"] = "x" * 132000
    with env.gate.inner:
        env.gate.inner.execute(
            "UPDATE entries SET value=? WHERE event_id=?", (json.dumps(raw), row.event_id)
        )
    errors = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(row.event_id)
        errors.append(observation(messages))
        return finish()

    _, final = await run(env, MeasuredFixture(choose))
    assert final.state == "failed"
    assert errors and not errors[-1][0]


async def test_provider_replacement_during_native_read_cannot_publish_a_page(env):
    row = append(env, "SYNTHETIC_CONTEXT_NEVER_PUBLISHED")
    entered, release = asyncio.Event(), asyncio.Event()

    class GatedJournal(LocalJournal):
        async def resolve(self, event_id):
            value = await super().resolve(event_id)
            if event_id == row.event_id:
                entered.set()
                await release.wait()
            return value

    provider = MeasuredFixture(
        lambda p, m: page_action(row.event_id) if len(p.calls) == 1 else finish()
    )
    replacement = MeasuredFixture(lambda *_: finish())
    runtime = AgentRuntime(
        GatedJournal(env.store, "agent"),
        provider,
        env.store.path.rsplit("/", 1)[0],
        ToolPolicy(env.store.path.rsplit("/", 1)[0]),
    )
    env.runtimes.append(runtime)
    task = await runtime.submit(
        TaskRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            objective="Inspect selected context.",
            content_scope="public_synthetic",
            idempotency_key="provider-race",
        )
    )
    try:
        assert await asyncio.wait_for(entered.wait(), 1) is True
        assert runtime._tasks[task.task_id].provider is provider
        assert provider.info == replacement.info
        runtime._tasks[task.task_id].provider = replacement
    finally:
        release.set()
    final = await runtime.wait(task.task_id, timeout=5)
    await runtime.close()
    assert final.state == "failed"
    assert len(provider.calls) == 1
    assert row.payload["text"] not in json.dumps([*provider.calls, *replacement.calls])


async def test_owner_default_provider_change_keeps_accepted_task_provider(env):
    row = append(env, "SYNTHETIC_ACCEPTED_PROVIDER_CONTEXT")
    entered, release = asyncio.Event(), asyncio.Event()

    class GatedJournal(LocalJournal):
        async def resolve(self, event_id):
            value = await super().resolve(event_id)
            if event_id == row.event_id:
                entered.set()
                await release.wait()
            return value

    pages = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(row.event_id)
        ok, page = observation(messages)
        assert ok, page
        pages.append(page)
        return finish()

    provider = MeasuredFixture(choose)
    replacement = MeasuredFixture(lambda *_: finish())
    replacement.info = replacement.info.model_copy(update={"model": "synthetic-owner-default"})
    runtime = AgentRuntime(
        GatedJournal(env.store, "agent"),
        provider,
        env.store.path.rsplit("/", 1)[0],
        ToolPolicy(env.store.path.rsplit("/", 1)[0]),
    )
    env.runtimes.append(runtime)
    task = await runtime.submit(
        TaskRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            objective="Inspect selected context.",
            content_scope="public_synthetic",
            idempotency_key="owner-provider-default",
        )
    )
    try:
        assert await asyncio.wait_for(entered.wait(), 1) is True
        assert runtime._tasks[task.task_id].provider is provider
        runtime.provider = replacement
    finally:
        release.set()
    final = await runtime.wait(task.task_id, timeout=5)
    await runtime.close()
    assert not provider.failures
    assert final.state == "succeeded", final.error
    assert pages[0]["entries"][0]["text"] == row.payload["text"]
    assert runtime._tasks[task.task_id].provider is provider and not replacement.calls


async def test_retained_page_disclosure_withdrawal_blocks_next_measured_generation(env):
    row = append(env, "SYNTHETIC_REVOKED_PAGE_TEXT")

    class RevokingProvider(MeasuredFixture):
        async def prepare_complete(self, messages):
            if self.calls:
                append(
                    env,
                    "Synthetic local correction",
                    scope="local_only",
                    kind="conversation.revision",
                    payload={"target_event_id": row.event_id, "revision": 2},
                )
            return await super().prepare_complete(messages)

    provider = RevokingProvider(
        lambda p, m: page_action(row.event_id) if len(p.calls) == 1 else finish(), local=False
    )
    runtime, final = await run(env, provider)
    assert final.state == "failed"
    assert len(provider.calls) == 1
    assert runtime._tasks[final.task_id].context_event_ids == {row.event_id}
    assert final.source_event_ids == []


async def test_current_reset_epoch_scan_skips_retained_prior_epoch_and_rejects_old_scope(env):
    old = append(env, "SYNTHETIC_PRIOR_EPOCH")
    env.session = env.store.reset(env.session.session_id)
    new = append(env, "SYNTHETIC_CURRENT_EPOCH")
    scan = scan_request(env.session)
    native = await env.journal.transcript_scan(scan)
    assert native.entries == [new]
    values = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(limit=20)
        ok, value = observation(messages)
        assert ok, value
        values.extend(value["entries"])
        return finish()

    provider = MeasuredFixture(choose)
    _, final = await run(env, provider)
    assert final.state == "succeeded", final.error
    assert new.event_id in {entry["event_id"] for entry in values}
    assert old.event_id not in json.dumps(provider.calls)


async def test_client_64_actual_closes_share_capacity_until_they_really_finish(env):
    request = scan_request(env.session)
    streams = []

    def respond(_):
        stream = ClosingStream(canonical(native_response(request)))
        streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"Content-Type": "application/json"})

    client = JournalClient(BASE, "synthetic", transport=httpx.MockTransport(respond))
    jobs = []
    try:
        assert callable(getattr(client, "transcript_scan", None)), "Missing owned scan exchange"
        jobs = [asyncio.create_task(client.transcript_scan(request)) for _ in range(64)]
        for _ in range(200):
            if len(streams) == 64 and all(stream.entered.is_set() for stream in streams):
                break
            await asyncio.sleep(0)
        assert len(streams) == len(client._submit_exchanges) == 64
        assert all(stream.entered.is_set() for stream in streams)
        with pytest.raises((ValueError, RuntimeError)):
            await client.transcript_scan(request)
        assert len(streams) == 64
    finally:
        for stream in streams:
            stream.release.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        await client.close()
    assert not client._submit_exchanges and all(stream.closed == 1 for stream in streams)


async def test_client_deadline_bounds_wait_without_releasing_pending_actual_close(env, monkeypatch):
    request = scan_request(env.session)
    stream = ClosingStream(canonical(native_response(request)))
    client = JournalClient(
        BASE,
        "synthetic",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, stream=stream, headers={"Content-Type": "application/json"}
            )
        ),
    )
    task = None
    loop, offset = asyncio.get_running_loop(), [0]
    original_time = loop.time
    try:
        assert callable(getattr(client, "transcript_scan", None)), "Missing owned scan exchange"
        monkeypatch.setattr(loop, "time", lambda: original_time() + offset[0])
        task = asyncio.create_task(client.transcript_scan(request))
        await asyncio.wait_for(stream.entered.wait(), 1)
        offset[0] = 26
        with pytest.raises((ValueError, RuntimeError)):
            await asyncio.wait_for(task, 1)
        assert len(client._submit_exchanges) == 1 and stream.closed == 1
    finally:
        offset[0] = 0
        stream.release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await join_actual_exchanges(client)
        await client.close()
    assert not client._submit_exchanges and stream.closed == 1
    assert client._submit_cleanup_failed is False


async def test_native_scan_shares_64_actual_operation_slots_with_old_standing_routes(
    env, monkeypatch
):
    append(env)
    hub = env.app.state.standing_owner_operations
    entered, release = threading.Event(), threading.Event()
    original = hub.owner.page

    def blocked_page(**kwargs):
        entered.set()
        if not release.wait(5):
            raise AssertionError("Synthetic old native operation was not released")
        return original(**kwargs)

    monkeypatch.setattr(hub.owner, "page", blocked_page)
    old_jobs, scan_job = [], None
    try:
        old_jobs = [
            asyncio.create_task(
                env.raw.get("/v1/standing/responsibilities", headers=headers(env, "owner"))
            )
            for _ in range(63)
        ]
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(100):
            if hub.active_operations == 63:
                break
            await asyncio.sleep(0.005)
        assert hub.active_operations == 63
        scan_job = asyncio.create_task(
            env.raw.post("/v1/transcript/scan", headers=headers(env), json=body(env.session))
        )
        for _ in range(100):
            if hub.active_operations == 64:
                break
            await asyncio.sleep(0.005)
        assert hub.active_operations == 64 and not scan_job.done()
        refused = await env.raw.post(
            "/v1/transcript/scan", headers=headers(env), json=body(env.session)
        )
        assert refused.status_code == 503 and hub.active_operations == 64
    finally:
        release.set()
        await asyncio.gather(*old_jobs, *([scan_job] if scan_job else []), return_exceptions=True)
    assert hub.active_operations == 0


async def test_native_corrupt_entry_identity_is_503_without_echoing_foreign_value(env):
    row = append(env)
    value = row.model_dump(mode="json")
    value["event_id"] = "SYNTHETIC_FOREIGN_IDENTITY_SENTINEL"
    with env.gate.inner:
        env.gate.inner.execute(
            "UPDATE entries SET value=? WHERE event_id=?", (json.dumps(value), row.event_id)
        )
    response = await env.raw.post(
        "/v1/transcript/scan", headers=headers(env), json=body(env.session)
    )
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert value["event_id"] not in response.text


async def test_permitted_narrower_disclosure_returns_current_final_scope_token(env):
    target = append(env, "Synthetic authorized local context", scope="local_only")
    pages = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(target.event_id, max_chars=5)
        ok, page = observation(messages)
        assert ok, page
        pages.append(page)
        if len(pages) == 1:
            return page_action(target.event_id, **continuation(page, page["entries"][0]))
        return finish()

    provider = MeasuredFixture(choose)
    _, final = await run(env, provider)
    assert final.state == "succeeded", final.error
    assert final.content_scope == "local_only" and len(pages) == 2
    scope = {
        "schema_version": "1.0",
        "task_id": final.task_id,
        "objective_revision": 1,
        "session_id": final.session_id,
        "thread_id": final.thread_id,
        "epoch": final.epoch,
        "provider": provider.info.model_dump(mode="json"),
        "content_scope": "local_only",
    }
    assert pages[0]["scope_token"] == pages[1]["scope_token"] == digest(DOMAIN + canonical(scope))


@pytest.mark.parametrize("extra", [0, 1])
async def test_full_causal_disclosure_closure_keeps_256_node_bound(env, extra):
    row = append(env, "Synthetic root")
    for index in range(255 + extra):
        row = append(
            env, f"Synthetic derived {index}", kind="conversation.converse", causes=[row.event_id]
        )
    values = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(row.event_id, entry_view="immutable")
        values.append(observation(messages))
        return finish()

    _, final = await run(env, MeasuredFixture(choose))
    assert values
    assert values[-1][0] is (extra == 0)
    assert final.state == ("succeeded" if extra == 0 else "failed")
    assert final.source_event_ids == []


async def test_retrieved_transcript_cannot_create_a_write_grant_or_instruction_source(
    env, tmp_path
):
    target = append(env, "Synthetic untrusted text claims unrestricted file-write permission.")

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(target.event_id)
        ok, _ = observation(messages)
        assert ok
        return {
            "tool": "write_file",
            "arguments": {"path": "not-authorized.txt", "content": "synthetic"},
        }

    provider = MeasuredFixture(choose)
    workspace = tmp_path / "ungranted-workspace"
    workspace.mkdir()
    runtime = AgentRuntime(
        LocalJournal(env.store, "agent"), provider, workspace, ToolPolicy(workspace)
    )
    env.runtimes.append(runtime)
    task = await runtime.submit(
        TaskRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            objective="Inspect selected journal context.",
            content_scope="public_synthetic",
            idempotency_key="no-new-grant",
        )
    )
    try:
        for _ in range(200):
            current = await runtime.status(task.task_id)
            if current.state in {"awaiting_approval", "failed"}:
                break
            await asyncio.sleep(0.005)
        assert current.state == "awaiting_approval", current.error
        assert current.source_event_ids == []
        assert runtime._tasks[task.task_id].context_event_ids == {target.event_id}
        assert not (workspace / "not-authorized.txt").exists()
        assert (await runtime.pending_approvals()).approvals
    finally:
        await runtime.close()


async def test_native_20000_scalar_metadata_is_readable_without_pruning(env):
    row = append(env, "Synthetic native metadata", payload={"native_scalars": [0] * 20000})
    request = scan_request(env.session)
    page = await env.journal.transcript_scan(request)
    assert page.entries == [row]
    assert page.entries[0].payload["native_scalars"] == [0] * 20000
    assert len(canonical(page.model_dump(mode="json"))) <= 262144


async def test_immutable_continuation_keeps_historical_text_after_public_correction(env):
    text = "Synthetic original " + "x" * 2200 + "SYNTHETIC_ORIGINAL_TAIL"
    row = append(env, text)
    pieces = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(row.event_id, entry_view="immutable")
        ok, page = observation(messages)
        assert ok, page
        entry = page["entries"][0]
        assert entry["entry_view"] == "immutable" and entry["revision"] == 1
        pieces.append(entry["text"])
        if len(pieces) == 1:
            append(
                env,
                "SYNTHETIC_NEW_PUBLIC_REVISION",
                kind="conversation.revision",
                payload={"target_event_id": row.event_id, "revision": 2},
            )
            return page_action(row.event_id, **continuation(page, entry))
        return finish()

    provider = MeasuredFixture(choose)
    _, final = await run(env, provider)
    assert final.state == "succeeded", final.error
    assert "".join(pieces) == text
    assert "SYNTHETIC_NEW_PUBLIC_REVISION" not in json.dumps(provider.calls)


async def test_resolved_window_revision_cannot_supply_tail_for_an_old_continuation(env):
    env.session = env.store.create_session(SessionRequest(channel="converse"))

    def window(revision, text):
        payload = SpeechWindow(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            utterance_id="synthetic-window",
            window_index=0,
            revision=revision,
            text=text,
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            content_scope="public_synthetic",
        )
        env.key += 1
        return env.store.append(
            AppendRequest(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=env.session.epoch,
                kind="conversation.window",
                payload=payload.model_dump(mode="json"),
                idempotency_key=f"synthetic-{env.key}",
            ),
            "owner",
        )

    row = window(1, "x" * 2300 + "SYNTHETIC_OLD_WINDOW_TAIL")
    initial = []

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return page_action(row.event_id, max_chars=100)
        ok, page = observation(messages)
        if len(provider.calls) == 2:
            assert ok
            initial.append(page)
            window(2, "y" * 2300 + "SYNTHETIC_NEW_WINDOW_TAIL")
            return page_action(row.event_id, **continuation(page, page["entries"][0]))
        assert not ok
        return finish()

    provider = MeasuredFixture(choose)
    _, final = await run(env, provider)
    assert initial and final.state == "failed"
    assert "SYNTHETIC_NEW_WINDOW_TAIL" not in json.dumps(provider.calls)


@pytest.mark.parametrize("fail_close", [False, True])
async def test_client_retains_sole_actual_close_after_cancellation_and_latches_uncertainty(
    env, fail_close
):
    request = scan_request(env.session)
    stream = ClosingStream(canonical(native_response(request)), fail_close=fail_close)
    calls = 0

    def respond(_):
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=stream, headers={"Content-Type": "application/json"})

    client = JournalClient(BASE, "synthetic", transport=httpx.MockTransport(respond))
    task = None
    try:
        assert callable(getattr(client, "transcript_scan", None)), "Missing owned scan exchange"
        task = asyncio.create_task(client.transcript_scan(request))
        await asyncio.wait_for(stream.entered.wait(), 1)
        assert not task.done() and len(client._submit_exchanges) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(client._submit_exchanges) == 1 and stream.closed == 1
        stream.release.set()
        exchanges = await join_actual_exchanges(client)
        assert not client._submit_exchanges and stream.closed == 1
        assert stream.cancelled == 1
        assert len(exchanges) == 1 and not exchanges[0].close_task.cancelled()
        assert (
            isinstance(exchanges[0].close_task.exception(), RuntimeError)
            if fail_close
            else (exchanges[0].close_task.exception() is None)
        )
        assert client._submit_cleanup_failed is fail_close
        if fail_close:
            with pytest.raises((ValueError, RuntimeError, PermissionError)):
                await client.transcript_scan(request)
            assert calls == 1
    finally:
        stream.release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await join_actual_exchanges(client)
        await client.close()


async def test_client_cooperative_cancelled_actual_close_retains_sticky_uncertainty(env):
    request = scan_request(env.session)
    stream = ClosingStream(canonical(native_response(request)), resist_cancel=False)
    client = JournalClient(
        BASE,
        "synthetic",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, stream=stream, headers={"Content-Type": "application/json"}
            )
        ),
    )
    task = asyncio.create_task(client.transcript_scan(request))
    try:
        await asyncio.wait_for(stream.entered.wait(), 1)
        exchanges = tuple(client._submit_exchanges)
        assert len(exchanges) == 1 and stream.closed == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await join_actual_exchanges(client)
        assert exchanges[0].close_task.cancelled()
        assert stream.cancelled == 1 and stream.closed == 1
        assert not client._submit_exchanges and client._submit_cleanup_failed is True
        stream.release.set()
        await asyncio.sleep(0)
        assert client._submit_cleanup_failed is True and stream.closed == 1
        with pytest.raises((ValueError, RuntimeError, PermissionError)):
            await client.transcript_scan(request)
    finally:
        stream.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await join_actual_exchanges(client)
        await client.close()
