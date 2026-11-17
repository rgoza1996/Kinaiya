"""Native live revision equivalence preserves content authority and legacy strictness."""

import httpx
import pytest

from packages.contracts.decisions_attempts import DecisionsSource
from packages.contracts.live_agent_sources import LiveAgentSourceQuery
from packages.contracts.models import AppendRequest
from services.session.live_agent_sources import authority
from services.session.live_windows import publish
from services.session.store import JournalError, LocalJournal
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_decisions_attempts import live as live
from tests.integration.test_live_effect_http import TOKENS, wire
from tests.integration.test_live_window_publication import original


def query(live, revision=1, source=None):
    return LiveAgentSourceQuery(
        schema_version="2.0",
        protocol="native_live_agent_source_v2",
        source=live.source.original if source is None else DecisionsSource.of(source),
        expected_revision=revision,
    )


def test_actual_captured_raw_revisions_qualify_without_writes_or_capture_permission(live):
    publish(live.store, original(live, revision=2))
    wake = live.store.journal_wake.snapshot()
    for revision in [1, 2]:
        result = authority(live.store, query(live, revision))
        assert result.live and result.revision_equivalent
    assert not authority(live.store, query(live, 3)).revision_equivalent
    assert live.store.journal_wake.snapshot() == wake
    assert not live.store.live_effects._present()


@pytest.mark.parametrize(
    "change,equivalent",
    [
        ({"text": "A different public instruction"}, False),
        ({"content_scope": "local_only"}, False),
        ({"capture_ended": True, "user_speaking": False}, True),
    ],
)
def test_equivalence_checks_historical_content_scope_and_separate_speaking_hints(
    live, change, equivalent
):
    publish(live.store, original(live, revision=2, **change))
    state = authority(live.store, query(live))
    assert state.live and state.revision_equivalent is equivalent
    assert authority(live.store, query(live, 2)).revision_equivalent


def test_legacy_windows_do_not_acquire_native_equivalence(live):
    request = original(live, utterance_id="public-legacy").request.model_copy(
        update={
            "idempotency_key": "legacy-public-1",
        }
    )
    source = live.store.append(request, "owner")
    live.store.append(
        AppendRequest(
            **(
                request.model_dump(mode="json")
                | {
                    "payload": request.payload | {"revision": 2},
                    "idempotency_key": "legacy-public-2",
                }
            )
        ),
        "owner",
    )
    state = authority(live.store, query(live, source=source))
    assert not state.live and not state.revision_equivalent


def test_changed_then_restored_capture_text_does_not_revive_old_revision_authority(live):
    publish(live.store, original(live, revision=2, text="Public changed instruction"))
    publish(live.store, original(live, revision=3))
    assert not authority(live.store, query(live)).revision_equivalent
    assert authority(live.store, query(live, 3)).revision_equivalent


def test_manual_transcript_edits_do_not_become_native_capture_equivalence(live):
    source = live.pair.source
    revision, text = 2, "Public owner edit"
    with pytest.raises(JournalError):
        live.store.append(
            AppendRequest(
                session_id=source.session_id,
                thread_id=source.thread_id,
                epoch=source.epoch,
                kind="conversation.revision",
                payload={"target_event_id": source.event_id, "revision": revision, "text": text},
                idempotency_key=f"public-owner-edit-{revision}",
            ),
            "owner",
        )
    assert live.store.resolve(source.event_id).payload == source.payload
    assert authority(live.store, query(live)).revision_equivalent


@pytest.mark.parametrize("role", ["owner", "converse", "decisions", "agent", "resources", None])
async def test_source_qualification_http_local_role_parity_and_no_mutation(live, role):
    transport = wire(live)
    wake = live.store.journal_wake.snapshot()
    q = query(live)
    headers = {} if role is None else {"Authorization": "Bearer " + TOKENS[role]}
    async with httpx.AsyncClient(transport=transport, base_url="http://session.local") as client:
        response = await client.post(
            "/v2/transcript/live-effects/source-authority",
            headers=headers,
            json=q.model_dump(mode="json"),
        )
    if role in {"owner", "converse", "decisions", "agent"}:
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json() == (
            await LocalJournal(live.store, role).live_agent_source_authority(q)
        ).model_dump(mode="json")
    else:
        assert response.status_code == (401 if role is None else 403)
        if role is not None:
            with pytest.raises(PermissionError):
                await LocalJournal(live.store, role).live_agent_source_authority(q)
    assert live.store.journal_wake.snapshot() == wake
