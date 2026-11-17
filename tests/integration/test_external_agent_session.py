"""External Agent grants and scoped pages across the real Session HTTP boundary."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.contracts.client import JournalClient
from packages.contracts.external_agent import (
    ExternalAgentBind,
    ExternalAgentClaim,
    ExternalAgentConnect,
    ExternalAgentReadTranscript,
    ExternalAgentRevoke,
    ExternalAgentScope,
    ExternalAgentSource,
    ExternalAgentSubmit,
    ExternalAgentTarget,
    ExternalAgentTranscriptPosition,
)
from packages.contracts.external_agent_client import (
    ExternalAgentNativeConflict,
    ExternalAgentSessionClient,
)
from packages.contracts.models import (
    AppendRequest,
    Decision,
    SessionRequest,
    SpeechWindow,
    WindowDecision,
    window_agent_key,
)
from services.common.auth import Auth
from services.session.api import create_app
from services.session.store import Store
from tests.integration.test_agent_submit_admission import ADMIT, TASK_B
from tests.integration.test_agent_submit_admission import proposal as native_proposal
from tests.integration.test_required_decisions_readiness import TOKENS, loopback


@asynccontextmanager
async def native(path):
    store = Store(path)
    try:
        async with loopback(create_app(store, Auth(TOKENS))) as url:
            owner = JournalClient(url, TOKENS["owner"])
            decisions = JournalClient(url, TOKENS["decisions"])
            try:
                yield SimpleNamespace(
                    store=store,
                    owner=ExternalAgentSessionClient(owner),
                    decisions=ExternalAgentSessionClient(decisions),
                    http=decisions.http,
                    owner_http=owner.http,
                )
            finally:
                await decisions.close()
                await owner.close()
    finally:
        store.close()


def scope_of(session):
    return ExternalAgentScope(
        session_id=session.session_id, thread_id=session.thread_id, epoch=session.epoch
    )


def connection(session, *, scopes=("public_synthetic",), backend="pi-original"):
    return ExternalAgentConnect(
        contract_version=1,
        request_id=uuid4().hex,
        operation="connect",
        profile_id="pi-fixture",
        scope=scope_of(session),
        allowed_content=scopes,
        expected_backend_session_id=backend,
    )


def bind_to(grant, backend="pi-original"):
    return ExternalAgentBind(
        contract_version=1,
        request_id=uuid4().hex,
        operation="bind",
        target=ExternalAgentTarget(attachment_id=grant.attachment_id, backend_session_id=backend),
    )


def read(target, *, position=None, through=None, limit=128):
    return ExternalAgentReadTranscript(
        contract_version=1,
        request_id=uuid4().hex,
        operation="read_transcript",
        target=target,
        after=position or ExternalAgentTranscriptPosition(sequence=0, byte_offset=0),
        through_sequence=through,
        limit=limit,
    )


def add(store, session, kind, text, *, scope="public_synthetic", causes=(), role="owner", **extra):
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind=kind,
            causes=list(causes),
            idempotency_key=uuid4().hex,
            payload={"text": text, "content_scope": scope, "final": True, **extra},
        ),
        role,
    )


def queued_claim(store, session, source, binding, instruction=None):
    revision = source.payload.get("revision", 1)
    primary = (
        source.payload["target_event_id"]
        if source.kind == "conversation.revision"
        else source.event_id
    )
    instruction = source.payload["text"] if instruction is None else instruction
    if source.kind == "conversation.window":
        decision = WindowDecision(
            source_event_id=source.event_id,
            source_sequence=source.sequence,
            source_revision=revision,
            conversation="WAIT_FOR_NEXT_WINDOW",
            agent_route="QUEUE_UNTIL_TASK_DONE",
        )
        kind = "routing.window_decided"
        key = window_agent_key(decision.decision_id, decision.agent_route)
    else:
        decision = Decision(
            source_event_ids=[primary],
            source_revision=revision,
            route="queue",
            instruction=instruction,
            idempotency_key=uuid4().hex,
        )
        kind, key = "routing.decided", decision.idempotency_key
    event = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind=kind,
            payload=decision.model_dump(mode="json"),
            causes=[source.event_id],
            idempotency_key=key,
        ),
        "decisions",
    )
    return ExternalAgentClaim(
        contract_version=1,
        request_id=uuid4().hex,
        operation="claim",
        decision_event_id=event.event_id,
        command=ExternalAgentSubmit(
            contract_version=1,
            request_id=uuid4().hex,
            operation="submit",
            target=binding.target,
            source=ExternalAgentSource(event_ids=(primary,), revision=revision),
            idempotency_key=key,
            instruction=instruction,
            content_scope="public_synthetic",
            execution_policy="backend_owned",
            queue_if_busy=True,
        ),
    )


async def test_exact_finalized_correction_claim_uses_root_and_revision_cause(tmp_path):
    async with native(tmp_path / "journal.sqlite") as peers:
        session = peers.store.create_session(SessionRequest(channel="converse"))
        source = add(peers.store, session, "conversation.user", "Original synthetic task")
        correction = add(
            peers.store,
            session,
            "conversation.revision",
            "Corrected synthetic task",
            revision=2,
            target_event_id=source.event_id,
        )
        grant = await peers.owner.authorize(connection(session))
        binding = bind_to(grant)
        await peers.decisions.bind(binding)
        claim = queued_claim(peers.store, session, correction, binding)
        assert claim.command.source.event_ids == (source.event_id,)
        assert peers.store.entry(claim.decision_event_id).causes == [correction.event_id]

        receipt = await peers.decisions.claim(claim)
        assert receipt.disposition == "original"
        assert (await peers.decisions.claim(claim)).admission_id == receipt.admission_id
        replacement = add(
            peers.store,
            session,
            "conversation.revision",
            "Another correction",
            revision=3,
            target_event_id=source.event_id,
        )
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.claim(queued_claim(peers.store, session, replacement, binding))


@pytest.mark.parametrize("invalid", ["stale", "private_root", "unrelated_root"])
async def test_correction_cannot_change_the_claims_native_authority(tmp_path, invalid):
    async with native(tmp_path / "journal.sqlite") as peers:
        session = peers.store.create_session(SessionRequest(channel="converse"))
        source = add(
            peers.store,
            session,
            "conversation.user",
            "Original synthetic task",
            scope="local_only" if invalid == "private_root" else "public_synthetic",
        )
        correction = add(
            peers.store,
            session,
            "conversation.revision",
            "Corrected synthetic task",
            revision=2,
            target_event_id=source.event_id,
        )
        grant = await peers.owner.authorize(connection(session))
        binding = bind_to(grant)
        await peers.decisions.bind(binding)
        claim = queued_claim(peers.store, session, correction, binding)
        if invalid == "stale":
            add(
                peers.store,
                session,
                "conversation.revision",
                "Newer correction",
                revision=3,
                target_event_id=source.event_id,
            )
        elif invalid == "unrelated_root":
            other = add(peers.store, session, "conversation.user", "Other synthetic task")
            command = claim.command.model_copy(
                update={"source": ExternalAgentSource(event_ids=(other.event_id,), revision=2)}
            )
            claim = claim.model_copy(update={"command": command})

        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.claim(claim)
        assert (
            peers.store.db.execute("SELECT count(*) FROM external_agent_claims").fetchone()[0] == 0
        )


async def test_one_exact_claim_survives_duplicate_delivery_and_restart(tmp_path):
    path = tmp_path / "journal.sqlite"
    async with native(path) as peers:
        session = peers.store.create_session(SessionRequest(channel="converse"))
        source = add(peers.store, session, "conversation.user", "Original request")
        grant = await peers.owner.authorize(connection(session))
        binding = bind_to(grant)
        await peers.decisions.bind(binding)
        request = queued_claim(peers.store, session, source, binding)
        duplicate = request.model_copy(
            update={
                "request_id": "correlation-replay",
                "command": request.command.model_copy(
                    update={"request_id": "nested-correlation-replay"}
                ),
            }
        )
        with pytest.raises(PermissionError):
            await peers.owner.claim(request)
        with pytest.raises(PermissionError):
            await peers.owner.claim(request, retain_only=True)
        assert await peers.decisions.claim(request, retain_only=True) is None
        competing = await asyncio.gather(
            peers.decisions.claim(request),
            peers.decisions.claim(duplicate),
        )
        assert {reply.disposition for reply in competing} == {"original", "retained"}
        (admission_id,) = {reply.admission_id for reply in competing}

    async with native(path) as peers:
        retained = await peers.decisions.claim(request, retain_only=True)
        assert retained.disposition == "retained" and retained.admission_id == admission_id
        changed = request.model_copy(
            update={"command": request.command.model_copy(update={"instruction": "Changed text"})}
        )
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.claim(changed)
        another_route = queued_claim(peers.store, session, source, binding)
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.claim(another_route)


async def test_claim_checks_native_route_text_revision_and_disclosure(tmp_path):
    async with native(tmp_path / "journal.sqlite") as peers:
        session = peers.store.create_session(SessionRequest(channel="converse"))
        grant = await peers.owner.authorize(connection(session))
        binding = bind_to(grant)
        await peers.decisions.bind(binding)
        source = add(peers.store, session, "conversation.user", "Queued text")
        request = queued_claim(peers.store, session, source, binding)
        forged = request.model_copy(update={"decision_event_id": source.event_id})
        changed = request.model_copy(
            update={
                "command": request.command.model_copy(
                    update={"instruction": "Altered forwarded text"}
                )
            }
        )
        for invalid in (forged, changed):
            with pytest.raises(ExternalAgentNativeConflict):
                await peers.decisions.claim(invalid)
        add(
            peers.store,
            session,
            "conversation.revision",
            "Newer text",
            revision=2,
            target_event_id=source.event_id,
        )
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.claim(request)
        private = add(peers.store, session, "conversation.user", "Local text", scope="local_only")
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.claim(queued_claim(peers.store, session, private, binding))

        handed_off = add(peers.store, session, "conversation.user", "Previously dispatched text")
        previous = queued_claim(peers.store, session, handed_off, binding)
        decision = peers.store.entry(previous.decision_event_id)
        peers.store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="routing.dispatch",
                causes=[handed_off.event_id],
                idempotency_key=uuid4().hex,
                payload={"decision_id": decision.payload["decision_id"], "state": "unknown"},
            ),
            "decisions",
        )
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.claim(previous)

        model = SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id="claim-window",
            window_index=0,
            revision=1,
            text="Verified spoken work",
            start_ms=0,
            end_ms=120,
            user_speaking=False,
            capture_ended=True,
            content_scope="public_synthetic",
        )
        rolling = peers.store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.window",
                payload=model.model_dump(mode="json"),
                idempotency_key=uuid4().hex,
            ),
            "owner",
        )
        claimed = await peers.decisions.claim(queued_claim(peers.store, session, rolling, binding))
        assert claimed.disposition == "original"


@pytest.mark.parametrize("first_executor", ["pi", "native", "concurrent"])
async def test_original_decisions_source_is_reserved_to_one_executor(tmp_path, first_executor):
    async with native(tmp_path / "journal.sqlite") as peers:
        session = peers.store.create_session(SessionRequest(channel="converse"))
        source = add(peers.store, session, "conversation.user", "One synthetic route")
        grant = await peers.owner.authorize(connection(session))
        binding = bind_to(grant)
        await peers.decisions.bind(binding)
        external = queued_claim(peers.store, session, source, binding)
        native_request = native_proposal(
            session,
            key=external.command.idempotency_key,
            content_scope="public_synthetic",
            objective=external.command.instruction,
            source_event_ids=[source.event_id],
        )
        native_request = native_request.model_copy(
            update={
                "causes": [source.event_id],
                "payload": {**native_request.payload, "actor": "decisions"},
            }
        )
        headers = {"Authorization": "Bearer " + TOKENS["agent"]}
        wire = native_request.model_dump(mode="json")
        if first_executor == "pi":
            assert (await peers.decisions.claim(external)).disposition == "original"
            blocked = await peers.http.post(ADMIT, json=wire, headers=headers)
            assert blocked.status_code == 409, blocked.text
            assert not peers.store.read(kinds=["agent.command"])
        elif first_executor == "native":
            original = await peers.http.post(ADMIT, json=wire, headers=headers)
            assert original.status_code == 200 and original.json()["disposition"] == "created"
            with pytest.raises(ExternalAgentNativeConflict):
                await peers.decisions.claim(external)
            retained = await peers.http.post(ADMIT, json=wire, headers=headers)
            assert retained.status_code == 200 and retained.json()["disposition"] == "retained"
            assert retained.json()["entry"] == original.json()["entry"]
        else:
            native_result, external_result = await asyncio.gather(
                peers.http.post(ADMIT, json=wire, headers=headers),
                peers.decisions.claim(external),
                return_exceptions=True,
            )
            if native_result.status_code == 200:
                assert isinstance(external_result, ExternalAgentNativeConflict)
                assert native_result.json()["disposition"] == "created"
            else:
                assert (
                    native_result.status_code == 409 and external_result.disposition == "original"
                )

        separate_owner = native_proposal(session, task_id=TASK_B, key="independent-owner-action")
        accepted = await peers.http.post(
            ADMIT,
            json=separate_owner.model_dump(mode="json"),
            headers=headers,
        )
        assert accepted.status_code == 200 and accepted.json()["disposition"] == "created"


@pytest.mark.parametrize("superseded", [False, True])
async def test_rolling_claim_checks_utterance_progress_atomically(tmp_path, superseded):
    async with native(tmp_path / "journal.sqlite") as peers:
        session = peers.store.create_session(SessionRequest(channel="converse"))
        grant = await peers.owner.authorize(connection(session))
        binding = bind_to(grant)
        await peers.decisions.bind(binding)

        def window(index, text, *, utterance="continuous-utterance"):
            model = SpeechWindow(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                utterance_id=utterance,
                window_index=index,
                text=text,
                start_ms=0,
                end_ms=100 * (index + 1),
                user_speaking=True,
                content_scope="public_synthetic",
            )
            return peers.store.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="conversation.window",
                    payload=model.model_dump(mode="json"),
                    idempotency_key=uuid4().hex,
                ),
                "owner",
            )

        initial = window(0, "Original spoken instruction")
        first = queued_claim(peers.store, session, initial, binding)
        if superseded:
            window(1, "Updated spoken instruction")
            proposal = first
        else:
            assert (await peers.decisions.claim(first)).disposition == "original"
            repeated = window(1, "Original spoken instruction")
            proposal = queued_claim(peers.store, session, repeated, binding)

        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.claim(proposal)
        fresh = window(2, "A fresh update")
        assert (
            await peers.decisions.claim(queued_claim(peers.store, session, fresh, binding))
        ).disposition == "original"
        independent = window(0, fresh.payload["text"], utterance="new-utterance")
        assert (
            await peers.decisions.claim(queued_claim(peers.store, session, independent, binding))
        ).disposition == "original"


async def test_session_retains_exact_grant_and_binding_across_restart(tmp_path):
    path = tmp_path / "journal.sqlite"
    async with native(path) as peers:
        conversation = peers.store.create_session(SessionRequest(channel="converse"))
        original = connection(conversation)
        with pytest.raises(PermissionError):
            await peers.decisions.authorize(original)
        proof = await peers.owner.authorize(original)
        assert proof.phase == "authorized" and proof.backend_session_id is None
        pending = bind_to(proof)
        with pytest.raises(PermissionError):
            await peers.owner.bind(pending)
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.transcript(read(pending.target), scope=original.scope)
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.bind(bind_to(proof, "wrong-backend"))
        bound = await peers.decisions.bind(pending)
        assert bound.phase == "bound" and bound.original == original
        add(peers.store, conversation, "conversation.user", "Visible through the original binding")

    async with native(path) as peers:
        assert await peers.decisions.bind(pending) == bound
        replay = await peers.owner.authorize(original)
        assert replay.attachment_id == proof.attachment_id and replay.phase == "bound"
        replaced = original.model_copy(update={"allowed_content": ("local_only",)})
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.owner.authorize(replaced)
        history = await peers.decisions.transcript(read(pending.target), scope=original.scope)
        assert [entry.text for entry in history.entries] == ["Visible through the original binding"]
        assert history.has_more is False

        state = ExternalAgentRevoke(
            contract_version=1,
            request_id=uuid4().hex,
            operation="revoke",
            attachment_id=proof.attachment_id,
        )
        with pytest.raises(PermissionError):
            await peers.decisions.revoke(state)
        assert (await peers.owner.revoke(state)).phase == "revoked"
        assert (await peers.owner.authorize(original)).phase == "revoked"
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.transcript(read(pending.target), scope=original.scope)
        foreign = peers.store.create_session(SessionRequest(channel="converse"))
        new = await peers.owner.authorize(connection(foreign))
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.bind(bind_to(new))


async def test_only_one_original_backend_can_bind_an_owner_grant(tmp_path):
    async with native(tmp_path / "journal.sqlite") as peers:
        conversation = peers.store.create_session(SessionRequest(channel="converse"))
        agent_chat = peers.store.create_session(SessionRequest(channel="agent"))
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.owner.authorize(connection(agent_chat))
        original = connection(conversation, backend=None)
        proof = await peers.owner.authorize(original)
        competing = (bind_to(proof, "first-backend"), bind_to(proof, "replacement-backend"))
        first, second = await asyncio.gather(
            *(peers.decisions.bind(request) for request in competing),
            return_exceptions=True,
        )
        outcomes = (first, second)
        successes = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
        assert len(successes) == 1
        assert sum(isinstance(outcome, ExternalAgentNativeConflict) for outcome in outcomes) == 1
        assert (await peers.owner.authorize(original)).backend_session_id == successes[
            0
        ].backend_session_id

        repeated_field = original.model_dump_json().replace(
            '"contract_version":1,',
            '"contract_version":1,"contract_version":1,',
        )
        malformed = await peers.owner_http.post(
            "/v1/external-agent/authorize",
            content=repeated_field,
        )
        assert malformed.status_code == 422


async def test_pages_keep_native_scope_ancestry_and_transcript_revisions(tmp_path):
    async with native(tmp_path / "journal.sqlite") as peers:
        session = peers.store.create_session(SessionRequest(channel="converse"))
        unrelated = peers.store.create_session(SessionRequest(channel="converse"))
        public = add(peers.store, session, "conversation.user", "Public original.")
        edit = add(
            peers.store,
            session,
            "conversation.revision",
            "Public correction.",
            revision=2,
            target_event_id=public.event_id,
        )
        secret = add(peers.store, session, "conversation.user", "Local source.", scope="local_only")
        add(
            peers.store,
            session,
            "conversation.converse",
            "Public label on private ancestry.",
            causes=(secret.event_id,),
            role="converse",
        )
        add(peers.store, unrelated, "conversation.user", "Foreign transcript.")
        model = SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id="speech-fixture",
            window_index=0,
            text="Public speech window.",
            start_ms=0,
            end_ms=100,
            user_speaking=False,
            capture_ended=True,
            content_scope="public_synthetic",
        )
        window = peers.store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.window",
                payload=model.model_dump(mode="json"),
                idempotency_key=uuid4().hex,
            ),
            "owner",
        )
        add(peers.store, unrelated, "conversation.user", "Foreign text after the selected scope.")
        grant = await peers.owner.authorize(connection(session))
        binding = bind_to(grant)
        await peers.decisions.bind(binding)
        initial = await peers.decisions.transcript(
            read(binding.target, limit=1), scope=scope_of(session)
        )
        assert initial.has_more
        assert initial.through_sequence == window.sequence
        assert [entry.event_id for entry in initial.entries] == [public.event_id]
        later = add(peers.store, session, "conversation.user", "After the snapshot.")
        rest = await peers.decisions.transcript(
            read(binding.target, position=initial.next_after, through=initial.through_sequence),
            scope=scope_of(session),
        )
        assert [(entry.event_id, entry.text) for entry in rest.entries] == [
            (edit.event_id, "Public correction."),
            (window.event_id, "Public speech window."),
        ]
        assert rest.entries[0].revises_event_id == public.event_id
        assert rest.entries[1].utterance_id == "speech-fixture"
        assert rest.has_more is False
        fresh = await peers.decisions.transcript(read(binding.target), scope=scope_of(session))
        assert fresh.entries[-1].event_id == later.event_id
        with pytest.raises(PermissionError):
            await peers.owner.transcript(read(binding.target), scope=scope_of(session))
        response = await peers.owner_http.post(f"/v1/sessions/{session.session_id}/reset")
        response.raise_for_status()
        stale = await peers.http.post(
            "/v1/external-agent/transcript",
            json=read(binding.target).model_dump(mode="json"),
        )
        assert stale.status_code == 409 and stale.json()["reason"] == "scope_changed"
        assert stale.headers["cache-control"] == "no-store"


async def test_utf8_pages_keep_exact_text_and_fence_a_later_scope_change(tmp_path):
    async with native(tmp_path / "journal.sqlite") as peers:
        session = peers.store.create_session(SessionRequest(channel="converse"))
        full = "a" * 32767 + "☂" + "été" * 2000
        entry = add(peers.store, session, "conversation.user", full)
        grant = await peers.owner.authorize(connection(session))
        binding = bind_to(grant)
        await peers.decisions.bind(binding)
        first = await peers.decisions.transcript(read(binding.target), scope=scope_of(session))
        assert first.next_after.byte_offset == 32767
        assert first.entries[0].end_of_entry is False
        inside = ExternalAgentTranscriptPosition(sequence=entry.sequence, byte_offset=32768)
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.transcript(
                read(binding.target, position=inside, through=first.through_sequence),
                scope=scope_of(session),
            )
        parts = [first.entries[0].text]
        cursor = first
        while cursor.has_more:
            cursor = await peers.decisions.transcript(
                read(binding.target, position=cursor.next_after, through=first.through_sequence),
                scope=scope_of(session),
            )
            parts.extend(item.text for item in cursor.entries)
            assert len(parts) < 10
        assert "".join(parts) == full and cursor.entries[-1].end_of_entry

        add(
            peers.store,
            session,
            "conversation.revision",
            "More restrictive correction.",
            scope="local_only",
            revision=2,
            target_event_id=entry.event_id,
        )
        with pytest.raises(ExternalAgentNativeConflict):
            await peers.decisions.transcript(
                read(binding.target, position=first.next_after, through=first.through_sequence),
                scope=scope_of(session),
            )
        latest = await peers.decisions.transcript(read(binding.target), scope=scope_of(session))
        assert latest.entries == ()
