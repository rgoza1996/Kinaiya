"""Source-bound, read-only runtime observations for the Converse service."""

import asyncio
import hashlib
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from packages.contracts.agent_outcome import MAX_EVIDENCE_COUNT
from packages.contracts.context import context_json
from packages.contracts.models import Evidence, SpeechWindow, new_id
from packages.contracts.task_context import (
    EVIDENCE_KINDS,
    MAX_TASK_CONTEXT_REQUEST_BYTES,
    MAX_TASK_CONTEXT_RESPONSE_BYTES,
    MAX_TASK_CONTEXT_TASKS,
    TaskContextConflict,
    TaskContextObservation,
    TaskContextRequest,
    TaskContextResponse,
    TaskContextSource,
    TaskContextUnavailable,
)
from packages.providers.disclosure import (
    ContextScopeClosure,
    combine_scopes,
    context_scope,
    source_scope,
)

_MAX_RECORDS = 65536
_MAX_DEPENDENCIES = 4096


async def _source(runtime, request):
    source = await runtime.journal.resolve(request.source.source_event_id)
    session = await runtime.journal.session(request.source.session_id)
    if (
        TaskContextSource.of(source) != request.source
        or source.origin != "authenticated-user"
        or source.kind not in {"conversation.user", "conversation.window"}
        or source.payload.get("deleted") is True
        or (session.session_id, session.thread_id, session.epoch, session.channel)
        != (source.session_id, source.thread_id, source.epoch, "converse")
    ):
        raise TaskContextConflict()
    if source.kind == "conversation.window":
        window = SpeechWindow.model_validate(source.payload)
        latest = await runtime.journal.latest_window(
            source.session_id, source.epoch, window.utterance_id
        )
        if latest is None or TaskContextSource.of(latest) != request.source:
            raise TaskContextConflict()
    elif source.payload.get("final") is not True or type(source.payload.get("text")) is not str:
        raise TaskContextConflict()
    return source


def _stamp(record):
    task = record.task
    pending = record.pending_steer
    return (
        id(task),
        task.task_id,
        task.session_id,
        task.thread_id,
        task.epoch,
        task.objective_revision,
        task.content_scope,
        tuple(task.source_event_ids),
        frozenset(record.context_event_ids),
        record.task_harness_publication_scope,
        None
        if pending is None
        else (id(pending), tuple(pending["source_event_ids"]), pending["content_scope"]),
    )


async def _scope(runtime, record, stamp):
    task = record.task
    scopes = [task.content_scope, record.task_harness_publication_scope]
    source_ids = stamp[7]
    if stamp[10] is not None:
        # Pending steering is admitted before those sources join the applied
        # task revision. Its current disclosure still qualifies observed facts.
        source_ids += stamp[10][1]
        scopes.append(stamp[10][2])
    for source_id in source_ids:
        source = await runtime.journal.resolve(source_id)
        if (
            (source.session_id, source.thread_id, source.epoch)
            != (task.session_id, task.thread_id, task.epoch)
            or source.origin != "authenticated-user"
            or source.kind not in {"conversation.user", "conversation.window"}
        ):
            raise TaskContextUnavailable()
        scopes.append(await source_scope(runtime.journal, source_id, source))
    closure = ContextScopeClosure(task.session_id, task.thread_id, task.epoch)
    for event_id in sorted(stamp[8]):
        entry = await runtime.journal.entry(event_id)
        if (entry.session_id, entry.thread_id, entry.epoch) != closure.identity:
            raise TaskContextUnavailable()
        scopes.append(await context_scope(runtime.journal, entry, closure=closure))
    return combine_scopes(*scopes)


def _program_receipt(record):
    from services.agent.task_program import TaskProgramProof, _binding

    proof = record.task_program_proof
    if proof is None:
        return "not_observed"
    try:
        if type(proof) is not TaskProgramProof or proof.binding != _binding(record.task):
            return "unavailable"
        receipt = context_json(proof.receipt, maximum=262144)
        stamp = hashlib.sha256(proof.request).hexdigest()
        if (
            type(receipt) is not dict
            or receipt.get("contract") != "task_program.v1"
            or receipt.get("request_sha256") != stamp
            or type(receipt.get("exit_code")) is not int
            or receipt["exit_code"] != 0
            or receipt.get("settled") is not True
            or record.proc_slot.get("proc") is not None
            or getattr(record, "_cleanup_uncertain", False)
            or any(
                receipt.get(key) != getattr(record.task, key)
                for key in ("task_id", "session_id", "thread_id", "epoch", "objective_revision")
            )
            or not any(
                item.get("binding") == proof.binding
                and item.get("request_sha256") == stamp
                and item.get("settled") is True
                and item.get("result") is not None
                for item in record.task_program_executions
            )
        ):
            return "unavailable"
        return "observed"
    except Exception:
        return "unavailable"


def _observation(record, scope):
    task = record.task
    if len(task.evidence) > MAX_EVIDENCE_COUNT:
        raise TaskContextUnavailable()
    counts = dict.fromkeys(sorted(EVIDENCE_KINDS), 0)
    for evidence in task.evidence:
        if type(evidence) is not Evidence or evidence.kind not in EVIDENCE_KINDS:
            raise TaskContextUnavailable()
        if evidence.objective_revision == task.objective_revision:
            counts[evidence.kind] += 1
    return TaskContextObservation(
        task_id=task.task_id,
        session_id=task.session_id,
        thread_id=task.thread_id,
        epoch=task.epoch,
        objective_revision=task.objective_revision,
        state=task.state.value if record.submit_confirmed else "unknown",
        submission_confirmed=record.submit_confirmed,
        steer_pending=record.pending_steer is not None,
        completion_publication_pending=record.completion_pending,
        content_scope=scope,
        evidence_counts=counts,
        required_work_count=len(task.remaining_required_work),
        program_execution_receipt=_program_receipt(record),
        semantic_verification="not_assessed",
        observation_basis="native_runtime_snapshot",
    )


async def snapshot(runtime, request, instance_id):
    try:
        async with asyncio.timeout(2):
            await _source(runtime, request)
            if runtime._closed or len(runtime._tasks) > _MAX_RECORDS:
                raise TaskContextUnavailable()
            identity = request.source.session_id, request.source.thread_id, request.source.epoch
            records = [
                record
                for record in runtime._tasks.values()
                if (
                    (record.task.session_id, record.task.thread_id, record.task.epoch) == identity
                    and (request.task_ids is None or record.task.task_id in request.task_ids)
                )
            ]
            records.sort(
                key=lambda record: (record.submit_sequence, record.task.task_id), reverse=True
            )
            truncated = len(records) > MAX_TASK_CONTEXT_TASKS
            records = records[:MAX_TASK_CONTEXT_TASKS]
            stamps = [_stamp(record) for record in records]
            if (
                sum(
                    len(stamp[7]) + len(stamp[8]) + (len(stamp[10][1]) if stamp[10] else 0)
                    for stamp in stamps
                )
                > _MAX_DEPENDENCIES
            ):
                raise TaskContextUnavailable()
            scopes = [
                await _scope(runtime, record, stamp)
                for record, stamp in zip(records, stamps, strict=True)
            ]
            await _source(runtime, request)
            if runtime._closed or any(
                runtime._tasks.get(record.task.task_id) is not record or _stamp(record) != stamp
                for record, stamp in zip(records, stamps, strict=True)
            ):
                raise TaskContextUnavailable()
            result = TaskContextResponse(
                contract="converse_task_context.v1",
                read_only=True,
                source=request.source,
                instance_id=instance_id,
                observed_at=datetime.now(UTC).isoformat(),
                tasks=[
                    _observation(record, scope)
                    for record, scope in zip(records, scopes, strict=True)
                ],
                truncated=truncated,
            ).validate_request(request)
            if len(result.model_dump_json().encode()) > MAX_TASK_CONTEXT_RESPONSE_BYTES:
                raise TaskContextUnavailable()
            return result
    except (TaskContextConflict, asyncio.CancelledError):
        raise
    except Exception:
        raise TaskContextUnavailable() from None


def router(runtime, auth):
    api = APIRouter()
    instance_id = new_id()

    @api.post("/v1/agent/task-context", dependencies=[Depends(auth.require("converse"))])
    async def task_context(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_TASK_CONTEXT_REQUEST_BYTES:
                raise HTTPException(413, "Native task context request exceeds its bound")
            raw.extend(chunk)
        try:
            proposal = TaskContextRequest.model_validate(
                context_json(bytes(raw), maximum=MAX_TASK_CONTEXT_REQUEST_BYTES)
            )
        except Exception:
            raise HTTPException(422, "Invalid native task context request") from None
        try:
            return await snapshot(runtime, proposal, instance_id)
        except TaskContextConflict:
            raise HTTPException(409, str(TaskContextConflict())) from None
        except TaskContextUnavailable:
            raise HTTPException(503, str(TaskContextUnavailable())) from None

    return api
