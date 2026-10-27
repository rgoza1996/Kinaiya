"""Real durable journal regressions for speech validity across process restart."""

import asyncio

import pytest

from packages.contracts.models import AppendRequest, ConverseRequest, SessionRequest
from packages.providers.text import ScriptedProvider
from services.converse.runtime import ConverseRuntime
from services.session.store import LocalJournal, Store


@pytest.mark.parametrize("interrupted", [False, True])
async def test_restart_never_reauthorizes_an_old_speech_chunk(tmp_path, interrupted):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        journal = LocalJournal(store, "converse")
        first = ConverseRuntime(journal, ScriptedProvider(["Hello from the synthetic fixture."]))
        result = await first.turn(
            ConverseRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                text="Public synthetic greeting",
                idempotency_key="first",
            )
        )
        chunk = next(e["entry"] for e in result["events"] if e["type"] == "text")
        generation = chunk["payload"]["generation_epoch"]
        if interrupted:
            await first.interrupt(session.session_id)
        restored = ConverseRuntime(journal, ScriptedProvider([]))
        with pytest.raises(ValueError, match="Obsolete"):
            await restored.validate_speech(chunk["event_id"], generation)
    finally:
        store.close()


async def test_runtime_binding_denies_old_audio_after_history_eviction_and_fresh_turn(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        journal = LocalJournal(store, "converse")
        first = ConverseRuntime(journal, ScriptedProvider(["Synthetic old reply."]))
        old = await first.turn(
            ConverseRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                text="Public old greeting",
                idempotency_key="old",
            )
        )
        chunk = next(event["entry"] for event in old["events"] if event["type"] == "text")
        for index in range(105):
            store.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="agent.tool",
                    payload={"synthetic": True},
                    idempotency_key=f"tool:{index}",
                ),
                "agent",
            )
        restored = ConverseRuntime(journal, ScriptedProvider(["Synthetic new reply."]))
        fresh = await restored.turn(
            ConverseRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                text="Public new greeting",
                idempotency_key="new",
            )
        )
        with pytest.raises(ValueError, match="Obsolete"):
            await restored.validate_speech(chunk["event_id"], chunk["payload"]["generation_epoch"])
        current = next(event["entry"] for event in fresh["events"] if event["type"] == "text")
        assert await restored.validate_speech(
            current["event_id"], current["payload"]["generation_epoch"]
        )
    finally:
        store.close()


async def test_failed_durable_barrier_is_not_cached_as_an_accepted_epoch(tmp_path, monkeypatch):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        journal = LocalJournal(store, "converse")
        first = ConverseRuntime(journal, ScriptedProvider(["Public old reply."]))
        result = await first.turn(
            ConverseRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                text="Public greeting",
                idempotency_key="old",
            )
        )
        old = next(event["entry"] for event in result["events"] if event["type"] == "text")
        original = journal.append

        async def fail(request):
            if request.kind == "converse.safety":
                raise OSError("Synthetic storage failure")
            return await original(request)

        monkeypatch.setattr(journal, "append", fail)
        restored = ConverseRuntime(journal, ScriptedProvider([]))
        with pytest.raises(OSError, match="Synthetic storage failure"):
            await restored.validate_speech(old["event_id"], old["payload"]["generation_epoch"])
        assert session.session_id not in restored.playback
    finally:
        store.close()


async def test_restart_barrier_is_durable_and_first_initialization_is_serialized(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        journal = LocalJournal(store, "converse")
        first = ConverseRuntime(journal, ScriptedProvider(["Synthetic first reply."]))
        await first.turn(
            ConverseRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                text="Public synthetic greeting",
                idempotency_key="first",
            )
        )
        prior = first.playback[session.session_id].generation
        restored = ConverseRuntime(journal, ScriptedProvider(["Synthetic fresh reply."]))
        original = restored._recent
        calls = 0

        async def delayed(session_id):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return await original(session_id)

        restored._recent = delayed
        epochs = await asyncio.gather(
            restored._epoch(session.session_id), restored._epoch(session.session_id)
        )
        assert epochs[0] is epochs[1] and calls == 1
        barrier = epochs[0].generation
        assert barrier > prior
        again = ConverseRuntime(journal, ScriptedProvider([]))
        assert (await again._epoch(session.session_id)).generation > barrier
        await restored.turn(
            ConverseRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                text="Another public greeting",
                idempotency_key="fresh",
            )
        )
        assert restored.playback[session.session_id].generation > barrier
    finally:
        store.close()
