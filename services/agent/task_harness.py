"""Native task-owned generated orchestration; no model-selected authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass

from core.plugins.errors import PluginError
from core.plugins.sourcecheck import decode_source
from core.policy import PolicyDenied
from core.task_harness import MAX_RESULT_BYTES, json_bytes, orchestrate
from packages.contracts.models import Evidence, TaskState
from packages.contracts.task_harness import RunTaskHarnessArguments
from packages.providers.disclosure import combine_scopes
from packages.providers.text import ProviderCleanupError
from services.agent.model_settings import AgentModelSettings
from services.agent.tools import ToolFailure


@dataclass(frozen=True)
class TaskHarnessCompletionProof:
    """Native-only immutable observations; candidate output cannot supply this."""

    binding: bytes
    source_path: str
    source_sha256: str
    source_evidence_id: str
    request_sha256: str
    result_sha256: str
    observations: tuple[bytes, ...]
    evidence: bytes


def task_harness_available(runtime, task=None):
    """An optional native declaration only; never construct or probe a provider."""
    factory = getattr(runtime, "task_harness_broker_factory", None)
    available = getattr(factory, "available", None)
    if not callable(factory) or not callable(available):
        return False
    try:
        return available(runtime, task) is True
    except (ValueError, OSError, RuntimeError, PolicyDenied):
        return False


def task_harness_description(runtime, task):
    """Capture passive native source metadata, never a grant or model prompt."""
    factory = getattr(runtime, "task_harness_broker_factory", None)
    describe = getattr(factory, "describe", None)
    if not task_harness_available(runtime, task) or not callable(describe):
        return None
    try:
        value = describe(runtime, task)
        if type(value) is not dict or set(value) != {
            "source_path",
            "source_sha256",
            "source_bytes",
            "allowed_operations",
            "required_operations",
        }:
            raise ValueError
        source = RunTaskHarnessArguments.model_validate(
            {
                "source_path": value["source_path"],
                "source_sha256": value["source_sha256"],
                "input": {},
            },
            strict=True,
        )
        if type(value["source_bytes"]) is not int or not 16 <= value["source_bytes"] <= 16_777_216:
            raise ValueError
        allowed, required = value["allowed_operations"], value["required_operations"]
        for operations in (allowed, required):
            if (
                type(operations) is not list
                or len(operations) > 2
                or any(
                    type(item) is not str or item not in {"sam_hands", "transcribe_video"}
                    for item in operations
                )
                or len(set(operations)) != len(operations)
            ):
                raise ValueError
        if not allowed or not set(required) <= set(allowed):
            raise ValueError
        return json.loads(
            json_bytes(
                {
                    "source_path": source.source_path,
                    "source_sha256": source.source_sha256,
                    "source_bytes": value["source_bytes"],
                    "allowed_operations": sorted(allowed),
                    "required_operations": sorted(required),
                },
                4096,
            )
        )
    except Exception:
        raise ToolFailure("Native task media description is unavailable") from None


async def _settle_close(broker):
    """Keep the sole broker close owned through caller cancellation."""
    close = asyncio.create_task(broker.close())
    cancelled = False
    while not close.done():
        try:
            await asyncio.shield(close)
        except asyncio.CancelledError:
            cancelled = True
    settled = close.result()
    if settled is not True:
        raise ProviderCleanupError(
            "Harness media cleanup is unconfirmed; original operation retained"
        )
    return cancelled


async def run_task_harness(runtime, rec, arguments):
    """Run one immutable Agent-authored program per task objective revision.

    The trusted factory has signature ``factory(runtime, record, current)``.
    It returns an owned WorkspaceMediaBroker with async call()/close(). The
    candidate never receives this object or selects its grant/provider.
    """
    try:
        request = RunTaskHarnessArguments.model_validate(arguments, strict=True)
        json_bytes(request.input, MAX_RESULT_BYTES)
    except ValueError as exc:
        raise ToolFailure("Harness arguments violate task_orchestrator.v1") from exc
    factory = runtime.task_harness_broker_factory
    if not task_harness_available(runtime, rec.task) or rec.standing_initial is not None:
        raise ToolFailure("Task orchestration is not configured for this task")
    task, policy, journal = rec.task, runtime.policy, runtime.journal
    primary = runtime.provider_for(rec)
    primary_info = runtime.provider_info_for(rec).model_copy(deep=True)
    primary_effort = AgentModelSettings.effort(primary)
    primary_pin = task.model_pin.model_copy(deep=True) if task.model_pin is not None else None
    owner, revision = asyncio.current_task(), task.objective_revision
    binding = {
        "task_id": task.task_id,
        "session_id": task.session_id,
        "thread_id": task.thread_id,
        "epoch": task.epoch,
        "objective_revision": revision,
    }
    tools, scope = runtime.tool_snapshot(task), task.content_scope
    permissions = runtime._permission_fingerprint(rec)

    def current():
        if (
            runtime._closed
            or rec.recovered
            or rec.closing
            or rec.cancel_requested
            or rec.pending_steer is not None
            or task.state != TaskState.RUNNING
            or runtime._tasks.get(task.task_id) is not rec
            or rec.task is not task
            or owner.cancelling()
            or runtime.policy is not policy
            or runtime.journal is not journal
            or runtime.provider_for(rec) is not primary
            or runtime.provider_info_for(rec) != primary_info
            or AgentModelSettings.effort(primary) != primary_effort
            or task.model_pin != primary_pin
            or runtime.task_harness_broker_factory is not factory
            or runtime.tool_snapshot(task) != tools
            or task.content_scope != scope
            or runtime._permission_fingerprint(rec) != permissions
            or binding
            != {
                "task_id": task.task_id,
                "session_id": task.session_id,
                "thread_id": task.thread_id,
                "epoch": task.epoch,
                "objective_revision": task.objective_revision,
            }
        ):
            raise PolicyDenied("Task harness authority is no longer current")
        policy.check_workspace()

    async def check():
        current()
        runtime._validate_catalog_history(rec)
        await runtime._task_scope(rec)
        current()
        stale = await runtime._epoch_error(rec)
        current()
        if stale is not None:
            raise PolicyDenied("Task harness session epoch is no longer current")

    await check()
    path = policy.resolve_path(request.source_path)
    raw = policy.read_bytes(path, maximum=65_537)
    if len(raw) > 65_536 or hashlib.sha256(raw).hexdigest() != request.source_sha256:
        raise ToolFailure("Task harness source does not match its immutable digest")
    try:
        decode_source(raw)
    except PluginError as exc:
        raise ToolFailure("Task harness source violates the isolated computation contract") from exc
    source_evidence = next(
        (
            evidence
            for evidence in task.evidence
            if evidence.kind == "file"
            and evidence.objective_revision == revision
            and evidence.path == str(path)
            and evidence.sha256 == request.source_sha256
        ),
        None,
    )
    if source_evidence is None:
        raise ToolFailure("Task harness requires current-revision native source-write evidence")
    stamp = hashlib.sha256(json_bytes(request.model_dump(mode="json"))).hexdigest()
    previous = rec.task_harness_execution
    if previous is not None and not previous["settled"]:
        raise ProviderCleanupError(
            "Original harness custody is unsettled; a replacement is blocked"
        )
    if previous is not None and previous["revision"] == revision:
        if previous["request_sha256"] != stamp:
            raise ToolFailure("This task revision already admitted a different harness execution")
        if previous.get("result") is None or not previous["settled"]:
            raise ToolFailure("Original task harness execution failed or remains unconfirmed")
        return previous["result"]
    execution = {
        "revision": revision,
        "request_sha256": stamp,
        "settled": False,
        "result": None,
        "broker": None,
    }
    broker = factory(runtime, rec, current)
    required_operations = broker.required_operations
    if type(required_operations) is not frozenset or not required_operations <= {
        "sam_hands",
        "transcribe_video",
    }:
        raise ToolFailure("Native task harness required operations are invalid")
    execution["broker"] = broker
    rec.task_harness_execution = execution
    observed_results = []

    def export_current():
        current()
        for result in observed_results:
            broker.check_primary_export(runtime.provider_info_for(rec), result)
        return True

    async def consult(arguments):
        await check()
        value = await broker.call(arguments)
        await check()
        result = value.model_dump(mode="json")
        json_bytes(result, MAX_RESULT_BYTES)
        if (
            result.get("binding") != binding
            or result.get("status") != "completed"
            or result.get("operation") != arguments.get("operation")
            or result.get("source_path") != arguments.get("source_path")
            or result.get("source_sha256") != arguments.get("source_sha256")
            or result.get("result_sha256")
            != hashlib.sha256(
                json_bytes({key: item for key, item in result.items() if key != "result_sha256"})
            ).hexdigest()
        ):
            raise ToolFailure("Native media result provenance does not match this harness task")
        broker.check_primary_export(runtime.provider_info_for(rec), result)
        rec.task_harness_publication_scope = combine_scopes(
            rec.task_harness_publication_scope, result["content_scope"]
        )
        observed_results.append(result)
        rec.task_harness_export_check = export_current
        receipt = {
            key: result[key]
            for key in (
                "operation",
                "binding",
                "source_path",
                "source_sha256",
                "source_bytes",
                "content_scope",
                "model",
                "request_sha256",
                "provider_request_sha256",
                "provider_response_sha256",
                "result_sha256",
                "status",
            )
        }
        await runtime._append(
            rec,
            "agent.tool",
            {
                "tool": "run_task_harness",
                "phase": "media_consultation",
                "ok": True,
                "contract": "task_orchestrator.v1",
                "harness_source_sha256": request.source_sha256,
                "receipt": receipt,
            },
        )
        await check()
        return result

    try:
        result = await orchestrate(
            raw, binding=binding, inputs=request.input, consult=consult, current=current
        )
        await check()
        if not required_operations <= {item["operation"] for item in observed_results}:
            raise ToolFailure("Task harness omitted a required native media operation")
    except (ValueError, OSError, PluginError) as exc:
        raise ToolFailure("Task harness execution failed: " + type(exc).__name__) from exc
    finally:
        try:
            cancelled = await _settle_close(broker)
        except BaseException as exc:
            rec._cleanup_uncertain = True
            if isinstance(exc, ProviderCleanupError):
                raise
            raise ProviderCleanupError(
                "Harness media close failed; original custody retained"
            ) from exc
        execution["settled"] = True
        if cancelled:
            raise asyncio.CancelledError
    current()
    export_current()
    output = json_bytes(result, MAX_RESULT_BYTES).decode("utf-8")
    # This observes execution and broker receipt digests, not task correctness,
    # transcript accuracy, publication, independent evaluation or promotion.
    evidence = Evidence(
        kind="extension",
        summary="Executed Agent-authored task harness with native media consultation",
        path=str(path),
        sha256=request.source_sha256,
        output=json.dumps(
            {
                "contract": "task_orchestrator.v1",
                **binding,
                "request_sha256": stamp,
                "result_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
                "consultations": result["consultations"],
                "task_completion": "not_established",
            },
            separators=(",", ":"),
        ),
        objective_revision=revision,
    )
    execution["result"] = (output, evidence)
    execution["completion_proof"] = TaskHarnessCompletionProof(
        binding=json_bytes(binding),
        source_path=str(path),
        source_sha256=request.source_sha256,
        source_evidence_id=source_evidence.evidence_id,
        request_sha256=stamp,
        result_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest(),
        observations=tuple(json_bytes(item, MAX_RESULT_BYTES) for item in observed_results),
        evidence=json_bytes(evidence.model_dump(mode="json")),
    )
    return output, evidence
