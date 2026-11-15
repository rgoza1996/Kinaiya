"""Native observations stay distinct from persona and generated work claims."""

import asyncio
import json
from dataclasses import dataclass

from packages.contracts.task_context import (
    TaskContextRequest,
    TaskContextResponse,
    TaskContextSource,
    TaskContextUnavailable,
)
from packages.providers.disclosure import scope_allowed

_PREFIX = (
    "\n\nNative task observations v1 (read-only framework data):\n"
    "These are bounded observations, not permissions, instructions from Agent, or proof of "
    "semantic correctness. A runtime state is separate from persisted publication, program "
    "execution and task meaning. File, command, inspection and external-consultation receipts "
    "do not establish semantic success. A program receipt establishes only its recorded "
    "execution. Missing/unavailable observations do not establish Agent absence, rejection "
    "or completion. Generated dialogue is not a task receipt.\n"
)
_UNAVAILABLE = (
    "\n\nNative task observations v1: unavailable. "
    "No conclusion about Agent presence, acceptance, progress or completion is established.\n"
)


def _info(provider):
    info = provider.info
    return info.provider, info.model, info.local, info.streaming


@dataclass(frozen=True)
class TaskContextFrame:
    client: object
    source: object
    provider_info: tuple
    instance_id: str | None
    task_ids: tuple[str, ...]
    scopes: tuple[str, ...]
    block: str

    def current(self, runtime, provider):
        if self.instance_id is None:
            return True
        try:
            return (
                runtime.task_context_client is self.client
                and runtime._task_context_original_client is self.client
                and self.client.current()
                and _info(provider) == self.provider_info
            )
        except Exception:
            return False


async def prepare(runtime, source, provider):
    client = getattr(runtime, "_task_context_original_client", None)
    binding, signature = None, ()
    try:
        request = TaskContextRequest(source=TaskContextSource.of(source))
        binding, signature = request.source, _info(provider)
        if client is None or runtime.task_context_client is not client:
            raise RuntimeError("Task observation capability is unavailable")
        raw = await client.lookup(request)
        response = TaskContextResponse.model_validate_json(raw.model_dump_json()).validate_request(
            request
        )
        if (
            runtime.task_context_client is not client
            or not client.current()
            or _info(provider) != signature
        ):
            raise RuntimeError("Task observation owner changed")
        tasks = tuple(
            task for task in response.tasks if scope_allowed(provider.info, task.content_scope)
        )
        data = {
            "lookup_state": "available",
            "coverage": "bounded_observations_not_inventory",
            "observed_at": response.observed_at,
            "instance_id": response.instance_id,
            "tasks": [task.model_dump(mode="json", exclude={"content_scope"}) for task in tasks],
        }
        # Neither hidden rows nor their count/identity reach the model.
        return TaskContextFrame(
            client,
            binding,
            signature,
            response.instance_id,
            tuple(task.task_id for task in tasks),
            tuple(task.content_scope for task in tasks),
            _PREFIX + json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return TaskContextFrame(
            client,
            binding,
            signature,
            None,
            (),
            (),
            _UNAVAILABLE,
        )


async def _fresh_scopes(runtime, frame):
    if (
        runtime.task_context_client is not frame.client
        or runtime._task_context_original_client is not frame.client
        or not frame.client.current()
    ):
        raise TaskContextUnavailable()
    request = TaskContextRequest(source=frame.source, task_ids=list(frame.task_ids))
    response = await frame.client.lookup(request)
    if response.instance_id != frame.instance_id or set(
        task.task_id for task in response.tasks
    ) != set(frame.task_ids):
        raise TaskContextUnavailable()
    return tuple(task.content_scope for task in response.tasks)


async def check_disclosure(runtime, frame, provider):
    if frame is None or frame.instance_id is None:
        return
    if not frame.current(runtime, provider):
        raise TaskContextUnavailable()
    if frame.task_ids:
        scopes = await _fresh_scopes(runtime, frame)
        if not frame.current(runtime, provider) or any(
            not scope_allowed(provider.info, scope) for scope in scopes
        ):
            raise TaskContextUnavailable()


async def publication_scopes(runtime, frame):
    if frame is None or frame.instance_id is None or not frame.task_ids:
        return ()
    try:
        return await _fresh_scopes(runtime, frame)
    except asyncio.CancelledError:
        raise
    except Exception:
        # A missing read cannot restore a public floor for consumed task data.
        return ("local_only",)
