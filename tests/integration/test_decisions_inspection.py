"""Public synthetic owner inspection; no provider, credentials or private journals."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent_submit_admission import proposal

from packages.contracts.decisions import MAX_REQUEST_BYTES, DecisionsCallFinish, DecisionsCallStart
from packages.contracts.models import AppendRequest, SessionRequest, SpeechWindow
from packages.providers.decision_capture import (
    CaptureNativeUnavailable,
    capture_request,
    decision_attempt,
)
from packages.providers.laya import LayaRoutingProvider
from packages.providers.laya_artifact import ARTIFACT_HEADER
from packages.providers.text import (
    CompatibleTextProvider,
    MetaResponsesProvider,
    ProviderError,
    ProviderRefusal,
)
from services.common.auth import Auth
from services.decisions.runtime import DecisionsWorker, ModelClassifier
from services.decisions.window_routing import WindowClassifier
from services.decisions.window_worker import WindowDecisionsWorker
from services.session.api import create_app
from services.session.decisions_store import date_range
from services.session.store import LocalJournal, Store

AUTH = Auth(
    {role: role * 32 for role in ("owner", "agent", "converse", "decisions", "resources")},
    allowed_origins=("http://synthetic",),
)
LITERAL = 'PUBLIC_FIXTURE Ω <script>alert("literal")</script>\nDo not execute.'


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "synthetic.sqlite")
    session = store.create_session(SessionRequest())
    client = TestClient(create_app(store, AUTH))
    yield SimpleNamespace(store=store, session=session, client=client)
    client.close()
    store.close()


def header(role="owner"):
    return {"Authorization": "Bearer " + AUTH.tokens[role]}


def source(env, *, session=None, kind="conversation.user", text=LITERAL, index=0):
    session = session or env.session
    if kind == "agent.command":
        return env.store.admit_submit(
            proposal(
                session,
                task_id="synthetic-task",
                key=f"source-{index}-{kind}-{len(env.store.read())}",
                objective=text,
            ),
            "agent",
        ).entry
    payload = {"text": text, "revision": 1, "final": True, "content_scope": "public_synthetic"}
    if kind == "conversation.window":
        payload = SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id="fixture",
            window_index=index,
            text=text,
            start_ms=0,
            end_ms=1000,
            capture_ended=True,
            user_speaking=False,
            content_scope="public_synthetic",
        ).model_dump(mode="json")
    if kind.startswith("agent."):
        payload = {"op": "submit", "objective": text, "actor": "owner", "task_id": "synthetic-task"}
    return env.store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind=kind,
            payload=payload,
            idempotency_key=f"source-{index}-{kind}-{len(env.store.read())}",
        ),
        "agent" if kind.startswith("agent.") else "owner",
    )


def start(entry, call="fixture-call", **kwargs):
    return DecisionsCallStart(
        call_id=call,
        attempt_id="attempt-" + call,
        attempt=1,
        source_event_id=entry.event_id,
        source_sequence=entry.sequence,
        source_revision=entry.payload.get("revision", 1),
        session_id=entry.session_id,
        thread_id=entry.thread_id,
        epoch=entry.epoch,
        provider="synthetic",
        model="historical-v1",
        request_format="chat_completions",
        request={"messages": [{"role": "user", "content": "PRIVATE_CAPTURE_FIXTURE " + LITERAL}]},
        **kwargs,
    )


def finish(entry, **kwargs):
    return DecisionsCallFinish(
        source_event_id=entry.event_id,
        status="completed",
        raw_response='{"actual":"' + "PUBLIC_SYNTHETIC" + '"}',
        validated_output={"route": "ignore"},
        **kwargs,
    )


def test_capture_roles_source_binding_idempotency_and_no_transcript_leak(env):
    entry = source(env)
    body = start(entry).model_dump(mode="json")
    for role in AUTH.tokens:
        expected = 200 if role == "decisions" else 403
        assert (
            env.client.post(
                "/v1/decisions/calls/start", json=body, headers=header(role)
            ).status_code
            == expected
        )
    original = env.client.post(
        "/v1/decisions/calls/start", json=body, headers=header("decisions")
    ).json()
    assert original["request"] == body["request"]
    conflict = {**body, "model": "replacement-model"}
    assert (
        env.client.post(
            "/v1/decisions/calls/start", json=conflict, headers=header("decisions")
        ).status_code
        == 409
    )
    other = env.store.create_session(SessionRequest())
    forged = {**body, "call_id": "forged", "session_id": other.session_id}
    assert (
        env.client.post(
            "/v1/decisions/calls/start", json=forged, headers=header("decisions")
        ).status_code
        == 409
    )
    completion = finish(entry).model_dump(mode="json")
    first = env.client.post(
        "/v1/decisions/calls/fixture-call/finish", json=completion, headers=header("decisions")
    )
    assert first.status_code == 200
    assert (
        env.client.post(
            "/v1/decisions/calls/fixture-call/finish", json=completion, headers=header("decisions")
        ).json()
        == first.json()
    )
    assert (
        env.client.post(
            "/v1/decisions/calls/fixture-call/finish",
            json={**completion, "status": "error"},
            headers=header("decisions"),
        ).status_code
        == 409
    )
    for role in AUTH.tokens:
        expected = 200 if role == "owner" else 403
        assert (
            env.client.get("/v1/decisions/inspections", headers=header(role)).status_code
            == expected
        )
        assert (
            env.client.get(
                f"/v1/decisions/inspections/{entry.event_id}", headers=header(role)
            ).status_code
            == expected
        )
        transcript = env.client.get("/v1/transcript", headers=header(role))
        assert "PRIVATE_CAPTURE_FIXTURE" not in transcript.text
    assert env.client.get("/v1/decisions/inspections").status_code == 401
    assert (
        env.client.get(
            "/v1/decisions/inspections", headers={**header(), "Origin": "http://hostile"}
        ).status_code
        == 403
    )
    detail = env.client.get(f"/v1/decisions/inspections/{entry.event_id}", headers=header()).json()
    assert detail["source"]["payload"]["text"] == LITERAL
    assert detail["calls"][0]["request"] == body["request"]
    assert detail["summary"]["model"] == "historical-v1"


def test_legacy_and_agent_sources_are_literal_no_inference_projections(env):
    legacy = source(env)
    agent_session = env.store.create_session(SessionRequest(channel="agent"))
    agent = source(env, session=agent_session, kind="agent.command")
    page = env.client.get("/v1/decisions/inspections", headers=header()).json()
    assert [r["source_event_id"] for r in page["rows"]] == [agent.event_id, legacy.event_id]
    assert page["rows"][0]["disposition"] == "no_inference"
    assert page["rows"][0]["capture_state"] == "not_applicable"
    assert page["rows"][1]["provider"] is None and page["rows"][1]["call_count"] == 0
    assert page["rows"][1]["capture_state"] == "unavailable"
    assert (
        env.client.post(
            "/v1/decisions/calls/start",
            json=start(agent).model_dump(mode="json"),
            headers=header("decisions"),
        ).status_code
        == 409
    )
    assert env.store.db.execute("SELECT count(*) FROM decisions_calls").fetchone()[0] == 0


def test_source_pagination_combined_filters_unicode_and_native_outcomes(env):
    entries = [source(env, text=f"fixture {i}", index=i) for i in range(5)]
    chosen = entries[2]
    env.store.decisions.start(start(chosen))
    env.store.decisions.finish("fixture-call", finish(chosen))
    env.store.append(
        AppendRequest(
            session_id=chosen.session_id,
            thread_id=chosen.thread_id,
            epoch=chosen.epoch,
            kind="routing.decided",
            payload={"decision_id": "historical-decision", "route": "ignore", "confidence": 0.75},
            causes=[chosen.event_id],
            idempotency_key="native-ignore",
        ),
        "decisions",
    )
    query = {
        "session_id": chosen.session_id,
        "source": "conversation",
        "outcome": "ignore",
        "search": "Ω <script>",
        "from_date": chosen.created_at.date().isoformat(),
        "to_date": chosen.created_at.date().isoformat(),
        "timezone": "UTC",
    }
    page = env.client.get("/v1/decisions/inspections", params=query, headers=header()).json()
    assert [r["source_event_id"] for r in page["rows"]] == [chosen.event_id]
    assert page["rows"][0]["confidence"] == 0.75
    assert (
        env.store.decisions.page(outcome="classified")["rows"][0]["source_event_id"]
        == chosen.event_id
    )
    first = env.store.decisions.page(limit=2)
    source(env, text="newer source", index=9)
    second = env.store.decisions.page(
        limit=2,
        through_sequence=first["through_sequence"],
        before_sequence=first["next_before_sequence"],
    )
    third = env.store.decisions.page(
        limit=2,
        through_sequence=first["through_sequence"],
        before_sequence=second["next_before_sequence"],
    )
    assert [r["source_event_id"] for p in (first, second, third) for r in p["rows"]] == [
        e.event_id for e in reversed(entries)
    ]
    assert third["exhausted"]


@pytest.mark.parametrize(
    "query",
    [
        "limit=21",
        "limit=01",
        "limit=1&limit=2",
        "surprise=1",
        "before_sequence=2",
        "from_date=2026-10-30",
        "from_date=2026-10-30&to_date=2026-10-29",
        "timezone=Not/A_Zone",
        "search=" + "a" * 201,
    ],
)
def test_ambiguous_or_unbounded_queries_are_rejected(env, query):
    assert env.client.get("/v1/decisions/inspections?" + query, headers=header()).status_code == 422


def test_source_dates_are_explicit_half_open_local_days_across_dst(env):
    first = source(env)
    second = source(env, index=1)
    for entry, timestamp in (
        (first, datetime(2026, 11, 1, 7, tzinfo=UTC)),
        (second, datetime(2026, 11, 2, 8, tzinfo=UTC)),
    ):
        value = entry.model_copy(update={"created_at": timestamp}).model_dump_json()
        env.store.db.execute("UPDATE entries SET value=? WHERE event_id=?", (value, entry.event_id))
    env.store.db.commit()
    page = env.store.decisions.page(
        from_date="2026-11-01",
        to_date="2026-11-01",
        timezone="America/Los_Angeles",
        session_id=first.session_id,
    )
    assert [r["source_event_id"] for r in page["rows"]] == [first.event_id]
    assert date_range("2026-11-01", "2026-11-01", "America/Los_Angeles") == (
        "2026-11-01T07:00:00+00:00",
        "2026-11-02T08:00:00+00:00",
    )


def test_detail_byte_paging_quota_reset_and_delete(env):
    entry = source(env)
    for i in range(3):
        env.store.decisions.start(start(entry, f"call-{i}"))
        env.store.decisions.finish(
            f"call-{i}",
            DecisionsCallFinish(
                source_event_id=entry.event_id, status="completed", raw_response="s" * 1048576
            ),
        )
    page = env.store.decisions.detail(entry.event_id)
    assert len(page["calls"]) == 2 and not page["exhausted"]
    assert len(json.dumps(page).encode()) < 4 * 1024 * 1024
    rest = env.store.decisions.detail(
        entry.event_id,
        through_call_sequence=page["through_call_sequence"],
        before_call_sequence=page["next_before_call_sequence"],
    )
    assert len(rest["calls"]) == 1 and rest["exhausted"]
    env.store.decisions.payload_quota = 10
    env.store.decisions.start(start(entry, "quota"))
    assert all(
        c["capture_state"] == "expired" and c["request"] is None
        for c in env.store.decisions.detail(entry.event_id)["calls"]
    )
    env.store.reset(entry.session_id)
    assert env.store.decisions.detail(entry.event_id)["source"]["epoch"] == 1
    env.store.decisions.finish("quota", finish(entry))
    with pytest.raises(ValueError):
        env.store.decisions.start(start(entry, "obsolete"))
    env.store.delete(entry.session_id)
    assert env.store.db.execute("SELECT count(*) FROM decisions_calls").fetchone()[0] == 0
    assert (
        env.client.get(f"/v1/decisions/inspections/{entry.event_id}", headers=header()).status_code
        == 404
    )
    assert (
        env.client.post(
            "/v1/decisions/calls/quota/finish",
            json=finish(entry).model_dump(mode="json"),
            headers=header("decisions"),
        ).status_code
        == 404
    )


def test_corrections_keep_original_source_and_refuse_cross_revision_capture(env):
    original = source(env)
    revision = env.store.append(
        AppendRequest(
            session_id=original.session_id,
            thread_id=original.thread_id,
            epoch=original.epoch,
            kind="conversation.revision",
            payload={
                "target_event_id": original.event_id,
                "revision": 2,
                "text": "Corrected public fixture",
                "content_scope": "public_synthetic",
            },
            causes=[original.event_id],
            idempotency_key="correction",
        ),
        "owner",
    )
    forged = start(original).model_copy(update={"source_revision": 2})
    with pytest.raises(ValueError):
        env.store.decisions.start(forged)
    env.store.decisions.start(start(revision))
    assert env.store.decisions.detail(original.event_id)["source"]["payload"]["text"] == LITERAL
    detail = env.store.decisions.detail(revision.event_id)
    assert detail["summary"]["source_revision"] == 2
    assert detail["calls"][0]["source_event_id"] == revision.event_id


@pytest.mark.parametrize(
    "body", ['{"call_id":"first","call_id":"second"}', '{"bad":NaN}', '{"source_sequence":true}']
)
def test_capture_json_rejects_ambiguous_nonfinite_and_coerced_input(env, body):
    response = env.client.post(
        "/v1/decisions/calls/start",
        content=body,
        headers={**header("decisions"), "Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_escaped_response_bound_and_cross_session_reuse(env):
    first = source(env)
    env.store.decisions.start(start(first))
    with pytest.raises(ValueError):
        DecisionsCallFinish(
            source_event_id=first.event_id, status="completed", raw_response="\0" * 200000
        )
    other = source(env, session=env.store.create_session(SessionRequest()))
    request = start(other, "reuse").model_copy(
        update={"request": None, "request_format": "reused", "reused_from_call_id": "fixture-call"}
    )
    with pytest.raises(ValueError):
        env.store.decisions.start(request)


class Agent:
    async def status(self):
        return []

    async def submit(self, *args, **kwargs):
        pytest.fail("Inspection or an ignored classification dispatched work")


def laya_transport(seen, *, refuse=False, retry=False):
    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        if retry and len(seen) == 1:
            return httpx.Response(500, stream=httpx.ByteStream(b'{"public_fixture":"retry"}'))
        answers = {}
        for name, question in body["questions"].items():
            labels = list(question["criteria"])
            picked = labels[0]
            answers[name] = {
                "type": "choice",
                "choice": picked,
                "probabilities": {k: float(k == picked) for k in labels},
                "answer_confidence": 1.0,
            }
        if refuse:
            answers["route"]["answer_confidence"] = 0.2
        return httpx.Response(
            200,
            stream=httpx.ByteStream(
                json.dumps(
                    {
                        "answers": answers,
                        "usage": {
                            "truncated": False,
                            "state_tokens_dropped": 0,
                            "truncated_questions": [],
                        },
                        "routing": {"model": "english"},
                    },
                    ensure_ascii=False,
                ).encode()
            ),
        )

    return httpx.MockTransport(respond)


@pytest.mark.parametrize("refuse", [False, True])
async def test_real_laya_boundary_retains_exact_script_answers_and_ignore_or_refusal(env, refuse):
    entry, seen = source(env), []
    provider = LayaRoutingProvider(
        "http://127.0.0.1:8870", transport=laya_transport(seen, refuse=refuse)
    )
    try:
        worker = DecisionsWorker(
            LocalJournal(env.store, "decisions"), Agent(), ModelClassifier(provider)
        )
        await worker.process(entry)
        detail = env.store.decisions.detail(entry.event_id)
        call = detail["calls"][0]
        assert call["request"] == seen[0]
        assert all(
            set(q) == {"type", "instructions", "criteria"}
            for q in call["request"]["questions"].values()
        )
        assert json.loads(call["raw_response"])["answers"]["route"]["answer_confidence"] == (
            0.2 if refuse else 1
        )
        assert call["status"] == ("refused" if refuse else "completed")
        assert call["provider"] == "laya" and call["model"] == "english"
        assert (
            call["validated_output"] is None
            if refuse
            else call["validated_output"]["route"] == "ignore"
        )
        assert detail["summary"]["disposition"] == ("refused" if refuse else "classified")
    finally:
        await provider.close()


async def test_window_retry_and_reuse_keep_distinct_original_call_identity(env, monkeypatch):
    monkeypatch.setattr("services.decisions.window_worker.RETRY_DELAY", 0)
    entry, seen = source(env, kind="conversation.window"), []
    provider = LayaRoutingProvider(
        "http://127.0.0.1:8870", transport=laya_transport(seen, retry=True)
    )
    try:
        worker = WindowDecisionsWorker(
            LocalJournal(env.store, "decisions"), Agent(), WindowClassifier(provider)
        )
        await worker._job(entry)
        calls = env.store.decisions.detail(entry.event_id)["calls"]
        assert [c["status"] for c in calls] == ["completed", "error"]
        assert [c["attempt"] for c in calls] == [2, 1]
        repeated = source(env, kind="conversation.window", index=1)
        await worker._job(repeated)
        reuse = env.store.decisions.detail(repeated.event_id)["calls"][0]
        assert len(seen) == 2 and reuse["status"] == "reused"
        assert reuse["reused_from_call_id"] == calls[0]["call_id"]
        assert reuse["reused_from_source_event_id"] == entry.event_id
        assert reuse["request"] is None
    finally:
        await provider.close()


@pytest.mark.parametrize("kind", ["compatible", "meta"])
async def test_other_decisions_adapters_capture_the_actual_wire_body(env, kind):
    entry, seen = source(env), []

    def respond(request):
        seen.append(json.loads(request.content))
        payload = (
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"route":"ignore"}'},
                        "finish_reason": "stop",
                    }
                ]
            }
            if kind == "compatible"
            else {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": '{"route":"ignore"}'}],
                    }
                ],
            }
        )
        return httpx.Response(200, json=payload)

    provider = (
        CompatibleTextProvider(
            "http://localhost/v1", "fixture", transport=httpx.MockTransport(respond)
        )
        if kind == "compatible"
        else MetaResponsesProvider(
            base_url="http://localhost/v1",
            api_key="synthetic",
            allow_cloud=True,
            transport=httpx.MockTransport(respond),
        )
    )
    try:
        async with decision_attempt(
            LocalJournal(env.store, "decisions"), entry, provider
        ) as capture:
            output = await provider.complete(
                [
                    {"role": "system", "content": "public fixture"},
                    {"role": "user", "content": LITERAL},
                ]
            )
            capture.validated_output = json.loads(output)
        call = env.store.decisions.detail(entry.event_id)["calls"][0]
        assert call["request"] == seen[0]
        assert call["validated_output"] == {"route": "ignore"}
        assert "Authorization" not in json.dumps(call["request"])
    finally:
        await provider.close()


async def test_unsettled_calls_remain_unknown_and_storage_failure_never_fabricates_capture(env):
    entry = source(env)
    env.store.decisions.start(start(entry))
    detail = env.store.decisions.detail(entry.event_id)
    assert detail["summary"]["disposition"] == "pending"
    assert detail["calls"][0]["status"] == "started" and detail["calls"][0]["completed_at"] is None

    class Unavailable:
        async def decisions_call_start(self, request):
            raise OSError("synthetic unavailable storage")

    async with decision_attempt(Unavailable(), entry, None) as capture:
        capture.validated_output = {"route": "ignore"}
    assert len(env.store.decisions.detail(entry.event_id)["calls"]) == 1


@pytest.mark.parametrize("committed_before_failure", [False, True])
async def test_real_request_capture_recovers_identical_start_after_uncertain_ack(
    env, committed_before_failure
):
    entry, seen, attempts = source(env), [], []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"route":"ignore"}'},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    class Flaky(LocalJournal):
        async def decisions_call_start(self, request):
            attempts.append(request.model_dump(mode="json"))
            if len(attempts) == 1:
                if committed_before_failure:
                    await super().decisions_call_start(request)
                raise OSError("synthetic uncertain capture acknowledgement")
            return await super().decisions_call_start(request)

    provider = CompatibleTextProvider(
        "http://localhost/v1", "fixture", transport=httpx.MockTransport(respond)
    )
    try:
        decision = await DecisionsWorker(
            Flaky(env.store, "decisions"), Agent(), ModelClassifier(provider)
        ).process(entry)
        call = env.store.decisions.detail(entry.event_id)["calls"][0]
        assert decision.route == "ignore"
        assert len(seen) == 1 and attempts[0] == attempts[1]
        assert call["request_format"] == "chat_completions" and call["request"] == seen[0]
        assert call["status"] == "completed" and call["capture_state"] == "retained"
        assert env.store.decisions.detail(entry.event_id)["summary"]["call_count"] == 1
    finally:
        await provider.close()


async def test_unrepresentable_request_capture_is_not_relabelled_as_unsubmitted(env):
    entry = source(env)
    async with decision_attempt(LocalJournal(env.store, "decisions"), entry, None):
        await capture_request(None, {"body": "x" * MAX_REQUEST_BYTES}, "chat_completions")
    detail = env.store.decisions.detail(entry.event_id)
    assert detail["calls"] == [] and detail["summary"]["capture_state"] == "unavailable"


@pytest.mark.parametrize("feedback", [False, True])
async def test_inspection_validation_preserves_native_missing_target_overrides(env, feedback):
    entry = env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.user",
            idempotency_key="missing-target-fixture",
            payload={
                "text": "Public synthetic correction",
                "final": True,
                "revision": 1,
                "content_scope": "public_synthetic",
                **({"outcome_event_id": "synthetic-outcome"} if feedback else {}),
            },
        ),
        "owner",
    )
    provider = CompatibleTextProvider(
        "http://localhost/v1",
        "fixture",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": '{"route":"interrupt"}'},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        ),
    )
    try:
        decision = await DecisionsWorker(
            LocalJournal(env.store, "decisions"), Agent(), ModelClassifier(provider)
        ).process(entry)
        assert decision.route == "ignore"
        assert decision.reason_code == (
            "outcome_feedback_loop_prevented" if feedback else "unknown_or_stale_target"
        )
        detail = env.store.decisions.detail(entry.event_id)
        assert detail["calls"][0]["validated_output"] is None
        assert "interrupt" in detail["calls"][0]["raw_response"]
        assert detail["summary"]["outcome"] == "ignore"
        assert [receipt["kind"] for receipt in detail["receipts"]] == ["routing.decided"]
    finally:
        await provider.close()


@pytest.mark.parametrize("kind", ["laya", "compatible", "meta"])
@pytest.mark.parametrize(
    "change", ["reset", "source_revision", "context_scope", "window_revision", "context_window"]
)
async def test_capture_await_rechecks_native_sources_before_export(env, kind, change):
    context = source(
        env, kind="conversation.window" if change == "context_window" else "conversation.user"
    )
    entry = source(
        env,
        kind="conversation.window"
        if change in {"window_revision", "context_window"}
        else "conversation.user",
        index=1,
    )
    provider = (
        LayaRoutingProvider(
            "http://localhost",
            transport=httpx.MockTransport(
                lambda request: pytest.fail("Obsolete or restricted source was exported")
            ),
        )
        if kind == "laya"
        else CompatibleTextProvider(
            "http://localhost",
            "fixture",
            transport=httpx.MockTransport(
                lambda request: pytest.fail("Obsolete or restricted source was exported")
            ),
        )
        if kind == "compatible"
        else MetaResponsesProvider(
            base_url="http://localhost",
            api_key="synthetic",
            allow_cloud=True,
            transport=httpx.MockTransport(
                lambda request: pytest.fail("Obsolete or restricted source was exported")
            ),
        )
    )
    # Exercise the cloud scope policy without contacting a remote transport.
    provider.info = provider.info.model_copy(update={"local": False})

    class Changed(LocalJournal):
        async def decisions_call_start(self, request):
            result = await super().decisions_call_start(request)
            if change == "reset":
                env.store.reset(entry.session_id)
            else:
                target = context if change in {"context_scope", "context_window"} else entry
                payload = {
                    **target.payload,
                    "revision": 2,
                    "text": "Synthetic replacement",
                    **(
                        {"content_scope": "local_only"}
                        if change in {"context_scope", "context_window"}
                        else {}
                    ),
                }
                env.store.append(
                    AppendRequest(
                        session_id=entry.session_id,
                        thread_id=entry.thread_id,
                        epoch=entry.epoch,
                        kind="conversation.window"
                        if change in {"window_revision", "context_window"}
                        else "conversation.revision",
                        payload=payload
                        if change in {"window_revision", "context_window"}
                        else {
                            **payload,
                            "target_event_id": target.event_id,
                        },
                        idempotency_key="changed-during-capture",
                    ),
                    "owner",
                )
            await asyncio.sleep(0)
            return result

    try:
        with pytest.raises(ProviderRefusal):
            async with decision_attempt(Changed(env.store, "decisions"), entry, provider):
                classifier = (
                    WindowClassifier(provider)
                    if change in {"window_revision", "context_window"}
                    else ModelClassifier(provider)
                )
                await classifier.classify(entry, [context], [])
        call = env.store.decisions.detail(entry.event_id)["calls"][0]
        assert call["status"] == "refused" and call["raw_response"] is None
    finally:
        await provider.close()


@pytest.mark.parametrize("kind", ["laya", "compatible", "meta"])
async def test_capture_dto_cannot_mutate_nested_outgoing_request(env, kind):
    entry, seen = source(env), []
    base = laya_transport(seen)

    def respond(request):
        if kind == "laya":
            return base.handle_request(request)
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"route":"ignore"}'},
                        "finish_reason": "stop",
                    }
                ]
            }
            if kind == "compatible"
            else {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": '{"route":"ignore"}'}],
                    }
                ],
            },
        )

    provider = (
        LayaRoutingProvider("http://localhost", transport=httpx.MockTransport(respond))
        if kind == "laya"
        else CompatibleTextProvider(
            "http://localhost", "fixture", transport=httpx.MockTransport(respond)
        )
        if kind == "compatible"
        else MetaResponsesProvider(
            base_url="http://localhost",
            api_key="synthetic",
            allow_cloud=True,
            transport=httpx.MockTransport(respond),
        )
    )

    class Mutated(LocalJournal):
        async def decisions_call_start(self, request):
            result = await super().decisions_call_start(request)
            if kind == "laya":
                request.request["questions"]["route"]["instructions"] = "MUTATED_CAPTURE_DTO"
            elif kind == "compatible":
                request.request["messages"][0]["content"] = "MUTATED_CAPTURE_DTO"
            else:
                request.request["input"][0]["content"] = "MUTATED_CAPTURE_DTO"
            await asyncio.sleep(0)
            return result

    try:
        await DecisionsWorker(
            Mutated(env.store, "decisions"), Agent(), ModelClassifier(provider)
        ).process(entry)
        call = env.store.decisions.detail(entry.event_id)["calls"][0]
        assert call["request"] == seen[0]
        assert "MUTATED_CAPTURE_DTO" not in json.dumps(seen)
        assert call["status"] == "completed"
    finally:
        await provider.close()


async def test_error_capture_has_total_deadline_and_preserves_original_provider_error(env):
    entry = source(env)

    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"incomplete":"'
            await asyncio.Event().wait()

    provider = CompatibleTextProvider(
        "http://localhost",
        "fixture",
        transport=httpx.MockTransport(lambda request: httpx.Response(500, stream=Stalled())),
    )
    try:
        with pytest.raises(ProviderError, match="Provider HTTP 500"):
            async with asyncio.timeout(1.5):
                async with decision_attempt(LocalJournal(env.store, "decisions"), entry, provider):
                    await ModelClassifier(provider).classify(entry, [], [])
        call = env.store.decisions.detail(entry.event_id)["calls"][0]
        assert call["status"] == "error" and call["raw_response"] is None
        assert call["error_type"] == "ProviderError"
    finally:
        await provider.close()


@pytest.mark.parametrize("window", [False, True])
async def test_transient_currentness_read_failure_keeps_native_backlog_retriable(
    env, window, monkeypatch
):
    monkeypatch.setattr("services.decisions.window_worker.RETRY_DELAY", 0)
    entry, seen = source(env, kind="conversation.window" if window else "conversation.user"), []
    provider = LayaRoutingProvider("http://localhost", transport=laya_transport(seen))

    class Flaky(LocalJournal):
        first = True
        fail_entry = False

        async def decisions_call_start(self, request):
            result = await super().decisions_call_start(request)
            if self.first:
                self.first, self.fail_entry = False, True
            return result

        async def entry(self, event_id):
            if self.fail_entry:
                self.fail_entry = False
                raise OSError("synthetic transient native read failure")
            return await super().entry(event_id)

    journal = Flaky(env.store, "decisions")
    try:
        if window:
            worker = WindowDecisionsWorker(journal, Agent(), WindowClassifier(provider))
            await worker._job(entry)
            assert worker.last_error == "journal_unavailable"
            assert any(item.event_id == entry.event_id for item in env.store.pending_windows())
        else:
            worker = DecisionsWorker(journal, Agent(), ModelClassifier(provider))
            with pytest.raises(CaptureNativeUnavailable):
                await worker.tick()
            assert await journal.cursor(worker.consumer) < entry.sequence
        assert seen == []
        receipts = env.store.decisions.detail(entry.event_id)["receipts"]
        assert all(
            receipt["kind"] == "routing.window_status"
            and receipt["payload"]["phase"] == "classifying"
            for receipt in receipts
        )
        if window:
            await worker._job(entry)
            assert env.store.window_decision(entry.event_id) is not None
        else:
            await worker.tick()
            assert await journal.cursor(worker.consumer) >= entry.sequence
        assert len(seen) == 1
        assert [call["status"] for call in env.store.decisions.detail(entry.event_id)["calls"]] == [
            "completed",
            "error",
        ]
    finally:
        await provider.close()


@pytest.mark.parametrize("match", [True, False])
async def test_artifact_identity_is_historical_only_after_verified_header(env, match):
    entry, seen = source(env), []
    base = laya_transport(seen)

    def respond(request):
        response = base.handle_request(request)
        response.headers[ARTIFACT_HEADER] = ("a" if match else "b") * 64
        return response

    provider = LayaRoutingProvider(
        "http://localhost",
        expected_artifact_sha256="a" * 64,
        transport=httpx.MockTransport(respond),
    )
    try:
        await DecisionsWorker(
            LocalJournal(env.store, "decisions"), Agent(), ModelClassifier(provider)
        ).process(entry)
        call = env.store.decisions.detail(entry.event_id)["calls"][0]
        assert call["artifact_sha256"] == ("a" * 64 if match else None)
        assert call["status"] == ("completed" if match else "refused")
        provider.info = provider.info.model_copy(update={"model": "current-replacement"})
        assert env.store.decisions.detail(entry.event_id)["calls"][0]["model"] == "english"
    finally:
        await provider.close()


@pytest.mark.parametrize("kind", ["laya", "compatible", "meta"])
async def test_capture_await_cannot_switch_the_selected_transport(env, kind):
    entry = source(env)
    transport = httpx.MockTransport(lambda request: pytest.fail("Changed transport was exported"))
    provider = (
        LayaRoutingProvider("http://localhost", transport=transport)
        if kind == "laya"
        else CompatibleTextProvider("http://localhost", "fixture", transport=transport)
        if kind == "compatible"
        else MetaResponsesProvider(
            base_url="http://localhost", api_key="synthetic", allow_cloud=True, transport=transport
        )
    )

    class Replaced(LocalJournal):
        async def decisions_call_start(self, request):
            result = await super().decisions_call_start(request)
            provider.http.headers["x-selected-profile"] = "replacement"
            return result

    try:
        with pytest.raises(ProviderRefusal):
            async with decision_attempt(Replaced(env.store, "decisions"), entry, provider):
                await ModelClassifier(provider).classify(entry, [], [])
        call = env.store.decisions.detail(entry.event_id)["calls"][0]
        assert call["raw_response"] is None
        assert call["status"] == "refused"
    finally:
        await provider.close()


async def test_cancelled_attempt_retains_truthful_status_and_resets_capture_scope(env):
    entry = source(env)
    with pytest.raises(asyncio.CancelledError):
        async with decision_attempt(LocalJournal(env.store, "decisions"), entry, None):
            await capture_request(
                None, {"fixture": "actual synthetic boundary"}, "chat_completions"
            )
            raise asyncio.CancelledError
    call = env.store.decisions.detail(entry.event_id)["calls"][0]
    assert call["status"] == "cancelled" and call["validated_output"] is None
    await capture_request(None, {"fixture": "outside classification"}, "chat_completions")
    assert len(env.store.decisions.detail(entry.event_id)["calls"]) == 1


def test_empty_inspection_and_dispositions_do_not_create_rows(env):
    before = env.store.db.total_changes
    assert env.store.decisions.page()["rows"] == []
    assert env.store.db.total_changes == before
    agent = source(
        env, session=env.store.create_session(SessionRequest(channel="agent")), kind="agent.state"
    )
    assert (
        env.store.decisions.page(outcome="no_inference")["rows"][0]["source_event_id"]
        == agent.event_id
    )
    assert env.store.db.execute("SELECT count(*) FROM decisions_calls").fetchone()[0] == 0
