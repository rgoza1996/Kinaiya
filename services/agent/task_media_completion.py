"""Native completion obligations for an actually enrolled media task."""

from __future__ import annotations

import hashlib
import json

from core.task_harness import MAX_PHASES, MAX_RESULT_BYTES, json_bytes
from packages.contracts.models import TaskState
from packages.providers.text import ProviderCleanupError
from services.agent.media_tools import WorkspaceMediaBroker, _snapshot
from services.agent.task_harness import TaskHarnessCompletionProof


class MediaCompletionError(RuntimeError):
    def __init__(self):
        super().__init__("Enrolled native media completion is not established")


def _require(value):
    if value is not True:
        raise MediaCompletionError()


def _source(runtime, rec, proof):
    path = runtime.policy.resolve_path(proof.source_path)
    _require(str(path) == proof.source_path)
    raw = runtime.policy.read_bytes(path, maximum=65_537)
    _require(len(raw) <= 65_536 and hashlib.sha256(raw).hexdigest() == proof.source_sha256)
    _require(
        any(
            evidence.evidence_id == proof.source_evidence_id
            and evidence.kind == "file"
            and evidence.objective_revision == rec.task.objective_revision
            and evidence.path == proof.source_path
            and evidence.sha256 == proof.source_sha256
            for evidence in rec.task.evidence
        )
    )


def _publication(runtime, rec, grant, observed):
    store = runtime.artifact_store
    _require(store is not None)
    task = runtime._publication_task(rec)
    _require(task.content_scope == grant.content_scope)
    required = set(grant.required_operations)
    sam = observed.get("sam_hands") if "sam_hands" in required else None
    transcription = observed.get("transcribe_video") if "transcribe_video" in required else None
    media = None
    if sam is not None:
        media = sam["data"].get("media_asset")
        _require(type(media) is dict)
        _require(media.get("source", {}).get("sha256") == grant.source_sha256)
        _require(
            json_bytes(media.get("binding")) == json_bytes(grant.binding.model_dump(mode="json"))
        )
    transcript = None
    if transcription is not None:
        transcript = " ".join(transcription["text"].split())
        _require(bool(transcript))
    latest = {
        evidence.path: evidence
        for evidence in rec.task.evidence
        if evidence.kind == "artifact"
        and evidence.objective_revision == task.objective_revision
        and type(evidence.path) is str
        and evidence.path.startswith("artifact:")
    }
    identity = {key: getattr(task, key) for key in ("session_id", "thread_id", "epoch")}
    binding = grant.binding.model_dump(mode="json")
    for path, evidence in latest.items():
        # Earlier public placeholders can coexist with a private final result.
        if not store.verify(evidence, task):
            continue
        artifact_id = path[len("artifact:") :]
        artifact = store.get(artifact_id, **identity)
        if (
            json_bytes({key: artifact.get(key) for key in binding}) != json_bytes(binding)
            or artifact.get("content_scope") != grant.content_scope
            or artifact.get("content_sha256") != evidence.sha256
        ):
            continue
        components = artifact["components"]
        if transcript is not None:
            text = " ".join(
                "\n".join(
                    component["data"]["text"]
                    for component in components
                    if component["type"] == "text"
                ).split()
            )
            if transcript not in text:
                continue
        if media is not None:
            asset_id = media.get("asset_id")
            if not any(
                component["type"] == "media" and component["data"].get("asset_id") == asset_id
                for component in components
            ):
                continue
            actual = store.media(artifact_id, asset_id, **identity)
            if json_bytes(actual) != json_bytes(media):
                continue
            # Protected metadata alone cannot establish usable persisted bytes.
            for variant in ("source", "highlighted"):
                data = store.media(artifact_id, asset_id, variant=variant, **identity)
                _require(
                    type(data) is bytes
                    and len(data) == media[variant]["bytes"]
                    and hashlib.sha256(data).hexdigest() == media[variant]["sha256"]
                )
        return
    raise MediaCompletionError()


def check_completion(factory, runtime, rec):
    """No awaits, provider calls, model evidence, or permission changes."""
    try:
        enrolled = factory._current(runtime, rec)
        grant = enrolled.grant
        task = rec.task
        _require(
            task.state == TaskState.RUNNING
            and not rec.failed_task_harness
            and not getattr(rec, "_cleanup_uncertain", False)
        )
        execution = rec.task_harness_execution
        _require(
            type(execution) is dict
            and type(execution.get("revision")) is int
            and execution.get("revision") == task.objective_revision
            and execution.get("settled") is True
        )
        broker = execution.get("broker")
        _require(
            type(broker) is WorkspaceMediaBroker
            and broker is factory._broker
            and broker.policy is enrolled.policy
            and broker.provider is factory.provider
            and broker.grant == grant
            and broker._closed is True
        )
        proof = execution.get("completion_proof")
        _require(type(proof) is TaskHarnessCompletionProof)
        _require(
            proof.binding == json_bytes(grant.binding.model_dump(mode="json"))
            and proof.request_sha256 == execution.get("request_sha256")
        )
        cached = execution.get("result")
        _require(type(cached) is tuple and len(cached) == 2 and type(cached[0]) is str)
        output, extension = cached
        _require(
            len(output.encode("utf-8")) <= MAX_RESULT_BYTES
            and hashlib.sha256(output.encode("utf-8")).hexdigest() == proof.result_sha256
            and json_bytes(extension.model_dump(mode="json")) == proof.evidence
            and extension.kind == "extension"
            and extension.path == proof.source_path
            and extension.sha256 == proof.source_sha256
            and extension.objective_revision == task.objective_revision
        )
        _require(
            any(
                json_bytes(evidence.model_dump(mode="json")) == proof.evidence
                for evidence in task.evidence
                if evidence.kind == "extension"
            )
        )
        result = json.loads(output)
        extension_data = json.loads(extension.output)
        _require(
            result.get("contract") == "task_orchestrator.v1"
            and result.get("source_sha256") == proof.source_sha256
            and json_bytes(result.get("binding")) == proof.binding
            and result.get("task_completion") == "not_established"
        )
        _require(
            extension_data.get("contract") == "task_orchestrator.v1"
            and extension_data.get("request_sha256") == proof.request_sha256
            and extension_data.get("result_sha256") == proof.result_sha256
            and extension_data.get("task_completion") == "not_established"
        )
        _require(
            json_bytes(
                {key: extension_data.get(key) for key in grant.binding.model_dump(mode="json")}
            )
            == proof.binding
        )
        _source(runtime, rec, proof)
        _snapshot(enrolled.policy, grant)
        _require(type(proof.observations) is tuple and 0 < len(proof.observations) < MAX_PHASES)
        observed = {}
        receipts = []
        targets = {target.operation: target for target in grant.targets}
        for raw in proof.observations:
            _require(type(raw) is bytes and len(raw) <= MAX_RESULT_BYTES)
            value = json.loads(raw)
            _require(json_bytes(value, MAX_RESULT_BYTES) == raw)
            broker.check_primary_export(runtime.provider_info_for(rec), value)
            operation = value["operation"]
            _require(operation in targets and value["model"] == targets[operation].model)
            _require(operation not in observed or json_bytes(observed[operation]) == raw)
            observed[operation] = value
            receipts.append(
                {
                    **{
                        key: value[key]
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
                    },
                    "observed_result_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        _require(
            bool(grant.required_operations) and set(grant.required_operations) <= set(observed)
        )
        _require(
            json_bytes(result.get("consultations")) == json_bytes(receipts)
            and json_bytes(extension_data.get("consultations")) == json_bytes(receipts)
        )
        _publication(runtime, rec, grant, observed)
        # Synchronous store hooks and external file replacements cannot revoke
        # authority unnoticed during publication validation.
        factory._current(runtime, rec)
        _source(runtime, rec, proof)
        _snapshot(enrolled.policy, grant)
        for value in observed.values():
            broker.check_primary_export(runtime.provider_info_for(rec), value)
        return True
    except ProviderCleanupError:
        raise
    except Exception:
        raise MediaCompletionError() from None
