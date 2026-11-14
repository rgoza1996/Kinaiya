"""Issue #371: native admission, real HTTP restart and original-owner oracles."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
import uvicorn
from test_decisions_finalized_refusal_recovery import (
    TOKENS,
    DummyAgent,
    admit_native,
    append_user_source,
    finish_native,
    session_http_env,
)

from packages.contracts.client import JournalClient
from packages.contracts.decisions_attempts import (
    DecisionsAttemptFinish,
    DecisionsAttemptLookup,
    DecisionsSource,
)
from packages.contracts.models import SessionRequest
from packages.providers.decision_capture import CaptureNativeUnavailable
from packages.providers.prompt_budget import PromptCountUnavailable
from packages.providers.text import CompatibleTextProvider, ProviderError
from services.common.auth import Auth
from services.decisions.runtime import DecisionsWorker, ModelClassifier
from services.session.api import create_app
from services.session.store import LocalJournal, Store


@asynccontextmanager
async def loopback(app):
    """Use a real ephemeral socket, without touching the user's services."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    root = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    task.result()
                    raise AssertionError("Temporary Session server stopped before readiness")
                await asyncio.sleep(0.01)
        yield root
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


def response(content='{"route":"queue","reason_code":"synthetic_request"}'):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ]
        },
    )


def app_with_faults(store, *, decided=False, refusal=False, inspection=False):
    app = create_app(store, Auth(dict(TOKENS)))

    @app.middleware("http")
    async def failures(request, call_next):
        from starlette.responses import JSONResponse

        if inspection and request.url.path.startswith("/v1/decisions/calls"):
            return JSONResponse({"detail": "synthetic inspection outage"}, status_code=503)
        if request.url.path == "/v1/transcript/entries":
            payload = await request.json()
            if (decided and payload["kind"] == "routing.decided") or (
                refusal and payload["kind"] == "routing.classification_refused"
            ):
                return JSONResponse({"detail": "synthetic publication outage"}, status_code=503)
        return await call_next(request)

    return app


async def state_of(client, source):
    return await client.decisions_attempt_lookup(
        DecisionsAttemptLookup(source=DecisionsSource.of(source))
    )


async def test_real_socket_restarts_freeze_policy_despite_capture_outage_and_model_change(tmp_path):
    path = tmp_path / "journal.sqlite"
    store = Store(path, decisions_max_attempts=2)
    session = store.create_session(SessionRequest(channel="converse"))
    source = append_user_source(store, session, idem="real-http-errors")
    entered = []

    def failing(request):
        entered.append(json.loads(request.content)["model"])
        return httpx.Response(503, text="synthetic unavailable")

    agent = DummyAgent()
    try:
        async with loopback(app_with_faults(store, inspection=True, refusal=True)) as root:
            # Both workers use the real JournalClient/socket/Session/SQLite path.
            for model in ("original-model", "changed-model"):
                client = JournalClient(root, TOKENS["decisions"])
                provider = CompatibleTextProvider(
                    "http://localhost/v1", model, transport=httpx.MockTransport(failing)
                )
                try:
                    worker = DecisionsWorker(client, agent, ModelClassifier(provider))
                    with pytest.raises((ProviderError, httpx.HTTPStatusError)):
                        await worker.tick()
                finally:
                    await client.close()
                    await provider.close()
            client = JournalClient(root, TOKENS["decisions"])
            try:
                state = await state_of(client, source)
            finally:
                await client.close()
            assert (state.state, state.charged_attempts, state.max_attempts) == ("exhausted", 2, 2)
        assert store.decisions.calls(source.event_id) == []
        store.close()
        # Changing startup configuration cannot change an accepted source policy.
        store = Store(path, decisions_max_attempts=7)
        async with loopback(create_app(store, Auth(dict(TOKENS)))) as root:
            client = JournalClient(root, TOKENS["decisions"])
            provider = CompatibleTextProvider(
                "http://localhost/v1", "third-model", transport=httpx.MockTransport(failing)
            )
            try:
                await DecisionsWorker(client, agent, ModelClassifier(provider)).tick()
                state = await state_of(client, source)
                assert (state.charged_attempts, state.max_attempts) == (2, 2)
            finally:
                await client.close()
                await provider.close()
        assert entered == ["original-model", "changed-model"]
        assert len(store.read(kinds=["routing.classification_refused"])) == 1
        assert not agent.submitted
        assert store.db.execute("SELECT count(*) FROM decisions_attempts").fetchone()[0] == 2
    finally:
        store.close()


async def test_real_socket_completed_result_survives_missing_capture_and_publication_crash(
    tmp_path,
):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    source = append_user_source(store, session, idem="real-http-completed")
    entered = []

    def completed(request):
        entered.append(request.content)
        return response()

    provider = CompatibleTextProvider(
        "http://localhost/v1", "m", transport=httpx.MockTransport(completed)
    )
    agent = DummyAgent()
    try:
        async with loopback(app_with_faults(store, inspection=True, decided=True)) as root:
            client = JournalClient(root, TOKENS["decisions"])
            try:
                with pytest.raises(httpx.HTTPStatusError):
                    await DecisionsWorker(client, agent, ModelClassifier(provider)).tick()
                assert (await state_of(client, source)).state == "completed"
            finally:
                await client.close()
        assert not store.decisions.calls(source.event_id)
        assert not store.read(kinds=["routing.decided"])
        async with loopback(create_app(store, Auth(dict(TOKENS)))) as root:
            client = JournalClient(root, TOKENS["decisions"])
            try:
                worker = DecisionsWorker(client, agent, ModelClassifier(provider))
                await worker.tick()
                await DecisionsWorker(client, agent, ModelClassifier(provider)).tick()
            finally:
                await client.close()
        assert len(entered) == len(agent.submitted) == 1
        assert len(store.read(kinds=["routing.decided"])) == 1
        assert len(store.read(kinds=["routing.dispatch"])) == 1
    finally:
        await provider.close()
        store.close()


@pytest.mark.parametrize("operation", ["admit", "finish"])
@pytest.mark.parametrize("committed", [False, True])
async def test_lost_native_ack_never_creates_a_second_original_request(
    tmp_path, operation, committed
):
    async with session_http_env(tmp_path) as (store, session, app, make_client):
        source = append_user_source(store, session, idem="lost-ack")
        asgi = httpx.ASGITransport(app=app)
        entered = []

        def complete(request):
            entered.append(request.content)
            return response()

        async def lose_ack(request):
            if request.url.path == f"/v1/decisions/attempts/{operation}":
                if committed:
                    result = await asgi.handle_async_request(request)
                    await result.aclose()
                return httpx.Response(503, json={"detail": "synthetic lost ACK"})
            return await asgi.handle_async_request(request)

        provider = CompatibleTextProvider(
            "http://localhost/v1", "m", transport=httpx.MockTransport(complete)
        )
        try:
            first = make_client(transport=httpx.MockTransport(lose_ack))
            with pytest.raises(CaptureNativeUnavailable):
                await DecisionsWorker(first, DummyAgent(), ModelClassifier(provider)).process(
                    source
                )
            assert len(entered) == (0 if operation == "admit" else 1)
            recovered = make_client()
            before = await state_of(recovered, source)
            assert before.state == (
                "known_absent"
                if operation == "admit" and not committed
                else "completed"
                if operation == "finish" and committed
                else "admitted"
            )
            agent = DummyAgent()
            await DecisionsWorker(recovered, agent, ModelClassifier(provider)).process(source)
            assert len(entered) == (0 if operation == "admit" and committed else 1)
            assert len(agent.submitted) == (
                1 if before.state in {"known_absent", "completed"} else 0
            )
        finally:
            await provider.close()


async def test_native_timeout_retains_the_actual_late_exchange_and_sole_close(tmp_path):
    async with session_http_env(tmp_path) as (store, session, app, make_client):
        source = append_user_source(store, session, idem="late-admission-ack")
        asgi, release, entered = httpx.ASGITransport(app=app), asyncio.Event(), []
        streams = []

        class LateStream(httpx.AsyncByteStream):
            def __init__(self, raw):
                self.raw, self.closes = raw, 0

            async def __aiter__(self):
                await release.wait()
                yield self.raw

            async def aclose(self):
                self.closes += 1

        async def late_ack(request):
            result = await asgi.handle_async_request(request)
            if request.url.path == "/v1/decisions/attempts/admit":
                raw = await result.aread()
                await result.aclose()
                stream = LateStream(raw)
                streams.append(stream)
                return httpx.Response(
                    200, headers={"content-type": "application/json"}, stream=stream
                )
            return result

        def complete(request):
            entered.append(request.content)
            return response()

        original = make_client(transport=httpx.MockTransport(late_ack))
        provider = CompatibleTextProvider(
            "http://localhost/v1", "m", transport=httpx.MockTransport(complete)
        )
        try:
            with pytest.raises(CaptureNativeUnavailable):
                await DecisionsWorker(original, DummyAgent(), ModelClassifier(provider)).process(
                    source
                )
            assert len(original._submit_exchanges) == 1
            exchange = next(iter(original._submit_exchanges))
            assert not exchange.task.done() and exchange.response.stream is not None
            assert streams[0].closes == 0
            await DecisionsWorker(make_client(), DummyAgent(), ModelClassifier(provider)).process(
                source
            )
            assert not entered
            release.set()
            await asyncio.wait_for(asyncio.shield(exchange.task), 2)
            assert streams[0].closes == 1
            assert not entered
            assert (await state_of(make_client(), source)).charged_attempts == 1
        finally:
            release.set()
            await provider.close()


@pytest.mark.parametrize("http", [False, True])
async def test_admission_cas_and_exact_completion_binding(tmp_path, http):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        source = append_user_source(store, session, idem="cas-original")
        client = make_client() if http else LocalJournal(store, "decisions")
        first = await admit_native(client, source)
        assert await client.decisions_attempt_admit(first.admission) == first
        with pytest.raises((httpx.HTTPStatusError, ValueError)):
            await admit_native(client, source)
        finish = DecisionsAttemptFinish(
            source=DecisionsSource.of(source),
            attempt_id=first.admission.attempt_id,
            owner_id=uuid4().hex,
            admission_sha256=first.admission_sha256,
            status="error",
        )
        with pytest.raises((httpx.HTTPStatusError, ValueError)):
            await client.decisions_attempt_finish(finish)
        finished = await finish_native(client, first, status="error")
        assert await client.decisions_attempt_finish(finished.completion) == finished
        with pytest.raises((httpx.HTTPStatusError, ValueError)):
            await finish_native(client, first, status="completed", output={"route": "queue"})
        second = await admit_native(client, source, expected_attempts=1)
        assert second.attempt == 2
        assert (await state_of(client, source)).state == "admitted"


async def test_replacement_cannot_steal_live_provider_or_publish_late_result_over_uncertainty(
    tmp_path,
):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        source = append_user_source(store, session, idem="live-original")
        release, reached, entered = asyncio.Event(), asyncio.Event(), []

        async def held(request):
            entered.append(request.content)
            reached.set()
            await release.wait()
            return response()

        provider = CompatibleTextProvider(
            "http://localhost/v1", "m", transport=httpx.MockTransport(held)
        )
        agent = DummyAgent()
        original = asyncio.create_task(
            DecisionsWorker(make_client(), agent, ModelClassifier(provider)).process(source)
        )
        try:
            await asyncio.wait_for(reached.wait(), 2)
            await DecisionsWorker(make_client(), agent, ModelClassifier(provider)).process(source)
            assert len(entered) == 1
            release.set()
            await asyncio.wait_for(original, 2)
            assert not agent.submitted
            assert not store.read(kinds=["routing.decided"])
            assert len(store.read(kinds=["routing.classification_refused"])) == 1
            assert (await state_of(make_client(), source)).state == "completed"
        finally:
            release.set()
            await original
            await provider.close()


async def test_explicit_legacy_cutover_never_infers_absence_from_empty_inspection(tmp_path):
    path = tmp_path / "journal.sqlite"
    store = Store(path)
    session = store.create_session(SessionRequest(channel="converse"))
    old = append_user_source(store, session, idem="legacy-empty")
    # A database written by the pre-contract version has no native ledger tables.
    with store.db:
        for table in (
            "decisions_attempt_outcomes",
            "decisions_attempts",
            "decisions_attempt_sources",
            "decisions_attempt_cutover",
        ):
            store.db.execute(f"DROP TABLE {table}")
    store.close()
    store = Store(path)
    try:
        journal = LocalJournal(store, "decisions")
        assert (await state_of(journal, old)).state == "legacy_uncertain"
        with pytest.raises(ValueError):
            await admit_native(journal, old)
        new = append_user_source(store, session, idem="after-cutover")
        assert (await state_of(journal, new)).state == "known_absent"
        admitted = await admit_native(journal, new)
        assert admitted.attempt == 1
        assert not store.decisions.calls(old.event_id)
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["missing_capability", "lookup", "admit"])
async def test_native_unavailable_preserves_uncertainty_and_allows_unrelated_work(
    tmp_path, failure
):
    async with session_http_env(tmp_path) as (store, session, app, make_client):
        blocked = append_user_source(store, session, idem="blocked")
        unrelated = append_user_source(store, session, idem="unrelated")
        asgi, entered = httpx.ASGITransport(app=app), []

        async def faulty(request):
            if (
                request.url.path == f"/v1/decisions/attempts/{failure}"
                and json.loads(request.content)["source"]["source_event_id"] == blocked.event_id
            ):
                return httpx.Response(503, json={"detail": "synthetic native storage failure"})
            return await asgi.handle_async_request(request)

        def complete(request):
            entered.append(request.content)
            return response()

        client = make_client(transport=httpx.MockTransport(faulty))
        if failure == "missing_capability":
            native_lookup = client.decisions_attempt_lookup

            async def missing(request):
                if request.source.source_event_id == blocked.event_id:
                    raise AttributeError("synthetic old client without native lookup")
                return await native_lookup(request)

            client.decisions_attempt_lookup = missing
        provider = CompatibleTextProvider(
            "http://localhost/v1", "m", transport=httpx.MockTransport(complete)
        )
        try:
            worker = DecisionsWorker(client, DummyAgent(), ModelClassifier(provider))
            with pytest.raises(CaptureNativeUnavailable):
                await worker.tick()
            assert not entered
            assert not store.db.execute("SELECT 1 FROM decisions_attempts").fetchall()
            await worker.tick()
            assert len(entered) == 1
            assert store.read(kinds=["routing.decided"])[0].causes == [unrelated.event_id]
            assert store.read(kinds=["routing.classification_refused"])[0].causes == [
                blocked.event_id
            ]
            assert (await state_of(make_client(), blocked)).charged_attempts == 0
        finally:
            await provider.close()


@pytest.mark.parametrize("role", ["owner", "converse", "agent"])
async def test_native_attempt_endpoints_require_decisions_identity(tmp_path, role):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        source = append_user_source(store, session, idem="native-auth")
        for operation in ("lookup", "admit", "finish"):
            result = await make_client(role).http.post(
                f"/v1/decisions/attempts/{operation}",
                json={"source": DecisionsSource.of(source).model_dump()},
            )
            assert result.status_code == 403
        assert not store.db.execute("SELECT 1 FROM decisions_attempts").fetchall()


async def test_final_transport_rejects_request_hook_substitution(tmp_path):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        source = append_user_source(store, session, idem="wire-substitution")
        entered = []

        def complete(request):
            entered.append(request.content)
            return response()

        async def substitute(request):
            # Leave cached request.content intact while replacing the wire stream.
            request.stream = httpx.ByteStream(b'{"model":"other","messages":[]}')

        provider = CompatibleTextProvider(
            "http://localhost/v1", "m", transport=httpx.MockTransport(complete)
        )
        provider.http.event_hooks["request"].append(substitute)
        try:
            with pytest.raises(PromptCountUnavailable):
                await DecisionsWorker(
                    make_client(), DummyAgent(), ModelClassifier(provider)
                ).process(source)
            assert not entered
            state = await state_of(make_client(), source)
            assert state.charged_attempts == 1  # Reservation retained, never refunded.
            assert state.last_attempt.admission.request_sha256 != "0" * 64
        finally:
            await provider.close()


async def test_provider_cancellation_retains_actual_close_before_original_completion(tmp_path):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        source = append_user_source(store, session, idem="close-retention")
        release, closing = asyncio.Event(), asyncio.Event()

        class HeldClose(httpx.AsyncByteStream):
            def __init__(self):
                self.closes = 0

            async def __aiter__(self):
                yield response().content

            async def aclose(self):
                self.closes += 1
                closing.set()
                await release.wait()

        stream = HeldClose()
        provider = CompatibleTextProvider(
            "http://localhost/v1",
            "m",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
        )
        agent = DummyAgent()
        original = asyncio.create_task(
            DecisionsWorker(make_client(), agent, ModelClassifier(provider)).process(source)
        )
        try:
            await asyncio.wait_for(closing.wait(), 2)
            original.cancel()
            await asyncio.sleep(0)
            assert not original.done()
            state = await state_of(make_client(), source)
            assert state.state == "admitted" and state.charged_attempts == 1
            await DecisionsWorker(make_client(), agent, ModelClassifier(provider)).process(source)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await original
            assert stream.closes == 1
            assert (await state_of(make_client(), source)).state == "uncertain"
            assert not agent.submitted
        finally:
            release.set()
            if not original.done():
                original.cancel()
            await asyncio.gather(original, return_exceptions=True)
            await provider.close()


async def test_source_specific_preparation_read_failure_is_uncharged_and_bounded(tmp_path):
    async with session_http_env(tmp_path) as (store, session, app, make_client):
        blocked = append_user_source(store, session, idem="preparation-blocked")
        other_session = store.create_session(SessionRequest(channel="converse"))
        unrelated = append_user_source(store, other_session, idem="preparation-unrelated")
        asgi, entered = httpx.ASGITransport(app=app), []

        async def read_failure(request):
            if (
                request.url.path == f"/v1/transcript/entries/{blocked.event_id}"
                and request.url.params.get("resolve") == "true"
            ):
                return httpx.Response(503, json={"detail": "synthetic source read failure"})
            return await asgi.handle_async_request(request)

        def complete(request):
            entered.append(request.content)
            return response()

        provider = CompatibleTextProvider(
            "http://localhost/v1", "m", transport=httpx.MockTransport(complete)
        )
        try:
            worker = DecisionsWorker(
                make_client(transport=httpx.MockTransport(read_failure)),
                DummyAgent(),
                ModelClassifier(provider),
            )
            for _ in range(2):
                with pytest.raises(httpx.HTTPStatusError):
                    await worker.tick()
            assert not entered
            await worker.tick()
            assert len(entered) == 1
            assert store.read(kinds=["routing.decided"])[0].causes == [unrelated.event_id]
            assert (await state_of(make_client(), blocked)).charged_attempts == 0
        finally:
            await provider.close()


@pytest.mark.parametrize("invalid", [True, 0, 17, "2", 1.5])
def test_invalid_native_retry_configuration_does_not_open_a_store(tmp_path, invalid):
    path = tmp_path / "invalid.sqlite"
    with pytest.raises(ValueError):
        Store(path, decisions_max_attempts=invalid)
    assert not path.exists()


async def test_late_uncommitted_admission_cannot_reclaim_a_replacement_opportunity(tmp_path):
    async with session_http_env(tmp_path) as (store, session, app, make_client):
        source = append_user_source(store, session, idem="late-before-commit")
        asgi, release, entered = httpx.ASGITransport(app=app), asyncio.Event(), []

        async def late_admission(request):
            if request.url.path == "/v1/decisions/attempts/admit":
                await release.wait()
            return await asgi.handle_async_request(request)

        def complete(request):
            entered.append(request.content)
            return response()

        provider = CompatibleTextProvider(
            "http://localhost/v1", "m", transport=httpx.MockTransport(complete)
        )
        original = make_client(transport=httpx.MockTransport(late_admission))
        try:
            with pytest.raises(CaptureNativeUnavailable):
                await DecisionsWorker(original, DummyAgent(), ModelClassifier(provider)).process(
                    source
                )
            exchange = next(iter(original._submit_exchanges))
            assert not exchange.task.done()
            assert (await state_of(make_client(), source)).state == "known_absent"
            await DecisionsWorker(make_client(), DummyAgent(), ModelClassifier(provider)).process(
                source
            )
            assert len(entered) == 1
            release.set()
            with pytest.raises(httpx.HTTPStatusError) as error:
                await asyncio.wait_for(asyncio.shield(exchange.task), 2)
            assert error.value.response.status_code == 409
            assert len(entered) == 1
            assert (await state_of(make_client(), source)).charged_attempts == 1
        finally:
            release.set()
            await provider.close()


async def test_suppressed_provider_cancellation_cannot_turn_into_a_usable_result(tmp_path):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        source = append_user_source(store, session, idem="suppressed-cancel")
        reached = asyncio.Event()

        async def suppress(request):
            reached.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return response()

        provider = CompatibleTextProvider(
            "http://localhost/v1", "m", transport=httpx.MockTransport(suppress)
        )
        agent = DummyAgent()
        original = asyncio.create_task(
            DecisionsWorker(make_client(), agent, ModelClassifier(provider)).process(source)
        )
        try:
            await asyncio.wait_for(reached.wait(), 2)
            original.cancel()
            with pytest.raises(asyncio.CancelledError):
                await original
            assert not agent.submitted
            assert (await state_of(make_client(), source)).state == "uncertain"
        finally:
            await provider.close()


@pytest.mark.parametrize("http", [False, True])
async def test_explicit_worker_limit_is_frozen_before_lost_refusal_publication(tmp_path, http):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        source = append_user_source(store, session, idem="explicit-worker-cap")
        entered = []

        def fail(request):
            entered.append(request.content)
            return httpx.Response(503, text="synthetic failure")

        provider = CompatibleTextProvider(
            "http://localhost/v1", "m", transport=httpx.MockTransport(fail)
        )
        original = make_client() if http else LocalJournal(store, "decisions")
        original_append = original.append

        async def lose_refusal(request):
            if request.kind == "routing.classification_refused":
                raise OSError("synthetic crash before terminal publication")
            return await original_append(request)

        original.append = lose_refusal
        try:
            worker = DecisionsWorker(
                original, DummyAgent(), ModelClassifier(provider), max_attempts=1
            )
            with pytest.raises(OSError):
                await worker.process(source)
            recovered = make_client() if http else LocalJournal(store, "decisions")
            before = await state_of(recovered, source)
            assert (before.max_attempts, before.charged_attempts, before.state) == (
                1,
                1,
                "exhausted",
            )
            assert before.last_attempt.admission.max_attempts == 1
            # Neither the default nor a larger explicit worker limit can reset it.
            for limit in (None, 16):
                replacement = DecisionsWorker(
                    recovered, DummyAgent(), ModelClassifier(provider), max_attempts=limit
                )
                await replacement.process(source)
            assert len(entered) == 1
            assert len(store.read(kinds=["routing.classification_refused"])) == 1
        finally:
            await provider.close()


async def test_worker_proposal_cannot_enlarge_native_policy(tmp_path):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        source = append_user_source(store, session, idem="larger-worker-proposal")
        entered = []

        def fail(request):
            entered.append(request.content)
            return httpx.Response(503, text="synthetic failure")

        provider = CompatibleTextProvider(
            "http://localhost/v1", "m", transport=httpx.MockTransport(fail)
        )
        try:
            with pytest.raises(ProviderError):
                await DecisionsWorker(
                    make_client(), DummyAgent(), ModelClassifier(provider), max_attempts=16
                ).process(source)
            await DecisionsWorker(
                make_client(), DummyAgent(), ModelClassifier(provider), max_attempts=16
            ).process(source)
            state = await state_of(make_client(), source)
            assert (state.max_attempts, state.charged_attempts, state.state) == (2, 2, "exhausted")
            assert len(entered) == 2
        finally:
            await provider.close()


async def test_later_lower_proposal_refuses_without_changing_frozen_native_allowance(tmp_path):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        source = append_user_source(store, session, idem="later-lower-proposal")
        client = make_client()
        first = await admit_native(client, source)
        await finish_native(client, first, status="error")
        lower = first.admission.model_copy(
            update={
                "expected_attempts": 1,
                "max_attempts": 1,
                "attempt_id": uuid4().hex,
                "owner_id": uuid4().hex,
            }
        )
        with pytest.raises(httpx.HTTPStatusError) as error:
            await client.decisions_attempt_admit(lower)
        assert error.value.response.status_code == 409
        state = await state_of(client, source)
        assert (state.max_attempts, state.charged_attempts) == (2, 1)


@pytest.mark.parametrize("lost_completion_ack", [False, True])
async def test_failed_original_close_stays_owned_after_uncertainty_publication_and_lookup(
    tmp_path, lost_completion_ack
):
    from packages.providers.decision_admission import _unsettled
    from packages.providers.text import ProviderCleanupError

    async with session_http_env(tmp_path) as (store, session, app, make_client):
        source = append_user_source(store, session, idem="failed-close-original-owner")
        asgi, entered, replacement_entries = httpx.ASGITransport(app=app), [], []

        class BrokenClose(httpx.AsyncByteStream):
            def __init__(self):
                self.closes = 0

            async def __aiter__(self):
                yield response().content

            async def aclose(self):
                self.closes += 1
                raise RuntimeError("synthetic unconfirmed physical close")

        stream = BrokenClose()

        def original_transport(request):
            entered.append(request.content)
            return httpx.Response(200, stream=stream)

        def replacement_transport(request):
            replacement_entries.append(request.content)
            return response()

        async def lose_ack(request):
            result = await asgi.handle_async_request(request)
            if lost_completion_ack and request.url.path == "/v1/decisions/attempts/finish":
                await result.aclose()
                return httpx.Response(503, json={"detail": "synthetic lost uncertainty ACK"})
            return result

        provider = CompatibleTextProvider(
            "http://localhost/v1", "m", transport=httpx.MockTransport(original_transport)
        )
        replacement_provider = CompatibleTextProvider(
            "http://localhost/v1", "m", transport=httpx.MockTransport(replacement_transport)
        )
        client = make_client(transport=httpx.MockTransport(lose_ack))
        task = asyncio.create_task(
            DecisionsWorker(client, DummyAgent(), ModelClassifier(provider)).process(source)
        )
        try:
            with pytest.raises(
                CaptureNativeUnavailable if lost_completion_ack else ProviderCleanupError
            ):
                await task
            retained = next(
                owner for owner in _unsettled if owner.source.source_event_id == source.event_id
            )
            assert retained.cleanup_unconfirmed
            assert retained.owner is task and task.done()
            assert retained.provider is provider and retained.client is provider.http
            state = await state_of(make_client(), source)
            assert (state.state, state.charged_attempts) == ("uncertain", 1)
            assert retained.completion == state.last_attempt.completion
            replacement = DecisionsWorker(
                make_client(), DummyAgent(), ModelClassifier(replacement_provider)
            )
            await replacement.process(source)
            # This includes observe_completion's exact-publication reconciliation.
            assert retained in _unsettled
            assert retained.owner is task and retained.client is provider.http
            assert (await state_of(make_client(), source)).charged_attempts == 1
            assert stream.closes == 1 and len(entered) == 1
            assert not replacement_entries
            assert not store.read(kinds=["routing.decided"])
            assert len(store.read(kinds=["routing.classification_refused"])) == 1
        finally:
            await provider.close()
            await replacement_provider.close()
