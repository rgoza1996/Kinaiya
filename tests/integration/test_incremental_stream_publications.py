"""Exact read-only journal evidence is separate from authorized retirement."""

import asyncio

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.models import AppendRequest, SpeechWindow
from packages.contracts.stt_settlement import digest
from packages.contracts.whistle_stream_owner import PUBLICATION_PROTOCOL, StreamPublication
from services.session.api import create_app as session_app
from tests.integration.test_incremental_stream_ownership import (
    install_settings,
    pcm_for,
    scope_for,
    stream_state,
)
from tests.integration.test_incremental_whistle_converse import AUTH
from tests.integration.test_incremental_whistle_converse import (
    converse_incremental_env as converse_incremental_env,
)

BASE = "/v1/converse/audio/stream"


@pytest.mark.parametrize("value", ["x" * 129, "é" * 65])
async def test_corrupt_publication_metadata_is_bounded_before_leaving_sqlite(
    converse_incremental_env, value
):
    from services.session.stream_publications import lookup_publication

    env = converse_incremental_env
    entry = await env.window_controller.ingest(
        SpeechWindow(
            **scope_for(env),
            window_index=0,
            revision=1,
            text="PUBLIC BOUNDED METADATA",
            start_ms=0,
            end_ms=10,
            user_speaking=True,
            capture_ended=False,
            audio_sha256="1" * 64,
        )
    )
    request = entry.model_dump(
        mode="json", exclude={"event_id", "sequence", "origin", "created_at"}
    )
    descriptor = StreamPublication(
        schema_version="1.0",
        protocol=PUBLICATION_PROTOCOL,
        session_id=entry.session_id,
        thread_id=entry.thread_id,
        epoch=entry.epoch,
        idempotency_key=entry.idempotency_key,
        request_sha256=digest(request),
    )
    # Observe values at the actual SQLite/Python boundary, before model validation.
    env.store.db.execute("UPDATE entries SET event_id=? WHERE event_id=?", (value, entry.event_id))
    rows, original_factory = [], env.store.db.row_factory

    def observe(cursor, row):
        rows.append(row)
        return original_factory(cursor, row) if original_factory is not None else row

    env.store.db.row_factory = observe
    try:
        with pytest.raises(ValueError):
            lookup_publication(env.store, descriptor)
    finally:
        env.store.db.row_factory = original_factory
    assert len(rows) == 1 and rows[0][2] is None
    assert env.engine.begin_calls == env.engine.step == env.engine.finish_calls == 0


async def test_session_http_publication_proof_binds_exact_original_without_append(
    converse_incremental_env,
):
    env = converse_incremental_env
    window = SpeechWindow(
        **scope_for(env),
        window_index=0,
        revision=1,
        text="PUBLIC EXACT ORIGINAL",
        start_ms=0,
        end_ms=10,
        user_speaking=True,
        capture_ended=False,
        audio_sha256="1" * 64,
    )
    expected = AppendRequest(
        session_id=window.session_id,
        thread_id=window.thread_id,
        epoch=window.epoch,
        kind="conversation.window",
        payload=window.model_dump(mode="json"),
        idempotency_key=f"speech-window:{window.epoch}:{window.utterance_id}:0:r1",
    )
    descriptor = StreamPublication(
        schema_version="1.0",
        protocol=PUBLICATION_PROTOCOL,
        session_id=expected.session_id,
        thread_id=expected.thread_id,
        epoch=expected.epoch,
        idempotency_key=expected.idempotency_key,
        request_sha256=digest(expected.model_dump(mode="json")),
    )
    app = session_app(env.store, AUTH)
    transport = httpx.ASGITransport(app=app)
    journal = JournalClient("http://session.local", AUTH.tokens["converse"], transport=transport)
    try:
        absent = await journal.lookup_stream_publication(descriptor.model_dump())
        assert absent["entry"] is None and env.store.read(kinds=["conversation.window"]) == []
        entry = await env.window_controller.ingest(window)
        proof = await journal.lookup_stream_publication(descriptor.model_dump())
        assert proof["entry"] == entry.model_dump(mode="json")
        assert (
            proof["original"] == descriptor.model_dump()
            and len(env.store.read(kinds=["conversation.window"])) == 1
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://session.local"
        ) as client:
            for role in ("owner", "agent", "resources"):
                denied = await client.post(
                    "/v1/transcript/stream-publications/lookup",
                    json=descriptor.model_dump(),
                    headers={"Authorization": "Bearer " + AUTH.tokens[role]},
                )
                assert denied.status_code == 403
            changed = await client.post(
                "/v1/transcript/stream-publications/lookup",
                json={**descriptor.model_dump(), "request_sha256": "f" * 64},
                headers={"Authorization": "Bearer " + AUTH.tokens["converse"]},
            )
            assert changed.status_code == 503
        assert env.engine.begin_calls == env.engine.step == env.engine.finish_calls == 0
        assert len(env.store.read(kinds=["conversation.window"])) == 1
    finally:
        await journal.close()


async def test_finish_lookup_is_read_only_and_reconcile_retires_without_second_append(
    converse_incremental_env,
):
    env = converse_incremental_env
    settings = install_settings(env)
    try:
        start = await env.client.post(BASE + "/begin", json=scope_for(env))
        assert start.status_code == 200
        pcm = await env.client.post(BASE + "/pcm", json=pcm_for(env, start.json()))
        assert pcm.status_code == 200
        original = {
            **scope_for(env),
            "stream_id": start.json()["stream_id"],
            "worker_incarnation": start.json()["worker_incarnation"],
            "sequence": 2,
            "sample_offset": 160,
            "capture_ended": False,
            "user_speaking": True,
        }
        ingest, attempts = env.window_controller.ingest, []

        async def lose_publication_ack(window):
            attempts.append(window.model_dump(mode="json"))
            await ingest(window)
            raise httpx.ReadError("PUBLIC-PRIVATE-TRANSCRIPT-MUST-NOT-REACH-ERROR")

        env.window_controller.ingest = lose_publication_ack
        try:
            with pytest.raises(
                RuntimeError, match="original stream result remains unconfirmed"
            ) as error:
                await env.client.post(BASE + "/finish", json=original)
            assert "PUBLIC-PRIVATE-TRANSCRIPT" not in str(error.value)
        finally:
            env.window_controller.ingest = ingest
        state = stream_state(env)
        failed = state.pending
        broker_record = env.broker.stream_owner.records[failed.native_original.operation_id]
        assert broker_record.state == "completed" and state.settlement is None
        assert len(attempts) == 1 and len(env.store.read(kinds=["conversation.window"])) == 2
        metadata = {
            "schema_version": "1.0",
            "protocol": "converse_stream_owner_v1",
            "owner_instance": (await env.client.get(BASE + "/ownership")).json()["owner_instance"],
            "request_sha256": digest(original),
        }
        proof = await env.client.post(BASE + "/finish/lookup", json=metadata)
        assert proof.status_code == 200 and proof.json()["outcome"] == "confirmed"
        assert (
            proof.json()["decoder_finished"] is True and proof.json()["ownership_retired"] is False
        )
        assert broker_record.state == "completed" and state.settlement is None
        assert not state.lifetime.done() and settings.snapshot()["busy"] is True
        assert len(attempts) == 1 and env.engine.finish_calls == 1
        reconciled = await env.client.post(BASE + "/finish/reconcile", json=metadata)
        assert reconciled.status_code == 200 and reconciled.json()["outcome"] == "confirmed"
        await asyncio.wait_for(asyncio.shield(state.lifetime), 5)
        assert (
            broker_record.state == "acknowledged"
            and not settings.leases
            and not settings._admissions()
        )
        retained = len(state.native.dependencies)
        for _ in range(20):
            observed = await env.client.post(BASE + "/finish/lookup", json=metadata)
            assert observed.status_code == 200 and observed.json()["ownership_retired"] is True
        assert len(state.native.dependencies) == retained
        assert len(attempts) == 1 and env.engine.finish_calls == 1
        assert len(env.store.read(kinds=["conversation.window"])) == 2
    finally:
        await settings.close()
