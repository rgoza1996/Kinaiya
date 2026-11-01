"""Independent actual standing-worker oracles frozen before implementation.

Native Session/SQLite, JournalClient, Agent loop, policy and files are real.
Only inference is synthetic; transport gates/faults surround actual native
responses. This establishes no scheduler, claim release or settlement.
"""

import asyncio
import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from test_agent_mode_permissions import bind_workspace
from test_standing_execution_api import SECRET, checked, headers
from test_standing_execution_api import factory as factory
from test_standing_execution_api import original as original_packet
from test_standing_export_admission import old_export

from core.policy import PolicyDenied, ToolPolicy
from packages.contracts.agent_submit import AgentSubmitUnavailable, AgentSubmitUnconfirmed
from packages.contracts.agent_tools import (
    capture_agent_export_admission,
    current_agent_tools,
)
from packages.contracts.client import JournalClient
from packages.contracts.models import Task, TaskRequest, TaskState
from packages.contracts.standing_admission import StandingTaskAdmission
from packages.contracts.standing_execution import StandingExecutionIntent
from packages.contracts.standing_export import StandingExportRequest
from packages.contracts.standing_work import StandingChange, StandingChangeReceipt
from packages.providers.completion_budget import PreparedComplete
from packages.providers.prompt_budget import PromptTokenCount, request_digest
from packages.providers.text import ProviderCleanupError, ScriptedProvider
from services.agent.model_settings import AgentModelSettings, BoundModelSettingsChange
from services.agent.runtime import AgentRuntime
from services.agent.task_model_lease import TaskModelLease

ADMIT = "/v1/standing/tasks/admit"
LOOKUP = "/v1/standing/tasks/lookup"
EXPORT = "/v1/standing/tasks/export-admission"
ORDINARY_EXPORT = "/v1/transcript/agent-export-admission"
TEXT = "Public synthetic standing file result.\n"
SOURCE = "Public synthetic captured input.\n"
FILES = ("read_file", "write_file", "finish")
REFUSALS = (PolicyDenied, AgentSubmitUnavailable, AgentSubmitUnconfirmed)


def full_model_pin(pin):
    # Old pins intentionally omit unset additive nulls from serialized history;
    # this new complete declaration compares their actual field values.
    return {
        **pin.model_dump(mode="json"),
        "catalog_binding": None
        if pin.catalog_binding is None
        else pin.catalog_binding.model_dump(mode="json"),
        "catalog_revision": pin.catalog_revision,
    }


class MeasuredFiles(ScriptedProvider):
    """A declared byte-token synthetic model with actual export callbacks."""

    def __init__(self):
        super().__init__(
            [
                json.dumps({"tool": "read_file", "arguments": {"path": "input.txt"}}),
                json.dumps(
                    {"tool": "write_file", "arguments": {"path": "result.txt", "content": TEXT}}
                ),
                json.dumps(
                    {
                        "tool": "finish",
                        "arguments": {"summary": "Verified.", "objective_revision": 1},
                    }
                ),
            ]
        )
        self.counts, self.generations, self.snapshots = [], [], []
        self.generating, self.closing = asyncio.Event(), asyncio.Event()
        self.generate_gate, self.close_gate = None, None
        self.prepare_error = self.close_error = None
        self.closed = False
        self.close_calls = 0

    async def prepare_complete(self, messages):
        snapshot, admission = current_agent_tools(), capture_agent_export_admission()
        assert snapshot is not None and callable(admission)
        body = {"messages": copy.deepcopy(messages), "tools": list(snapshot.available_tools)}
        stamp = request_digest(body)
        await admission("complete_count", stamp)
        self.counts.append(stamp)
        self.snapshots.append(snapshot)
        if self.prepare_error is not None:
            raise self.prepare_error
        measurement = PromptTokenCount(
            input_tokens=len(json.dumps(body, ensure_ascii=False).encode()),
            context_tokens=1_000_000,
            reserved_output_tokens=1000,
            request_sha256=stamp,
            binding_sha256=hashlib.sha256(b"synthetic-byte-token-v1").hexdigest(),
        )

        async def generate():
            assert current_agent_tools() is snapshot
            self.generating.set()
            if self.generate_gate is not None:
                await self.generate_gate.wait()
            await admission("complete_generation", stamp)
            self.generations.append(stamp)
            return await self.complete(body["messages"])

        return PreparedComplete(measurement, generate)

    async def close(self):
        self.close_calls += 1
        self.closing.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.close_error is not None:
            error, self.close_error = self.close_error, None
            raise error
        self.closed = True


class NativeBoundary(httpx.AsyncBaseTransport):
    """Change only explicit observation boundaries, after real native work."""

    def __init__(self, app):
        self.delegate = httpx.ASGITransport(app=app)
        self.requests = []
        self.mode = None
        self.gated_path = None
        self.entered, self.release, self.settled = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.fired = False
        self.after_ack = None
        self.before_admit = None

    async def handle_async_request(self, request):
        body = await request.aread()
        path = request.url.path
        self.requests.append((path, body))
        if path == ADMIT and self.mode == "before_commit":
            self.fired = True
            raise httpx.ConnectError("Synthetic precommit boundary", request=request)
        if path == ADMIT and self.before_admit is not None:
            callback, self.before_admit = self.before_admit, None
            await callback()
        response = await self.delegate.handle_async_request(request)
        if path == self.gated_path:
            self.fired = True
            self.entered.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    pass  # Deliberately resistant actual acquired response.
            self.settled.set()
        if path == ADMIT and self.after_ack is not None:
            callback, self.after_ack = self.after_ack, None
            callback()
        if path in {ADMIT, EXPORT} and self.mode is not None:
            target = EXPORT if self.mode.startswith("export_") else ADMIT
            if path == target:
                assert response.status_code == 200
                self.fired = True
                raw = json.loads(await response.aread())
                await response.aclose()
                if self.mode == "lost_ack":
                    raise httpx.ReadError("Synthetic committed ACK loss", request=request)
                if self.mode == "malformed_ack":
                    raw["proof"]["entry_digest"] = "f" * 64
                elif self.mode == "retained_ack":
                    raw["disposition"] = "retained"
                elif self.mode == "export_null":
                    raw = None
                elif self.mode == "export_wrong_original":
                    raw["request_digest"] = "f" * 64
                return httpx.Response(200, json=raw, request=request)
        return response

    def paths(self, path):
        return [raw for actual, raw in self.requests if actual == path]

    async def aclose(self):
        await self.delegate.aclose()


@dataclass
class Stack:
    env: object
    runtime: AgentRuntime
    journal: JournalClient
    transport: NativeBoundary
    base: MeasuredFiles
    original: StandingExecutionIntent
    claim: object
    workspace: object
    workspace_owner: object
    made: list = field(default_factory=list)
    runtime_closed: bool = False

    @property
    def task_id(self):
        return self.original.task.task_id

    @property
    def record(self):
        return self.runtime._tasks[self.task_id]


@pytest.fixture
async def stack(factory, tmp_path):
    opened = []

    async def make():
        env = await factory()
        workspace = tmp_path / f"worker-{len(opened)}"
        workspace.mkdir(mode=0o700)
        (workspace / "input.txt").write_text(SOURCE)
        transport = NativeBoundary(env.app)
        journal = JournalClient("http://standing.test", "a" * 32, transport=transport)
        base = MeasuredFiles()
        runtime = AgentRuntime(journal, base, workspace, ToolPolicy(workspace, allow_write=True))
        owner = bind_workspace(runtime, tmp_path / f"workspace-owner-{len(opened)}")
        context = owner.active_context()
        made = []

        def fresh(effort):
            assert effort is None
            value = MeasuredFiles()
            made.append(value)
            return value

        runtime.model_settings = AgentModelSettings(
            base, {}, host_factory=fresh, path=tmp_path / f"models-{len(opened)}.sqlite"
        )
        value = Stack(env, runtime, journal, transport, base, None, None, workspace, owner, made)
        opened.append(value)
        packet = await original_packet(env)
        record = env.app.state.standing_owner.snapshot(packet.responsibility_id).record
        raw = record.receipt.request.model_dump(mode="json")
        raw.update(
            expected_revision=record.receipt.revision, idempotency_key="worker-real-workspace"
        )
        raw["authorization"]["workspace_pin"] = context.pin.model_dump(mode="json")
        grant = StandingChange.model_validate(raw)
        env.wall.value = datetime(2026, 11, 1, 11, 59, 55, tzinfo=UTC)
        reply = await env.client.post(
            "/v1/standing/changes",
            content=grant.canonical_bytes(),
            headers=headers(**{"Content-Type": "application/json"}),
        )
        checked(reply, 200, StandingChangeReceipt).validate_request(grant)
        record = env.app.state.standing_owner.snapshot(packet.responsibility_id).record
        env.wall.value = datetime(2026, 11, 1, 12, 0, 10, tzinfo=UTC)
        _, pin = runtime.model_settings.selection_info("seed-v1")
        raw = packet.model_dump(mode="json")
        raw.update(
            authorization_revision=record.receipt.revision,
            authorization_digest=record.receipt.authorization_digest,
            record_digest=record.record_digest,
        )
        raw["task"]["workspace_pin"] = context.pin.model_dump(mode="json")
        raw["task"]["model_pin"] = full_model_pin(pin)
        packet = StandingExecutionIntent.model_validate(raw)
        claim = (await journal.standing_prepare(packet)).validate_request(packet).claim
        value.original, value.claim = packet, claim
        return value

    try:
        yield make
    finally:
        for value in reversed(opened):
            value.transport.release.set()
            for provider in [value.base, *value.made]:
                if provider.generate_gate is not None:
                    provider.generate_gate.set()
                if provider.close_gate is not None:
                    provider.close_gate.set()
                provider.close_error = None
            if not value.runtime_closed:
                if value.runtime.workspace_settings is None:
                    value.runtime.workspace_settings = value.workspace_owner
                await asyncio.wait_for(value.runtime.close(), 8)
            await asyncio.wait_for(value.journal.close(), 5)


def submit(value, original=None, *, capability=SECRET):
    method = getattr(value.runtime, "submit_standing", None)
    assert callable(method), "Frozen actual standing Agent worker capability is absent"
    return method(value.original if original is None else original, capability=capability)


def entries(value):
    return value.env.store.read(kinds=["agent.standing-command"])


async def finish(value):
    task = await asyncio.wait_for(value.runtime.wait(value.task_id, timeout=5), 6)
    worker = value.record.worker
    assert worker is not None
    await asyncio.wait_for(asyncio.shield(worker), 5)
    return task


async def assert_no_work(value):
    assert value.base.calls == [] and all(provider.calls == [] for provider in value.made)
    assert not (value.workspace / "result.txt").exists()
    assert not any(rec.worker is not None for rec in value.runtime._tasks.values())
    assert value.runtime._submit_idem == {}
    assert await value.journal.standing_unresolved() == value.claim


async def unused_captures_closed(value):
    async with asyncio.timeout(3):
        while any(not provider.closed for provider in value.made):
            await asyncio.sleep(0)


async def test_existing_fixture_real_claim_workspace_model_and_measured_old_loop(stack):
    value = await stack()
    assert value.claim.request == value.original
    assert value.claim == await value.journal.standing_claim_lookup(value.original)
    assert not entries(value) and value.made == []
    assert (
        value.original.task.workspace_pin == value.runtime.workspace_settings.active_context().pin
    )
    assert value.original.task.model_pin.model_dump(mode="json") == (
        full_model_pin(value.runtime.model_settings.selection_info("seed-v1")[1])
    )
    task = await value.runtime.submit(
        TaskRequest(
            **value.original.task.task_session.model_dump(mode="json"),
            objective="Read input.txt and write result.txt, then finish.",
            idempotency_key="old-measured-control",
            content_scope="local_only",
        )
    )
    final = await value.runtime.wait(task.task_id, timeout=5)
    await value.runtime._tasks[task.task_id].worker
    assert (
        final.state == TaskState.SUCCEEDED and (value.workspace / "result.txt").read_text() == TEXT
    )
    assert len(value.base.calls) == len(value.base.generations) == 3
    assert len(value.base.counts) >= 3 and value.transport.paths(EXPORT) == []
    assert not entries(value) and await value.journal.standing_unresolved() == value.claim


async def test_existing_native_fixture_can_admit_and_export_the_complete_actual_initial(stack):
    value = await stack()
    result = await value.journal.standing_admit(value.original, capability=SECRET)
    assert result.validate_request(value.original).disposition == "created"
    assert result.proof.journal_entry() == value.env.store.entry(result.proof.event_id)
    request = StandingExportRequest(
        schema_version="1.0", initial=result.proof, export=old_export(result.proof)
    )
    response = await value.journal.standing_export_admission(request)
    assert response.validate_request(request).admission.provider.model_dump(mode="json") == (
        value.base.info.model_dump(mode="json")
    )
    assert len(entries(value)) == 1 and value.made == [] and value.base.calls == []
    assert await value.journal.standing_unresolved() == value.claim


async def test_created_ack_runs_actual_file_loop_with_exact_native_identity_and_no_release(stack):
    value = await stack()
    result = await submit(value)
    assert type(result) is StandingTaskAdmission
    assert result.validate_request(value.original).disposition == "created"
    assert type(value.record.model_lease) is TaskModelLease
    assert any(adapter is value.record.provider for adapter in value.record.model_lease.adapters)
    task = await finish(value)
    assert task.state == TaskState.SUCCEEDED, task.error
    assert task.task_id == value.task_id and task.workspace_pin == value.original.task.workspace_pin
    assert full_model_pin(task.model_pin) == value.original.task.model_pin.model_dump(mode="json")
    assert (
        task.objective_revision == 1 and task.source_event_ids == [] and task.auxiliary_models == {}
    )
    assert (value.workspace / "result.txt").read_text() == TEXT
    assert any(item.sha256 == hashlib.sha256(TEXT.encode()).hexdigest() for item in task.evidence)
    (provider,) = value.made
    assert value.base.calls == [] and len(provider.calls) == len(provider.generations) == 3
    assert provider.closed and provider.close_calls == 1
    assert all(snapshot.available_tools == FILES for snapshot in provider.snapshots)
    assert all(snapshot.task_subset == "native-files-v1" for snapshot in provider.snapshots)
    for messages in provider.calls:
        assert SECRET.hex() not in json.dumps(messages) and repr(SECRET) not in json.dumps(messages)
        schema = next(
            line for line in messages[0]["content"].splitlines() if line.startswith("Schema:")
        )
        assert all(json.dumps(name) in schema for name in FILES)
        assert all(
            json.dumps(name) not in schema
            for name in ("shell", "search", "mcp_call", "consult_model")
        )
    record = value.record
    assert record.standing_initial == result.proof and record.native_task_identity is None
    assert record.submit_sequence == result.proof.sequence and record.submit_confirmed
    assert type(record.model_lease) is TaskModelLease and record.model_lease.settled
    assert not record.standing_cleanup_uncertain and record.workspace_context is (
        value.runtime.workspace_settings.active_context()
    )
    assert len(entries(value)) == len(value.transport.paths(ADMIT)) == 1
    assert value.transport.paths(ORDINARY_EXPORT) == [] and value.runtime._submit_idem == {}
    exports = [StandingExportRequest.from_json(raw) for raw in value.transport.paths(EXPORT)]
    assert {item.export.purpose for item in exports} == {"complete_count", "complete_generation"}
    assert all(item.initial == result.proof for item in exports)
    assert (
        value.env.store.read(kinds=["agent.outcome"], task_id=value.task_id)[0].payload["task"][
            "state"
        ]
        == "succeeded"
    )
    assert await value.journal.standing_unresolved() == value.claim


async def test_retained_lookup_before_capture_is_readonly_and_never_recovers_a_worker(
    stack, monkeypatch
):
    value = await stack()
    accepted = await value.journal.standing_admit(value.original, capability=SECRET)
    before = entries(value)
    with monkeypatch.context() as patch:
        patch.setattr(
            value.runtime.model_settings,
            "task_selection",
            lambda *a, **k: pytest.fail("retained selection"),
        )
        patch.setattr(
            value.runtime.workspace_settings,
            "active_context",
            lambda: pytest.fail("retained workspace selection"),
        )
        result = await submit(value)
    assert result.disposition == "retained" and result.proof == accepted.proof
    assert value.made == [] and entries(value) == before
    assert value.runtime._tasks == {}
    await assert_no_work(value)


async def test_duplicate_during_original_created_ack_does_not_suppress_its_launch(stack):
    value = await stack()
    value.transport.gated_path = ADMIT
    original = asyncio.create_task(submit(value))
    await asyncio.wait_for(value.transport.entered.wait(), 5)
    duplicate = await submit(value)
    assert duplicate.disposition == "retained"
    assert not original.done() and len(value.made) == 1 and not value.made[0].closed
    value.transport.release.set()
    first = await original
    assert first.disposition == "created" and first.proof == duplicate.proof
    assert (await finish(value)).state == TaskState.SUCCEEDED
    record, worker = value.record, value.record.worker
    assert (await submit(value)).disposition == "retained"
    assert value.record is record and value.record.worker is worker
    assert len(value.made) == 1 and len(value.made[0].calls) == 3


@pytest.mark.parametrize("fault", ["before_commit", "lost_ack", "malformed_ack", "retained_ack"])
async def test_real_native_reply_uncertainty_or_retention_never_launches(stack, fault):
    value = await stack()
    value.transport.mode = fault
    if fault == "retained_ack":
        assert (await submit(value)).disposition == "retained"
    else:
        with pytest.raises(REFUSALS):
            await submit(value)
    assert value.transport.fired
    assert len(entries(value)) == (0 if fault == "before_commit" else 1)
    await assert_no_work(value)
    value.transport.mode = None
    if fault != "before_commit":
        assert (await submit(value)).disposition == "retained"
        await assert_no_work(value)
    await unused_captures_closed(value)


@pytest.mark.parametrize(
    "problem", ["wrong_secret", "short_secret", "mutable_secret", "outer_extra", "nested_coercion"]
)
async def test_invalid_private_capability_or_actual_graph_refuses_before_capture(stack, problem):
    value = await stack()
    packet, secret = value.original, SECRET
    if problem == "wrong_secret":
        secret = b"x" * 32
    elif problem == "short_secret":
        secret = SECRET[:-1]
    elif problem == "mutable_secret":
        secret = bytearray(SECRET)
    elif problem == "outer_extra":
        packet = packet.model_copy(update={"untrusted_extra": "never-authority"})
    else:
        packet = packet.model_copy(
            update={
                "task": packet.task.model_copy(
                    update={"model_pin": packet.task.model_pin.model_copy(update={"local": 1})}
                )
            }
        )
    before = len(value.transport.requests)
    with pytest.raises(REFUSALS) as refused:
        await submit(value, packet, capability=secret)
    assert "never-authority" not in str(refused.value) and SECRET.hex() not in str(refused.value)
    assert len(value.transport.requests) == before and value.made == [] and not entries(value)
    await assert_no_work(value)


@pytest.mark.parametrize(
    "authority",
    ["model_revision", "workspace_missing", "workspace_displaced", "session_reset", "shared_alias"],
)
async def test_changed_exact_authority_refuses_before_native_admission(stack, authority):
    value = await stack()
    if authority == "model_revision":
        settings = value.runtime.model_settings
        observed = settings.snapshot()["write_authority"]
        settings.change(
            BoundModelSettingsChange(
                schema_version="1.1",
                expected_backend=observed["backend_id"],
                expected_instance=observed["instance_id"],
                expected_revision=0,
                idempotency_key="worker-changed-model-revision",
                profile_id="host-default",
            )
        )
    elif authority == "workspace_missing":
        value.runtime.workspace_settings = None
    elif authority == "workspace_displaced":
        value.workspace.rename(value.workspace.with_name(value.workspace.name + "-retired"))
        value.workspace.mkdir(mode=0o700)
    elif authority == "session_reset":
        value.env.store.reset(value.env.session.session_id)
    else:
        value.runtime.model_settings.host_factory = lambda effort: value.base
    with pytest.raises(REFUSALS):
        await submit(value)
    assert value.transport.paths(ADMIT) == [] and not entries(value)
    assert not value.base.closed and value.base.calls == []
    await assert_no_work(value)
    await unused_captures_closed(value)


@pytest.mark.parametrize("event", ["caller_cancel", "runtime_close"])
async def test_cancel_or_close_retains_actual_late_created_owner_and_never_launches(stack, event):
    value = await stack()
    value.transport.gated_path = ADMIT
    operation = asyncio.create_task(submit(value))
    await asyncio.wait_for(value.transport.entered.wait(), 5)
    assert len(entries(value)) == 1
    if event == "caller_cancel":
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 2)
    else:
        await asyncio.wait_for(value.runtime.close(), 8)
        value.runtime_closed = True
        with pytest.raises(REFUSALS):
            await operation
    assert value.runtime._submit_operations or value.journal._submit_exchanges
    assert value.journal._submit_exchanges
    assert not value.transport.settled.is_set()
    value.transport.release.set()
    await asyncio.wait_for(value.transport.settled.wait(), 5)
    async with asyncio.timeout(5):
        while value.runtime._submit_operations or value.journal._submit_exchanges:
            await asyncio.sleep(0)
    await assert_no_work(value)
    await unused_captures_closed(value)


async def test_absolute_real_deadline_leaves_resistant_created_reply_without_launch(stack):
    value = await stack()
    value.transport.gated_path = ADMIT
    operation = asyncio.create_task(submit(value))
    await asyncio.wait_for(value.transport.entered.wait(), 5)
    assert len(entries(value)) == 1
    with pytest.raises(REFUSALS):
        await asyncio.wait_for(operation, 27)
    assert not value.transport.release.is_set()
    assert value.runtime._submit_operations or value.journal._submit_exchanges
    value.transport.release.set()
    await asyncio.wait_for(value.transport.settled.wait(), 5)
    await assert_no_work(value)


@pytest.mark.parametrize("changed", ["workspace", "session"])
async def test_actual_native_commit_then_changed_prelaunch_authority_never_runs(stack, changed):
    value = await stack()

    def displace():
        if changed == "session":
            value.env.store.reset(value.env.session.session_id)
        else:
            value.workspace.rename(value.workspace.with_name(value.workspace.name + "-retired"))
            value.workspace.mkdir(mode=0o700)

    value.transport.after_ack = displace
    with pytest.raises(REFUSALS):
        await submit(value)
    assert len(entries(value)) == 1
    await assert_no_work(value)


@pytest.mark.parametrize("fault", ["export_null", "export_wrong_original"])
async def test_full_native_export_proof_failure_prevents_all_model_generation(stack, fault):
    value = await stack()
    value.transport.mode = fault
    assert (await submit(value)).disposition == "created"
    assert (await finish(value)).state == TaskState.FAILED
    assert value.transport.fired and value.transport.paths(EXPORT)
    assert value.transport.paths(ORDINARY_EXPORT) == []
    assert all(provider.calls == [] for provider in value.made)
    assert not (value.workspace / "result.txt").exists()


async def test_terminal_wait_precedes_actual_lease_close_without_claim_release(stack):
    value = await stack()
    close_gate = asyncio.Event()
    factory_fn = value.runtime.model_settings.host_factory

    def gated(effort):
        provider = factory_fn(effort)
        provider.close_gate = close_gate
        return provider

    value.runtime.model_settings.host_factory = gated
    assert (await submit(value)).disposition == "created"
    final = await value.runtime.wait(value.task_id, timeout=5)
    assert final.state == TaskState.SUCCEEDED
    await asyncio.wait_for(value.made[0].closing.wait(), 5)
    assert not value.made[0].closed and not value.record.worker.done()
    assert value.record.model_lease in value.runtime.model_settings.catalog_leases
    assert await value.journal.standing_unresolved() == value.claim
    close_gate.set()
    await asyncio.wait_for(asyncio.shield(value.record.worker), 5)
    assert value.made[0].closed and value.record.model_lease.settled
    assert await value.journal.standing_unresolved() == value.claim


@pytest.mark.parametrize("where", ["prepare", "close"])
async def test_provider_cleanup_error_is_sticky_even_after_later_successful_close(stack, where):
    value = await stack()
    factory_fn = value.runtime.model_settings.host_factory

    def failing(effort):
        provider = factory_fn(effort)
        if where == "prepare":
            provider.prepare_error = ProviderCleanupError("Synthetic cleanup unconfirmed")
        else:
            provider.close_error = ProviderCleanupError("Synthetic cleanup unconfirmed")
        return provider

    value.runtime.model_settings.host_factory = failing
    await submit(value)
    final = await finish(value)
    assert final.state == (TaskState.FAILED if where == "prepare" else TaskState.SUCCEEDED)
    assert value.record.standing_cleanup_uncertain is True
    value.made[0].prepare_error = None
    await value.made[0].close()
    assert value.made[0].closed and value.record.standing_cleanup_uncertain is True
    assert await value.journal.standing_unresolved() == value.claim


@pytest.mark.parametrize("boundary", ["dispatch", "operation"])
async def test_native_subset_cannot_be_bypassed_without_permissions_owner(
    stack, monkeypatch, boundary
):
    value = await stack()
    gate = asyncio.Event()
    factory_fn = value.runtime.model_settings.host_factory

    def held(effort):
        provider = factory_fn(effort)
        provider.generate_gate = gate
        return provider

    value.runtime.model_settings.host_factory = held
    await submit(value)
    await asyncio.wait_for(value.made[0].generating.wait(), 5)
    assert value.runtime.tool_permissions is None
    called = []

    def forbidden(*args, **kwargs):
        called.append(True)
        raise AssertionError("Forbidden native search reached its actual broker")

    monkeypatch.setattr("services.agent.runtime.run_search", forbidden)
    arguments = {"pattern": "synthetic", "path": "."}
    digest = value.runtime.policy.digest_for("search", arguments, 1)
    try:
        if boundary == "dispatch":
            await value.runtime._dispatch(
                value.record, "search", arguments, digest, "", approval_id=None
            )
        else:
            await value.runtime._run_scoped_builtin(
                value.record,
                "search",
                arguments,
                digest,
                1,
                None,
                reviewed=False,
                speech_review=False,
                audit={},
            )
    except PolicyDenied:
        pass
    assert called == [] and value.runtime.tool_snapshot(value.record.task).available_tools == FILES
    gate.set()
    assert (await finish(value)).state == TaskState.SUCCEEDED


async def test_restart_reads_retained_initial_without_replacement_models_or_worker(stack):
    value = await stack()
    accepted = await value.journal.standing_admit(value.original, capability=SECRET)
    replacement = AgentRuntime(value.journal, value.base, value.workspace, value.runtime.policy)
    try:
        assert callable(getattr(replacement, "submit_standing", None)), (
            "Standing launch adapter absent"
        )
        result = await replacement.submit_standing(value.original, capability=SECRET)
        assert result.disposition == "retained" and result.proof == accepted.proof
        assert replacement._tasks == {} and replacement.model_settings is None
        assert value.made == [] and value.base.calls == [] and len(entries(value)) == 1
    finally:
        await replacement.close()


async def test_caller_mutation_during_real_lookup_cannot_change_frozen_native_original(stack):
    value = await stack()
    original = StandingExecutionIntent.from_json(value.original.canonical_bytes())
    value.transport.gated_path = LOOKUP
    operation = asyncio.create_task(submit(value))
    await asyncio.wait_for(value.transport.entered.wait(), 5)
    object.__setattr__(
        value.original.task, "objective", "Untrusted mutation after this attempt began."
    )
    value.transport.release.set()
    result = await operation
    assert result.validate_request(original).disposition == "created"
    assert result.proof.claim.request.task.objective == original.task.objective
    assert value.record.task.objective == original.task.objective
    value.original = original
    assert (await finish(value)).state == TaskState.SUCCEEDED
    assert len(entries(value)) == 1


async def test_real_existing_task_id_is_never_overwritten_or_adopted(stack, monkeypatch):
    value = await stack()

    class FixedTask(Task):
        task_id: str = value.task_id

    monkeypatch.setattr("services.agent.runtime.Task", FixedTask)
    accepted = await value.runtime.submit(
        TaskRequest(
            **value.original.task.task_session.model_dump(mode="json"),
            objective="Complete the distinct ordinary file task.",
            idempotency_key="ordinary-task-id-collision",
            content_scope="local_only",
        )
    )
    assert accepted.task_id == value.task_id
    await value.runtime.wait(accepted.task_id, timeout=5)
    record = value.runtime._tasks[accepted.task_id]
    await record.worker
    worker, before_calls = record.worker, len(value.base.calls)
    with pytest.raises(REFUSALS):
        await submit(value)
    assert value.runtime._tasks[value.task_id] is record and record.worker is worker
    assert record.task.objective == "Complete the distinct ordinary file task."
    assert len(value.base.calls) == before_calls and value.made == []
    assert value.transport.paths(ADMIT) == [] and not entries(value)
    assert await value.journal.standing_unresolved() == value.claim


async def test_already_closed_runtime_cannot_lookup_capture_or_admit(stack):
    value = await stack()
    await value.runtime.close()
    value.runtime_closed = True
    before = len(value.transport.requests)
    with pytest.raises(REFUSALS):
        await submit(value)
    assert len(value.transport.requests) == before and value.made == [] and not entries(value)


async def test_native_ack_object_extra_field_cannot_bypass_runtime_full_graph_validation(
    stack, monkeypatch
):
    value = await stack()
    actual_admit = value.journal.standing_admit
    observed = []

    async def corrupt_reply(original, *, capability):
        result = await actual_admit(original, capability=capability)
        observed.append(result)
        return result.model_copy(update={"untrusted_ack_extra": "not-authority"})

    monkeypatch.setattr(value.journal, "standing_admit", corrupt_reply)
    with pytest.raises(REFUSALS):
        await submit(value)
    assert len(observed) == 1 and observed[0].disposition == "created"
    assert len(entries(value)) == 1
    await assert_no_work(value)
    await unused_captures_closed(value)
