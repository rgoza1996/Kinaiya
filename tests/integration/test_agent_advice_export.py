"""#566 native task/source admission, independent of model behavior or Converse."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from test_agent_submit_admission import proposal

from packages.contracts.agent_advice import (
    MAX_ADVICE_EXPORT_BYTES,
    MAX_ADVICE_EXPORT_RESPONSE_BYTES,
    AgentAdviceExportAdmission,
    AgentAdviceExportRequest,
)
from packages.contracts.agent_control import AgentTaskIdentity
from packages.contracts.agent_submit import submit_entry
from packages.contracts.client import JournalClient
from packages.contracts.models import AppendRequest, SessionRequest
from packages.contracts.operation_ownership import ActualOperation, actual_owner
from services.common.auth import Auth
from services.session.agent_advice import AgentAdviceConflict, AgentAdviceUnavailable, admit_export
from services.session.api import create_app
from services.session.store import LocalJournal, Store

TOKENS = {
    role: c * 40
    for role, c in zip(
        ("owner", "agent", "converse", "decisions", "resources"), "oacdr", strict=True
    )
}
PATH = "/v1/transcript/agent-advice-export-admission"


def append(env, kind, payload, *, role="agent", causes=None):
    return env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind=kind,
            payload=payload,
            task_refs=[env.initial.payload["task_id"]] if role == "agent" else [],
            causes=[] if causes is None else causes,
            idempotency_key=f"agent:{env.initial.payload['task_id']}:{kind}:{len(env.store.read())}",
        ),
        role,
    )


def progress(env, state="running", **changes):
    return append(
        env,
        "agent.state",
        {
            "task_id": env.initial.payload["task_id"],
            "state": state,
            "objective_revision": 1,
            "content_scope": "public_synthetic",
            **changes,
        },
    )


def request(env, **changes):
    return AgentAdviceExportRequest.model_validate(
        {
            "task": env.identity.model_dump(),
            "objective_revision": 1,
            "source_revisions": {env.source.event_id: 1},
            "content_scope": "public_synthetic",
            "instruction_scope": "public_synthetic",
            "provider": {"provider": "meta", "model": "synthetic", "local": False},
            "policy_sha256": "a" * 64,
            "logical_request_id": "synthetic-original-advice",
            "request_sha256": "b" * 64,
            "purpose": "complete_count",
            "request_nonce": "synthetic-count-1",
            **changes,
        }
    )


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "session.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    source = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload={
                "text": "Synthetic source kept outside export receipts.",
                "revision": 1,
                "final": True,
                "content_scope": "public_synthetic",
            },
            idempotency_key="source",
        ),
        "owner",
    )
    body = proposal(session, content_scope="public_synthetic", source_event_ids=[source.event_id])
    initial = submit_entry(store.admit_submit(body, "agent").entry)
    identity = AgentTaskIdentity(
        schema_version="1.0",
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        task_id=initial.payload["task_id"],
        submit_event_id=initial.event_id,
        submit_sequence=initial.sequence,
        submit_idempotency_key=initial.payload["idempotency_key"],
        submit_payload_digest=initial.payload["payload_digest"],
    )
    value = SimpleNamespace(
        store=store, session=session, source=source, initial=initial, identity=identity
    )
    progress(value)
    value.app = create_app(store, Auth(TOKENS))
    try:
        yield value
    finally:
        store.close()


def test_atomic_advice_admission_preserves_native_journal_and_exact_body_binding(env):
    original = request(env)
    before = env.store.read()
    result = admit_export(env.store, original)
    assert result.validate_request(original) is result
    assert result.task_state == "running" and result.export.effective_scope == "public_synthetic"
    expected = hashlib.sha256(
        json.dumps(
            original.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    assert result.binding_sha256 == expected
    assert result.export.request_sha256 == "b" * 64
    assert env.store.read() == before
    assert env.source.payload["text"] not in result.canonical_bytes().decode()
    assert (
        AgentAdviceExportAdmission.from_json(result.canonical_bytes()).validate_request(original)
        == result
    )


def test_task_and_source_proof_use_one_sqlite_snapshot_across_peer_writer(env, monkeypatch):
    peer = Store(env.store.path)
    peer_env = SimpleNamespace(**(vars(env) | {"store": peer}))
    before_sequence = env.store.read()[-1].sequence
    original_scope = env.store._agent_source_scope
    writes = []

    def cancel_from_peer(original, resolved):
        writes.append(progress(peer_env, "cancel_requested"))
        return original_scope(original, resolved)

    monkeypatch.setattr(env.store, "_agent_source_scope", cancel_from_peer)
    try:
        result = admit_export(env.store, request(env))
        assert len(writes) == 1 and writes[0].sequence > before_sequence
        assert peer.read(kinds=["agent.state"])[-1].payload["state"] == "cancel_requested"
        # A completed snapshot can precede the peer's cancellation. It cannot
        # claim running at a journal watermark that already includes that cancel.
        assert result.export.journal_sequence == before_sequence
        with pytest.raises(AgentAdviceConflict):
            admit_export(env.store, request(env))
    finally:
        peer.close()


def test_advice_read_refuses_an_existing_transaction_without_ending_it(env):
    env.store.db.execute("BEGIN")
    try:
        with pytest.raises(AgentAdviceUnavailable):
            admit_export(env.store, request(env))
        assert env.store.db.in_transaction is True
    finally:
        env.store.db.rollback()


@pytest.mark.parametrize(
    "state",
    [
        "accepted",
        "queued",
        "paused",
        "blocked",
        "awaiting_approval",
        "verifying",
        "cancel_requested",
        "cancelled",
        "failed",
        "succeeded",
    ],
)
def test_nonrunning_native_task_cannot_admit_new_advice_exports(env, state):
    progress(env, state)
    with pytest.raises(AgentAdviceConflict):
        admit_export(env.store, request(env))


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "different-task"),
        ("submit_event_id", "missing"),
        ("submit_sequence", 999),
        ("submit_idempotency_key", "different-submit"),
        ("submit_payload_digest", "c" * 64),
    ],
)
def test_model_or_caller_claims_cannot_replace_native_task_identity(env, field, value):
    changed = env.identity.model_dump() | {field: value}
    with pytest.raises((AgentAdviceConflict, AgentAdviceUnavailable)):
        admit_export(env.store, request(env, task=changed))


def test_objective_revision_and_complete_native_source_set_are_required(env):
    with pytest.raises(AgentAdviceConflict):
        admit_export(env.store, request(env, objective_revision=2))
    with pytest.raises(AgentAdviceConflict):
        admit_export(env.store, request(env, source_revisions={}))
    with pytest.raises(AgentAdviceConflict):
        admit_export(
            env.store, request(env, source_revisions={**{env.source.event_id: 1}, "invented": 1})
        )


@pytest.mark.parametrize("scope", ["local_only", "private_cloud_authorized"])
def test_instruction_scope_does_not_inherit_public_task_consent(env, scope):
    with pytest.raises(PermissionError):
        admit_export(env.store, request(env, instruction_scope=scope))
    local = request(
        env,
        instruction_scope=scope,
        provider={"provider": "lmstudio", "model": "synthetic", "local": True},
    )
    assert admit_export(env.store, local).export.effective_scope == scope


def test_native_task_scope_remains_a_disclosure_floor(env):
    progress(env, content_scope="local_only")
    with pytest.raises(PermissionError):
        admit_export(env.store, request(env))
    local = request(env, provider={"provider": "lmstudio", "model": "synthetic", "local": True})
    result = admit_export(env.store, local)
    assert result.task_content_scope == result.export.effective_scope == "local_only"


def test_source_revision_and_sticky_disclosure_are_rechecked(env):
    append(
        env,
        "conversation.revision",
        {
            "target_event_id": env.source.event_id,
            "text": "Synthetic correction",
            "revision": 2,
            "content_scope": "local_only",
        },
        role="owner",
    )
    with pytest.raises(AgentAdviceUnavailable):
        admit_export(env.store, request(env))
    updated = request(env, source_revisions={env.source.event_id: 2})
    with pytest.raises(PermissionError):
        admit_export(env.store, updated)
    updated = request(
        env,
        source_revisions={env.source.event_id: 2},
        provider={"provider": "lmstudio", "model": "synthetic", "local": True},
    )
    assert admit_export(env.store, updated).export.effective_scope == "local_only"


@pytest.mark.parametrize("operation", ["reset", "delete"])
def test_session_retirement_cannot_keep_an_export_admission_current(env, operation):
    original = request(env)
    getattr(env.store, operation)(env.session.session_id)
    with pytest.raises(AgentAdviceUnavailable):
        admit_export(env.store, original)


def test_retained_context_causal_privacy_is_rechecked_without_omission(env):
    private = append(
        env,
        "conversation.user",
        {"text": "Synthetic local-only context", "final": True, "content_scope": "local_only"},
        role="owner",
    )
    public = append(
        env,
        "conversation.user",
        {"text": "Synthetic public derivative", "final": True, "content_scope": "public_synthetic"},
        role="owner",
        causes=[private.event_id],
    )
    original = request(env, context_event_ids=[public.event_id])
    with pytest.raises(PermissionError):
        admit_export(env.store, original)
    local = request(
        env,
        context_event_ids=[public.event_id],
        provider={"provider": "lmstudio", "model": "synthetic", "local": True},
    )
    result = admit_export(env.store, local)
    assert result.export.context_event_ids == [public.event_id]
    assert result.export.effective_scope == "local_only"


@pytest.mark.parametrize("role", [None, "owner", "agent", "converse", "decisions", "resources"])
async def test_new_boundary_is_decisions_only_and_does_not_expand_legacy_agent_auth(env, role):
    headers = {} if role is None else {"Authorization": "Bearer " + TOKENS[role]}
    async with httpx.AsyncClient(
        base_url="http://session", transport=httpx.ASGITransport(app=env.app)
    ) as client:
        response = await client.post(
            PATH, json=request(env).model_dump(mode="json"), headers=headers
        )
        assert response.status_code == (
            200 if role == "decisions" else 401 if role is None else 403
        )
        if role == "decisions":
            assert response.headers["cache-control"] == "no-store"
            legacy = await client.post(
                "/v1/transcript/agent-export-admission",
                json=request(env).export_request("public_synthetic").model_dump(mode="json"),
                headers=headers,
            )
            assert legacy.status_code == 403
    journal = LocalJournal(env.store, role or "owner")
    if role == "decisions":
        assert (await journal.agent_advice_export_admission(request(env))).task_state == "running"
    else:
        with pytest.raises(PermissionError):
            await journal.agent_advice_export_admission(request(env))


async def test_actual_client_uses_new_native_endpoint_and_validates_exact_echo(env):
    client = JournalClient(
        "http://session", TOKENS["decisions"], transport=httpx.ASGITransport(app=env.app)
    )
    try:
        original = request(env, purpose="complete_generation", request_nonce="native-generation-1")
        result = await client.agent_advice_export_admission(original)
        assert result.validate_request(original).export.purpose == "complete_generation"
        for field, value in {
            "logical_request_id": "different",
            "policy_sha256": "c" * 64,
            "purpose": "complete_count",
            "request_nonce": "different",
            "request_sha256": "d" * 64,
        }.items():
            with pytest.raises(ValueError):
                result.validate_request(
                    AgentAdviceExportRequest.model_validate(original.model_dump() | {field: value})
                )
    finally:
        await client.close()


@pytest.mark.parametrize(
    "headers,raw,status",
    [
        ({"Content-Type": "text/plain"}, b"{}", 415),
        ({"Content-Type": "application/json", "Content-Encoding": "gzip"}, b"{}", 415),
        ({"Content-Type": "application/json"}, b'{"contract_version":1,"contract_version":1}', 422),
        ({"Content-Type": "application/json"}, b'{"request_sha256":NaN}', 422),
        ({"Content-Type": "application/json"}, b"{}", 422),
        ({"Content-Type": "application/json"}, b"x" * (MAX_ADVICE_EXPORT_BYTES + 1), 413),
    ],
    ids=["media-type", "encoding", "duplicate-key", "nonfinite", "missing-fields", "over-bound"],
)
async def test_http_ingress_rejects_ambiguous_or_oversized_metadata(env, headers, raw, status):
    async with httpx.AsyncClient(
        base_url="http://session", transport=httpx.ASGITransport(app=env.app)
    ) as client:
        response = await client.post(
            PATH, content=raw, headers={**headers, "Authorization": "Bearer " + TOKENS["decisions"]}
        )
        assert response.status_code == status and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "changes",
    [
        {"contract_version": True},
        {"objective_revision": True},
        {"instruction_scope": "public"},
        {"context_event_ids": ["same", "same"]},
        {"policy_sha256": "invalid"},
        {"instructions": "SYNTHETIC_PRIVATE_DETAIL"},
    ],
)
def test_request_grammar_is_strict_and_does_not_reflect_inputs(env, changes):
    with pytest.raises(ValidationError) as caught:
        request(env, **changes)
    assert "SYNTHETIC_PRIVATE_DETAIL" not in str(caught.value)


@pytest.mark.parametrize(
    "headers",
    [
        {
            "Content-Type": "application/json",
            "Content-Length": str(MAX_ADVICE_EXPORT_RESPONSE_BYTES + 1),
        },
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/json", "Content-Encoding": "deflate"},
    ],
)
async def test_client_refuses_unqualified_response_envelopes(env, headers):
    async def handle(_):
        # Empty content avoids HTTPX attempting a decompression before the
        # native response-envelope guard can inspect its metadata.
        return httpx.Response(200, headers=headers, content=b"")

    client = JournalClient(
        "http://session", TOKENS["decisions"], transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(ValueError):
            await client.agent_advice_export_admission(request(env))
    finally:
        await client.close()


@pytest.mark.parametrize(
    "case", ["binding", "task-state", "uploaded-body", "original-request", "scope"]
)
async def test_client_refuses_changed_native_proof_fields(env, case):
    original = request(
        env,
        instruction_scope="local_only",
        provider={"provider": "lmstudio", "model": "synthetic", "local": True},
    )
    result = admit_export(env.store, original)
    changed = json.loads(result.canonical_bytes())
    if case == "binding":
        changed["binding_sha256"] = "c" * 64
    elif case == "task-state":
        changed["task_state"] = "paused"
    elif case == "uploaded-body":
        changed["export"]["request_sha256"] = "d" * 64
    elif case == "original-request":
        changed["request"]["logical_request_id"] = "different-original"
    else:
        changed["export"]["effective_scope"] = "public_synthetic"

    async def handle(_):
        return httpx.Response(200, json=changed)

    client = JournalClient(
        "http://session", TOKENS["decisions"], transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(ValueError):
            await client.agent_advice_export_admission(original)
    finally:
        await client.close()


@pytest.mark.parametrize("model", [AgentAdviceExportRequest, AgentAdviceExportAdmission])
def test_new_native_advice_schema_is_exported_without_changing_legacy_contracts(model):
    path = (
        Path(__file__).resolve().parents[2]
        / "packages/contracts/schemas"
        / f"{model.__name__}.json"
    )
    assert json.loads(path.read_bytes()) == model.model_json_schema()
    assert model.model_json_schema()["additionalProperties"] is False


async def test_cancelled_metadata_wait_keeps_actual_response_close_owned(env):
    original = request(env)
    raw = admit_export(env.store, original).canonical_bytes()
    entered, release = asyncio.Event(), asyncio.Event()
    closed = False

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw

        async def aclose(self):
            nonlocal closed
            entered.set()
            await release.wait()
            closed = True

    async def handle(_):
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Body())

    ownership = ActualOperation(lambda owner: None)
    caller = object()
    ownership.hold(caller)
    client = JournalClient(
        "http://session", TOKENS["decisions"], transport=httpx.MockTransport(handle)
    )
    try:
        with actual_owner(ownership):
            waiting = asyncio.create_task(client.agent_advice_export_admission(original))
        await entered.wait()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        ownership.release(caller)
        assert ownership.finished is False and closed is False
        release.set()
        await asyncio.wait_for(ownership.join(), 1)
        assert ownership.finished is True and ownership.failed is False and closed is True
    finally:
        release.set()
        await client.close()
