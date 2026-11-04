"""Issue #371: Durable finalized Decisions retry & recovery over HTTP JournalClient."""

import json
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.decisions import DecisionsCallFinish, DecisionsCallStart
from packages.contracts.models import (
    AppendRequest,
    ProviderInfo,
    SessionRequest,
    Task,
    TaskState,
)
from packages.providers.decision_capture import (
    CaptureNativeUnavailable,
    capture_request,
    capture_response,
)
from packages.providers.text import ProviderError
from services.common.auth import Auth
from services.decisions.runtime import (
    DecisionsWorker,
    InvalidClassificationError,
    ModelClassifier,
    validate_classification_dict,
)
from services.session.api import create_app
from services.session.store import Store

TOKENS = {
    "owner": "owner-token-371-synthetic-000000",
    "converse": "converse-token-371-synthetic-000",
    "decisions": "decisions-token-371-synthetic-00",
    "agent": "agent-token-371-synthetic-000000",
}


class DummyAgent:
    def __init__(self, tasks=None):
        self.tasks = list(tasks or [])
        self.submitted = []
        self.steered = []
        self.interrupted = []

    async def status(self):
        return list(self.tasks)

    async def submit(self, request, actor="decisions"):
        self.submitted.append((request, actor))
        return Task(
            task_id="task-created-371",
            session_id=request.session_id,
            thread_id=request.thread_id,
            epoch=request.epoch,
            objective=request.objective,
            objective_revision=1,
            state=TaskState.QUEUED,
            idempotency_key=request.idempotency_key,
            source_event_ids=request.source_event_ids,
            source_revision=request.source_revision,
        )

    async def steer(self, task_id, request, actor="decisions"):
        self.steered.append((task_id, request, actor))
        return Task(
            task_id=task_id,
            session_id="s-1",
            thread_id="t-1",
            epoch=1,
            objective=request.instruction,
            objective_revision=request.expected_objective_revision + 1,
            state=TaskState.RUNNING,
            idempotency_key=request.idempotency_key,
            source_event_ids=request.source_event_ids,
            source_revision=1,
        )

    async def interrupt(self, task_id, request, actor="decisions"):
        self.interrupted.append((task_id, request, actor))
        return Task(
            task_id=task_id,
            session_id="s-1",
            thread_id="t-1",
            epoch=1,
            objective="Interrupted",
            objective_revision=request.expected_objective_revision,
            state=TaskState.CANCELLED,
            idempotency_key=request.idempotency_key,
            source_event_ids=request.source_event_ids,
            source_revision=request.source_revision,
        )


@asynccontextmanager
async def session_http_env(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    auth = Auth(dict(TOKENS))
    app = create_app(store, auth)
    clients = []

    def make_client(role="decisions", transport=None):
        active_transport = transport or httpx.ASGITransport(app=app)
        client = JournalClient("http://session.local", TOKENS[role], transport=active_transport)
        clients.append(client)
        return client

    try:
        session = store.create_session(SessionRequest(channel="converse"))
        yield store, session, app, make_client
    finally:
        for client in clients:
            await client.close()
        store.close()


def append_user_source(
    store,
    session,
    *,
    text="Build the verification report",
    idem="u-1",
    revision=1,
    content_scope="public_synthetic",
):
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload={
                "text": text,
                "final": True,
                "revision": revision,
                "content_scope": content_scope,
            },
            idempotency_key=idem,
        ),
        "owner",
    )


@pytest.mark.asyncio
async def test_http_decisions_calls_endpoint_enforces_roles_and_scope(tmp_path):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        source = append_user_source(store, session, idem="auth-source")
        decisions_client = make_client("decisions")
        owner_client = make_client("owner")
        converse_client = make_client("converse")
        agent_client = make_client("agent")

        assert (
            await decisions_client.decisions_calls(
                source_event_id=source.event_id,
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
            )
            == []
        )
        assert (
            await owner_client.decisions_calls(
                source_event_id=source.event_id,
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
            )
            == []
        )

        for forbidden in (converse_client, agent_client):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await forbidden.decisions_calls(source_event_id=source.event_id)
            assert exc_info.value.response.status_code == 403

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await decisions_client.decisions_calls(
                source_event_id=source.event_id,
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch + 1,
            )
        assert exc_info.value.response.status_code == 422


@pytest.mark.asyncio
async def test_http_journal_client_retry_budget_persists_across_worker_restarts(tmp_path):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        source = append_user_source(store, session, idem="retry-restart")
        calls = 0

        class FailingProvider:
            info = ProviderInfo(provider="test", model="m", local=True)

            async def complete(self, messages):
                nonlocal calls
                calls += 1
                await capture_request(
                    self, {"model": "m", "messages": messages}, "chat_completions"
                )
                raise ProviderError("Provider HTTP 503")

        agent = DummyAgent()
        worker_1 = DecisionsWorker(
            make_client("decisions"), agent, ModelClassifier(FailingProvider())
        )
        with pytest.raises(ProviderError, match="503"):
            await worker_1.tick()
        assert calls == 1
        assert store.read(kinds=["routing.classification_refused"]) == []
        assert store.cursor("decisions", worker_1.consumer) == 0

        # Recreate DecisionsWorker with a brand new HTTP JournalClient; must see 1 prior attempt.
        worker_2 = DecisionsWorker(
            make_client("decisions"), agent, ModelClassifier(FailingProvider())
        )
        persisted = await worker_2._persisted_call_state(source)
        assert persisted.state == "retryable_error"
        assert persisted.prior_attempts == 1

        await worker_2.tick()
        assert calls == 2
        refusals = store.read(kinds=["routing.classification_refused"])
        assert len(refusals) == 1
        assert refusals[0].payload == {
            "source_event_id": source.event_id,
            "source_revision": 1,
            "reason": "provider_failed",
            "error_type": "ProviderError",
        }
        assert store.cursor("decisions", worker_2.consumer) >= source.sequence

        # A third worker must not make any further provider calls.
        worker_3 = DecisionsWorker(
            make_client("decisions"), agent, ModelClassifier(FailingProvider())
        )
        await worker_3.tick()
        assert calls == 2


@pytest.mark.asyncio
async def test_http_journal_client_recovers_completed_call_after_crash_without_second_inference(
    tmp_path,
):
    async with session_http_env(tmp_path) as (store, session, app, make_client):
        source = append_user_source(store, session, idem="recover-completed")
        calls = 0

        class CompletingProvider:
            info = ProviderInfo(provider="test", model="m", local=True)

            async def complete(self, messages):
                nonlocal calls
                calls += 1
                await capture_request(
                    self, {"model": "m", "messages": messages}, "chat_completions"
                )
                raw = '{"route":"queue","reason_code":"user_request","confidence":0.95}'
                capture_response(raw)
                return raw

        asgi = httpx.ASGITransport(app=app)

        async def crash_on_decided_append(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/v1/transcript/entries":
                body = json.loads(request.content)
                if body.get("kind") == "routing.decided":
                    return httpx.Response(503, json={"detail": "simulated crash before decided"})
            return await asgi.handle_async_request(request)

        crashing_client = make_client(
            "decisions", transport=httpx.MockTransport(crash_on_decided_append)
        )
        agent = DummyAgent()
        worker_1 = DecisionsWorker(crashing_client, agent, ModelClassifier(CompletingProvider()))
        with pytest.raises(httpx.HTTPStatusError):
            await worker_1.tick()
        assert calls == 1
        assert store.read(kinds=["routing.decided"]) == []
        assert store.cursor("decisions", worker_1.consumer) == 0

        # New worker with fresh JournalClient recovers the completed call without re-invoking provider.
        worker_2 = DecisionsWorker(
            make_client("decisions"), agent, ModelClassifier(CompletingProvider())
        )
        persisted = await worker_2._persisted_call_state(source)
        assert persisted.state == "completed"
        assert persisted.prior_attempts == 1
        assert persisted.output == {
            "route": "queue",
            "reason_code": "user_request",
            "confidence": 0.95,
        }

        await worker_2.tick()
        assert calls == 1
        decided = store.read(kinds=["routing.decided"])
        assert len(decided) == 1
        assert decided[0].payload["route"] == "queue"
        assert len(agent.submitted) == 1
        assert store.cursor("decisions", worker_2.consumer) >= source.sequence


@pytest.mark.asyncio
async def test_unsettled_started_call_counts_against_retry_budget_over_http_client(tmp_path):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        source = append_user_source(store, session, idem="unsettled-start")
        client = make_client("decisions")

        # Simulate a worker that persisted decisions_call_start(attempt=1) and crashed before finish.
        await client.decisions_call_start(
            DecisionsCallStart(
                call_id=str(uuid.uuid4()),
                attempt_id=str(uuid.uuid4()),
                attempt=1,
                source_event_id=source.event_id,
                source_sequence=source.sequence,
                source_revision=1,
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                provider="test",
                model="m",
                request_format="chat_completions",
                request={"model": "m", "messages": []},
            )
        )

        calls = 0

        class FailingProvider:
            info = ProviderInfo(provider="test", model="m", local=True)

            async def complete(self, messages):
                nonlocal calls
                calls += 1
                await capture_request(
                    self, {"model": "m", "messages": messages}, "chat_completions"
                )
                raise ProviderError("Provider HTTP 503")

        worker = DecisionsWorker(
            make_client("decisions"), DummyAgent(), ModelClassifier(FailingProvider())
        )
        persisted = await worker._persisted_call_state(source)
        assert persisted.state == "pending_uncertain"
        assert persisted.prior_attempts == 1

        # Because 1 attempt was already started, only 1 more attempt (attempt=2) is permitted.
        await worker.tick()
        assert calls == 1
        refusals = store.read(kinds=["routing.classification_refused"])
        assert len(refusals) == 1
        assert refusals[0].payload == {
            "source_event_id": source.event_id,
            "source_revision": 1,
            "reason": "provider_failed",
            "error_type": "ProviderError",
        }


@pytest.mark.asyncio
async def test_two_unsettled_started_calls_refuse_without_additional_inference(tmp_path):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        source = append_user_source(store, session, idem="two-unsettled-starts")
        client = make_client("decisions")

        for attempt in (1, 2):
            await client.decisions_call_start(
                DecisionsCallStart(
                    call_id=str(uuid.uuid4()),
                    attempt_id=str(uuid.uuid4()),
                    attempt=attempt,
                    source_event_id=source.event_id,
                    source_sequence=source.sequence,
                    source_revision=1,
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    provider="test",
                    model="m",
                    request_format="chat_completions",
                    request={"model": "m", "messages": []},
                )
            )

        calls = 0

        class UnexpectedProvider:
            info = ProviderInfo(provider="test", model="m", local=True)

            async def complete(self, messages):
                nonlocal calls
                calls += 1
                return '{"route":"queue"}'

        worker = DecisionsWorker(
            make_client("decisions"), DummyAgent(), ModelClassifier(UnexpectedProvider())
        )
        persisted = await worker._persisted_call_state(source)
        assert persisted.state == "refused"
        assert persisted.prior_attempts == 2
        assert persisted.refusal == ("provider_failed", "UncertainCallAttempt")

        await worker.tick()
        assert calls == 0
        refusals = store.read(kinds=["routing.classification_refused"])
        assert len(refusals) == 1
        assert refusals[0].payload["reason"] == "provider_failed"


@pytest.mark.asyncio
async def test_failed_decisions_calls_lookup_blocks_tick_without_paid_inference_and_bounds_starvation(
    tmp_path,
):
    async with session_http_env(tmp_path) as (store, session, app, make_client):
        source_1 = append_user_source(store, session, text="First task", idem="lookup-fail-1")
        source_2 = append_user_source(store, session, text="Second task", idem="lookup-fail-2")
        asgi = httpx.ASGITransport(app=app)
        fail_for_first = True

        async def flaky_calls_lookup(request: httpx.Request) -> httpx.Response:
            if (
                fail_for_first
                and request.url.path == "/v1/decisions/calls"
                and request.url.params.get("source_event_id") == source_1.event_id
            ):
                return httpx.Response(503, json={"detail": "decisions_calls unavailable"})
            return await asgi.handle_async_request(request)

        calls = 0

        class QueueProvider:
            info = ProviderInfo(provider="test", model="m", local=True)

            async def complete(self, messages):
                nonlocal calls
                calls += 1
                await capture_request(
                    self, {"model": "m", "messages": messages}, "chat_completions"
                )
                raw = '{"route":"queue","reason_code":"user_request","confidence":0.9}'
                capture_response(raw)
                return raw

        flaky_client = make_client("decisions", transport=httpx.MockTransport(flaky_calls_lookup))
        agent = DummyAgent()
        worker = DecisionsWorker(flaky_client, agent, ModelClassifier(QueueProvider()))

        persisted = await worker._persisted_call_state(source_1)
        assert persisted.state == "unavailable"
        assert persisted.prior_attempts == 2

        # First tick: lookup failure blocks classification without calling the provider.
        with pytest.raises(CaptureNativeUnavailable, match="call history is unavailable"):
            await worker.tick()
        assert calls == 0
        assert store.read(kinds=["routing.classification_refused"]) == []
        assert store.read(kinds=["routing.decided"]) == []
        assert store.cursor("decisions", worker.consumer) == 0

        # Second consecutive lookup failure for source_1 refuses source_1 without calling
        # the provider and allows source_2 in the same batch to proceed without starvation.
        await worker.tick()
        assert calls == 1
        refusals = store.read(kinds=["routing.classification_refused"])
        assert len(refusals) == 1
        assert refusals[0].payload == {
            "source_event_id": source_1.event_id,
            "source_revision": 1,
            "reason": "call_history_unavailable",
            "error_type": "CaptureNativeUnavailable",
        }
        decided = store.read(kinds=["routing.decided"])
        assert len(decided) == 1
        assert decided[0].causes == [source_2.event_id]
        assert len(agent.submitted) == 1


@pytest.mark.asyncio
async def test_targeted_routes_require_target_fields_while_retaining_stale_target_override(
    tmp_path,
):
    for bad_payload in (
        {"route": "interrupt"},
        {"route": "steer"},
        {"route": "interrupt", "target_task_id": None, "expected_objective_revision": 1},
        {"route": "steer", "target_task_id": "task-1", "expected_objective_revision": None},
    ):
        with pytest.raises(InvalidClassificationError) as exc_info:
            validate_classification_dict(bad_payload)
        assert exc_info.value.reason_code == "invalid_target_fields"

    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        s_missing = append_user_source(store, session, text="Cancel that", idem="target-missing")
        s_stale = append_user_source(store, session, text="Cancel that", idem="target-stale")

        outputs = iter(
            [
                '{"route":"interrupt"}',
                '{"route":"interrupt","target_task_id":"task-gone","expected_objective_revision":2}',
            ]
        )

        class ScriptProvider:
            info = ProviderInfo(provider="test", model="m", local=True)

            async def complete(self, messages):
                await capture_request(
                    self, {"model": "m", "messages": messages}, "chat_completions"
                )
                raw = next(outputs)
                capture_response(raw)
                return raw

        worker = DecisionsWorker(
            make_client("decisions"), DummyAgent(), ModelClassifier(ScriptProvider())
        )
        await worker.tick()

        refusals = store.read(kinds=["routing.classification_refused"])
        assert len(refusals) == 1
        assert refusals[0].causes == [s_missing.event_id]
        assert refusals[0].payload["reason"] == "invalid_target_fields"

        decided = store.read(kinds=["routing.decided"])
        assert len(decided) == 1
        assert decided[0].causes == [s_stale.event_id]
        assert decided[0].payload["route"] == "ignore"
        assert decided[0].payload["reason_code"] == "unknown_or_stale_target"


@pytest.mark.asyncio
async def test_recovered_completed_output_is_strictly_validated_and_revalidates_native_bindings(
    tmp_path,
):
    async with session_http_env(tmp_path) as (store, session, _app, make_client):
        client = make_client("decisions")

        # 1. Legacy/invalid recovered output: {'route': 'interrupt'} without target fields.
        s_invalid = append_user_source(store, session, text="Stop it", idem="rec-invalid-target")
        call_invalid = str(uuid.uuid4())
        await client.decisions_call_start(
            DecisionsCallStart(
                call_id=call_invalid,
                attempt_id=str(uuid.uuid4()),
                attempt=1,
                source_event_id=s_invalid.event_id,
                source_sequence=s_invalid.sequence,
                source_revision=1,
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                provider="test",
                model="m",
                request_format="chat_completions",
                request={"model": "m", "messages": []},
            )
        )
        await client.decisions_call_finish(
            call_invalid,
            DecisionsCallFinish(
                source_event_id=s_invalid.event_id,
                status="completed",
                raw_response='{"route":"interrupt"}',
                validated_output={"route": "interrupt"},
            ),
        )

        # 2. Mismatched model binding on a completed call.
        s_wrong_model = append_user_source(
            store, session, text="Queue work", idem="rec-wrong-model"
        )
        call_wrong_model = str(uuid.uuid4())
        await client.decisions_call_start(
            DecisionsCallStart(
                call_id=call_wrong_model,
                attempt_id=str(uuid.uuid4()),
                attempt=1,
                source_event_id=s_wrong_model.event_id,
                source_sequence=s_wrong_model.sequence,
                source_revision=1,
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                provider="test",
                model="other-model",
                request_format="chat_completions",
                request={"model": "other-model", "messages": []},
            )
        )
        await client.decisions_call_finish(
            call_wrong_model,
            DecisionsCallFinish(
                source_event_id=s_wrong_model.event_id,
                status="completed",
                raw_response='{"route":"queue","reason_code":"ok","confidence":0.9}',
                validated_output={"route": "queue", "reason_code": "ok", "confidence": 0.9},
            ),
        )

        # 3. Mismatched source_revision / epoch on a completed call record.
        s_wrong_rev = append_user_source(store, session, text="Queue work", idem="rec-wrong-rev")
        call_wrong_rev = str(uuid.uuid4())
        await client.decisions_call_start(
            DecisionsCallStart(
                call_id=call_wrong_rev,
                attempt_id=str(uuid.uuid4()),
                attempt=1,
                source_event_id=s_wrong_rev.event_id,
                source_sequence=s_wrong_rev.sequence,
                source_revision=1,
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                provider="test",
                model="m",
                request_format="chat_completions",
                request={"model": "m", "messages": []},
            )
        )
        await client.decisions_call_finish(
            call_wrong_rev,
            DecisionsCallFinish(
                source_event_id=s_wrong_rev.event_id,
                status="completed",
                raw_response='{"route":"queue","reason_code":"ok","confidence":0.9}',
                validated_output={"route": "queue", "reason_code": "ok", "confidence": 0.9},
            ),
        )
        with store.lock, store.db:
            row = store.db.execute(
                "SELECT value FROM decisions_calls WHERE call_id=?", (call_wrong_rev,)
            ).fetchone()
            tampered = json.loads(row[0])
            tampered["source_revision"] = 2
            store.db.execute(
                "UPDATE decisions_calls SET value=? WHERE call_id=?",
                (json.dumps(tampered), call_wrong_rev),
            )

        # 4. Disclosure policy violation on recovered call (local_only source with cloud provider).
        s_local = append_user_source(
            store,
            session,
            text="Private local instruction",
            idem="rec-disclosure",
            content_scope="local_only",
        )
        call_local = str(uuid.uuid4())
        await client.decisions_call_start(
            DecisionsCallStart(
                call_id=call_local,
                attempt_id=str(uuid.uuid4()),
                attempt=1,
                source_event_id=s_local.event_id,
                source_sequence=s_local.sequence,
                source_revision=1,
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                provider="cloud",
                model="m",
                request_format="chat_completions",
                request={"model": "m", "messages": []},
            )
        )
        await client.decisions_call_finish(
            call_local,
            DecisionsCallFinish(
                source_event_id=s_local.event_id,
                status="completed",
                raw_response='{"route":"queue","reason_code":"ok","confidence":0.9}',
                validated_output={"route": "queue", "reason_code": "ok", "confidence": 0.9},
            ),
        )

        calls = 0

        class NeverCalledLocalProvider:
            info = ProviderInfo(provider="test", model="m", local=True)

            async def complete(self, messages):
                nonlocal calls
                calls += 1
                return '{"route":"queue"}'

        class NeverCalledCloudProvider:
            info = ProviderInfo(provider="cloud", model="m", local=False)

            async def complete(self, messages):
                nonlocal calls
                calls += 1
                return '{"route":"queue"}'

        local_worker = DecisionsWorker(
            make_client("decisions"), DummyAgent(), ModelClassifier(NeverCalledLocalProvider())
        )
        for entry in (s_invalid, s_wrong_model, s_wrong_rev):
            await local_worker.process(entry)

        cloud_worker = DecisionsWorker(
            make_client("decisions"), DummyAgent(), ModelClassifier(NeverCalledCloudProvider())
        )
        await cloud_worker.process(s_local)

        assert calls == 0
        assert store.read(kinds=["routing.decided"]) == []

        refusals = {
            r.payload["source_event_id"]: r.payload
            for r in store.read(kinds=["routing.classification_refused"])
        }
        assert refusals[s_invalid.event_id] == {
            "source_event_id": s_invalid.event_id,
            "source_revision": 1,
            "reason": "invalid_target_fields",
            "error_type": "InvalidClassificationError",
        }
        assert refusals[s_wrong_model.event_id] == {
            "source_event_id": s_wrong_model.event_id,
            "source_revision": 1,
            "reason": "invalid_call_binding",
            "error_type": "InvalidClassificationError",
        }
        assert refusals[s_wrong_rev.event_id] == {
            "source_event_id": s_wrong_rev.event_id,
            "source_revision": 1,
            "reason": "invalid_call_binding",
            "error_type": "InvalidClassificationError",
        }
        assert refusals[s_local.event_id] == {
            "source_event_id": s_local.event_id,
            "source_revision": 1,
            "reason": "provider_refused",
            "error_type": "ProviderRefusal",
        }


@pytest.mark.asyncio
async def test_native_source_read_failures_do_not_consume_provider_attempts_or_trigger_402_budget_refusal(
    tmp_path,
):
    async with session_http_env(tmp_path) as (store, session, app, make_client):
        source = append_user_source(store, session, idem="native-read-fail")
        asgi = httpx.ASGITransport(app=app)
        fail_resolve_count = 2

        async def flaky_resolve(request: httpx.Request) -> httpx.Response:
            nonlocal fail_resolve_count
            if (
                fail_resolve_count > 0
                and request.url.path.startswith("/v1/transcript/entries/")
                and request.url.params.get("resolve") == "true"
            ):
                fail_resolve_count -= 1
                return httpx.Response(503, json={"detail": "native resolve failure on port 1402"})
            return await asgi.handle_async_request(request)

        provider_calls = 0

        class Port402ThenSuccessProvider:
            info = ProviderInfo(provider="test", model="m", local=True)

            async def complete(self, messages):
                nonlocal provider_calls
                provider_calls += 1
                await capture_request(
                    self, {"model": "m", "messages": messages}, "chat_completions"
                )
                if provider_calls == 1:
                    # Contains '402' in a port number, NOT an HTTP 402 or token budget refusal!
                    raise ProviderError("Connection to 127.0.0.1:1402 reset by peer")
                raw = '{"route":"queue","reason_code":"recovered_after_retry","confidence":0.92}'
                capture_response(raw)
                return raw

        client = make_client("decisions", transport=httpx.MockTransport(flaky_resolve))
        agent = DummyAgent()
        worker = DecisionsWorker(client, agent, ModelClassifier(Port402ThenSuccessProvider()))

        # Two temporary native read failures must NOT consume provider attempts or refuse.
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                await worker.tick()
        assert provider_calls == 0
        assert worker._transport_attempts.get(source.event_id, 0) == 0
        assert store.read(kinds=["routing.classification_refused"]) == []

        # Attempt 1 fails with a transient error containing ':1402' (not a budget refusal).
        with pytest.raises(ProviderError, match="1402"):
            await worker.tick()
        assert provider_calls == 1
        assert store.read(kinds=["routing.classification_refused"]) == []

        # Attempt 2 succeeds and routes the decision.
        await worker.tick()
        assert provider_calls == 2
        assert store.read(kinds=["routing.classification_refused"]) == []
        decided = store.read(kinds=["routing.decided"])
        assert len(decided) == 1
        assert decided[0].payload["route"] == "queue"
        assert len(agent.submitted) == 1
