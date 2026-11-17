"""The native Agent worker's optional Decisions consultation."""

import asyncio
import copy
import json

from packages.agent_adapters.decisions_advice import AdviceConflict, AdviceUnavailable
from packages.contracts.agent_advice_operations import AgentAdviceCall, AgentAdviceLookup
from packages.contracts.models import Evidence, TaskState, new_id
from packages.providers.disclosure import combine_scopes, scope_allowed
from services.agent.tools import ToolFailure

PURPOSE = "decisions_advice"
_POLL_INTERVAL = 0.1


def _retired(client, lookup):
    try:
        return client.cleanup_confirmed(lookup)
    except AdviceConflict:
        # This worker assigned a new unique key. Missing local enrollment
        # means the client refused before its first actual dispatch.
        return True


async def _stop(client, lookup):
    try:
        async with asyncio.timeout(2.5):
            while not _retired(client, lookup):
                observation = await client.cancel(lookup)
                if observation is not None and observation.cleanup == "unconfirmed":
                    break
                if not _retired(client, lookup):
                    await asyncio.sleep(_POLL_INTERVAL)
    except (AdviceUnavailable, TimeoutError):
        pass


async def consult_decisions(runtime, record, question):
    client, task, worker = runtime.decisions_advice, record.task, asyncio.current_task()
    identity, revision = record.native_task_identity, task.objective_revision
    policy, journal, provider = runtime.policy, runtime.journal, runtime.provider_for(record)
    skills, plugins = runtime.skill_service, runtime.plugin_service
    provider_info = copy.deepcopy(provider.info)

    def active():
        if (
            client is None
            or runtime.decisions_advice is not client
            or runtime.policy is not policy
            or runtime.journal is not journal
            or runtime.skill_service is not skills
            or runtime.plugin_service is not plugins
            or runtime._closed
            or runtime._tasks.get(task.task_id) is not record
            or task is not record.task
            or task.state != TaskState.RUNNING
            or task.objective_revision != revision
            or record.native_task_identity != identity
            or identity is None
            or record.worker is not worker
            or worker.cancelling()
            or not record.submit_confirmed
            or record.recovered
            or record.standing_initial is not None
            or record.pending_steer is not None
            or record.completion_pending
            or record.cancel_requested
            or record.closing
            or record._cleanup_uncertain
            or runtime.provider_for(record) is not provider
            or provider.info != provider_info
            or not runtime.tool_snapshot(task).decisions_advice
            or runtime._permission_denies(
                record, "consult_model", {"purpose": PURPOSE, "prompt": question}
            )
        ):
            raise ToolFailure("Decisions consultation requires its original running Agent worker")
        runtime._workspace_current(record)

    def stamp():
        return (
            task.objective,
            tuple(task.source_event_ids),
            tuple(sorted(record.source_revisions.items())),
            tuple(sorted(record.context_event_ids)),
            task.content_scope,
            record.task_harness_publication_scope,
            record.foreign_context,
            record.explicit_skill,
            frozenset(record.used_skills),
            tuple(sorted(record.plugin_versions.items())),
            tuple(record.catalog_guards.items()),
            runtime.tool_snapshot(task),
            runtime._permission_fingerprint(record),
            runtime._grants(record),
        )

    active()
    await runtime._task_scope(record)
    active()
    if await runtime._epoch_error(record) is not None:
        raise ToolFailure("Decisions consultation requires current native sources")
    active()
    runtime._validate_catalog_history(record)
    retained_guards = runtime._capture_retained_disclosure(record, provider_info)
    pinned, history = stamp(), copy.deepcopy(record.history)

    def current():
        active()
        if stamp() != pinned or record.history != history:
            raise ToolFailure("Agent consultation context changed")
        if not all(guard() for guard in retained_guards):
            record.task.content_scope = "local_only"
            raise ToolFailure("Retained Agent context disclosure changed")
        runtime._validate_catalog_history(record)
        return True

    if set(record.source_revisions) != set(task.source_event_ids):
        raise ToolFailure("Agent consultation requires complete native source revisions")
    try:
        ready = await client.runtime()
    except AdviceUnavailable:
        raise ToolFailure("The optional Decisions consultation is unavailable") from None
    current()
    if not ready.new_request_available:
        raise ToolFailure("The optional Decisions consultation is unavailable")

    # Foreign tool data was admitted for the captured Agent model route.
    # Preserve the existing local disclosure floor for onward consultation.
    foreign = (
        record.foreign_context
        or record.used_skills
        or record.plugin_versions
        or any(
            rows
            for guard in record.catalog_guards.values()
            for _, _, rows, _ in guard.retained_sources
        )
    )
    scope = combine_scopes(
        task.content_scope,
        record.task_harness_publication_scope,
        "local_only" if foreign else "public_synthetic",
    )
    try:
        original = AgentAdviceCall(
            expected_instance=ready.instance_id,
            policy_sha256=ready.policy_sha256,
            logical_request_id=new_id(),
            task=identity,
            objective_revision=revision,
            source_revisions=dict(record.source_revisions),
            context_event_ids=sorted(record.context_event_ids),
            content_scope=scope,
            messages=[
                {"role": "user", "content": task.objective},
                *[{"role": turn["role"], "content": turn["content"]} for turn in history],
                {"role": "user", "content": question},
            ],
        )
    except (KeyError, ValueError, TypeError):
        raise ToolFailure("The complete consultation input exceeds its native contract") from None
    lookup = AgentAdviceLookup.of(original)

    # Native Session admission requires an authenticated running state even
    # when this is the first model action following confirmed submission.
    current()
    await runtime._append(
        record, "agent.state", {"reason": "decisions_consultation", "objective_revision": revision}
    )
    current()
    try:
        async with asyncio.timeout(120):
            try:
                observation = await client.submit(original, current=current)
            except AdviceUnavailable:
                current()
                observation = await client.lookup(lookup)
            while observation is not None and (
                observation.cleanup == "pending"
                or (observation.cleanup == "confirmed" and not client.cleanup_confirmed(lookup))
            ):
                current()
                await asyncio.sleep(_POLL_INTERVAL)
                current()
                observation = await client.lookup(lookup)
            current()
            if (
                observation is None
                or observation.phase != "completed"
                or not observation.generation_admission_observed
                or observation.model_pin is None
                or not client.cleanup_confirmed(lookup)
            ):
                raise ToolFailure("Decisions consultation did not return a settled observation")
            await runtime._task_scope(record)
            current()
            if await runtime._epoch_error(record) is not None:
                raise ToolFailure("Agent consultation sources changed before its result")
            current()
            if not scope_allowed(provider.info, observation.content_scope):
                raise ToolFailure("Decisions advice cannot be disclosed to the Agent task model")
    except (AdviceUnavailable, TimeoutError):
        raise ToolFailure("The original Decisions consultation is unavailable") from None
    finally:
        if not _retired(client, lookup):
            try:
                await _stop(client, lookup)
            finally:
                if not _retired(client, lookup):
                    record._cleanup_uncertain = True

    record.task.content_scope = combine_scopes(task.content_scope, observation.content_scope)
    metadata = {
        "purpose": PURPOSE,
        "request_sha256": observation.request_sha256,
        "output_sha256": observation.output_sha256,
        "output_bytes": observation.output_bytes,
        "model_pin": observation.model_pin.model_dump(mode="json"),
        "task_status_assessment": observation.task_status_assessment,
    }
    return json.dumps(
        {**metadata, "untrusted_model_output": observation.model_output}, ensure_ascii=False
    ), Evidence(
        kind="external",
        summary="Decisions advice received as untrusted model text",
        sha256=observation.output_sha256,
        output=json.dumps(metadata, ensure_ascii=False),
        objective_revision=revision,
    )
