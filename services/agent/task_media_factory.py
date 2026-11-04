"""Owner-only enrollment of one authenticated voice task's exact media grant.

The owner writes this plan before forwarding its synthetic voice clip. Neither
the generated program nor its model supplies a grant, trusted prompt, task ID,
or provider. This module performs no provider, credential, or network access.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from packages.contracts.agent_media import (
    MAX_SOURCE_BYTES,
    Digest,
    Identifier,
    MediaPrimaryResultTarget,
    MediaRecord,
    MediaSourceGrant,
    MediaTarget,
    MediaTaskBinding,
    source_path,
)
from packages.contracts.agent_submit import submit_entry
from packages.contracts.models import SpeechWindow, TaskState, WindowDecision
from packages.providers.disclosure import combine_scopes
from services.agent.media_tools import WorkspaceMediaBroker, _identity, _snapshot
from services.agent.model_settings import AgentModelSettings


class MediaEnrollmentError(RuntimeError):
    def __init__(self):
        # Owner plan contents and transcript text never enter error receipts.
        super().__init__("Native media task enrollment is unavailable")


class TaskMediaOwnerPlan(MediaRecord):
    """Private native file DTO; JSON arrays become immutable target tuples."""

    schema_version: Literal["task_media_owner.v1"]
    plan_id: Identifier
    session_id: Identifier
    thread_id: Identifier
    epoch: int = Field(ge=1)
    utterance_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    audio_sha256: Digest
    source_path: str
    source_sha256: Digest
    source_bytes: int = Field(ge=16, le=MAX_SOURCE_BYTES)
    content_scope: Literal["public_synthetic", "private_cloud_authorized"]
    targets: tuple[MediaTarget, ...] = Field(min_length=1, max_length=2)
    primary_result_target: MediaPrimaryResultTarget
    required_operations: tuple[Literal["sam_hands", "transcribe_video"], ...] = Field(
        min_length=1, max_length=2
    )
    completion_mode: Literal["media-v1", "executable-v1"] = "media-v1"

    _path = field_validator("source_path")(source_path)

    @model_validator(mode="after")
    def exact_targets(self):
        operations = {target.operation for target in self.targets}
        if (
            len(operations) != len(self.targets)
            or len(set(self.required_operations)) != len(self.required_operations)
            or not set(self.required_operations) <= operations
            or any(
                target.operation == "transcribe_video"
                and (
                    target.model != self.primary_result_target.model
                    or target.reasoning != ("max" if target.model == "muse-wonder-1.0" else "xhigh")
                )
                for target in self.targets
            )
        ):
            raise ValueError("Owner media targets or required operations are invalid")
        if self.completion_mode == "executable-v1" and "sam_hands" not in self.required_operations:
            raise ValueError("Executable media completion requires an actual SAM hand result")
        return self

    def grant_for(self, task):
        return MediaSourceGrant(
            grant_id=self.plan_id,
            binding=MediaTaskBinding(
                task_id=task.task_id,
                objective_revision=task.objective_revision,
                session_id=task.session_id,
                thread_id=task.thread_id,
                epoch=task.epoch,
            ),
            **self.model_dump(
                mode="python",
                include={
                    "source_path",
                    "source_sha256",
                    "source_bytes",
                    "content_scope",
                    "targets",
                    "primary_result_target",
                    "required_operations",
                },
            ),
        )


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MediaEnrollmentError()
        result[key] = value
    return result


def _owner_plan(path, workspace):
    """Read a stable, owner-only regular file through no-follow directories."""
    descriptor = parent = None
    try:
        path = Path(path)
        if not path.is_absolute() or path != Path(os.path.abspath(path)):
            raise MediaEnrollmentError()
        if path.is_relative_to(workspace):
            raise MediaEnrollmentError()
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = following
        directory = os.fstat(parent)
        if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
            raise MediaEnrollmentError()
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 1 <= before.st_size <= 65536
        ):
            raise MediaEnrollmentError()
        chunks, size = [], 0
        while chunk := os.read(descriptor, min(65536, 65537 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > 65536:
                raise MediaEnrollmentError()
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or size != before.st_size:
            raise MediaEnrollmentError()
        raw = b"".join(chunks)
        # Reject duplicate keys before the strict JSON-mode tuple validation.
        json.loads(raw, object_pairs_hook=_unique)
        plan = TaskMediaOwnerPlan.model_validate_json(raw)
        return plan, (_identity(after), hashlib.sha256(raw).hexdigest())
    except Exception:
        raise MediaEnrollmentError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


@dataclass(frozen=True)
class _Enrollment:
    runtime: object
    record: object
    task: object
    policy: object
    journal: object
    provider: object
    pin: object
    identity: object
    plan: TaskMediaOwnerPlan
    stamp: tuple
    grant: MediaSourceGrant
    source_event_id: str
    source_revision: int
    objective_sha256: str


class TaskMediaFactory:
    """Native injection: runtime factory + async preworker_hook=enroll.

    One instance can enroll only one first native task. An unrelated task,
    changed plan, revised source, altered model, or replacement workspace is
    refused. Recovered tasks cannot silently recreate this ephemeral grant.
    """

    def __init__(self, *, plan_path, provider):
        self.plan_path = Path(plan_path)
        self.provider = provider
        self._enrollment = None
        self._broker = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _primary(runtime, rec):
        provider = runtime.provider_for(rec)
        info, pin = runtime.provider_info_for(rec), rec.task.model_pin
        effort = "max" if info.model == "muse-wonder-1.0" else "xhigh"
        if (
            info.provider != "meta"
            or info.model not in {"muse-spark-1.3-contributor", "muse-wonder-1.0"}
            or info.local is not False
            or AgentModelSettings.effort(provider) != effort
            or pin is None
            or pin.local is not False
            or (pin.provider, pin.model, pin.local, pin.reasoning)
            != ("meta", info.model, False, effort)
        ):
            raise MediaEnrollmentError()
        return provider

    def _current(self, runtime, rec):
        enrolled = self._enrollment
        task = rec.task
        if (
            enrolled is None
            or enrolled.runtime is not runtime
            or enrolled.record is not rec
            or enrolled.task is not task
            or runtime._tasks.get(task.task_id) is not rec
            or runtime.task_harness_broker_factory is not self
            or runtime.task_preworker_hook != self.enroll
            or runtime._closed
            or rec.recovered
            or rec.closing
            or rec.cancel_requested
            or rec.pending_steer is not None
            or runtime.policy is not enrolled.policy
            or runtime.journal is not enrolled.journal
            or rec.native_task_identity != enrolled.identity
            or task.model_pin != enrolled.pin
            or self._primary(runtime, rec) is not enrolled.provider
            or MediaTaskBinding.model_validate(
                {
                    key: getattr(task, key)
                    for key in ("task_id", "objective_revision", "session_id", "thread_id", "epoch")
                }
            )
            != enrolled.grant.binding
            or task.source_event_ids != [enrolled.source_event_id]
            or rec.source_revisions != {enrolled.source_event_id: enrolled.source_revision}
            or hashlib.sha256(task.objective.encode("utf-8")).hexdigest()
            != enrolled.objective_sha256
            or task.content_scope != "public_synthetic"
        ):
            raise MediaEnrollmentError()
        runtime.policy.check_workspace()
        plan, stamp = _owner_plan(self.plan_path, runtime.policy.workspace)
        if plan != enrolled.plan or stamp != enrolled.stamp:
            raise MediaEnrollmentError()
        return enrolled

    def available(self, runtime, task=None):
        if task is None:
            return False
        rec = runtime._tasks.get(task.task_id)
        if rec is None or rec.task is not task:
            return False
        try:
            self._current(runtime, rec)
            return task.state in {TaskState.PAUSED, TaskState.ACCEPTED, TaskState.RUNNING}
        except Exception:
            return False

    def program_available(self, runtime, task=None):
        """Fresh explicit owner mode; no legacy enrollment can acquire it."""
        return (
            self.available(runtime, task)
            and self._enrollment.plan.completion_mode == "executable-v1"
        )

    def describe(self, runtime, task):
        """Passive source reference for the first native Agent prompt only."""
        if not self.available(runtime, task):
            raise MediaEnrollmentError()
        grant = self._enrollment.grant
        return {
            "source_path": grant.source_path,
            "source_sha256": grant.source_sha256,
            "source_bytes": grant.source_bytes,
            "allowed_operations": sorted(target.operation for target in grant.targets),
            "required_operations": sorted(grant.required_operations),
        }

    async def enroll(self, runtime, rec):
        async with self._lock:
            if self._enrollment is not None:
                self._current(runtime, rec)
                return True
            try:
                await self._enroll(runtime, rec)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise MediaEnrollmentError() from None
            return True

    async def _enroll(self, runtime, rec):
        task, policy, journal = rec.task, runtime.policy, runtime.journal
        plan, stamp = _owner_plan(self.plan_path, policy.workspace)
        primary = self._primary(runtime, rec)
        identity = rec.native_task_identity

        def guard():
            if (
                runtime.task_harness_broker_factory is not self
                or runtime.task_preworker_hook != self.enroll
                or runtime._closed
                or rec.closing
                or rec.cancel_requested
                or rec.recovered
                or rec.worker is not None
                or not rec.submit_confirmed
                or task.state != TaskState.PAUSED
                or asyncio.current_task().cancelling()
                or runtime._tasks.get(task.task_id) is not rec
                or rec.task is not task
                or runtime.policy is not policy
                or runtime.journal is not journal
                or rec.native_task_identity != identity
                or identity is None
                or self._primary(runtime, rec) is not primary
                or plan.primary_result_target.model != runtime.provider_info_for(rec).model
                or (task.session_id, task.thread_id, task.epoch)
                != (plan.session_id, plan.thread_id, plan.epoch)
                or task.objective_revision != 1
                or len(task.source_event_ids) != 1
                or task.content_scope != "public_synthetic"
                or rec.pending_steer is not None
            ):
                raise MediaEnrollmentError()
            policy.check_workspace()

        guard()
        source = await journal.entry(task.source_event_ids[0])
        guard()
        resolved = await journal.resolve(source.event_id)
        guard()
        window = SpeechWindow.model_validate(source.payload)
        if (
            source.kind != "conversation.window"
            or source.origin != "authenticated-user"
            or source.sequence < 1
            or source.event_id != resolved.event_id
            or source != resolved
            or (source.session_id, source.thread_id, source.epoch)
            != (plan.session_id, plan.thread_id, plan.epoch)
            or (window.session_id, window.thread_id, window.epoch)
            != (plan.session_id, plan.thread_id, plan.epoch)
            or window.utterance_id != plan.utterance_id
            or window.audio_sha256 != plan.audio_sha256
            or window.window_index != 0
            or window.revision != 1
            or not window.capture_ended
            or window.user_speaking
            or window.content_scope != "public_synthetic"
            or not window.text.strip()
            or task.objective != window.text
            or rec.source_revisions != {source.event_id: 1}
        ):
            raise MediaEnrollmentError()
        submitted = submit_entry(await journal.entry(identity.submit_event_id))
        guard()
        if (
            runtime._native_task_identity(submitted) != identity
            or submitted.sequence != rec.submit_sequence
            or submitted.payload.get("actor") != "decisions"
            or submitted.payload.get("source_event_ids") != [source.event_id]
            or submitted.payload.get("source_revision") != 1
            or submitted.payload.get("objective") != window.text
        ):
            raise MediaEnrollmentError()
        # The bounded page must include this receipt, proving that no earlier
        # submit was skipped. This intentionally accepts only a fresh session.
        entries = await journal.read(
            after=0,
            limit=200,
            session_id=plan.session_id,
            thread_id=plan.thread_id,
            epoch=plan.epoch,
            kinds=["agent.command", "routing.window_decided"],
        )
        guard()
        earlier = [entry for entry in entries if entry.sequence <= submitted.sequence]
        submits = [
            entry
            for entry in earlier
            if entry.kind == "agent.command" and entry.payload.get("op") == "submit"
        ]
        if len(submits) != 1 or submits[0] != submitted:
            raise MediaEnrollmentError()
        matched = []
        for entry in earlier:
            if entry.kind != "routing.window_decided" or entry.origin != "decisions":
                continue
            decision = WindowDecision.model_validate(entry.payload)
            key = (
                "window:queue_until_task_done:"
                + hashlib.sha256(
                    f"{decision.decision_id}:QUEUE_UNTIL_TASK_DONE".encode()
                ).hexdigest()[:32]
            )
            if (
                source.sequence < entry.sequence < submitted.sequence
                and entry.causes == [source.event_id]
                and decision.source_event_id == source.event_id
                and decision.source_sequence == source.sequence
                and decision.source_revision == 1
                and decision.agent_route == "QUEUE_UNTIL_TASK_DONE"
                and decision.target_task_id is None
                and decision.expected_objective_revision is None
                and task.idempotency_key == key
            ):
                matched.append(entry)
        if len(matched) != 1:
            raise MediaEnrollmentError()
        # Recheck current native epoch and the still-private owner file after
        # all awaited provenance reads, then atomically enroll the one task.
        session = await journal.session(plan.session_id)
        guard()
        if (session.thread_id, session.epoch) != (plan.thread_id, plan.epoch):
            raise MediaEnrollmentError()
        latest_plan, latest_stamp = _owner_plan(self.plan_path, policy.workspace)
        if (latest_plan, latest_stamp) != (plan, stamp):
            raise MediaEnrollmentError()
        grant = plan.grant_for(task)
        # Qualify the actual imported workspace bytes before advertising the
        # tool. Each provider operation takes a fresh exact snapshot again.
        _snapshot(policy, grant)
        guard()
        self._enrollment = _Enrollment(
            runtime,
            rec,
            task,
            policy,
            journal,
            primary,
            task.model_pin.model_copy(deep=True),
            identity.model_copy(deep=True),
            plan,
            stamp,
            grant,
            source.event_id,
            window.revision,
            hashlib.sha256(window.text.encode("utf-8")).hexdigest(),
        )
        # Capture this owner on the native record. Removing/replacing the
        # advertised factory cannot remove the enrolled completion obligation.
        rec.task_media_completion_check = lambda: self.check_completion(runtime, rec)
        if plan.completion_mode == "executable-v1":
            # Ordinary research/build shell runs before run_task_program and can
            # already read this exact source. Protect all resulting task output
            # before the worker starts, including failed-command stdout/stderr.
            rec.task_harness_publication_scope = combine_scopes(
                rec.task_harness_publication_scope, grant.content_scope
            )

            def program_source_export():
                actual = self._current(runtime, rec)
                _snapshot(actual.policy, actual.grant)
                info = runtime.provider_info_for(rec)
                target = actual.grant.primary_result_target
                if target is None or (info.provider, info.model, info.local) != (
                    target.provider,
                    target.model,
                    target.local,
                ):
                    raise MediaEnrollmentError()
                return True

            rec.task_program_export_check = program_source_export

    def check_completion(self, runtime, rec):
        from services.agent.task_media_completion import check_completion

        return check_completion(self, runtime, rec)

    def __call__(self, runtime, rec, current):
        enrolled = self._current(runtime, rec)
        if self._broker is not None:
            raise MediaEnrollmentError()

        def check():
            self._current(runtime, rec)
            current()

        self._broker = WorkspaceMediaBroker(
            policy=enrolled.policy, grant=enrolled.grant, provider=self.provider, current=check
        )
        return self._broker
