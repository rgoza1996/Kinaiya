"""Native receipts for real task-owned programs in the existing OS sandbox.

Generated source, dependencies and binaries remain untrusted. This module never
imports or evaluates them, and a zero exit code does not establish task quality.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from core.policy import PolicyDenied
from core.policy.tool_admission import native_tool_admission
from core.task_harness import json_bytes
from packages.contracts.models import Evidence, TaskState
from packages.contracts.task_program import RunTaskProgramArguments
from packages.providers.disclosure import combine_scopes
from packages.providers.text import ProviderCleanupError
from services.agent.media_tools import _snapshot
from services.agent.tools import ToolFailure, run_program, task_program_command

MAX_PROGRAM_RUNS = 2000
MAX_TEXT_BYTES = 65536


@dataclass(frozen=True)
class TaskProgramProof:
    binding: bytes
    request: bytes
    receipt: bytes
    source_evidence_id: str
    output: bytes
    evidence: bytes


def task_program_available(runtime, task=None):
    factory = getattr(runtime, "task_harness_broker_factory", None)
    method = getattr(factory, "program_available", None)
    try:
        return callable(method) and method(runtime, task) is True
    except Exception:
        return False


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


@contextmanager
def _opened(policy, path):
    resolved = policy.check_read(path)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = policy.workspace / candidate
    if resolved != Path(os.path.abspath(candidate)):
        raise ToolFailure("Program files cannot use symlink aliases")
    with policy.scoped_parent(path) as (parent, name, resolved):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ToolFailure("Program files require stable regular workspace files")
            yield fd, resolved, before
        finally:
            os.close(fd)


def _read(policy, path, maximum):
    with _opened(policy, path) as (fd, _, before):
        if not 0 < before.st_size <= maximum:
            raise ToolFailure("Program file exceeds its complete-file bound")
        chunks, size = [], 0
        while chunk := os.read(fd, min(65536, maximum + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                break
        data = b"".join(chunks)
        if len(data) != before.st_size or _identity(before) != _identity(os.fstat(fd)):
            raise ToolFailure("Program file changed during its native snapshot")
        return data


def _binding(task):
    return json_bytes(
        {
            key: getattr(task, key)
            for key in ("task_id", "session_id", "thread_id", "epoch", "objective_revision")
        }
    )


async def run_task_program(runtime, rec, arguments):
    """Run a pinned image once per exact request; never infer compiler causality."""
    request = RunTaskProgramArguments.model_validate(arguments, strict=True)
    if not task_program_available(runtime, rec.task):
        raise ToolFailure("Executable task programs are not configured for this task")
    factory = runtime.task_harness_broker_factory
    enrolled = factory._current(runtime, rec)
    grant = enrolled.grant
    if (request.input_path, request.input_sha256) != (grant.source_path, grant.source_sha256):
        raise PolicyDenied("Program input does not match its exact native source grant")
    task, policy, revision = rec.task, runtime.policy, rec.task.objective_revision
    owner, binding = asyncio.current_task(), _binding(task)
    fingerprint = runtime._permission_fingerprint(rec)
    provider = runtime.provider_for(rec)
    info = runtime.provider_info_for(rec).model_copy(deep=True)
    snapshot = runtime.tool_snapshot(task)
    argv = [
        "/kinaiya-input"
        if value == "{input}"
        else "/workspace/" + request.output_path
        if value == "{output}"
        else value
        for value in request.argv
    ]
    command = task_program_command(request.interpreter, argv)
    shell_args = {"command": command, "timeout_s": request.timeout_s}
    decision = runtime._permission_decision(rec, "shell", shell_args)
    digest = policy.digest_for("shell", shell_args, revision)

    def current():
        factory._current(runtime, rec)
        if (
            runtime._closed
            or rec.recovered
            or rec.closing
            or rec.cancel_requested
            or rec.pending_steer is not None
            or owner.cancelling()
            or rec.task is not task
            or task.state != TaskState.RUNNING
            or runtime._tasks.get(task.task_id) is not rec
            or _binding(task) != binding
            or runtime.policy is not policy
            or runtime.provider_for(rec) is not provider
            or runtime.provider_info_for(rec) != info
            or runtime.tool_snapshot(task) != snapshot
            or runtime._permission_fingerprint(rec) != fingerprint
            or runtime._permission_decision(rec, "shell", shell_args) != decision
            or policy.deny_shell
            or policy.deny_write
            or (decision is None and not policy.effective_shell)
            or (decision is not None and decision["effective"] != "allow")
        ):
            raise PolicyDenied("Executable task program authority is unavailable or changed")
        target = grant.primary_result_target
        if target is None or (info.provider, info.model, info.local) != (
            target.provider,
            target.model,
            target.local,
        ):
            raise PolicyDenied("Program result has no exact primary disclosure grant")
        policy.check_workspace()

    async def check():
        current()
        await runtime._task_scope(rec)
        current()
        if await runtime._epoch_error(rec) is not None:
            raise PolicyDenied("Program session epoch is no longer current")
        if await runtime._uncommitted_speech_sources(rec):
            raise PolicyDenied("Program execution requires committed owner speech authority")
        current()

    await check()
    if rec.proc_slot.get("proc") is not None or getattr(rec, "_cleanup_uncertain", False):
        raise ProviderCleanupError("Original task process custody remains unconfirmed")
    source = _read(policy, request.source_path, 262144)
    if hashlib.sha256(source).hexdigest() != request.source_sha256:
        raise ToolFailure("Agent-authored program source digest does not match")
    source_path = str(policy.resolve_path(request.source_path))
    authored = next(
        (
            item
            for item in task.evidence
            if item.kind == "file"
            and item.objective_revision == revision
            and item.path == source_path
            and item.sha256 == request.source_sha256
        ),
        None,
    )
    if authored is None:
        raise ToolFailure("Program requires this task revision's native source-write evidence")
    encoded = json_bytes(request.model_dump(mode="json"))
    stamp = hashlib.sha256(encoded).hexdigest()
    executions = rec.task_program_executions
    for old in executions:
        if not old["settled"]:
            raise ProviderCleanupError("Original task program custody remains unconfirmed")
        if old["binding"] == binding and old["request_sha256"] == stamp:
            if old["result"] is None:
                raise ToolFailure("Original task program failed; it will not be replayed")
            return old["result"]
    step_bound = runtime.max_steps
    if type(step_bound) is not int or not 1 <= step_bound <= MAX_PROGRAM_RUNS:
        raise ToolFailure("Native task step bound is unavailable")
    if len(executions) >= step_bound:
        raise ToolFailure("Task program execution bound reached")
    # A fresh output is mandatory: an earlier file cannot stand in for this run.
    output_path = policy.resolve_path(request.output_path)
    policy._deny_readonly_write(output_path)
    if policy.is_protected(output_path):
        raise PolicyDenied("Program output overlaps protected native state")
    with policy.scoped_parent(output_path, create=True) as (parent, name, _):
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ToolFailure("Program output must be a fresh workspace path")
    execution = {"binding": binding, "request_sha256": stamp, "settled": False, "result": None}
    executions.append(execution)
    rec.task_program_proof = None
    # Even a failing program can emit source-derived stdout/stderr. Install the
    # native floor and exact result-export authority before making input visible.
    rec.task_harness_publication_scope = combine_scopes(
        rec.task_harness_publication_scope, grant.content_scope
    )

    def export_current():
        current()
        _snapshot(policy, grant)
        return True

    rec.task_program_export_check = export_current
    try:
        with _opened(policy, request.entrypoint_path) as (program_fd, _, program_stat):
            with _opened(policy, request.input_path) as (input_fd, _, input_stat):
                current()
                kwargs = {
                    "input_fd": input_fd,
                    "source_sha256": request.entrypoint_sha256,
                    "input_sha256": request.input_sha256,
                    "proc_slot": rec.proc_slot,
                    "digest": digest,
                }
                if decision is None:
                    _, command_evidence = await run_program(
                        policy,
                        program_fd,
                        request.interpreter,
                        argv,
                        request.timeout_s,
                        revision,
                        **kwargs,
                    )
                else:
                    with native_tool_admission(
                        policy=policy,
                        tool="shell",
                        arguments=shell_args,
                        broker_arguments={**shell_args, "objective_revision": revision},
                        digest=digest,
                        revision=revision,
                        decision=decision,
                        current=current,
                        approval_id=None,
                        reviewed=False,
                        speech_review=False,
                    ):
                        _, command_evidence = await run_program(
                            policy,
                            program_fd,
                            request.interpreter,
                            argv,
                            request.timeout_s,
                            revision,
                            **kwargs,
                        )
        await check()
        if (
            type(command_evidence) is not Evidence
            or command_evidence.kind != "command"
            or type(command_evidence.exit_code) is not int
            or command_evidence.exit_code != 0
            or command_evidence.objective_revision != revision
            or rec.proc_slot.get("proc") is not None
        ):
            raise ProviderCleanupError("Original task program exit or custody is unconfirmed")
        output = _read(policy, output_path, MAX_TEXT_BYTES)
        try:
            text = output.decode("utf-8")
        except UnicodeError as exc:
            raise ToolFailure("Program result requires complete UTF-8 text") from exc
        if not text.strip() or any(
            (ord(char) < 32 and char not in "\t\n\r") or 127 <= ord(char) <= 159 for char in text
        ):
            raise ToolFailure("Program result requires nonempty UTF-8 text")
        receipt = {
            "contract": "task_program.v1",
            **json.loads(binding),
            "request_sha256": stamp,
            "source_path": request.source_path,
            "source_sha256": request.source_sha256,
            "entrypoint_path": request.entrypoint_path,
            "entrypoint_sha256": request.entrypoint_sha256,
            "entrypoint_bytes": program_stat.st_size,
            "interpreter": request.interpreter,
            "argv": argv,
            "input_path": request.input_path,
            "input_sha256": request.input_sha256,
            "input_bytes": input_stat.st_size,
            "network_allowed": policy.network,
            "output_path": request.output_path,
            "output_sha256": hashlib.sha256(output).hexdigest(),
            "output_bytes": len(output),
            "exit_code": command_evidence.exit_code,
            "settled": True,
            "compiler_causality": "not_established",
            "task_completion": "not_established",
        }
        receipt_bytes = json_bytes(receipt)
        evidence = Evidence(
            kind="extension",
            summary="Executed pinned Agent task program in the native OS sandbox",
            path=source_path,
            sha256=request.source_sha256,
            output=receipt_bytes.decode("utf-8"),
            objective_revision=revision,
        )
        rec.task_program_proof = TaskProgramProof(
            binding,
            encoded,
            receipt_bytes,
            authored.evidence_id,
            output,
            json_bytes(evidence.model_dump(mode="json")),
        )
        execution["result"] = (json.dumps({"receipt": receipt, "text": text}), evidence)
        return execution["result"]
    finally:
        execution["settled"] = rec.proc_slot.get("proc") is None
        if not execution["settled"]:
            rec._cleanup_uncertain = True


def check_program_completion(factory, runtime, rec):
    """Verify native execution and persisted output; never grade its semantics."""
    enrolled = factory._current(runtime, rec)
    _snapshot(runtime.policy, enrolled.grant)
    proof = rec.task_program_proof
    if type(proof) is not TaskProgramProof or proof.binding != _binding(rec.task):
        raise ToolFailure("No current native task program execution proof")
    request = RunTaskProgramArguments.model_validate_json(proof.request, strict=True)
    receipt = json.loads(proof.receipt)
    stamp = hashlib.sha256(proof.request).hexdigest()
    execution = next(
        (
            item
            for item in rec.task_program_executions
            if item["binding"] == proof.binding and item["request_sha256"] == stamp
        ),
        None,
    )
    if (
        execution is None
        or execution["settled"] is not True
        or execution["result"] is None
        or rec.proc_slot.get("proc") is not None
        or getattr(rec, "_cleanup_uncertain", False)
        or receipt.get("contract") != "task_program.v1"
        or json_bytes({key: receipt.get(key) for key in json.loads(proof.binding)}) != proof.binding
        or receipt.get("request_sha256") != stamp
        or receipt.get("exit_code") != 0
        or receipt.get("settled") is not True
        or (request.input_path, request.input_sha256)
        != (enrolled.grant.source_path, enrolled.grant.source_sha256)
        or receipt.get("output_sha256") != hashlib.sha256(proof.output).hexdigest()
        or receipt.get("output_bytes") != len(proof.output)
        or not any(
            json_bytes(item.model_dump(mode="json")) == proof.evidence
            for item in rec.task.evidence
            if item.kind == "extension"
        )
        or not any(
            item.evidence_id == proof.source_evidence_id
            and item.kind == "file"
            and item.objective_revision == rec.task.objective_revision
            and item.path == str(runtime.policy.resolve_path(request.source_path))
            and item.sha256 == request.source_sha256
            for item in rec.task.evidence
        )
    ):
        raise ToolFailure("Native task program provenance is not established")
    for path, expected, maximum in (
        (request.source_path, request.source_sha256, 262144),
        (request.entrypoint_path, request.entrypoint_sha256, 64 * 1024 * 1024),
        (request.output_path, receipt["output_sha256"], MAX_TEXT_BYTES),
    ):
        if hashlib.sha256(_read(runtime.policy, path, maximum)).hexdigest() != expected:
            raise ToolFailure("Native task program source or result changed")
    return proof.output.decode("utf-8")
