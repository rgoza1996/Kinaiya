"""Frozen inference-free standing export HTTP/client integration oracles."""

import importlib
import threading
import time

import httpx
import pytest
from test_standing_agent_client import SECRET, Body
from test_standing_agent_client import native as native

from packages.contracts.agent_export import AgentExportAdmission, AgentExportRequest
from packages.contracts.client import JournalClient
from packages.contracts.standing_work import StandingUnavailable

PATH = "/v1/standing/tasks/export-admission"
MARKER = "untrusted-export-content-do-not-echo"


def types():
    try:
        return importlib.import_module("packages.contracts.standing_export")
    except ModuleNotFoundError as error:
        if error.name != "packages.contracts.standing_export":
            raise
        pytest.fail("Standing export packet capability is absent", pytrace=False)


def export_request(proof, purpose="complete_generation"):
    task = proof.claim.request.task
    pin = task.model_pin
    return AgentExportRequest(
        schema_version="1.0",
        task_id=task.task_id,
        objective_revision=1,
        session_id=task.task_session.session_id,
        thread_id=task.task_session.thread_id,
        epoch=task.task_session.epoch,
        source_event_ids=[],
        source_revisions={},
        context_event_ids=[],
        content_scope=task.content_scope,
        provider={"provider": pin.provider, "model": pin.model, "local": pin.local},
        request_sha256="a" * 64,
        purpose=purpose,
        request_nonce="synthetic-export:" + purpose,
    )


def packet(proof, purpose="complete_generation"):
    return types().StandingExportRequest(
        schema_version="1.0", initial=proof, export=export_request(proof, purpose)
    )


def reply_for(original):
    return types().StandingExportAdmission(
        schema_version="1.0",
        request_digest=types().standing_export_digest(original),
        admission=AgentExportAdmission(
            **original.export.model_dump(mode="python"),
            binding_sha256=original.export.binding_digest(),
            effective_scope=original.export.content_scope,
            journal_sequence=original.initial.sequence,
        ),
    )


def headers(env, role="agent", **extra):
    return {
        **({"Authorization": "Bearer " + env.tokens[role]} if role is not None else {}),
        "Content-Type": "application/json",
        **extra,
    }


def checked(response, status):
    assert response.status_code == status, (response.status_code, response.text)
    assert response.headers.get("cache-control") == "no-store"
    assert MARKER not in response.text and SECRET.hex() not in response.text


@pytest.fixture
def accepted(native):
    native.app.state.standing_execution_owner.prepare(native.original)
    return native.app.state.standing_task_owner.admit(native.original, capability=SECRET).proof


async def test_existing_actual_native_accepted_fixture_control(native, accepted):
    assert native.app.state.standing_task_owner.lookup(native.original) == accepted
    assert accepted.journal_entry().origin == "standing-supervisor"
    assert native.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 1


class BodyWitness(httpx.AsyncByteStream):
    def __init__(self):
        self.reads = 0

    async def __aiter__(self):
        self.reads += 1
        yield MARKER.encode()


@pytest.mark.parametrize("role", [None, "owner", "converse", "decisions", "resources"])
async def test_agent_export_route_authentication_precedes_query_and_body(native, role):
    stream = BodyWitness()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=native.app), base_url="http://synthetic.invalid"
    ) as client:
        result = await client.post(PATH + "?bad=1", content=stream, headers=headers(native, role))
    checked(result, 401 if role is None else 403)
    assert stream.reads == 0


@pytest.mark.parametrize("purpose", ["complete_count", "complete_generation"])
async def test_production_client_actual_native_export_and_ordinary_bypass_refusal(
    native, accepted, purpose
):
    original = packet(accepted, purpose)
    client = JournalClient(
        "http://synthetic.invalid",
        native.tokens["agent"],
        transport=httpx.ASGITransport(app=native.app),
    )
    method = getattr(client, "standing_export_admission", None)
    assert callable(method), "Standing export transport capability is absent"
    changes = native.store.db.total_changes
    try:
        receipt = await method(original)
        receipt.validate_request(original)
        assert receipt.admission.effective_scope == original.export.content_scope
        assert receipt.admission.journal_sequence == accepted.sequence
        assert not client._submit_exchanges
        assert native.store.db.total_changes == changes
        refused = await client.http.post(
            "/v1/transcript/agent-export-admission",
            content=original.export.model_dump_json().encode(),
            headers={"Content-Type": "application/json"},
        )
        assert refused.status_code == 409
    finally:
        await client.close()


@pytest.mark.parametrize("case", ["at_limit", "over_limit", "duplicate", "query", "media"])
async def test_export_route_owns_its_exact_raw_bound_and_input_guards(native, accepted, case):
    original = packet(accepted)
    raw = original.canonical_bytes()
    suffix = ""
    custom = headers(native)
    if case in {"at_limit", "over_limit"}:
        raw += b" " * (262144 - len(raw) + (case == "over_limit"))
    elif case == "duplicate":
        raw = b'{"schema_version":"1.0",' + raw[1:]
    elif case == "query":
        suffix = "?id=1"
    else:
        custom["Content-Type"] = "text/plain"
    changes = native.store.db.total_changes
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=native.app), base_url="http://synthetic.invalid"
    ) as client:
        result = await client.post(PATH + suffix, content=raw, headers=custom)
    checked(result, 200 if case == "at_limit" else 415 if case == "media" else 422)
    if case == "at_limit":
        types().StandingExportAdmission.from_json(result.content).validate_request(original)
    assert native.store.db.total_changes == changes


@pytest.mark.parametrize("case", ["null", "old_type", "wrong_original"])
async def test_export_route_refuses_unbound_native_results(native, accepted, monkeypatch, case):
    original = packet(accepted)
    good = reply_for(original)
    if case == "null":
        result = None
    elif case == "old_type":
        result = good.admission
    else:
        raw = original.model_dump(mode="json")
        raw["export"]["request_nonce"] = "synthetic-other-request"
        result = reply_for(types().StandingExportRequest.model_validate(raw))
    owner = native.app.state.standing_task_owner
    assert callable(getattr(owner, "export_admission", None)), "Native export capability absent"
    monkeypatch.setattr(owner, "export_admission", lambda *args, **kwargs: result)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=native.app), base_url="http://synthetic.invalid"
    ) as client:
        response = await client.post(
            PATH, content=original.canonical_bytes(), headers=headers(native)
        )
    checked(response, 503)


async def test_export_native_operation_uses_owned_thread_and_same_absolute_guard(
    native, accepted, monkeypatch
):
    original = packet(accepted)
    expected = reply_for(original)
    observed = {}
    main_thread = threading.get_ident()

    def invoke(request, *, deadline, cancelled):
        observed.update(
            request=request,
            thread=threading.get_ident(),
            remaining=deadline - time.monotonic(),
            cancelled=cancelled(),
        )
        return expected

    monkeypatch.setattr(
        native.app.state.standing_task_owner, "export_admission", invoke, raising=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=native.app), base_url="http://synthetic.invalid"
    ) as client:
        result = await client.post(
            PATH, content=original.canonical_bytes(), headers=headers(native)
        )
    checked(result, 200)
    assert observed["request"] == original and observed["thread"] != main_thread
    assert 0 < observed["remaining"] <= 25 and observed["cancelled"] is False
    assert native.app.state.standing_owner_operations.active_operations == 0


@pytest.mark.parametrize(
    "case", ["canonical", "padded_limit", "oversized", "null", "wrong_original"]
)
async def test_export_client_exact_wire_and_bounded_original_reply(accepted, case):
    original = packet(accepted)
    response = reply_for(original)
    if case == "wrong_original":
        raw = original.model_dump(mode="json")
        raw["export"]["request_nonce"] = "synthetic-other-request"
        response = reply_for(types().StandingExportRequest.model_validate(raw))
    raw = response.canonical_bytes()
    if case in {"padded_limit", "oversized"}:
        raw += b" " * (73728 - len(raw) + (case == "oversized"))
    elif case == "null":
        raw = b"null"
    body = Body(raw)
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=body, headers={"Content-Type": "application/json"})

    client = JournalClient(
        "http://synthetic.invalid", "synthetic-agent", transport=httpx.MockTransport(handle)
    )
    method = getattr(client, "standing_export_admission", None)
    assert callable(method), "Standing export transport capability is absent"
    try:
        if case in {"canonical", "padded_limit"}:
            assert (await method(original)).validate_request(original) == response
        else:
            with pytest.raises(StandingUnavailable):
                await method(original)
        assert len(requests) == 1
        request = requests[0]
        assert (request.method, request.url.path, request.url.query) == ("POST", PATH, b"")
        assert request.content == original.canonical_bytes()
        assert request.headers["authorization"] == "Bearer synthetic-agent"
        assert request.headers["accept-encoding"] == "identity"
        assert "x-kinaiya-standing-capability" not in request.headers
        assert body.close_calls == 1 and body.close_completed.is_set()
        assert not client._submit_exchanges
    finally:
        await client.close()
