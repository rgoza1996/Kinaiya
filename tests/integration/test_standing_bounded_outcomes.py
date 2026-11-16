"""Issue #633: real standing admission, file receipts and terminal settlement.

Only inference and named terminal transport faults are synthetic. Native
Session, one-use admission, export checks, captured model and file tools run.
The fixed file path/read count and assertions precede the implementation.
"""

import asyncio
import copy
import hashlib
import importlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from test_standing_agent_runtime import REFUSALS, SECRET, MeasuredFiles, submit
from test_standing_agent_runtime import factory as factory
from test_standing_agent_runtime import stack as stack

from core.policy import PolicyDenied, ToolPolicy
from packages.contracts.agent_outcome import (
    MAX_OUTCOME_BYTES,
    OutcomeEvidenceUnavailable,
    read_outcome_task,
)
from packages.contracts.agent_submit import AgentSubmitUnavailable
from packages.contracts.client import JournalClient
from packages.contracts.models import InterruptRequest, TaskState
from packages.providers.text import ScriptedProvider
from services.agent.runtime import AgentRuntime
from services.converse.runtime import ConverseRuntime

READS = 40
RELATIVE = "/".join(["public-" + "x" * 190] * 3 + ["receipt.txt"])
PUBLIC_BYTES = b"Public synthetic standing outcome input.\n"


def script(monkeypatch, state, *, reads=READS):
    original_init, original_complete = MeasuredFiles.__init__, MeasuredFiles.complete

    def initialize(self):
        original_init(self)
        self.responses.clear()
        self.responses.extend(
            [json.dumps({"tool": "read_file", "arguments": {"path": RELATIVE}})] * reads
        )
        self.responses.append(
            RuntimeError("Public synthetic standing failure")
            if state == "failed"
            else json.dumps(
                {
                    "tool": "finish",
                    "arguments": {"summary": "Inspected public file.", "objective_revision": 1},
                }
            )
        )
        self.final_entered, self.final_release = asyncio.Event(), asyncio.Event()
        self.model_progress, self.model_entered = asyncio.Event(), 0

    async def complete(self, messages):
        self.model_entered += 1
        self.model_progress.set()
        if state == "cancelled" and len(self.calls) == reads:
            self.final_entered.set()
            await self.final_release.wait()
        return await original_complete(self, messages)

    monkeypatch.setattr(MeasuredFiles, "__init__", initialize)
    monkeypatch.setattr(MeasuredFiles, "complete", complete)


async def wait_for_cancel_phase(value, reads, *, stall_timeout=15):
    """Observe bounded original progress, never a replacement or model result."""
    provider, worker = value.made[0], value.record.worker
    observed = 0
    while not provider.final_entered.is_set():
        provider.model_progress.clear()
        entered = provider.model_entered
        assert observed <= entered <= reads + 1, "Unexpected original provider phase count"
        observed = entered
        if worker.done():
            raise AssertionError(
                "Original worker ended before intended cancellation phase: "
                f"provider_entries={entered}, worker_cancelled={worker.cancelled()}"
            )
        observer = asyncio.create_task(provider.model_progress.wait())
        try:
            done, _ = await asyncio.wait(
                {observer, worker}, timeout=stall_timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done and provider.model_entered == observed:
                raise TimeoutError(
                    f"Original worker made no provider-phase progress: entries={observed}"
                )
        finally:
            # Retire only this observer. Timeout/cancellation cannot cancel or
            # replace the original worker or establish its physical cleanup.
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)


async def execute(stack, monkeypatch, state, mode, *, reads=READS):
    script(monkeypatch, state, reads=reads)
    value = await stack()
    # A predeclared test budget, with the original production defaults intact.
    value.runtime.max_steps = reads + 2
    path = value.workspace / RELATIVE
    path.parent.mkdir(parents=True)
    path.write_bytes(PUBLIC_BYTES)
    attempts, actual_append = [], value.journal.append

    async def append(body):
        if body.kind != "agent.outcome":
            return await actual_append(body)
        attempts.append(body.model_copy(deep=True))
        if mode == "absent":
            raise OSError("Public synthetic standing outcome unavailable")
        entry = await actual_append(body)
        if mode == "lost-ack":
            raise OSError("Public synthetic standing outcome ACK loss")
        return entry

    monkeypatch.setattr(value.journal, "append", append)
    admission = await submit(value)
    assert admission.disposition == "created"
    if state == "cancelled":
        await wait_for_cancel_phase(value, reads)
        await value.runtime.interrupt(
            value.task_id,
            InterruptRequest(expected_objective_revision=1, idempotency_key="public-stop"),
        )
    final = await value.runtime.wait(value.task_id, timeout=20)
    await asyncio.wait_for(asyncio.shield(value.record.worker), 5)
    assert path.read_bytes() == PUBLIC_BYTES
    assert not (value.workspace / "result.txt").exists()
    assert value.base.calls == []
    assert len(value.made) == 1
    assert len(value.made[0].calls) == reads + (state != "cancelled")
    assert value.made[0].closed and value.made[0].close_calls == 1
    assert (await value.journal.standing_task_lookup(value.original)) == admission.proof
    return value, admission, final, attempts


async def restart(value, wrap=None):
    provider = ScriptedProvider([])
    journal = JournalClient(
        "http://standing.test", "a" * 32, transport=httpx.ASGITransport(app=value.env.app)
    )
    runtime = AgentRuntime(
        journal if wrap is None else wrap(journal),
        provider,
        value.workspace,
        ToolPolicy(value.workspace),
    )
    try:
        recovered = await runtime.recover()
        assert provider.calls == []
        assert runtime._submit_idem == {}
        assert all(record.worker is None for record in runtime._tasks.values())
        assert all(record.standing_owner is None for record in runtime._tasks.values())
        assert all(record.native_task_identity is None for record in runtime._tasks.values())
        # A new process can inspect the retained admission, never consume a
        # launch signal or allocate another provider from that knowledge.
        retained = await runtime.submit_standing(value.original, capability=SECRET)
        assert retained.disposition == "retained"
        assert provider.calls == []
        assert all(
            owner.fenced
            and owner.worker is None
            and owner.lease is None
            and owner.attempt.capability is None
            for owner in runtime._standing_claims
        )
        return recovered
    finally:
        await asyncio.wait_for(runtime.close(), 3)
        await asyncio.wait_for(journal.close(), 3)


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
@pytest.mark.parametrize("mode", ["confirmed", "lost-ack", "absent"])
async def test_standing_terminal_receipts_are_bounded_exact_and_read_only_after_restart(
    stack, monkeypatch, state, mode
):
    value, admission, final, attempts = await execute(stack, monkeypatch, state, mode)
    expected = TaskState.PAUSED if mode == "absent" else TaskState(state)
    assert final.state == expected, final.error
    assert len(final.evidence) == READS
    tools = value.env.store.read(task_id=value.task_id, kinds=["agent.tool"])
    original = [row.payload["evidence"] for row in tools if "evidence" in row.payload]
    assert [e.model_dump(mode="json") for e in final.evidence] == original
    assert all(e["kind"] == "read" and e["output"] == "" for e in original)
    assert len(attempts) == 1
    body = attempts[0]
    assert len(body.model_dump_json().encode()) <= MAX_OUTCOME_BYTES
    assert body.payload["evidence"] == body.payload["task"]["evidence"] == []
    reference = body.payload["evidence_ref"]
    assert reference["contract"] == "agent.evidence-prefix.v2"
    assert reference["evidence_count"] == READS
    initial = admission.proof.journal_entry()
    digest = hashlib.sha256(
        json.dumps(
            initial.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    assert reference["admission"] == {
        "kind": "agent.standing-command",
        "origin": "standing-supervisor",
        "event_id": initial.event_id,
        "sha256": digest,
    }
    full = body.model_copy(deep=True)
    full.payload["task"]["evidence"] = copy.deepcopy(original)
    full.payload["evidence"] = copy.deepcopy(original)
    del full.payload["evidence_ref"]
    assert len(full.model_dump_json().encode()) > MAX_OUTCOME_BYTES
    outcomes = value.env.store.read(task_id=value.task_id, kinds=["agent.outcome"])
    assert len(outcomes) == (mode != "absent")
    recovered = await restart(value)
    if mode == "absent":
        assert recovered == []  # Incomplete standing admission grants no work/status authority.
        assert final.summary == "" and "unconfirmed" in final.error
        assert value.record.completion_snapshot.state == TaskState(state)
        assert value.record.completion_snapshot.evidence == final.evidence
        # The already-retired standing owner rejects before ordinary terminal
        # controls. Preserve that stricter native lifetime fence.
        with pytest.raises(PolicyDenied, match="permanently fenced"):
            await value.runtime.interrupt(
                value.task_id,
                InterruptRequest(expected_objective_revision=1, idempotency_key="second-stop"),
            )
        assert value.record.standing_owner.fenced
    else:
        assert len(recovered) == 1
        assert recovered[0] == final
        assert await read_outcome_task(value.journal, outcomes[0]) == final
        assert value.record.completion_snapshot is None
    # Retained admission remains knowledge, including after a failed final POST.
    calls = len(value.made[0].calls)
    assert (await submit(value)).disposition == "retained"
    assert len(value.made) == 1 and len(value.made[0].calls) == calls
    assert len(attempts) == 1


async def test_small_standing_outcome_keeps_inline_shape_and_recovers_without_launch(
    stack, monkeypatch
):
    value, _, final, attempts = await execute(stack, monkeypatch, "succeeded", "confirmed", reads=1)
    assert final.state == TaskState.SUCCEEDED
    assert len(attempts) == 1 and "evidence_ref" not in attempts[0].payload
    assert attempts[0].payload["task"]["evidence"] == attempts[0].payload["evidence"]
    assert await restart(value) == [final]


async def test_standing_native_observers_refuse_missing_changed_or_cross_family_proof(
    stack, monkeypatch
):
    value, admission, final, _ = await execute(stack, monkeypatch, "succeeded", "confirmed")
    outcome = value.env.store.read(task_id=value.task_id, kinds=["agent.outcome"])[0]
    receipts = value.env.store.read(task_id=value.task_id, kinds=["agent.tool"])
    assert await read_outcome_task(value.journal, outcome) == final
    converse_journal = JournalClient(
        "http://standing.test", "c" * 32, transport=httpx.ASGITransport(app=value.env.app)
    )
    converse = ConverseRuntime(converse_journal, ScriptedProvider([]))
    try:
        notice = await converse.outcome_notice(outcome)
        # Standing work is bound to an Agent channel. Hydration succeeds, but
        # the existing native channel boundary correctly prevents speech.
        assert notice is None and converse.provider.calls == []
        assert not value.env.store.read(kinds=["conversation.converse"])
    finally:
        await converse.close()
        await converse_journal.close()

    class Damaged:
        def __init__(self, damage, delegate=None):
            self.damage = damage
            self.delegate = value.journal if delegate is None else delegate

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        async def read(self, **query):
            rows = [row.model_copy(deep=True) for row in await self.delegate.read(**query)]
            changed = []
            for row in rows:
                if row.event_id == admission.proof.event_id:
                    if self.damage == "initial-missing":
                        continue
                    if self.damage == "initial-authorization":
                        row.payload["standing"]["authorization_digest"] = "f" * 64
                    elif self.damage == "initial-family":
                        row.kind, row.origin = "agent.command", "agent-supervisor"
                elif row.event_id == receipts[5].event_id:
                    if self.damage == "receipt-missing":
                        continue
                    if self.damage == "receipt-changed":
                        row.payload["evidence"]["sha256"] = "f" * 64
                    elif self.damage == "receipt-provenance":
                        row.event_id = "foreign-native-event"
                changed.append(row)
            return changed

    for damage in (
        "initial-missing",
        "initial-authorization",
        "initial-family",
        "receipt-missing",
        "receipt-changed",
        "receipt-provenance",
    ):
        journal = Damaged(damage)
        with pytest.raises(OutcomeEvidenceUnavailable):
            await read_outcome_task(journal, outcome)
        if damage in {"initial-missing", "initial-family"}:
            assert await restart(value, lambda reader, damage=damage: Damaged(damage, reader)) == []
        else:
            with pytest.raises(AgentSubmitUnavailable):
                await restart(value, lambda reader, damage=damage: Damaged(damage, reader))
        observer_client = JournalClient(
            "http://standing.test", "c" * 32, transport=httpx.ASGITransport(app=value.env.app)
        )
        observer = ConverseRuntime(Damaged(damage, observer_client), ScriptedProvider([]))
        try:
            with pytest.raises(OutcomeEvidenceUnavailable):
                await observer.outcome_notice(outcome)
            assert observer.provider.calls == []
            assert not value.env.store.read(kinds=["conversation.converse"])
        finally:
            await observer.close()
            await observer_client.close()
    assert len(value.made) == 1 and value.base.calls == []


async def test_standing_prefix_validation_consumes_original_launch_deadline(stack, monkeypatch):
    value = await stack()
    module = importlib.import_module("services.agent.standing_runtime")
    native_clock, actual_index = time.monotonic, module.OutcomeEvidenceIndex
    observation = SimpleNamespace(offset=0.0, checked=False)
    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=lambda: native_clock() + observation.offset)
    )

    def delayed_index(*args, **kwargs):
        result = actual_index(*args, **kwargs)
        assert result.is_standing
        observation.checked = True
        observation.offset = 26.0
        return result

    monkeypatch.setattr(module, "OutcomeEvidenceIndex", delayed_index)
    with pytest.raises(REFUSALS):
        await submit(value)
    assert observation.checked and time.monotonic is native_clock
    proof = await value.journal.standing_task_lookup(value.original)
    assert proof.journal_entry() == value.env.store.entry(proof.event_id)
    assert value.base.calls == [] and all(provider.calls == [] for provider in value.made)
    assert all(provider.closed for provider in value.made)
    assert not value.runtime._tasks and not value.runtime._submit_idem
    assert not (value.workspace / "result.txt").exists()
