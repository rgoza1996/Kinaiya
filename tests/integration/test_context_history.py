"""ADR 0057 native paging; synthetic journals and local/HTTP equivalence, no models."""

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from packages.contracts.client import JournalClient
from packages.contracts.context import (
    MAX_CONTEXT_PAGE_BYTES,
    MAX_CONTEXT_REQUEST_BYTES,
    ContextQualificationRequest,
)
from packages.contracts.models import AppendRequest, SessionRequest, SpeechWindow
from packages.providers.disclosure import entry_allowed
from services.common.auth import Auth
from services.converse.history import fold_users
from services.session.api import create_app
from services.session.store import JournalError, LocalJournal, Store


@pytest.fixture(params=["local", "http"])
async def env(tmp_path, request):
    store = Store(tmp_path / "synthetic.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    auth = Auth({role: role[0] * 32 for role in ("owner", "converse", "agent", "resources")})
    app = create_app(store, auth)
    journal = (
        LocalJournal(store, "converse")
        if request.param == "local"
        else JournalClient(
            "http://fixture", auth.tokens["converse"], transport=httpx.ASGITransport(app=app)
        )
    )
    result = SimpleNamespace(
        store=store, session=session, journal=journal, app=app, auth=auth, key=0
    )
    try:
        yield result
    finally:
        if isinstance(journal, JournalClient):
            await journal.close()
        store.close()


def write(env, kind="conversation.user", payload=None, *, role="converse", session=None):
    session = session or env.session
    env.key += 1
    return env.store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind=kind,
            payload=payload
            or {
                "text": f"Synthetic user {env.key}",
                "final": True,
                "revision": 1,
                "content_scope": "public_synthetic",
            },
            idempotency_key=f"synthetic-{env.key}",
        ),
        role,
    )


def window(env, utterance, index=0, revision=1, scope="public_synthetic"):
    return write(
        env,
        "conversation.window",
        SpeechWindow(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            utterance_id=utterance,
            window_index=index,
            revision=revision,
            text=f"Synthetic {utterance} {index}:{revision}",
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            content_scope=scope,
        ).model_dump(mode="json"),
    )


async def history(env, source, *, limit=3):
    page = await env.journal.context_history(source_event_id=source.event_id, limit=limit)
    pages, entries = [page], list(page.entries)
    while not page.exhausted:
        previous = page
        page = await env.journal.context_history(
            source_event_id=source.event_id,
            through_sequence=page.through_sequence,
            before_sequence=page.next_before_sequence,
            limit=limit,
        )
        assert page.through_sequence == previous.through_sequence
        assert (page.session_id, page.thread_id, page.epoch) == (
            source.session_id,
            source.thread_id,
            source.epoch,
        )
        assert all(e.sequence < previous.next_before_sequence for e in page.entries)
        pages.append(page)
        entries.extend(page.entries)
    return entries, pages


async def qualifications(env, source, through, bases, *, limit=3):
    options = dict(
        source_event_id=source.event_id,
        through_sequence=through,
        base_event_ids=[e.event_id for e in bases],
        limit=limit,
    )
    entries, pages = [], []
    while True:
        page = await env.journal.context_qualifications(**options)
        entries.extend(page.entries)
        pages.append(page)
        if page.exhausted:
            return entries, pages
        options["before_sequence"] = page.next_before_sequence


async def test_semantic_pages_ignore_operational_flood_and_match_local_store(env):
    old = [write(env) for _ in range(25)]
    for index in range(350):
        write(env, "converse.window_state", {"phase": "synthetic", "index": index})
        write(env, "resource.changed", {"index": index}, role="resources")
    foreign = env.store.create_session(SessionRequest(channel="converse"))
    write(env, session=foreign)
    source = write(env)
    entries, pages = await history(env, source)
    assert [e.event_id for e in entries] == [e.event_id for e in reversed(old)]
    assert len(pages) == 9
    assert pages[-1].exhausted and pages[-1].next_before_sequence is None
    assert all(e.kind == "conversation.user" for e in entries)
    for page, previous in zip(pages, [None, *pages[:-1]], strict=True):
        direct = env.store.context_history(
            source_event_id=source.event_id,
            through_sequence=previous.through_sequence if previous else None,
            before_sequence=previous.next_before_sequence if previous else None,
            limit=3,
        )
        assert page == direct


async def test_revision_closure_crosses_200_records_without_public_fallback(env):
    original = write(env)
    source = write(env)
    revisions = []
    for revision in range(2, 208):
        revisions.append(
            write(
                env,
                "conversation.revision",
                {
                    "target_event_id": original.event_id,
                    "text": f"Synthetic corrected {revision}",
                    "revision": revision,
                    "content_scope": "local_only" if revision == 101 else "public_synthetic",
                },
            )
        )
    bases, pages = await history(env, source)
    assert bases == [original]
    records, qualification_pages = await qualifications(
        env, source, pages[0].through_sequence, bases, limit=100
    )
    assert records == list(reversed(revisions))
    assert len(qualification_pages) == 3
    contributor = SimpleNamespace(local=False, provider="meta", model="synthetic-contributor")
    folded = fold_users(
        sorted([*bases, *records], key=lambda e: e.sequence),
        lambda entry: entry_allowed(contributor, entry),
    )[original.event_id]
    assert folded.text == "Synthetic corrected 207"
    assert folded.floor == "local_only" and folded.permitted is False


async def test_native_legacy_malformed_revision_is_returned_and_cannot_restore_public_text(env):
    original = write(env)
    source = write(env)
    malformed = write(
        env,
        "conversation.revision",
        {
            "target_event_id": original.event_id,
            "text": "Synthetic malformed private correction",
            "revision": 2.0,
            "content_scope": "local_only",
        },
    )
    later = write(
        env,
        "conversation.revision",
        {
            "target_event_id": original.event_id,
            "text": "Synthetic later public label",
            "revision": 3,
            "content_scope": "public_synthetic",
        },
    )
    page = await env.journal.context_history(source_event_id=source.event_id)
    records, _ = await qualifications(env, source, page.through_sequence, [original], limit=1)
    assert records == [later, malformed]
    folded = fold_users(sorted([original, *records], key=lambda e: e.sequence), lambda _: True)
    assert folded[original.event_id].permitted is False


async def test_watermark_freezes_revisions_and_includes_stop_before_late_chunk(env):
    original = write(env)
    stop = write(
        env,
        "delivery.updated",
        {
            "control": "generation_stop",
            "runtime_id": "synthetic-runtime",
            "generation_epoch": 1,
            "next_generation_epoch": 3,
            "status": "interrupted",
            "alignment": "delivery_unknown",
        },
    )
    assistant = write(
        env,
        "conversation.converse",
        {
            "text": "Synthetic late generated sentence.",
            "final": True,
            "runtime_id": "synthetic-runtime",
            "generation_epoch": 2,
            "chunk_id": "synthetic-chunk",
            "content_scope": "public_synthetic",
        },
    )
    receipt = write(
        env,
        "delivery.updated",
        {"event_id": assistant.event_id, "status": "played"},
    )
    write(env, "delivery.updated", {"event_id": "unrelated", "status": "played"})
    write(env, "delivery.updated", {**stop.payload, "runtime_id": "other-runtime"})
    write(env, "delivery.updated", {**stop.payload, "next_generation_epoch": 2})
    source = write(env)
    page = await env.journal.context_history(source_event_id=source.event_id)
    correction = write(
        env,
        "conversation.revision",
        {
            "target_event_id": original.event_id,
            "text": "Synthetic private correction",
            "revision": 2,
            "content_scope": "local_only",
        },
    )
    records, pages = await qualifications(
        env, source, page.through_sequence, [original, assistant], limit=1
    )
    assert records == [receipt, stop]
    assert stop.sequence < assistant.sequence
    assert all(p.base_event_ids == [original.event_id, assistant.event_id] for p in pages)
    fresh = await env.journal.context_history(source_event_id=source.event_id)
    records, _ = await qualifications(env, source, fresh.through_sequence, [original, assistant])
    assert records == [correction, receipt, stop]


async def test_window_heads_freeze_rank_before_disclosure_and_exclude_current_utterance(env):
    window(env, "prior", 0)
    head = window(env, "prior", 1)
    source = window(env, "current")
    window(env, "prior", 0, 2, "local_only")
    window(env, "future")
    frozen = await env.journal.context_history(source_event_id=source.event_id, limit=1)
    assert frozen.entries == [head] and frozen.exhausted
    private = window(env, "prior", 1, 2, "local_only")
    window(env, "current", 1)
    fresh = await env.journal.context_history(source_event_id=source.event_id, limit=1)
    assert fresh.entries == [private]
    assert private.sequence > source.sequence
    frozen_qualifications, _ = await qualifications(env, source, frozen.through_sequence, [head])
    assert frozen_qualifications == []
    fresh_qualifications, _ = await qualifications(env, source, fresh.through_sequence, [private])
    assert fresh_qualifications == []
    with pytest.raises((JournalError, httpx.HTTPStatusError)):
        await env.journal.context_qualifications(
            source_event_id=source.event_id,
            through_sequence=fresh.through_sequence,
            base_event_ids=[head.event_id],
        )


async def test_byte_limited_short_page_does_not_exhaust_or_consume_unreturned_row(env):
    old = [
        write(env, payload={"text": "x" * 60000, "final": True, "revision": 1}) for _ in range(9)
    ]
    source = write(env)
    entries, pages = await history(env, source, limit=200)
    assert entries == list(reversed(old))
    assert len(pages) == 3 and len(pages[0].entries) < 200 and not pages[0].exhausted
    assert all(
        sum(len(e.model_dump_json().encode()) for e in page.entries) <= MAX_CONTEXT_PAGE_BYTES
        for page in pages
    )


async def test_qualification_byte_pages_preserve_every_correction(env):
    original = write(env)
    source = write(env)
    revisions = [
        write(
            env,
            "conversation.revision",
            {
                "target_event_id": original.event_id,
                "revision": revision,
                "text": "x" * 60000,
                "content_scope": "local_only",
            },
        )
        for revision in range(2, 11)
    ]
    page = await env.journal.context_history(source_event_id=source.event_id)
    entries, pages = await qualifications(env, source, page.through_sequence, [original], limit=200)
    assert entries == list(reversed(revisions))
    assert len(pages) == 3 and not pages[0].exhausted and len(pages[0].entries) < 200


async def test_reset_refuses_both_page_chains_and_new_epoch_has_no_old_history(env):
    original = write(env)
    write(env)
    source = write(env)
    page = await env.journal.context_history(source_event_id=source.event_id, limit=1)
    env.session = env.store.reset(env.session.session_id)
    with pytest.raises((JournalError, httpx.HTTPStatusError)):
        await env.journal.context_history(
            source_event_id=source.event_id,
            through_sequence=page.through_sequence,
            before_sequence=page.next_before_sequence,
        )
    with pytest.raises((JournalError, httpx.HTTPStatusError)):
        await env.journal.context_qualifications(
            source_event_id=source.event_id,
            through_sequence=page.through_sequence,
            base_event_ids=[original.event_id],
        )
    new_source = write(env)
    new_page = await env.journal.context_history(source_event_id=new_source.event_id)
    assert new_page.entries == [] and new_page.exhausted


async def test_sources_and_qualification_bases_must_be_native_current_context(env):
    original = write(env)
    source = write(env)
    assistant = write(env, "conversation.converse")
    agent_session = env.store.create_session(SessionRequest(channel="agent"))
    agent_user = write(env, session=agent_session)
    for invalid_source in (assistant, agent_user):
        with pytest.raises((JournalError, httpx.HTTPStatusError)):
            await env.journal.context_history(source_event_id=invalid_source.event_id)
    foreign = env.store.create_session(SessionRequest(channel="converse"))
    foreign_user = write(env, session=foreign)
    page = await env.journal.context_history(source_event_id=source.event_id)
    for invalid_base in (source, assistant, foreign_user):
        with pytest.raises((JournalError, httpx.HTTPStatusError)):
            await env.journal.context_qualifications(
                source_event_id=source.event_id,
                through_sequence=page.through_sequence,
                base_event_ids=[original.event_id, invalid_base.event_id],
            )


async def test_forged_watermark_is_not_committed_source_coverage(env):
    original = write(env)
    source = write(env)
    for through in (original.sequence, source.sequence + 100):
        with pytest.raises((JournalError, httpx.HTTPStatusError)):
            await env.journal.context_history(
                source_event_id=source.event_id, through_sequence=through, before_sequence=1
            )
        with pytest.raises((JournalError, httpx.HTTPStatusError)):
            await env.journal.context_qualifications(
                source_event_id=source.event_id,
                through_sequence=through,
                base_event_ids=[original.event_id],
            )


async def test_oversized_native_row_fails_explicitly(env):
    original = write(env)
    source = write(env)
    raw = original.model_dump(mode="json")
    raw["payload"]["text"] = "x" * MAX_CONTEXT_PAGE_BYTES
    with env.store.db:
        env.store.db.execute(
            "UPDATE entries SET value=? WHERE event_id=?", (json.dumps(raw), original.event_id)
        )
    with pytest.raises((JournalError, httpx.HTTPStatusError)):
        await env.journal.context_history(source_event_id=source.event_id)


async def test_paging_authority_matches_local_and_http(env):
    original = write(env)
    source = write(env)
    page = await env.journal.context_history(source_event_id=source.event_id)
    for role in ("agent", "resources"):
        local = LocalJournal(env.store, role)
        with pytest.raises(PermissionError):
            await local.context_history(source_event_id=source.event_id)
        with pytest.raises(PermissionError):
            await local.context_qualifications(
                source_event_id=source.event_id,
                through_sequence=page.through_sequence,
                base_event_ids=[original.event_id],
            )
        client = JournalClient(
            "http://fixture", env.auth.tokens[role], transport=httpx.ASGITransport(app=env.app)
        )
        try:
            with pytest.raises(httpx.HTTPStatusError) as denied:
                await client.context_history(source_event_id=source.event_id)
            assert denied.value.response.status_code == 403
            with pytest.raises(httpx.HTTPStatusError) as denied:
                await client.context_qualifications(
                    source_event_id=source.event_id,
                    through_sequence=page.through_sequence,
                    base_event_ids=[original.event_id],
                )
            assert denied.value.response.status_code == 403
        finally:
            await client.close()
    assert (
        await LocalJournal(env.store, "owner").context_history(source_event_id=source.event_id)
    ) == page


@pytest.mark.parametrize(
    "overrides",
    [
        {"limit": True},
        {"limit": 0},
        {"limit": 201},
        {"limit": 1.0},
        {"before_sequence": 1},
        {"through_sequence": 1},
        {"through_sequence": 1, "before_sequence": 2},
    ],
)
async def test_invalid_history_requests_refuse_locally_before_io(env, overrides):
    source = write(env)
    with pytest.raises((JournalError, ValidationError)):
        await env.journal.context_history(source_event_id=source.event_id, **overrides)


@pytest.mark.parametrize("value", [[], ["duplicate", "duplicate"], ["x"] * 201, [True]])
def test_qualification_identity_list_is_strict_and_bounded(value):
    with pytest.raises(ValidationError):
        ContextQualificationRequest(
            source_event_id="source", through_sequence=1, base_event_ids=value
        )


async def test_qualification_http_json_is_bounded_unambiguous_and_strict(env):
    original = write(env)
    source = write(env)
    body = {
        "source_event_id": source.event_id,
        "through_sequence": source.sequence,
        "base_event_ids": [original.event_id],
    }
    async with httpx.AsyncClient(
        base_url="http://fixture",
        transport=httpx.ASGITransport(app=env.app),
        headers={"Authorization": "Bearer " + env.auth.tokens["converse"]},
    ) as client:
        for raw in (
            '{"limit":1,' + json.dumps({**body, "limit": 2})[1:],
            json.dumps({**body, "through_sequence": True}),
            json.dumps({**body, "limit": 1.0}),
            json.dumps({**body, "extra": "unrecognized"}),
            json.dumps(body).replace(
                f'"through_sequence": {source.sequence}', '"through_sequence": NaN'
            ),
        ):
            response = await client.post(
                "/v1/transcript/context-qualifications",
                content=raw,
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 422
        response = await client.post(
            "/v1/transcript/context-qualifications",
            content=b" " * (MAX_CONTEXT_REQUEST_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413


async def test_history_http_query_rejects_ambiguous_or_coerced_counters(env):
    source = write(env)
    async with httpx.AsyncClient(
        base_url="http://fixture",
        transport=httpx.ASGITransport(app=env.app),
        headers={"Authorization": "Bearer " + env.auth.tokens["converse"]},
    ) as client:
        for query in ("limit=1.0", "limit=1&limit=2", "limit=01", "extra=unknown"):
            response = await client.get(
                f"/v1/transcript/context-history?source_event_id={source.event_id}&{query}"
            )
            assert response.status_code == 422


@pytest.mark.parametrize("kind", ["conversation.revision", "delivery.updated"])
async def test_malformed_native_qualification_refuses_without_restoring_old_text(env, kind):
    base = write(
        env, "conversation.converse" if kind == "delivery.updated" else "conversation.user"
    )
    bad = write(
        env,
        kind,
        {
            "target_event_id": base.event_id,
            "event_id": base.event_id,
            "text": "Public correction",
            "revision": 2,
        },
    )
    source = write(env)
    page = await env.journal.context_history(source_event_id=source.event_id)
    with env.store.db:
        env.store.db.execute(
            "UPDATE entries SET value='not-json' WHERE event_id=?", (bad.event_id,)
        )
    try:
        with pytest.raises((JournalError, httpx.HTTPStatusError)) as error:
            await env.journal.context_qualifications(
                source_event_id=source.event_id,
                through_sequence=page.through_sequence,
                base_event_ids=[base.event_id],
            )
        if isinstance(error.value, httpx.HTTPStatusError):
            assert error.value.response.status_code == 409
            assert error.value.response.json()["detail"] == "Native context records are unavailable"
        else:
            assert str(error.value) == "Native context records are unavailable"
    finally:
        # Leave fixture cleanup independent of deliberately malformed bytes.
        with env.store.db:
            env.store.db.execute("DELETE FROM entries WHERE event_id=?", (bad.event_id,))
