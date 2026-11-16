"""Durable canonical discovery through native SQL, local and authenticated HTTP."""

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.live_decisions_pending import LiveDecisionsPendingQuery
from services.common.auth import Auth
from services.session.api import create_app
from services.session.live_windows import publish
from services.session.store import LocalJournal
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_capture_permission import fence
from tests.integration.test_live_decisions_attempts import admission, finish, publication
from tests.integration.test_live_decisions_attempts import live as live
from tests.integration.test_live_window_publication import original


def query(after=0, limit=32):
    return LiveDecisionsPendingQuery(
        schema_version="2.0", protocol="native_live_decisions_v2", after_sequence=after, limit=limit
    )


def utterance(live, number):
    request = original(live, utterance_id=f"public-live-scope-{number}")
    request = request.model_copy(
        update={
            "request": request.request.model_copy(
                update={"idempotency_key": f"public-pending-{number}"}
            )
        }
    )
    return publish(live.store, request)


def test_discovery_is_passive_and_unchanged_raw_input_keeps_the_original_cursor(live):
    owner = live.store.live_decisions_attempts
    first = owner.pending(query())
    assert first.sources == [live.source] and not first.has_more
    assert first.through_sequence == live.source.source_sequence
    for index in range(1, 50):
        publish(live.store, original(live, index))
    wake = live.store.journal_wake.snapshot()
    assert owner.pending(query()) == first
    assert owner.pending(query(first.through_sequence)).sources == []
    assert live.store.journal_wake.snapshot() == wake
    assert live.store.db.execute("SELECT count(*) FROM decisions_attempts").fetchone()[0] == 0


def test_bounded_pages_recover_every_current_scope_without_an_ephemeral_ack(live):
    expected = [live.source.source_event_id]
    expected.extend(utterance(live, number).source.event_id for number in range(40))
    first = live.owner.pending(query())
    assert len(first.sources) == 32 and first.has_more
    second = live.owner.pending(query(first.through_sequence))
    assert len(second.sources) == 9 and not second.has_more
    assert [s.source_event_id for s in first.sources + second.sources] == expected
    # A replacement consumer begins at zero: no RAM queue ACK lost these inputs.
    assert live.owner.pending(query()) == first
    assert live.owner.pending(query(second.through_sequence)).sources == []


def test_latest_eligible_revision_coalesces_without_losing_historical_input(live):
    pair = publish(live.store, original(live, 1, text="Public fresh input while inference runs"))
    page = live.owner.pending(query())
    assert len(page.sources) == 1
    assert page.sources[0].source_event_id == pair.source.event_id
    assert page.sources[0].eligible_revision == 2
    assert live.store.entry(live.source.source_event_id) == live.pair.source


def test_completed_unpublished_input_survives_consumer_restart_and_exact_publish_removes_it(live):
    attempt = live.owner.admit(admission(live))
    assert live.owner.pending(query()).sources == [live.source]
    completed = live.owner.finish(finish(live, attempt))
    assert live.owner.pending(query()).sources == [live.source]
    live.owner.publish_result(publication(live, completed))
    assert live.owner.pending(query()).sources == []
    assert live.store.db.execute("SELECT count(*) FROM decisions_attempts").fetchone()[0] == 1


@pytest.mark.parametrize("cause", ["owner-off", "reset"])
def test_closed_capture_scope_is_removed_without_synthesizing_physical_cleanup(live, cause):
    if cause == "owner-off":
        live.store.live_captures.fence(fence(live.source.capture.original))
    else:
        live.store.reset(live.session.session_id)
    assert live.owner.pending(query()).sources == []
    assert live.store.entry(live.source.source_event_id) == live.pair.source


@pytest.mark.parametrize("role", ["owner", "converse", "decisions", "agent", "resources", "none"])
async def test_pending_discovery_requires_native_decisions_role_and_preserves_query(live, role):
    auth = Auth(
        {
            r: r + "-public-pending-token-000000000"
            for r in ["owner", "converse", "decisions", "agent", "resources"]
        }
    )
    transport = httpx.ASGITransport(app=create_app(live.store, auth))
    if role == "decisions":
        client = JournalClient("http://session.local", auth.tokens[role], transport=transport)
        try:
            result = await client.pending_live_decisions(query())
            assert result.sources == [live.source]
            assert result == await LocalJournal(live.store, role).pending_live_decisions(query())
            assert not client._submit_exchanges
        finally:
            await client.close()
    else:
        headers = {} if role == "none" else {"Authorization": "Bearer " + auth.tokens[role]}
        async with httpx.AsyncClient(
            transport=transport, base_url="http://session.local"
        ) as client:
            result = await client.post(
                "/v2/transcript/live-decisions/pending",
                json=query().model_dump(mode="json"),
                headers=headers,
            )
        assert result.status_code == (401 if role == "none" else 403)
        with pytest.raises(PermissionError):
            await LocalJournal(live.store, role).pending_live_decisions(query())


@pytest.mark.parametrize(
    "change",
    [
        {"limit": 0},
        {"limit": 33},
        {"limit": True},
        {"after_sequence": -1},
        {"after_sequence": 1.0},
        {"protocol": "1.0"},
        {"provider_authorized": True},
    ],
)
def test_discovery_bounds_do_not_coerce_or_grant_authority(change):
    with pytest.raises(ValueError):
        LiveDecisionsPendingQuery.model_validate(query().model_dump(mode="json") | change)
