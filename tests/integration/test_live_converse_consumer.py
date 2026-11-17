"""Actual native v2 conversation effects, with synthetic generation only."""

import asyncio
import copy

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.live_decisions_attempts import LiveDecisionsSource
from packages.contracts.live_effect_pending import LiveEffectPendingItem
from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.live import LiveConverseController
from services.converse.runtime import ConverseRuntime
from services.session.api import create_app as session_app
from services.session.live_windows import publish
from services.session.store import LocalJournal
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_capture_permission import fence
from tests.integration.test_live_decisions_attempts import live as live
from tests.integration.test_live_effect_http import TOKENS
from tests.integration.test_live_effects import outcome
from tests.integration.test_live_window_publication import original


def selected(live, conversation):
    proof = outcome(live, conversation)
    entry = live.owner.lookup_publication(proof).receipt
    return LiveEffectPendingItem(
        event_id=entry.event_id, sequence=entry.sequence, publication=proof
    )


def consumer(live, text, *, journal=None):
    class Observed(ScriptedProvider):
        def __init__(self):
            super().__init__(text if isinstance(text, list) else [text])
            self.inputs = []

        async def stream(self, messages):
            self.inputs.append(copy.deepcopy(messages))
            async for delta in super().stream(messages):
                yield delta

    provider = Observed()
    runtime = ConverseRuntime(
        LocalJournal(live.store, "converse") if journal is None else journal,
        provider,
        personality="Public owner persona; keep the actual source and model output.",
        personality_scope="public_synthetic",
    )
    return LiveConverseController(runtime), provider


def revised(live, **changes):
    current = copy.copy(live)
    current.pair = publish(live.store, original(live, **changes))
    current.source = LiveDecisionsSource.of(current.pair, live.source.capture)
    return current


async def completed(controller, item, effect):
    async with asyncio.timeout(4):
        while True:
            await controller.tick()
            result = await controller.process_live(item, effect)
            if result is not None and result.completion is not None:
                return result
            await asyncio.sleep(0)


@pytest.mark.parametrize(
    "conversation,effect",
    [
        ("WAIT_WITH_BACKCHANNEL", "converse_backchannel"),
        ("RESPOND_INTERRUPT_USER", "converse_reply"),
    ],
)
async def test_native_converse_uses_original_owner_and_preserves_model_output(
    live, conversation, effect
):
    text = "  Public synthetic response.\n"
    controller, provider = consumer(live, text)
    item = selected(live, conversation)
    before = live.store.db.execute("SELECT request,receipt,fence FROM live_captures").fetchall()
    try:
        result = await completed(controller, item, effect)
        assert result.completion.status == "recorded", controller.last_error
        replies = live.store.read(kinds=["conversation.converse"])
        assert len(replies) == 1
        event = replies[0]
        assert event.origin == "converse" and event.causes == [live.pair.source.event_id]
        assert event.payload["window_behavior"] == conversation
        assert event.payload["live_effect"] == effect
        assert event.event_id == result.completion.evidence.event_id
        assert len(provider.inputs) == 1
        assert provider.inputs[0][-1] == {
            "role": "user",
            "content": live.pair.source.payload["text"],
        }
        assert provider.inputs[0][0]["content"].startswith(controller.runtime.personality)
        assert '"conversation":"' + conversation + '"' in provider.inputs[0][0]["content"]
        observed = [
            e
            for e in live.store.read(kinds=["converse.window_state"])
            if e.payload.get("phase") == "live_output_observed"
        ]
        assert len(observed) == 1
        assert observed[0].payload["provider_output"] == text
        assert observed[0].payload["provider_output_complete"] is True
        assert await controller.process_live(item, effect) == result
        assert (
            len(provider.inputs) == 1 and len(live.store.read(kinds=["conversation.converse"])) == 1
        )
        assert (
            live.store.db.execute("SELECT request,receipt,fence FROM live_captures").fetchall()
            == before
        )
        assert (
            live.store.window_response(
                live.session.session_id,
                live.session.epoch,
                live.pair.source.payload["utterance_id"],
            )
            is None
        )
        assert (
            await controller.runtime.validate_speech(
                event.event_id, event.payload["generation_epoch"]
            )
            == event
        )
    finally:
        await controller.close()
        await controller.runtime.close()


async def test_native_backchannel_can_return_no_acknowledgment_without_a_rewrite(live):
    controller, provider = consumer(live, "")
    item = selected(live, "WAIT_WITH_BACKCHANNEL")
    try:
        result = await completed(controller, item, "converse_backchannel")
        assert result.completion.status == "declined"
        assert len(provider.inputs) == 1
        assert live.store.read(kinds=["conversation.converse"]) == []
        observed = [
            e
            for e in live.store.read(kinds=["converse.window_state"])
            if e.payload.get("phase") == "live_output_observed"
        ]
        assert len(observed) == 1 and observed[0].payload["provider_output"] == ""
    finally:
        await controller.close()
        await controller.runtime.close()


async def test_noninterrupting_native_reply_waits_for_actual_speaking_hint(live):
    assert live.pair.source.payload["user_speaking"] is True
    controller, provider = consumer(live, "Public buffered reply.")
    item = selected(live, "RESPOND_WITHOUT_INTERRUPTING_USER")
    try:
        await controller.process_live(item, "converse_reply")
        prepared = controller.pending[live.session.session_id]
        await asyncio.wait_for(asyncio.shield(prepared.task), 2)
        assert len(provider.inputs) == 1
        assert live.store.read(kinds=["conversation.converse"]) == []
        held = await controller.process_live(item, "converse_reply")
        assert held.completion is None
        assert not prepared.abandoned
        changed = original(live, revision=2, user_speaking=False)
        publish(live.store, changed)
        await controller.flush(prepared)
        result = await completed(controller, item, "converse_reply")
        assert result.completion.status == "recorded"
        assert len(provider.inputs) == 1
        replies = live.store.read(kinds=["conversation.converse"])
        assert len(replies) == 1 and replies[0].payload["text"] == "Public buffered reply."
        event = replies[0]
        assert (
            await controller.runtime.validate_speech(
                event.event_id, event.payload["generation_epoch"]
            )
            == event
        )
    finally:
        await controller.close()
        await controller.runtime.close()


@pytest.mark.parametrize("change", ["text", "scope", "restored", "capture_fenced"])
async def test_held_reply_cannot_survive_changed_native_user_authority(live, change):
    controller, provider = consumer(live, "Public reply that must stay unplayed.")
    item = selected(live, "RESPOND_WITHOUT_INTERRUPTING_USER")
    try:
        await controller.process_live(item, "converse_reply")
        prepared = controller.pending[live.session.session_id]
        await asyncio.wait_for(asyncio.shield(prepared.task), 2)
        assert len(provider.inputs) == 1
        if change == "capture_fenced":
            live.store.live_captures.fence(fence(live.source.capture.original))
        else:
            changes = (
                {"text": "Public changed instruction"}
                if change != "scope"
                else {"content_scope": "local_only"}
            )
            publish(live.store, original(live, revision=2, **changes))
        if change == "restored":
            publish(live.store, original(live, revision=3, user_speaking=False))
        elif change != "capture_fenced":
            publish(live.store, original(live, revision=3, user_speaking=False, **changes))
        await controller.flush(prepared)
        result = await completed(controller, item, "converse_reply")
        assert result.completion.status == "declined"
        assert len(provider.inputs) == 1
        assert live.store.read(kinds=["conversation.converse"]) == []
    finally:
        await controller.close()
        await controller.runtime.close()


@pytest.mark.parametrize("action", ["WAIT_FOR_NEXT_WINDOW", "IGNORE"])
async def test_later_native_wait_controls_the_original_buffered_reply(live, action):
    controller, provider = consumer(live, "Public buffered reply.")
    item = selected(live, "RESPOND_WITHOUT_INTERRUPTING_USER")
    try:
        await controller.process_live(item, "converse_reply")
        prepared = controller.pending[live.session.session_id]
        await asyncio.wait_for(asyncio.shield(prepared.task), 2)
        current = revised(live, revision=2, user_speaking=False)
        selected(current, action)
        await controller.tick()
        result = await completed(controller, item, "converse_reply")
        assert len(provider.inputs) == 1
        if action == "WAIT_FOR_NEXT_WINDOW":
            assert result.completion.status == "declined"
            assert prepared.abandoned
            assert live.store.read(kinds=["conversation.converse"]) == []
        else:
            assert result.completion.status == "recorded"
            assert not prepared.abandoned
            assert len(live.store.read(kinds=["conversation.converse"])) == 1
    finally:
        await controller.close()
        await controller.runtime.close()


async def test_older_native_wait_does_not_fence_a_later_response(live):
    selected(live, "WAIT_FOR_NEXT_WINDOW")
    current = revised(live, revision=2, user_speaking=False)
    item = selected(current, "RESPOND_WITHOUT_INTERRUPTING_USER")
    controller, provider = consumer(live, "Public later reply.")
    try:
        result = await completed(controller, item, "converse_reply")
        assert result.completion.status == "recorded"
        assert len(provider.inputs) == 1
        assert len(live.store.read(kinds=["conversation.converse"])) == 1
    finally:
        await controller.close()
        await controller.runtime.close()


async def test_native_backchannel_preserves_the_same_utterance_for_a_later_full_reply(live):
    controller, provider = consumer(live, ["Public acknowledgment.", "Public full reply."])
    before = live.store.db.execute("SELECT request,receipt,fence FROM live_captures").fetchall()
    first = selected(live, "WAIT_WITH_BACKCHANNEL")
    try:
        backchannel = await completed(controller, first, "converse_backchannel")
        current = revised(live, revision=2, user_speaking=False)
        second = selected(current, "RESPOND_WITHOUT_INTERRUPTING_USER")
        response = await completed(controller, second, "converse_reply")
        assert backchannel.completion.status == response.completion.status == "recorded"
        assert len(provider.inputs) == 2
        replies = live.store.read(kinds=["conversation.converse"])
        assert [e.payload["text"] for e in replies] == [
            "Public acknowledgment.",
            "Public full reply.",
        ]
        assert [e.payload["live_effect"] for e in replies] == [
            "converse_backchannel",
            "converse_reply",
        ]
        assert {e.payload["window_utterance_id"] for e in replies} == {
            live.source.publication.utterance_id
        }
        assert (
            live.store.db.execute("SELECT request,receipt,fence FROM live_captures").fetchall()
            == before
        )
        assert (
            live.store.window_response(
                live.session.session_id, live.session.epoch, live.source.publication.utterance_id
            )
            is None
        )
    finally:
        await controller.close()
        await controller.runtime.close()


async def test_original_stream_cleanup_still_runs_when_live_consumer_close_raises(
    live, monkeypatch
):
    controller, _ = consumer(live, "Public unused reply.")
    app = create_app(
        controller.runtime, Auth(TOKENS), window_controller=controller, live_consumer=controller
    )
    close_consumer, close_streams = controller.close, app.state.live_streams.close
    calls = []

    async def failed_close():
        await close_consumer()
        raise RuntimeError("Public synthetic close failure")

    async def streams_close():
        calls.append("streams")
        await close_streams()

    monkeypatch.setattr(controller, "close", failed_close)
    monkeypatch.setattr(app.state.live_streams, "close", streams_close)
    try:
        with pytest.raises(RuntimeError, match="Public synthetic close failure"):
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)
        assert calls == ["streams"]
    finally:
        await close_consumer()
        await close_streams()
        await controller.runtime.close()


@pytest.mark.parametrize("change", ["text", "scope", "capture_fenced", "native_wait", "owner_off"])
async def test_native_speech_rechecks_authority_after_chunk_publication(live, change):
    controller, provider = consumer(live, "Public original acknowledgment.")
    item = selected(live, "WAIT_WITH_BACKCHANNEL")
    try:
        result = await completed(controller, item, "converse_backchannel")
        event = live.store.entry(result.completion.evidence.event_id)
        generation = event.payload["generation_epoch"]
        assert await controller.runtime.validate_speech(event.event_id, generation) == event
        if change == "text":
            revised(live, revision=2, text="Public corrected user instruction")
        elif change == "scope":
            revised(live, revision=2, content_scope="local_only")
        elif change == "capture_fenced":
            live.store.live_captures.fence(fence(live.source.capture.original))
        elif change == "native_wait":
            current = revised(live, revision=2, user_speaking=False)
            selected(current, "WAIT_FOR_NEXT_WINDOW")
        else:
            await controller.runtime.set_enabled(False, owner=True)
        with pytest.raises(ValueError):
            await controller.runtime.validate_speech(event.event_id, generation)
        assert len(provider.inputs) == 1
        assert live.store.entry(event.event_id).payload["text"] == "Public original acknowledgment."
    finally:
        await controller.close()
        await controller.runtime.close()


async def test_cancelled_facade_retains_original_native_start_and_generation(live, monkeypatch):
    controller, provider = consumer(live, "Public single response.")
    item = selected(live, "WAIT_WITH_BACKCHANNEL")
    entered, release = asyncio.Event(), asyncio.Event()
    native_start = controller.journal.live_effect_start
    starts = []

    async def held_start(request):
        starts.append(request)
        result = await native_start(request)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(controller.journal, "live_effect_start", held_start)
    facade = asyncio.create_task(controller.process_live(item, "converse_backchannel"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        facade.cancel()
        with pytest.raises(asyncio.CancelledError):
            await facade
        route = controller.routes[(item.event_id, "converse_backchannel")]
        assert not route.task.done() and not route.start_task.done()
        assert await controller.process_live(item, "converse_backchannel", timeout=0.01) is None
        assert len(starts) == 1
        release.set()
        result = await completed(controller, item, "converse_backchannel")
        assert result.completion.status == "recorded"
        assert len(starts) == len(provider.inputs) == 1
    finally:
        release.set()
        await controller.close()
        await controller.runtime.close()


@pytest.mark.parametrize("committed", [False, True])
async def test_native_chunk_lost_ack_never_replays_generation_or_write(
    live, monkeypatch, committed
):
    controller, provider = consumer(live, "Public original chunk.")
    item = selected(live, "WAIT_WITH_BACKCHANNEL")
    append = controller.journal.append
    writes = []

    async def failed_ack(request):
        if request.kind != "conversation.converse":
            return await append(request)
        writes.append(request)
        if committed:
            await append(request)
        raise TimeoutError("Public synthetic native acknowledgment loss")

    monkeypatch.setattr(controller.journal, "append", failed_ack)
    try:
        result = await completed(controller, item, "converse_backchannel")
        expected = "recorded" if committed else "uncertain"
        assert result.completion.status == expected
        for _ in range(3):
            repeated = await controller.process_live(item, "converse_backchannel")
            assert repeated.completion.status == expected
        assert len(provider.inputs) == len(writes) == 1
        assert len(live.store.read(kinds=["conversation.converse"])) == int(committed)
        replacement, replacement_provider = consumer(live, "Public forbidden replay.")
        try:
            observed = await completed(replacement, item, "converse_backchannel")
            assert observed.completion.status == expected
            assert replacement_provider.inputs == []
            assert len(writes) == 1
        finally:
            await replacement.close()
            await replacement.runtime.close()
    finally:
        await controller.close()
        await controller.runtime.close()


async def test_provider_completion_wakes_loop_and_unknown_native_wait_stays_owned(
    live, monkeypatch
):
    controller, provider = consumer(live, "")
    item = selected(live, "WAIT_WITH_BACKCHANNEL")
    provider_entered, provider_release = asyncio.Event(), asyncio.Event()
    wait_entered, wait_release = asyncio.Event(), asyncio.Event()
    terminal = asyncio.Event()
    stream, wait, finish = (
        provider.stream,
        controller.journal.wait_for_journal_change,
        controller.journal.live_effect_finish,
    )
    waits = []

    async def held_stream(messages):
        provider_entered.set()
        await provider_release.wait()
        async for delta in stream(messages):
            yield delta

    async def held_wait(*args, **kwargs):
        waits.append(asyncio.current_task())
        wait_entered.set()
        await wait_release.wait()
        return await wait(*args, **kwargs)

    async def native_finish(request):
        result = await finish(request)
        if result.completion is not None:
            terminal.set()
        return result

    monkeypatch.setattr(provider, "stream", held_stream)
    monkeypatch.setattr(controller.journal, "wait_for_journal_change", held_wait)
    monkeypatch.setattr(controller.journal, "live_effect_finish", native_finish)
    loop = asyncio.create_task(controller.run())
    try:
        await asyncio.wait_for(provider_entered.wait(), 2)
        await asyncio.wait_for(wait_entered.wait(), 2)
        assert len(waits) == 1 and not waits[0].done()
        provider_release.set()
        await asyncio.wait_for(terminal.wait(), 2)
        result = await controller.journal.live_effect_lookup(item.query("converse_backchannel"))
        assert result.record.completion.status == "declined"
        assert len(provider.inputs) == 1
        assert len(waits) == 1 and not waits[0].done() and not waits[0].cancelled()
        assert live.store.read(kinds=["conversation.converse"]) == []
        assert await controller.close() is False
        assert not waits[0].done() and not controller.run_owner.finished
        wait_release.set()
        await asyncio.wait_for(asyncio.shield(waits[0]), 2)
        await asyncio.wait_for(asyncio.shield(loop), 2)
        assert await controller.close() is True
    finally:
        provider_release.set()
        wait_release.set()
        await controller.close()
        await asyncio.gather(loop, return_exceptions=True)
        await controller.runtime.close()


@pytest.mark.parametrize(
    "action,effect",
    [
        ("WAIT_WITH_BACKCHANNEL", "converse_backchannel"),
        ("RESPOND_WITHOUT_INTERRUPTING_USER", "converse_reply"),
    ],
)
async def test_actual_converse_consumer_uses_authenticated_http_native_owners(live, action, effect):
    current = (
        live
        if action == "WAIT_WITH_BACKCHANNEL"
        else revised(live, revision=2, user_speaking=False)
    )
    item = selected(current, action)
    journal = JournalClient(
        "http://session.local",
        TOKENS["converse"],
        transport=httpx.ASGITransport(app=session_app(live.store, Auth(TOKENS))),
    )
    controller, provider = consumer(live, "Public HTTP-bound response.", journal=journal)
    try:
        result = await completed(controller, item, effect)
        assert result.completion.status == "recorded"
        assert len(provider.inputs) == 1
        event = live.store.entry(result.completion.evidence.event_id)
        assert (
            await controller.runtime.validate_speech(
                event.event_id, event.payload["generation_epoch"]
            )
            == event
        )
        assert len(live.store.read(kinds=["conversation.converse"])) == 1
        assert await controller.process_live(item, effect) == result
        assert len(provider.inputs) == 1
    finally:
        await controller.close()
        await controller.runtime.close()
        await journal.close()


async def test_queued_old_backchannel_cannot_follow_a_newer_native_full_reply(live):
    acknowledgment = selected(live, "WAIT_WITH_BACKCHANNEL")
    current = revised(live, revision=2, user_speaking=False)
    reply = selected(current, "RESPOND_WITHOUT_INTERRUPTING_USER")
    controller, provider = consumer(live, "Public newest full reply.")
    try:
        result = await completed(controller, reply, "converse_reply")
        assert result.completion.status == "recorded"
        old = await controller.journal.live_effect_lookup(
            acknowledgment.query("converse_backchannel")
        )
        assert old.source_current is False
        assert await controller.process_live(acknowledgment, "converse_backchannel") is None
        assert len(provider.inputs) == 1
        assert [e.payload["text"] for e in live.store.read(kinds=["conversation.converse"])] == [
            "Public newest full reply."
        ]
    finally:
        await controller.close()
        await controller.runtime.close()


async def test_new_native_backchannel_supersedes_an_older_buffered_full_reply(live):
    controller, provider = consumer(
        live, ["Public old buffered full reply.", "Public current acknowledgment."]
    )
    full = selected(live, "RESPOND_WITHOUT_INTERRUPTING_USER")
    try:
        await controller.process_live(full, "converse_reply")
        prepared = controller.pending[live.session.session_id]
        await asyncio.wait_for(asyncio.shield(prepared.task), 2)
        assert len(provider.inputs) == 1 and not prepared.published
        revised(live, revision=2, user_speaking=False)
        current = revised(live, revision=3, user_speaking=True)
        acknowledgment = selected(current, "WAIT_WITH_BACKCHANNEL")
        old = await controller.journal.live_effect_lookup(full.query("converse_reply"))
        assert old.source_current is False
        result = await completed(controller, acknowledgment, "converse_backchannel")
        assert result.completion.status == "recorded"
        retired = await completed(controller, full, "converse_reply")
        assert retired.completion.status == "declined"
        assert len(provider.inputs) == 2
        assert [e.payload["text"] for e in live.store.read(kinds=["conversation.converse"])] == [
            "Public current acknowledgment."
        ]
        observed = [
            e
            for e in live.store.read(kinds=["converse.window_state"])
            if e.payload.get("phase") == "live_output_observed"
        ]
        assert any(
            e.payload["provider_output"] == "Public old buffered full reply." for e in observed
        )
    finally:
        await controller.close()
        await controller.runtime.close()


@pytest.mark.parametrize("pending_close", [False, True])
async def test_terminal_native_wake_failure_recovers_without_releasing_original_cleanup(
    live, monkeypatch, pending_close
):
    from packages.contracts.operation_ownership import retain_task

    controller, provider = consumer(live, "Public recovered response.")
    failed, terminal, resumed, release_close = (asyncio.Event() for _ in range(4))
    wait, finish = controller.journal.wait_for_journal_change, controller.journal.live_effect_finish
    waits, closes = [], []

    async def original_close():
        await release_close.wait()

    async def transient_wait(*args, **kwargs):
        waits.append(asyncio.current_task())
        if len(waits) == 1:
            if pending_close:
                closes.append(retain_task(asyncio.create_task(original_close()), cleanup=True))
            failed.set()
            raise OSError("Public synthetic terminal wake read failure")
        resumed.set()
        return await wait(*args, **kwargs)

    async def native_finish(request):
        record = await finish(request)
        if record.completion is not None:
            terminal.set()
        return record

    monkeypatch.setattr(controller.journal, "wait_for_journal_change", transient_wait)
    monkeypatch.setattr(controller.journal, "live_effect_finish", native_finish)
    loop = asyncio.create_task(controller.run())
    try:
        await asyncio.wait_for(failed.wait(), 2)
        item = selected(live, "WAIT_WITH_BACKCHANNEL")
        await asyncio.wait_for(terminal.wait(), 2)
        await asyncio.wait_for(resumed.wait(), 2)
        result = await controller.journal.live_effect_lookup(item.query("converse_backchannel"))
        assert result.record.completion.status == "recorded"
        assert len(provider.inputs) == 1
        assert len(live.store.read(kinds=["conversation.converse"])) == 1
        assert waits[0].done() and not waits[0].cancelled()
        assert controller.native_wait is not waits[0]
        if pending_close:
            assert len(closes) == 1 and not closes[0].done()
            assert await controller.close() is False
            assert not closes[0].done() and not closes[0].cancelled()
            release_close.set()
            await asyncio.wait_for(asyncio.shield(closes[0]), 2)
        assert await controller.close() is True
    finally:
        release_close.set()
        await controller.close()
        await asyncio.gather(loop, return_exceptions=True)
        await controller.runtime.close()
