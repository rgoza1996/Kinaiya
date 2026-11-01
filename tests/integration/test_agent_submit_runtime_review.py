"""Codex-authored regressions from the independent #199 runtime review.

Use real native SQLite receipts and existing no-inference runtime fixtures.
Only source-read scheduling is gated; stored history and receipt validation
remain real, including the Session reader's byte-limited pagination.
"""

import asyncio
from unittest.mock import Mock

import pytest
from test_agent_submit_admission import proposal
from test_agent_submit_native_revision import table_rows
from test_agent_submit_recovery_authority import receipt_traps
from test_agent_submit_uncertainty import (
    assert_no_worker,
    checkpoint,
    request,
    settled,
    start,
    submits,
)
from test_agent_submit_uncertainty import env as env

from packages.contracts.agent_submit import (
    AgentSubmitLookupRequest,
    AgentSubmitReceipt,
    AgentSubmitUnavailable,
    submit_entry,
)
from packages.contracts.models import AppendRequest, Task, TaskState


class ResistantFreshSource:
    """Finish the fresh resolve after its owner has cancelled or closed."""

    def __init__(self, journal):
        self.journal = journal
        self.original_resolve = journal.resolve
        self.original_entry = journal.entry
        self.original_read = journal.read
        self.resolves = 0
        self.cancellations = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.operation = None
        self.retired = False
        self.reads_after_retirement = []

    async def resolve(self, event_id):
        self.resolves += 1
        if self.journal.committed.is_set() and not self.entered.is_set():
            self.operation = asyncio.current_task()
            self.entered.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
        return await self.original_resolve(event_id)

    async def entry(self, event_id):
        if self.retired:
            self.reads_after_retirement.append("entry")
        return await self.original_entry(event_id)

    async def read(self, **kwargs):
        if self.retired:
            self.reads_after_retirement.append("read")
        return await self.original_read(**kwargs)


@pytest.mark.parametrize("retire_by", ["caller_cancel", "close"])
async def test_retired_fresh_source_cannot_start_further_journal_reads(env, monkeypatch, retire_by):
    source = ResistantFreshSource(env.journal)
    for name in ("resolve", "entry", "read"):
        monkeypatch.setattr(env.journal, name, getattr(source, name))
    # Catalog-era task_selection captures primary and auxiliary adapters in one
    # owned lease; metadata-only selection no longer constructs either adapter.
    selection = Mock(wraps=env.runtime.model_settings.task_selection)
    monkeypatch.setattr(env.runtime.model_settings, "task_selection", selection)
    operation = start(env, request(env))
    try:
        await asyncio.wait_for(source.entered.wait(), timeout=2)
        canonical = submit_entry(submits(env)[0])
        AgentSubmitReceipt(disposition="created", entry=canonical).validate_request(
            env.journal.attempts[0]
        )
        assert len(submits(env)) == len(env.journal.attempts) == 1
        assert source.operation in env.runtime._submit_operations
        before = table_rows(env.store.db)
        selected_counts = selection.call_count
        assert selected_counts > 0
        source.retired = True
        if retire_by == "close":
            await asyncio.wait_for(env.runtime.close(), timeout=2)
            assert env.runtime._closed
            result = await settled(operation)
            assert isinstance(result, AgentSubmitUnavailable)
        else:
            assert operation.cancel()
            result = await settled(operation)
            assert isinstance(result, asyncio.CancelledError)
        await checkpoint()
        assert source.cancellations >= 1, "The resistant native read was not cancelled"
        assert not source.operation.done()
        assert source.operation in env.runtime._submit_operations
    finally:
        source.release.set()
        if source.operation is not None:
            await asyncio.wait_for(
                asyncio.gather(source.operation, return_exceptions=True), timeout=2
            )
        await checkpoint()
    assert source.operation not in env.runtime._submit_operations
    assert table_rows(env.store.db) == before
    assert submit_entry(submits(env)[0]) == canonical
    assert (await env.runtime.status(canonical.payload["task_id"])).state == TaskState.PAUSED
    assert not env.store.read(kinds=["agent.outcome"])
    assert selection.call_count == selected_counts
    assert_no_worker(env)
    assert source.reads_after_retirement == [], (
        "A retired source response started new journal reads",
        source.reads_after_retirement,
    )


@pytest.mark.parametrize("progress,size", [(2, 100), (60, 45_000)], ids=["small", "byte_pages"])
async def test_canonical_terminal_receipt_survives_byte_limited_history(
    env, monkeypatch, progress, size
):
    body = request(env, sources=False)
    original = proposal(
        env.session,
        key=body.idempotency_key,
        objective=body.objective,
        content_scope=body.content_scope,
    )
    receipt = env.store.admit_submit(original, "agent")
    assert receipt.disposition == "created"
    receipt.validate_request(original)
    canonical = submit_entry(receipt.entry)
    task_id = canonical.payload["task_id"]
    for index in range(progress):
        env.store.append(
            AppendRequest(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=env.session.epoch,
                kind="agent.tool",
                task_refs=[task_id],
                idempotency_key=f"synthetic-tool-{index}",
                payload={
                    "task_id": task_id,
                    "state": "running",
                    "plugin_versions": {},
                    "content_scope": body.content_scope,
                    "observation": "x" * size,
                },
            ),
            "agent",
        )
    terminal = Task(
        task_id=task_id,
        session_id=body.session_id,
        thread_id=body.thread_id,
        epoch=body.epoch,
        objective=body.objective,
        objective_revision=canonical.payload["objective_revision"],
        source_event_ids=[],
        content_scope=canonical.payload["content_scope"],
        idempotency_key=body.idempotency_key,
        harness_version=canonical.payload["harness_version"],
        model_pin=canonical.payload["model_pin"],
        auxiliary_models=canonical.payload.get("auxiliary_models", {}),
        state=TaskState.CANCELLED,
        summary="Synthetic cancellation receipt.",
        error="cancelled by owner",
    )
    outcome = env.store.append(
        AppendRequest(
            session_id=body.session_id,
            thread_id=body.thread_id,
            epoch=body.epoch,
            kind="agent.outcome",
            task_refs=[task_id],
            idempotency_key="synthetic-outcome",
            payload={
                "task_id": task_id,
                "state": "cancelled",
                "plugin_versions": {},
                "content_scope": body.content_scope,
                "task": terminal.model_dump(mode="json"),
            },
        ),
        "agent",
    )
    history, pages, after = [], [], 0
    while batch := env.store.read(after=after, limit=200, task_id=task_id):
        pages.append(len(batch))
        history.extend(batch)
        after = batch[-1].sequence
    assert len(history) == progress + 2
    assert history[0] == canonical and history[-1] == outcome
    if progress == 60:
        assert len(pages) > 8, "The real byte-limited history fixture did not span enough pages"
    query = AgentSubmitLookupRequest(
        session_id=body.session_id,
        thread_id=body.thread_id,
        epoch=body.epoch,
        idempotency_key=body.idempotency_key,
    )
    found = env.store.lookup_submit(query, "agent").validate_request(query)
    assert found.status == "recorded" and found.entry == canonical
    before = table_rows(env.store.db)
    traps = receipt_traps(env.runtime, monkeypatch)
    try:
        recovered = await env.runtime.submit(body)
    finally:
        assert table_rows(env.store.db) == before
        assert all(trap.call_count == 0 for trap in traps)
        assert_no_worker(env)
    assert recovered == terminal
    assert env.store.lookup_submit(query, "agent").entry == canonical
    assert env.journal.attempts == []
