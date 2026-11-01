"""Bounded native task-command recovery records, never a second dispatch plan."""

import hashlib
import json
import re

from packages.contracts.agent_submit import agent_submit_json
from packages.contracts.models import AppendRequest, JournalEntry, TaskRequest, TaskState

MAX_FROZEN_COMMANDS = 128
MAX_FROZEN_BYTES = 69_632
MAX_TASK_REPLY_BYTES = 4_096
MAX_APPEND_BYTES = 65_536
UNCONFIRMED = "Command dispatch is unconfirmed; retry this exact command to check its receipt"


def encode(value, maximum=MAX_FROZEN_BYTES):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError("Task command recovery record exceeds its byte limit")
    return encoded


def decode(value):
    if type(value) is not str:
        raise ValueError("Task command recovery record is unavailable")
    return agent_submit_json(value.encode("utf-8"), maximum=MAX_FROZEN_BYTES)


def task_intent(command, args, request, *, skill_id=None):
    if skill_id is None:
        if command != "create-harness" or not args or not " ".join(args).strip():
            raise ValueError("Usage: /create-harness <request>")
        objective = (
            "Create a specialized Kinaiya harness candidate for this owner request:\n\n"
            + " ".join(args)
            + "\n\nProduce the candidate source, manifest and usage notes in the granted "
            "workspace. Inspect the available native extension contracts before choosing "
            "an implementation. The current protected harness contract is context_selector.v1; "
            "do not claim unsupported runtime capabilities. Check the candidate within existing "
            "permissions. Creation does not authorize registration, evaluation-suite changes, "
            "promotion or activation. Report the produced paths and any remaining owner steps."
        )
        metadata = {"workflow": "harness_candidate", "activation": "owner_review_required"}
        prefix = "harness-command:"
    else:
        if type(skill_id) is not str or not 1 <= len(skill_id) <= 256 or "\0" in skill_id:
            raise ValueError("Invalid explicit skill id")
        objective = (
            " ".join(args) or "Follow the selected skill's procedure within current permissions."
        )
        metadata = {"skill_id": skill_id}
        prefix = "skill-command:"
    task = TaskRequest(
        session_id=request.session_id,
        thread_id=request.thread_id,
        epoch=request.epoch,
        objective=objective,
        content_scope=request.content_scope,
        idempotency_key=prefix + hashlib.sha256(request.idempotency_key.encode()).hexdigest(),
    )
    value = {
        "schema_version": "1.0",
        "command": command,
        "task_request": task.model_dump(mode="json"),
        "explicit_skill": skill_id,
        "result_metadata": metadata,
    }
    return load_intent(encode(value), request)


def load_intent(encoded, request):
    value = decode(encoded)
    if type(value) is not dict or set(value) != {
        "schema_version",
        "command",
        "task_request",
        "explicit_skill",
        "result_metadata",
    }:
        raise ValueError("Task command recovery record is invalid")
    if value["schema_version"] != "1.0" or type(value["command"]) is not str:
        raise ValueError("Task command recovery identity is invalid")
    if re.fullmatch(r"[a-z][a-z0-9_-]{0,127}", value["command"]) is None:
        raise ValueError("Task command recovery identity is invalid")
    raw = value["task_request"]
    task = TaskRequest.model_validate(raw, strict=True)
    if raw != task.model_dump(mode="json"):
        raise ValueError("Task command recovery request is incomplete")
    for identity in (task.session_id, task.thread_id):
        if not 1 <= len(identity) <= 128 or "\0" in identity:
            raise ValueError("Task command recovery session identity is invalid")
    if not 1 <= len(request.idempotency_key) <= 248 or "\0" in request.idempotency_key:
        # The established journal key adds the eight-character command prefix.
        raise ValueError("Task command key exceeds the native journal limit")
    skill = value["explicit_skill"]
    if skill is None:
        if value["command"] != "create-harness":
            raise ValueError("Task command recovery skill identity is missing")
        prefix = "harness-command:"
        metadata = {"workflow": "harness_candidate", "activation": "owner_review_required"}
    else:
        if type(skill) is not str or not 1 <= len(skill) <= 256 or "\0" in skill:
            raise ValueError("Task command recovery skill identity is invalid")
        prefix = "skill-command:"
        metadata = {"skill_id": skill}
    if (
        value["result_metadata"] != metadata
        or (task.session_id, task.thread_id, task.epoch, task.content_scope, task.idempotency_key)
        != (
            request.session_id,
            request.thread_id,
            request.epoch,
            request.content_scope,
            prefix + hashlib.sha256(request.idempotency_key.encode()).hexdigest(),
        )
        or task.source_event_ids
        or task.source_revision != 1
    ):
        raise ValueError("Task command recovery binding is invalid")
    # Reserve enough native append space before any inference can start, using
    # the longest supported identity/state rather than the current Task state.
    largest_response = {
        "schema_version": "1.0",
        "status": "ok",
        "command": value["command"],
        "result": {
            "accepted_task_id": "\x01" * 128,
            "state": max((state.value for state in TaskState), key=len),
            **metadata,
        },
    }
    largest_append = command_append(request, largest_response)
    encode({"response": largest_response, "append": largest_append.model_dump(mode="json")})
    return value


def command_append(request, response):
    append = AppendRequest(
        session_id=request.session_id,
        thread_id=request.thread_id,
        epoch=request.epoch,
        kind="agent.command",
        idempotency_key="command:" + request.idempotency_key,
        payload={
            "op": "control",
            "actor": "owner",
            "input": request.input,
            "response": response,
            "content_scope": "local_only",
        },
    )
    if len(append.model_dump_json().encode("utf-8")) > MAX_APPEND_BYTES:
        raise ValueError("Task command journal receipt exceeds the native entry limit")
    return append


def task_receipt(task, intent, request):
    frozen = TaskRequest.model_validate(intent["task_request"], strict=True)
    if (task.session_id, task.thread_id, task.epoch, task.idempotency_key) != (
        frozen.session_id,
        frozen.thread_id,
        frozen.epoch,
        frozen.idempotency_key,
    ):
        raise ValueError("Task command returned a different native identity")
    response = {
        "schema_version": "1.0",
        "status": "ok",
        "command": intent["command"],
        "result": {
            "accepted_task_id": task.task_id,
            "state": task.state.value,
            **intent["result_metadata"],
        },
    }
    value = {
        "response": response,
        "append": command_append(request, response).model_dump(mode="json"),
    }
    return load_receipt(encode(value), intent, request)


def load_receipt(encoded, intent, request):
    value = decode(encoded)
    if type(value) is not dict or set(value) != {"response", "append"}:
        raise ValueError("Task command receipt is invalid")
    response = value["response"]
    encode(response, MAX_TASK_REPLY_BYTES)
    if type(response) is not dict or set(response) != {
        "schema_version",
        "status",
        "command",
        "result",
    }:
        raise ValueError("Task command response is invalid")
    if (response["schema_version"], response["status"], response["command"]) != (
        "1.0",
        "ok",
        intent["command"],
    ):
        raise ValueError("Task command response identity is invalid")
    result = response["result"]
    metadata = intent["result_metadata"]
    if type(result) is not dict or set(result) != {"accepted_task_id", "state", *metadata}:
        raise ValueError("Task command result is invalid")
    if (
        type(result["accepted_task_id"]) is not str
        or not 1 <= len(result["accepted_task_id"]) <= 128
        or "\0" in result["accepted_task_id"]
    ):
        raise ValueError("Task command result identity is invalid")
    if type(result["state"]) is not str or result["state"] not in {
        state.value for state in TaskState
    }:
        raise ValueError("Task command result state is invalid")
    if any(result[key] != child for key, child in metadata.items()):
        raise ValueError("Task command result metadata is invalid")
    append = AppendRequest.model_validate(value["append"], strict=True)
    if value["append"] != append.model_dump(mode="json") or append != command_append(
        request, response
    ):
        raise ValueError("Task command journal receipt is invalid")
    return value


def validate_append_receipt(raw, receipt):
    entry = JournalEntry.model_validate(raw, strict=True)
    if (
        entry.origin != "agent-supervisor"
        or type(entry.sequence) is not int
        or not 1 <= entry.sequence <= 9_007_199_254_740_991
        or not 1 <= len(entry.event_id) <= 128
        or "\0" in entry.event_id
    ):
        raise ValueError("Task command journal confirmation is invalid")
    encoded = entry.model_dump(mode="json")
    projection = {key: encoded[key] for key in AppendRequest.model_fields}
    if projection != receipt["append"]:
        raise ValueError("Task command journal confirmation does not match its frozen request")


def unconfirmed(command=None, *, receipt=None):
    result = {
        "schema_version": "1.0",
        "status": "unconfirmed",
        "command": command,
        "error": UNCONFIRMED,
    }
    if receipt is not None:
        # Positive task acceptance is distinct from the command journal ACK.
        result["task_result"] = receipt["response"]["result"]
    return result
