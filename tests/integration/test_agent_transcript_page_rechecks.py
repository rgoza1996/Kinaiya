"""Late native disclosure changes at actual transcript-page publication fences.

This is a focused source-review regression, separate from the original frozen
#255 oracles. Real SQLite, Session ASGI, JournalClient and AgentRuntime are used;
the measured provider is synthetic and makes no model or network calls.
"""

import asyncio
import hashlib
import json

import httpx
import pytest
from test_agent_transcript_paging import MeasuredFixture, append, canonical, finish, page_action
from test_agent_transcript_paging import env as env

from core.policy import PolicyDenied, ToolPolicy
from packages.contracts.agent_export import AgentExportRequest
from packages.contracts.models import TaskRequest
from services.agent.runtime import AgentRuntime
from services.agent.tools import ToolFailure


class PublicationGate:
    """Hold an actual completed HTTP read while a native correction commits."""

    def __init__(self, journal, event_id, phase):
        self.journal, self.event_id, self.phase = journal, event_id, phase
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.active = False
        self.entry_reads = self.session_reads = 0
        self.returned = self.refused = None

    def __getattr__(self, name):
        return getattr(self.journal, name)

    async def hold(self):
        if not self.entered.is_set():
            self.entered.set()
            await self.release.wait()

    async def entry(self, event_id):
        value = await self.journal.entry(event_id)
        if self.active and event_id == self.event_id:
            self.entry_reads += 1
            # Initial immutable read, two complete source-closure passes, then
            # the final raw-view consistency reread in the reviewed source.
            if self.phase == "raw_reread" and self.entry_reads == 5:
                await self.hold()
        return value

    async def session(self, session_id):
        value = await self.journal.session(session_id)
        if self.active:
            self.session_reads += 1
            if self.phase == "final_session" and self.session_reads == 2:
                await self.hold()
        return value


def scope_token(task, provider, scope):
    body = {
        "schema_version": "1.0",
        "task_id": task.task_id,
        "objective_revision": task.objective_revision,
        "session_id": task.session_id,
        "thread_id": task.thread_id,
        "epoch": task.epoch,
        "provider": provider.info.model_dump(mode="json"),
        "content_scope": scope,
    }
    return hashlib.sha256(b"kinaiya.agent.transcript-page-scope.v1\0" + canonical(body)).hexdigest()


@pytest.mark.parametrize("phase", ("raw_reread", "final_session"))
@pytest.mark.parametrize("restrictive_correction", (False, True))
async def test_new_immutable_root_rechecks_late_native_disclosure_before_publication(
    env, monkeypatch, record_property, phase, restrictive_correction
):
    target = append(env, "Synthetic immutable context selected before the late native correction.")
    gate = PublicationGate(env.journal, target.event_id, phase)
    provider = MeasuredFixture(
        lambda current, _: (
            page_action(target.event_id, entry_view="immutable")
            if len(current.calls) == 1
            else finish()
        ),
        local=False,
    )
    workspace = env.store.path.rsplit("/", 1)[0]
    runtime = AgentRuntime(gate, provider, workspace, ToolPolicy(workspace), max_steps=4)
    env.runtimes.append(runtime)
    invoke = runtime._read_transcript

    async def observed(rec, args):
        gate.active = True
        try:
            result = await invoke(rec, args)
            gate.returned = {"scope": rec.task.content_scope, "page": json.loads(result[0])}
            return result
        except (PolicyDenied, ToolFailure) as error:
            gate.refused = type(error).__name__
            raise
        finally:
            gate.active = False

    monkeypatch.setattr(runtime, "_read_transcript", observed)
    task = await runtime.submit(
        TaskRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            objective="Inspect one bounded immutable transcript reference.",
            content_scope="public_synthetic",
            idempotency_key="late-page-publication",
        )
    )
    export_status = None
    try:
        assert await asyncio.wait_for(gate.entered.wait(), 4) is True
        assert gate.returned is None and gate.refused is None
        assert runtime._tasks[task.task_id].context_event_ids == set()
        if restrictive_correction:
            append(
                env,
                "Synthetic current source disclosure is now local only.",
                kind="conversation.revision",
                scope="local_only",
                payload={"target_event_id": target.event_id, "revision": 2},
            )
        # Exercise actual native export admission while the Task is live. The
        # proposed context has to be refused after withdrawal even if its raw
        # historical row and a pending page still have the original public text.
        request = AgentExportRequest(
            task_id=task.task_id,
            objective_revision=task.objective_revision,
            session_id=task.session_id,
            thread_id=task.thread_id,
            epoch=task.epoch,
            source_event_ids=[],
            source_revisions={},
            context_event_ids=[target.event_id],
            content_scope="public_synthetic",
            provider=provider.info,
            request_sha256="a" * 64,
            purpose="complete_generation",
            request_nonce="late-page-export-admission",
        )
        if restrictive_correction:
            with pytest.raises(httpx.HTTPStatusError) as refused:
                await env.journal.agent_export_admission(request)
            export_status = refused.value.response.status_code
            assert export_status == 403
        else:
            admitted = await env.journal.agent_export_admission(request)
            assert admitted.validate_request(request).effective_scope == "public_synthetic"
            export_status = 200
    finally:
        gate.release.set()
    final = await runtime.wait(task.task_id, timeout=8)
    await runtime.close()
    if provider.failures:
        raise provider.failures[0]
    record_property(
        "actual_late_page_observation",
        json.dumps(
            {
                "phase": phase,
                "restrictive_correction": restrictive_correction,
                "native_export_status": export_status,
                "entry_reads": gate.entry_reads,
                "session_reads": gate.session_reads,
                "page_refusal": gate.refused,
                "returned_scope": None if gate.returned is None else gate.returned["scope"],
                "returned_token": None
                if gate.returned is None
                else gate.returned["page"]["scope_token"],
                "actual_final_scope": final.content_scope,
                "measured_generation_calls": len(provider.calls),
            },
            sort_keys=True,
        ),
    )
    if restrictive_correction:
        assert target.payload["text"] not in json.dumps(provider.calls)
        if gate.returned is not None:
            assert gate.returned["scope"] == "local_only"
            assert gate.returned["page"]["scope_token"] == scope_token(
                final, provider, "local_only"
            )
        else:
            assert gate.refused in {"PolicyDenied", "ToolFailure"}
    else:
        assert final.state == "succeeded", final.error
        assert gate.refused is None and gate.returned is not None
        assert gate.returned["scope"] == "public_synthetic"
        page = gate.returned["page"]
        assert page["scope_token"] == scope_token(final, provider, "public_synthetic")
        assert len(page["entries"]) == 1
        assert page["entries"][0]["event_id"] == target.event_id
        assert page["entries"][0]["text"] == target.payload["text"]
        assert page["entries"][0]["entry_view"] == "immutable"
        assert page["entries"][0]["next_offset"] is None
        assert target.payload["text"] in json.dumps(provider.calls[1:])
