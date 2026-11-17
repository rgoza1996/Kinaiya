"""Passive bounded consumer discovery through real SQLite and authenticated HTTP."""

from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.live_decisions_attempts import LiveDecisionsSource
from packages.contracts.live_effect_pending import LiveEffectPendingQuery
from packages.contracts.live_effects import PROTOCOL
from services.session.live_windows import publish
from services.session.store import LocalJournal
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_decisions_attempts import live as live
from tests.integration.test_live_effect_http import TOKENS, wire
from tests.integration.test_live_effects import (
    completion,
    outcome,
    start_record,
)
from tests.integration.test_live_effects import (
    effects as effects,
)
from tests.integration.test_live_window_publication import original


def query(effect="converse_backchannel", *, after=0, limit=32):
    return LiveEffectPendingQuery(
        schema_version="2.0",
        protocol=PROTOCOL,
        effect=effect,
        after_sequence=after,
        limit=limit,
    )


def next_outcome(live, index, conversation="IGNORE", route="QUEUE_UNTIL_TASK_DONE"):
    pair = publish(live.store, original(live, index, text=f"Public synthetic instruction {index}"))
    selected = SimpleNamespace(
        **(
            vars(live)
            | {
                "pair": pair,
                "source": LiveDecisionsSource.of(pair, live.source.capture),
            }
        )
    )
    return outcome(selected, conversation, route)


def test_discovery_is_passive_and_does_not_reserve_or_start_work(effects):
    e = effects
    wake = e.store.journal_wake.snapshot()
    page = e.effects.pending(query())
    assert len(page.items) == 1 and page.exhausted
    assert page.items[0].publication == e.query.publication
    assert page.items[0].query("converse_backchannel") == e.query
    assert page.through_sequence == page.items[0].sequence
    assert not e.effects._present() and e.store.journal_wake.snapshot() == wake
    assert e.effects.pending(query(after=page.through_sequence)).items == []
    assert e.effects.pending(query("agent_route")).items == []


def test_reserved_and_uncertain_rows_remain_discoverable_until_positive_completion(effects):
    e = effects
    record = e.effects.admit(e.request, "converse")
    assert len(e.effects.pending(query()).items) == 1
    start_record(e, record)
    e.effects.finish(completion(record.receipt, status="uncertain"), "converse")
    assert len(e.effects.pending(query()).items) == 1
    assert e.effects.lookup(e.query).record.completion.status == "uncertain"


def test_definitive_no_effect_completion_is_not_offered_as_new_work(effects):
    e = effects
    record = e.effects.admit(e.request, "converse")
    e.effects.finish(completion(record.receipt), "converse")
    wake = e.store.journal_wake.snapshot()
    assert e.effects.pending(query()).items == []
    assert e.store.journal_wake.snapshot() == wake


@pytest.mark.parametrize("reserved", [False, True])
def test_reset_excludes_fresh_historical_work_but_retains_existing_owners(effects, reserved):
    e = effects
    if reserved:
        e.effects.admit(e.request, "converse")
    e.store.reset(e.session.session_id)
    page = e.effects.pending(query())
    assert bool(page.items) == reserved and page.exhausted
    if reserved:
        assert not e.effects.lookup(page.items[0].query("converse_backchannel")).source_current


async def test_full_page_is_bounded_closed_and_native_ordered_across_continuation(live):
    for index in range(1, 34):
        next_outcome(live, index)
    journal = JournalClient("http://session.local", TOKENS["decisions"], transport=wire(live))
    try:
        first = await journal.pending_live_effects(query("agent_route"))
        assert len(first.items) == 32 and not first.exhausted
        assert len({item.event_id for item in first.items}) == 32
        second = await journal.pending_live_effects(
            query("agent_route", after=first.through_sequence)
        )
        assert len(second.items) == 1 and second.exhausted
        assert second.items[0].sequence > first.through_sequence
        assert first.items[0].publication.source.original.source_sequence < first.items[0].sequence
        assert not live.store.live_effects._present()
    finally:
        await journal.close()


@pytest.mark.parametrize("role", ["owner", "converse", "decisions", "agent", "resources", None])
async def test_pending_http_and_local_read_roles_match_without_mutation(effects, role):
    e = effects
    transport = wire(e)
    wake = e.store.journal_wake.snapshot()
    q = query()
    headers = {} if role is None else {"Authorization": "Bearer " + TOKENS[role]}
    async with httpx.AsyncClient(transport=transport, base_url="http://session.local") as client:
        response = await client.post(
            "/v2/transcript/live-effects/pending", headers=headers, json=q.model_dump(mode="json")
        )
    if role in {"owner", "converse", "decisions", "agent"}:
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json() == (
            await LocalJournal(e.store, role).pending_live_effects(q)
        ).model_dump(mode="json")
    else:
        assert response.status_code == (401 if role is None else 403)
        if role is not None:
            with pytest.raises(PermissionError):
                await LocalJournal(e.store, role).pending_live_effects(q)
    assert not e.effects._present() and e.store.journal_wake.snapshot() == wake
