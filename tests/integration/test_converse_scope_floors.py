"""Independent public synthetic native probes; no real model/provider/network."""

import asyncio
import json

import pytest

from packages.contracts.models import (
    AppendRequest,
    ConverseRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    WindowDecision,
)
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.session.store import LocalJournal, Store

LOCAL = ProviderInfo(provider="synthetic", model="local-fixture", local=True)
META = ProviderInfo(provider="meta", model="muse-spark-1.3-contributor", local=False)


class Spy:
    def __init__(self, info=LOCAL, response="Public synthetic reply."):
        self.info, self.response, self.calls = (info, response, [])

    async def stream(self, messages):
        self.calls.append(json.loads(json.dumps(messages)))
        yield self.response


def req(session, key="current", scope="public_synthetic", text="Public synthetic current turn."):
    return ConverseRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        text=text,
        content_scope=scope,
        idempotency_key=key,
    )


async def write(journal, session, kind, payload, key, causes=None, task_refs=None):
    return await journal.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind=kind,
            payload=payload,
            idempotency_key=key,
            causes=causes or [],
            task_refs=task_refs or [],
        )
    )


async def window(store, journal, controller, session, name):
    source = await controller.ingest(
        SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id=name,
            window_index=0,
            text="Public synthetic window current.",
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            capture_ended=True,
            content_scope="public_synthetic",
        )
    )
    decision = WindowDecision(
        source_event_id=source.event_id,
        source_sequence=source.sequence,
        source_revision=1,
        conversation="RESPOND_WITHOUT_INTERRUPTING_USER",
        agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
    )
    event = await write(
        LocalJournal(store, "decisions"),
        session,
        "routing.window_decided",
        decision.model_dump(mode="json"),
        "decision:" + source.event_id,
        causes=[source.event_id],
    )
    await controller.process(event)
    tasks = [p.task for p in controller.pending.values() if p.task]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
    return source


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["local_only", "private_cloud_authorized"])
async def test_window_reply_must_keep_actual_private_history_floor(tmp_path, scope):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    journal = LocalJournal(store, "converse")
    owner = LocalJournal(store, "owner")
    sentinel = "PUBLIC_FIXTURE_DERIVED_WINDOW_FLOOR_" + scope.upper()
    local = Spy(response="Derived " + sentinel + ".")
    runtime = ConverseRuntime(journal, local)
    controller = WindowConverseController(runtime)
    try:
        await write(
            owner,
            session,
            "conversation.user",
            {"text": sentinel, "final": True, "revision": 1, "content_scope": scope},
            "private-prior",
        )
        await window(store, journal, controller, session, "public-window")
        replies = store.read(kinds=["conversation.converse"])
        assert (
            len(local.calls) == 1 and sentinel in json.dumps(local.calls[0]) and (len(replies) == 1)
        )
        target = Spy(META)
        runtime.provider = target
        await runtime.turn(req(session))
        leaked = [p for p in target.calls if sentinel in json.dumps(p)]
        assert replies[0].payload["content_scope"] == scope, (
            "Local window output discarded its actually consumed history floor"
        )
        assert leaked == [], (
            "Mislabeled window output exported derived private history to Contributor"
        )
    finally:
        await controller.close()
        await runtime.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant,expected",
    [
        ("missing", "local_only"),
        ("private-base", "local_only"),
        ("private-intermediate", "private_cloud_authorized"),
        ("missing-intermediate", "local_only"),
        ("boolean-base", "local_only"),
    ],
)
async def test_outcome_notice_must_keep_every_source_scope_floor(tmp_path, variant, expected):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    journal = LocalJournal(store, "converse")
    owner = LocalJournal(store, "owner")
    runtime = ConverseRuntime(journal, Spy())
    sentinel = "PUBLIC_FIXTURE_OUTCOME_FLOOR_" + variant.upper()
    payload = {
        "text": "Public synthetic source.",
        "final": True,
        "revision": True if variant == "boolean-base" else 1,
    }
    if variant != "missing":
        payload["content_scope"] = "local_only" if variant == "private-base" else "public_synthetic"
    try:
        source = await write(owner, session, "conversation.user", payload, "base")
        if variant in {"private-base", "private-intermediate", "missing-intermediate"}:
            first = {
                "target_event_id": source.event_id,
                "revision": 2,
                "text": "Public synthetic corrected source.",
            }
            if variant != "missing-intermediate":
                first["content_scope"] = (
                    "private_cloud_authorized"
                    if variant == "private-intermediate"
                    else "public_synthetic"
                )
            await write(
                owner,
                session,
                "conversation.revision",
                first,
                "revision2",
                causes=[source.event_id],
            )
            if variant != "private-base":
                await write(
                    owner,
                    session,
                    "conversation.revision",
                    {
                        "target_event_id": source.event_id,
                        "revision": 3,
                        "text": "Public synthetic latest source.",
                        "content_scope": "public_synthetic",
                    },
                    "revision3",
                    causes=[source.event_id],
                )
        task = {
            "task_id": "public-task-" + variant,
            "session_id": session.session_id,
            "thread_id": session.thread_id,
            "epoch": session.epoch,
            "state": "succeeded",
            "objective_revision": 1,
            "summary": sentinel,
            "content_scope": "public_synthetic",
            "source_event_ids": [source.event_id],
            "remaining_required_work": [],
            "evidence": [
                {
                    "kind": "read",
                    "path": "public-fixture",
                    "sha256": "f" * 64,
                    "objective_revision": 1,
                }
            ],
        }
        outcome = await write(
            LocalJournal(store, "agent"),
            session,
            "agent.outcome",
            {"task": task},
            "outcome",
            task_refs=[task["task_id"]],
        )
        notice = await runtime.outcome_notice(outcome)
        assert notice is not None
        target = Spy(META)
        runtime.provider = target
        await runtime.turn(req(session))
        assert notice.payload["content_scope"] == expected, (
            "Outcome summary upgraded missing/original/intermediate source provenance"
        )
        assert not any(sentinel in json.dumps(p) for p in target.calls), (
            "Outcome notice exported source-derived private text to Contributor"
        )
    finally:
        await runtime.close()
        store.close()


@pytest.mark.asyncio
async def test_actual_target_rechecked_after_awaited_user_append(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    target = Spy(META)
    switched = []

    class Journal(LocalJournal):
        async def append(self, request):
            entry = await super().append(request)
            if request.kind == "conversation.user" and (not switched):
                switched.append(True)
                runtime.provider = target
            return entry

    runtime = ConverseRuntime(Journal(store, "converse"), Spy())
    try:
        try:
            await runtime.turn(
                req(session, scope="local_only", text="PUBLIC_FIXTURE_AWAITED_TARGET_SWITCH_95")
            )
        except (ValueError, RuntimeError):
            pass
        assert switched and target.calls == [], (
            "Local-only current text reached replacement Contributor target after awaited admission"
        )
    finally:
        await runtime.close()
        store.close()


@pytest.mark.asyncio
async def test_actual_typed_reply_does_keep_consumed_history_floor(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    local = Spy()
    runtime = ConverseRuntime(LocalJournal(store, "converse"), local)
    try:
        await write(
            LocalJournal(store, "owner"),
            session,
            "conversation.user",
            {
                "text": "PUBLIC_FIXTURE_PRIVATE_PRIOR",
                "final": True,
                "revision": 1,
                "content_scope": "local_only",
            },
            "prior",
        )
        await runtime.turn(req(session))
        reply = store.read(kinds=["conversation.converse"])[-1]
        assert (
            "PUBLIC_FIXTURE_PRIVATE_PRIOR" in json.dumps(local.calls)
            and reply.payload["content_scope"] == "local_only"
        )
    finally:
        await runtime.close()
        store.close()


@pytest.mark.asyncio
async def test_actual_custom_personality_default_refuses_contributor(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    spy = Spy(META)
    runtime = ConverseRuntime(
        LocalJournal(store, "converse"), spy, personality="PUBLIC_FIXTURE_CUSTOM_PRIVATE_PERSONA"
    )
    error = None
    try:
        try:
            await runtime.turn(req(session))
        except ValueError as exc:
            error = type(exc).__name__
        assert error and runtime.personality_scope == "local_only" and (spy.calls == [])
    finally:
        await runtime.close()
        store.close()
